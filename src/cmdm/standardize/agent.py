"""The rule-mining agent, and shadow evaluation.

This closes the learning loop. The AI fallback handles what the deterministic
pass could not; this module watches what it handled, notices when the same shape
of problem keeps recurring, and proposes a deterministic rule that would have
handled it — so those records never reach a model again.

The loop, and where the safety lives:

1.  **Mine.** Group logged exceptions by the check they failed and by a
    structural signature of the input. A signature that recurs often enough is a
    candidate.
2.  **Propose.** Emit a regex rule. Data, never code — the worst a bad rule can
    do is rewrite a string badly.
3.  **Shadow-evaluate.** Run the candidate over the corpus it claims to handle
    *and* over a regression set it must not disturb. Both numbers are recorded.
4.  **Review.** A steward approves. The database will not hold an ACTIVE rule
    that was never shadow-tested or that regressed anything.

Step 3 is the one that matters, and it is why auto-promotion was not chosen.
Normalization that rewrites itself into production unreviewed can corrupt every
record it touches, uniformly — and uniform corruption is the hardest kind to
notice, because nothing looks anomalous relative to anything else.

The "agent" here is deliberately a pattern miner rather than a language model
writing regexes freehand. A generated regex that is *nearly* right is far more
dangerous than one that is obviously wrong: it passes review and then quietly
mangles an unanticipated shape. Signature-derived patterns are narrow by
construction, and each is anchored to the exact token shape it was mined from.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row

from cmdm.model.ids import uuid7
from cmdm.standardize.gate import apply_gate, checks_for
from cmdm.standardize.rules import (
    RuleKind,
    StandardizationRule,
    apply_rules,
    insert_rule,
    record_shadow_result,
)

__all__ = [
    "Signature",
    "signature_of",
    "mine_signatures",
    "propose_rules",
    "shadow_evaluate",
    "ShadowResult",
    "MIN_EVIDENCE",
]

#: How many distinct records must share a signature before it is worth a rule.
#: Set high enough that a rule is never proposed from noise: a pattern seen
#: three times is a coincidence, and a steward's attention is the scarcest
#: resource in the loop.
MIN_EVIDENCE = 25


@dataclass(frozen=True, slots=True)
class Signature:
    """A structural abstraction of an input value.

    Not the value itself — that would group nothing — but its *shape*. Names
    become token-length patterns: ``KATHERINE A O'BRIEN`` becomes ``W-I-W-W``
    (word, initial, word, word). Records sharing a shape share a fix.

    Abstracting to shape is what makes the mining tractable and the resulting
    rule narrow. A rule mined from ``W-I-W`` applies to exactly three-token
    names whose middle token is a single character, and to nothing else.
    """

    field_name: str
    failed_check: str
    shape: str
    party_type: str = "PERSON"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.field_name, self.failed_check, self.shape, self.party_type)

    @property
    def rule_name(self) -> str:
        return f"{self.failed_check}__{self.shape.lower().replace('-', '_')}"


def _token_shape(value: str) -> str:
    """Reduce a value to a token-shape string.

    ``W`` word, ``I`` single-character initial, ``N`` numeric, ``P`` known
    particle, ``S`` symbol-only.
    """
    from cmdm.standardize.ai import _PARTICLES

    tokens = [t for t in re.split(r"\s+", (value or "").strip().upper()) if t]
    out: list[str] = []
    for token in tokens:
        if token in _PARTICLES:
            out.append("P")
        elif token.isdigit():
            out.append("N")
        elif len(token) == 1 and token.isalpha():
            out.append("I")
        elif token.isalpha():
            out.append("W")
        else:
            out.append("S")
    return "-".join(out)


def signature_of(
    value: str, field_name: str, failed_check: str, party_type: str = "PERSON"
) -> Signature:
    """Build the signature for one failed value."""
    return Signature(
        field_name=field_name,
        failed_check=failed_check,
        shape=_token_shape(value),
        party_type=party_type,
    )


def mine_signatures(
    exceptions: pl.DataFrame,
    *,
    min_evidence: int = MIN_EVIDENCE,
    value_column: str = "deterministic_value",
) -> pl.DataFrame:
    """Group logged exceptions into recurring signatures.

    Expects the columns the exception log carries: ``field_name``,
    ``failed_check``, the value, and optionally ``party_type``.

    Only signatures the fallback actually *resolved* are counted. A shape the
    model could not fix either is not a candidate for a deterministic rule — the
    rule would have nothing to imitate.
    """
    if exceptions.height == 0:
        return pl.DataFrame(
            schema={
                "field_name": pl.String, "failed_check": pl.String, "shape": pl.String,
                "party_type": pl.String, "evidence_count": pl.UInt32,
                "sample": pl.List(pl.String),
            }
        )

    frame = exceptions
    if "party_type" not in frame.columns:
        frame = frame.with_columns(pl.lit("PERSON").alias("party_type"))
    if "ai_resolved" in frame.columns:
        frame = frame.filter(pl.col("ai_resolved"))

    shaped = frame.with_columns(
        pl.col(value_column)
        .map_elements(_token_shape, return_dtype=pl.String)
        .alias("shape")
    )

    return (
        shaped.group_by(["field_name", "failed_check", "shape", "party_type"])
        .agg(
            pl.len().alias("evidence_count"),
            pl.col(value_column).head(5).alias("sample"),
        )
        .filter(pl.col("evidence_count") >= min_evidence)
        .sort("evidence_count", descending=True)
    )


def _particle_alternation() -> str:
    """Regex alternation over the known surname particles.

    Rendered explicitly rather than as a generic word fragment. With ``P`` as
    ``[A-Z]+`` every particle shape was also matched by the plain-word shape, so
    the miner proposed two overlapping rules for the same inputs and a steward
    had to choose between rules that were not actually distinguishable.
    """
    from cmdm.standardize.ai import _PARTICLES

    return "(?:" + "|".join(sorted(_PARTICLES, key=len, reverse=True)) + ")"


#: Shape token -> the regex fragment that matches it. Capture names are assigned
#: by position when the rule is built.
_SHAPE_FRAGMENT = {
    "W": r"[A-Z']+",
    "I": r"[A-Z]",
    "N": r"\d+",
    "P": _particle_alternation(),
    "S": r"\S+",
}


def _name_extract_pattern(shape: str) -> tuple[str, tuple[str, ...]] | None:
    """Build a name-splitting EXTRACT pattern for a token shape.

    The first token is the given name, the last is the surname, and everything
    between is the middle. Particles bind rightwards into the surname, which is
    what makes ``VAN DER BERG`` one name rather than two middles and a surname.

    Returns ``None`` for shapes this cannot safely handle: fewer than two
    tokens, or anything containing a numeric or symbol token, which is not a
    name and must not be parsed as one.
    """
    parts = shape.split("-")
    if len(parts) < 2 or any(p in ("N", "S") for p in parts):
        return None

    # Surname starts at the first trailing particle, if any.
    surname_start = len(parts) - 1
    while surname_start > 1 and parts[surname_start - 1] == "P":
        surname_start -= 1

    given = _SHAPE_FRAGMENT[parts[0]]
    middles = parts[1:surname_start]
    surnames = parts[surname_start:]

    fragments = [f"(?P<given_name_derived>{given})"]
    if middles:
        middle_body = r"\s+".join(_SHAPE_FRAGMENT[p] for p in middles)
        fragments.append(rf"(?P<middle_name_derived>{middle_body})")
    surname_body = r"\s+".join(_SHAPE_FRAGMENT[p] for p in surnames)
    fragments.append(rf"(?P<surname_derived>{surname_body})")

    pattern = r"^" + r"\s+".join(fragments) + r"$"
    targets = ["given_name_derived", "surname_derived"]
    if middles:
        targets.insert(1, "middle_name_derived")
    return pattern, tuple(targets)


def propose_rules(
    signatures: pl.DataFrame, *, proposed_by: str = "rule-miner", model_name: str | None = None
) -> list[StandardizationRule]:
    """Turn mined signatures into candidate rules.

    Only shapes that map to a safe, narrow pattern produce a proposal. A
    signature the builder cannot express precisely is skipped rather than
    approximated — an approximate rule is the dangerous kind, because it passes
    review and then mangles inputs nobody anticipated.
    """
    proposals: list[StandardizationRule] = []

    for row in signatures.iter_rows(named=True):
        if row["failed_check"] != "name_parsed_cleanly":
            # Other checks need their own builders. Proposing a name-splitting
            # rule for an address failure would be worse than proposing nothing.
            continue
        if row["party_type"] != "PERSON":
            continue

        built = _name_extract_pattern(row["shape"])
        if built is None:
            continue
        pattern, targets = built

        signature = Signature(
            field_name=row["field_name"],
            failed_check=row["failed_check"],
            shape=row["shape"],
            party_type=row["party_type"],
        )
        proposals.append(
            StandardizationRule(
                rule_id=uuid7(),
                field_name="full_name_normalized",
                rule_name=signature.rule_name,
                pattern=pattern,
                rule_kind=RuleKind.EXTRACT,
                target_fields=targets,
                targets_check=row["failed_check"],
                evidence_count=int(row["evidence_count"]),
                evidence_sample=tuple(row["sample"]),
                proposed_by=proposed_by,
                model_name=model_name,
                # Learned rules run after the built-in pass, so they fill gaps
                # rather than pre-empt logic that was reviewed by a human.
                priority=500,
            )
        )

    return proposals


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """Outcome of evaluating a candidate rule without applying it."""

    rule_id: uuid.UUID
    rule_name: str
    matched: int
    fixed: int
    regressions: int
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def safe(self) -> bool:
        """Whether the rule may be offered for approval at all."""
        return self.regressions == 0 and self.fixed > 0


def shadow_evaluate(
    rule: StandardizationRule,
    target_corpus: pl.DataFrame,
    regression_corpus: pl.DataFrame,
) -> ShadowResult:
    """Measure a candidate rule against two corpora.

    ``target_corpus`` is the population the rule claims to fix — records that
    currently fail the gate. ``regression_corpus`` is the population it must not
    disturb — records that currently pass.

    The second is the one that makes this evaluation worth anything. Measuring
    only what a rule fixes will approve a rule that fixes a thousand records and
    silently breaks ten thousand others, because nobody looked at the ten
    thousand.

    A regression is defined as a record that passed the gate before and does not
    after, or whose already-populated value the rule changed. Both matter: a rule
    that overwrites a correct name with a differently-correct one has still
    destroyed information the pipeline had.
    """
    checks = checks_for(target_corpus.columns)

    def gated(frame: pl.DataFrame) -> pl.DataFrame:
        return apply_gate(frame, checks).collect()

    matched = 0
    fixed = 0

    if target_corpus.height:
        before = gated(target_corpus)
        after = gated(apply_rules(target_corpus, [rule]).collect())

        # A record the rule touched at all.
        matched = int(
            target_corpus.select(
                pl.col(rule.field_name).str.contains(rule.pattern).fill_null(False).sum()
            ).item()
        )
        fixed = int(
            (~before["gate_passed"] & after["gate_passed"]).sum()
        )

    regressions = 0
    detail: dict[str, Any] = {}

    if regression_corpus.height:
        before_reg = gated(regression_corpus)
        after_reg = gated(apply_rules(regression_corpus, [rule]).collect())

        newly_failing = int((before_reg["gate_passed"] & ~after_reg["gate_passed"]).sum())

        # Values the rule rewrote where something was already present. Checked
        # per target column, since an EXTRACT rule writes several.
        overwritten = 0
        for target in rule.target_fields or (rule.field_name,):
            if target not in regression_corpus.columns:
                continue
            had_value = before_reg[target].is_not_null()
            changed = before_reg[target] != after_reg[target]
            overwritten += int((had_value & changed).sum())

        regressions = newly_failing + overwritten
        detail = {"newly_failing": newly_failing, "overwritten": overwritten}

    return ShadowResult(
        rule_id=rule.rule_id,
        rule_name=rule.rule_name,
        matched=matched,
        fixed=fixed,
        regressions=regressions,
        report={
            "pattern": rule.pattern,
            "target_rows": target_corpus.height,
            "regression_rows": regression_corpus.height,
            "matched": matched,
            "fixed": fixed,
            **detail,
        },
    )


def mine_and_propose(
    conn: psycopg.Connection,
    *,
    target_corpus: pl.DataFrame,
    regression_corpus: pl.DataFrame,
    min_evidence: int = MIN_EVIDENCE,
    proposed_by: str = "rule-miner",
) -> list[ShadowResult]:
    """Run one full mining pass: mine, propose, shadow-evaluate, store.

    Stops short of approval. Every rule this produces sits in SHADOW awaiting a
    human, which is the whole design.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT field_name,
                   unnest(failed_checks) AS failed_check,
                   deterministic_value,
                   ai_value,
                   ai_value IS NOT NULL AS ai_resolved
            FROM mdm.standardization_exception
            WHERE covered_by_rule_id IS NULL
            """
        )
        rows = cur.fetchall()

    if not rows:
        return []

    exceptions = pl.DataFrame(rows)
    signatures = mine_signatures(exceptions, min_evidence=min_evidence)
    proposals = propose_rules(signatures, proposed_by=proposed_by)

    results: list[ShadowResult] = []
    for rule in proposals:
        stored = insert_rule(conn, rule)
        if stored is None:
            # Already proposed on an earlier pass; do not re-evaluate or
            # re-queue it for a steward who has already seen it.
            continue
        outcome = shadow_evaluate(rule, target_corpus, regression_corpus)
        record_shadow_result(
            conn,
            rule.rule_id,
            matched=outcome.matched,
            fixed=outcome.fixed,
            regressions=outcome.regressions,
            report=outcome.report,
        )
        results.append(outcome)

    return results

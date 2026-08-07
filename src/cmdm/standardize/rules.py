"""The learned rule store.

Rules are the output of the learning loop and the input to the deterministic
pass. A rule that reaches ACTIVE is compiled into a Polars expression and runs
over every subsequent batch at column speed, which is the whole point: work the
model did once becomes work no model ever does again.

Two kinds, and the second one is load-bearing:

*   **REWRITE** — a regex and a replacement over a text column. The classic
    normalization rule.
*   **EXTRACT** — a regex with named capture groups populating several derived
    columns at once. Name component inference needs this; it is not a string
    cleanup but a structured parse, and without it the largest category of
    quality-gate failure could never be retired into the deterministic path.

Rules are data, not code. A rule is a regex and a target list — never a callable,
never something eval'd. That constraint is deliberate and is what makes
machine-authored rules safe to run: the worst a bad rule can do is rewrite a
string badly, which shadow evaluation catches. A rule that could execute
arbitrary code would make the whole learning loop an unacceptable risk regardless
of how good the review process was.

Promotion is gated. ``PROPOSED -> SHADOW -> APPROVED -> ACTIVE``, and the
database refuses to hold an ACTIVE rule that was never reviewed or that regressed
anything during shadow evaluation. That is a check constraint, not a convention
the promotion code is trusted to follow.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cmdm.model.ids import uuid7

__all__ = [
    "RuleKind",
    "RuleState",
    "StandardizationRule",
    "compile_rule",
    "apply_rules",
    "load_active_rules",
    "insert_rule",
    "record_shadow_result",
    "approve_rule",
    "reject_rule",
    "list_rules",
    "InvalidRule",
]


class RuleKind:
    REWRITE = "REWRITE"
    EXTRACT = "EXTRACT"


class RuleState:
    PROPOSED = "PROPOSED"
    SHADOW = "SHADOW"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class InvalidRule(ValueError):
    """A rule that cannot be compiled or would be unsafe to run."""


#: Patterns with unbounded nested quantifiers can backtrack catastrophically.
#: Polars uses the Rust regex crate, which is linear-time and immune to that, but
#: a rule is also shown to humans and may be copied elsewhere, so obviously
#: pathological constructs are refused at the door.
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*]\)[+*]")

#: Upper bound on pattern length. A machine-authored rule that runs to a thousand
#: characters is not a rule anybody can review, and an unreviewable rule cannot
#: be approved, so it is rejected before it wastes a steward's time.
MAX_PATTERN_LENGTH = 500


@dataclass(frozen=True, slots=True)
class StandardizationRule:
    """One deterministic rule."""

    rule_id: uuid.UUID
    field_name: str
    rule_name: str
    pattern: str
    replacement: str = ""
    rule_kind: str = RuleKind.REWRITE
    target_fields: tuple[str, ...] = ()
    state: str = RuleState.PROPOSED
    priority: int = 100
    targets_check: str | None = None
    evidence_count: int = 0
    proposed_by: str = "unknown"
    model_name: str | None = None
    evidence_sample: tuple[str, ...] = ()

    def validate(self) -> None:
        """Refuse a rule that cannot be run or reviewed.

        Called before a proposal is stored, so a malformed machine-authored rule
        never reaches a steward's queue in the first place.
        """
        if not self.pattern:
            raise InvalidRule(f"{self.rule_name}: empty pattern")
        if len(self.pattern) > MAX_PATTERN_LENGTH:
            raise InvalidRule(
                f"{self.rule_name}: pattern is {len(self.pattern)} characters; "
                f"a rule longer than {MAX_PATTERN_LENGTH} cannot be meaningfully reviewed"
            )
        if _NESTED_QUANTIFIER.search(self.pattern):
            raise InvalidRule(f"{self.rule_name}: nested quantifier in pattern")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise InvalidRule(f"{self.rule_name}: invalid regex: {exc}") from None
        if self.rule_kind == RuleKind.EXTRACT:
            if not self.target_fields:
                raise InvalidRule(f"{self.rule_name}: EXTRACT rule names no target fields")
            groups = set(re.compile(self.pattern).groupindex)
            missing = set(self.target_fields) - groups
            if missing:
                raise InvalidRule(
                    f"{self.rule_name}: target fields {sorted(missing)} have no matching "
                    f"named capture group; pattern defines {sorted(groups)}"
                )


def compile_rule(rule: StandardizationRule) -> list[pl.Expr]:
    """Turn a rule into Polars expressions.

    Returns one expression per column the rule writes. A REWRITE writes its own
    field; an EXTRACT writes one column per named group, but only where the
    target is currently empty — a learned rule fills gaps and must never
    overwrite a value the deterministic pass already established.
    """
    rule.validate()

    if rule.rule_kind == RuleKind.REWRITE:
        return [
            pl.col(rule.field_name)
            .str.replace_all(rule.pattern, rule.replacement)
            .alias(rule.field_name)
        ]

    groups = pl.col(rule.field_name).str.extract_groups(rule.pattern)
    exprs: list[pl.Expr] = []
    for target in rule.target_fields:
        extracted = groups.struct.field(target)
        exprs.append(
            # coalesce, not assignment: only fill what is missing.
            pl.coalesce(pl.col(target), extracted).alias(target)
            if target != rule.field_name
            else extracted.alias(target)
        )
    return exprs


def apply_rules(
    frame: pl.DataFrame | pl.LazyFrame,
    rules: Sequence[StandardizationRule],
    *,
    only_where: pl.Expr | None = None,
) -> pl.LazyFrame:
    """Apply rules in priority order, as one vectorized pass per rule.

    ``only_where`` restricts a rule's effect to a subset without splitting the
    frame — used to apply learned rules only to records that failed the gate, so
    a rule cannot perturb records that were already fine.

    Rules whose input or target columns are absent are skipped rather than
    raising. Batches legitimately differ in which columns they carry, and a rule
    learned from an address-bearing feed must not break a feed without one.
    """
    lazy = frame.lazy() if isinstance(frame, pl.DataFrame) else frame
    if not rules:
        return lazy

    for rule in sorted(rules, key=lambda r: (r.priority, r.rule_name)):
        columns = set(lazy.collect_schema().names())
        needed = {rule.field_name, *rule.target_fields}
        if not needed <= columns:
            continue

        try:
            exprs = compile_rule(rule)
        except InvalidRule:
            # A stored rule that no longer compiles must not take the batch down
            # with it. It is skipped; the rule audit surfaces it separately.
            continue

        if only_where is not None:
            guarded = []
            for expr in exprs:
                name = expr.meta.output_name()
                guarded.append(
                    pl.when(only_where.fill_null(False))
                    .then(expr)
                    .otherwise(pl.col(name))
                    .alias(name)
                )
            exprs = guarded

        lazy = lazy.with_columns(exprs)

    return lazy


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _row_to_rule(row: dict[str, Any]) -> StandardizationRule:
    sample = row.get("evidence_sample") or []
    if isinstance(sample, dict):
        sample = sample.get("values", [])
    return StandardizationRule(
        rule_id=row["rule_id"],
        field_name=row["field_name"],
        rule_name=row["rule_name"],
        pattern=row["pattern"],
        replacement=row["replacement"],
        rule_kind=row["rule_kind"],
        target_fields=tuple(row["target_fields"] or ()),
        state=row["state"],
        priority=row["priority"],
        targets_check=row.get("targets_check"),
        evidence_count=row.get("evidence_count", 0),
        proposed_by=row.get("proposed_by", "unknown"),
        model_name=row.get("model_name"),
        evidence_sample=tuple(sample),
    )


def load_active_rules(conn: psycopg.Connection) -> list[StandardizationRule]:
    """Rules currently in force, in priority order."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM mdm.standardization_rule WHERE state = 'ACTIVE' "
            "ORDER BY priority, rule_name"
        )
        return [_row_to_rule(r) for r in cur.fetchall()]


def list_rules(conn: psycopg.Connection, state: str | None = None) -> list[StandardizationRule]:
    """Rules in a given state, newest first. Backs the steward review queue."""
    with conn.cursor(row_factory=dict_row) as cur:
        if state:
            cur.execute(
                "SELECT * FROM mdm.standardization_rule WHERE state = %s "
                "ORDER BY created_at DESC",
                (state,),
            )
        else:
            cur.execute("SELECT * FROM mdm.standardization_rule ORDER BY created_at DESC")
        return [_row_to_rule(r) for r in cur.fetchall()]


def insert_rule(conn: psycopg.Connection, rule: StandardizationRule) -> uuid.UUID | None:
    """Store a proposed rule.

    Validated first, so a malformed machine-authored rule never reaches the
    review queue. Returns ``None`` when a rule of the same name already exists —
    the miner re-runs over an overlapping corpus and would otherwise propose the
    same rule on every pass.
    """
    rule.validate()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.standardization_rule
                (rule_id, field_name, rule_name, pattern, replacement, rule_kind,
                 target_fields, targets_check, state, priority, evidence_count,
                 evidence_sample, proposed_by, model_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (field_name, rule_name) DO NOTHING
            RETURNING rule_id
            """,
            (
                rule.rule_id or uuid7(), rule.field_name, rule.rule_name, rule.pattern,
                rule.replacement, rule.rule_kind, list(rule.target_fields),
                rule.targets_check, RuleState.PROPOSED, rule.priority,
                rule.evidence_count, Jsonb({"values": list(rule.evidence_sample)}),
                rule.proposed_by, rule.model_name,
            ),
        )
        row = cur.fetchone()
    return row[0] if row else None


def record_shadow_result(
    conn: psycopg.Connection,
    rule_id: uuid.UUID,
    *,
    matched: int,
    fixed: int,
    regressions: int,
    report: dict[str, Any],
) -> None:
    """Store the outcome of a shadow evaluation.

    Moves the rule to SHADOW. It is now reviewable but still not live: approval
    is a separate, human act.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mdm.standardization_rule
            SET state = 'SHADOW', shadow_tested_at = now(), shadow_matched = %s,
                shadow_fixed = %s, shadow_regressions = %s, shadow_report = %s,
                updated_at = now()
            WHERE rule_id = %s AND state IN ('PROPOSED', 'SHADOW')
            """,
            (matched, fixed, regressions, Jsonb(report), rule_id),
        )


def approve_rule(
    conn: psycopg.Connection, rule_id: uuid.UUID, *, reviewer: str, note: str | None = None
) -> bool:
    """Approve a shadow-tested rule and make it active.

    The database will refuse this for a rule that was never shadow-tested or
    that regressed anything, via check constraints on the table. That is
    deliberate: the approval gate must not be something a future refactor of
    this function can accidentally remove.

    Returns False when the rule was not in a state that permits approval.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mdm.standardization_rule
            SET state = 'ACTIVE', reviewed_by = %s, reviewed_at = now(),
                review_note = %s, updated_at = now()
            WHERE rule_id = %s AND state = 'SHADOW'
            RETURNING rule_id
            """,
            (reviewer, note, rule_id),
        )
        return cur.fetchone() is not None


def reject_rule(
    conn: psycopg.Connection, rule_id: uuid.UUID, *, reviewer: str, note: str | None = None
) -> bool:
    """Reject a proposed rule.

    Rejected rules are kept, not deleted. The miner would otherwise re-propose
    the same rule on its next pass, and a steward would review the same bad idea
    forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mdm.standardization_rule
            SET state = 'REJECTED', reviewed_by = %s, reviewed_at = now(),
                review_note = %s, updated_at = now()
            WHERE rule_id = %s AND state IN ('PROPOSED', 'SHADOW')
            RETURNING rule_id
            """,
            (reviewer, note, rule_id),
        )
        return cur.fetchone() is not None

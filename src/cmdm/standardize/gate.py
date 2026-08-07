"""The quality gate.

The hinge of the hybrid design. After the vectorized deterministic pass has done
what it can, this decides — per field, per record — whether the result is good
enough. Records that pass never touch a model. Only failures go to the AI
fallback, which is what keeps AI cost proportional to how messy the data
actually is rather than to how much of it there is.

Every check is a Polars expression, so the gate costs one pass over the batch
regardless of how many checks are declared. A gate that was itself slow would
defeat its own purpose.

Two design choices worth stating:

**Checks are declared, not hard-coded.** A :class:`QualityCheck` names a field,
a predicate and a reason. The gate is therefore introspectable — the observability
stage reports pass rates per check, and the rule-mining agent groups exceptions
by which check failed, which is what makes recurring patterns visible.

**A check answers "is this usable", not "is this correct".** The gate cannot
know whether ``JOHN SMITH`` is the right name; it can know that a name which
parsed into eight tokens, or contains digits, or produced no phonetic key, is
not something the matcher should be trusted with. Confusing those two questions
is how a quality gate turns into a validation rule that rejects real data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

__all__ = [
    "QualityCheck",
    "CHECKS",
    "checks_for",
    "gate_expressions",
    "apply_gate",
    "gate_summary",
    "FAILED_CHECKS_COLUMN",
    "GATE_PASSED_COLUMN",
]

#: Column holding the list of check names a record failed. Empty list means the
#: record passed. A list rather than a boolean because the AI fallback is
#: prompted differently depending on what went wrong, and because the mining
#: agent groups by exactly this.
FAILED_CHECKS_COLUMN = "failed_checks"

#: Convenience boolean derived from the above.
GATE_PASSED_COLUMN = "gate_passed"


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """One declared quality condition.

    ``predicate`` must evaluate to a boolean expression that is **true when the
    record is acceptable**. Null-safety is the check's own responsibility: a
    null in a boolean column is not false, and a check that returns null for
    missing input would silently neither pass nor fail.
    """

    name: str
    field: str
    reason: str
    predicate: pl.Expr
    #: Only evaluated where this is true. Used to skip checks that are
    #: meaningless for a record — date-of-birth quality for an organization,
    #: for instance.
    applies_when: pl.Expr | None = None

    def evaluate(self) -> pl.Expr:
        """Boolean expression: true when this record satisfies the check."""
        ok = self.predicate.fill_null(False)
        if self.applies_when is not None:
            # Not applicable counts as passing. A check that does not apply must
            # not push a record into the AI path.
            return pl.when(self.applies_when.fill_null(False)).then(ok).otherwise(True)
        return ok


def _has(column: str) -> pl.Expr:
    """True where a text column carries something other than blank."""
    return pl.col(column).is_not_null() & (pl.col(column).cast(pl.String).str.len_chars() > 0)


# ---------------------------------------------------------------------------
# The declared checks
# ---------------------------------------------------------------------------

#: Names longer than this almost always mean two parties were concatenated into
#: one field, or a company name landed in a person column. Either way the token
#: set is not a person's name and the matcher should not treat it as one.
MAX_NAME_TOKENS = 6

CHECKS: tuple[QualityCheck, ...] = (
    # -- name -------------------------------------------------------------
    QualityCheck(
        name="name_present",
        field="full_name",
        reason="No usable name after normalization.",
        predicate=_has("full_name_normalized"),
    ),
    QualityCheck(
        name="name_token_count",
        field="full_name",
        reason=(
            "Name normalized to an implausible number of tokens: either a single "
            "token with no surname, or so many that two parties were probably "
            "concatenated into one field."
        ),
        predicate=(pl.col("name_tokens").list.len() >= 2)
        & (pl.col("name_tokens").list.len() <= MAX_NAME_TOKENS),
        # Organizations legitimately have one token ("ACME") or many, so this
        # test is about natural persons only.
        applies_when=pl.col("party_type") == "PERSON",
    ),
    QualityCheck(
        name="name_no_digits",
        field="full_name",
        reason="Name contains digits, which usually means a reference number leaked into it.",
        predicate=~pl.col("full_name_normalized").str.contains(r"\d"),
    ),
    QualityCheck(
        name="name_parsed_cleanly",
        field="full_name",
        reason="Given name and surname could not be separated.",
        # Tests the components, not the confidence score. A learned EXTRACT rule
        # populates the components; if this check asked about confidence -- a
        # number only the built-in pass writes -- then no rule could ever satisfy
        # the check it was mined to satisfy, and the learning loop would propose
        # rules that fix nothing forever.
        predicate=_has("given_name_derived") & _has("surname_derived"),
        applies_when=pl.col("party_type") == "PERSON",
    ),
    QualityCheck(
        name="name_not_placeholder",
        field="full_name",
        reason="Name is a placeholder rather than a party.",
        # Anchored alternation over whole normalized strings, so a real surname
        # containing one of these substrings is unaffected.
        predicate=~pl.col("full_name_normalized").str.contains(
            r"^(?:UNKNOWN|N ?A|NA|NIL|NONE|TEST|DUMMY|XXX+|SAME AS ABOVE|TBC|TBA)$"
        ),
    ),
    QualityCheck(
        name="name_has_phonetic_key",
        field="full_name",
        reason="Name produced no phonetic key, so it cannot be blocked on.",
        predicate=_has("name_phonetic_key"),
    ),

    # -- email ------------------------------------------------------------
    QualityCheck(
        name="email_syntax",
        field="email_address",
        reason="Email was populated but does not parse as an address.",
        predicate=_has("email_normalized"),
        # Only judged where the source supplied something. A missing email is
        # missing data, not a standardization failure, and sending every record
        # without one to a model would be absurd.
        applies_when=_has("email_address"),
    ),

    # -- phone ------------------------------------------------------------
    QualityCheck(
        name="phone_syntax",
        field="phone_raw",
        reason="Phone was populated but did not normalize to E.164.",
        predicate=_has("phone_e164"),
        applies_when=_has("phone_raw"),
    ),

    # -- address ----------------------------------------------------------
    QualityCheck(
        name="address_has_number",
        field="address_line1",
        reason="Address line carries no numeric token, so no co-residence key can be built.",
        predicate=pl.col("address_normalized").str.contains(r"\d"),
        applies_when=_has("address_line1"),
    ),
    QualityCheck(
        name="address_has_postcode",
        field="postal_code",
        reason="Postal code missing, so the address cannot be blocked on.",
        predicate=_has("postal_code"),
        applies_when=_has("address_line1"),
    ),
    QualityCheck(
        name="address_split",
        field="address_line1",
        reason=(
            "Address did not split into a usable street/locality/postcode "
            "triple; it is probably one run-together string."
        ),
        predicate=_has("address_key") & _has("city"),
        applies_when=_has("address_line1"),
    ),

    # -- dates ------------------------------------------------------------
    QualityCheck(
        name="dob_plausible",
        field="date_of_birth",
        reason="Date of birth is outside a plausible range for a living policyholder.",
        predicate=(pl.col("date_of_birth") > pl.date(1900, 1, 1))
        & (pl.col("date_of_birth") < pl.date(2020, 1, 1)),
        applies_when=pl.col("date_of_birth").is_not_null(),
    ),
)


def checks_for(fields: Sequence[str] | None = None) -> tuple[QualityCheck, ...]:
    """The checks whose inputs are present in a frame.

    A batch that carries no address columns should not fail every address check;
    it should not run them. This filters by the columns actually available.
    """
    if fields is None:
        return CHECKS
    available = set(fields)
    required = {
        "name_present": {"full_name_normalized"},
        "name_token_count": {"name_tokens", "party_type"},
        "name_no_digits": {"full_name_normalized"},
        "name_parsed_cleanly": {"given_name_derived", "surname_derived", "party_type"},
        "name_not_placeholder": {"full_name_normalized"},
        "name_has_phonetic_key": {"name_phonetic_key"},
        "email_syntax": {"email_address", "email_normalized"},
        "phone_syntax": {"phone_raw", "phone_e164"},
        "address_has_number": {"address_line1", "address_normalized"},
        "address_has_postcode": {"address_line1", "postal_code"},
        "address_split": {"address_line1", "address_key", "city"},
        "dob_plausible": {"date_of_birth"},
    }
    return tuple(c for c in CHECKS if required.get(c.name, set()) <= available)


def gate_expressions(checks: Sequence[QualityCheck]) -> list[pl.Expr]:
    """One boolean column per check, named after it.

    Kept as individual columns rather than collapsed immediately, because the
    observability stage reports pass rate per check and the mining agent groups
    exceptions by which check failed. Collapsing early would throw that away.
    """
    return [c.evaluate().alias(f"_qc_{c.name}") for c in checks]


def apply_gate(
    frame: pl.DataFrame | pl.LazyFrame,
    checks: Sequence[QualityCheck] | None = None,
    *,
    keep_check_columns: bool = False,
) -> pl.LazyFrame:
    """Evaluate the gate, adding ``failed_checks`` and ``gate_passed``.

    One pass over the batch. The failed-check names are assembled with a
    concat-list of conditional literals, which stays vectorized — building that
    list per row in Python would make the gate cost more than the normalization
    it is judging.
    """
    lazy = frame.lazy() if isinstance(frame, pl.DataFrame) else frame
    columns = lazy.collect_schema().names()
    checks = checks if checks is not None else checks_for(columns)

    if not checks:
        return lazy.with_columns(
            pl.lit([], dtype=pl.List(pl.String)).alias(FAILED_CHECKS_COLUMN),
            pl.lit(True).alias(GATE_PASSED_COLUMN),
        )

    lazy = lazy.with_columns(gate_expressions(checks))

    # A null-typed literal in a concat_list would poison the whole list, so each
    # element is an empty-string sentinel that is filtered out afterwards.
    parts = [
        pl.when(pl.col(f"_qc_{c.name}")).then(pl.lit("")).otherwise(pl.lit(c.name))
        for c in checks
    ]
    lazy = lazy.with_columns(
        pl.concat_list(parts)
        .list.eval(pl.element().filter(pl.element().str.len_chars() > 0))
        .alias(FAILED_CHECKS_COLUMN)
    ).with_columns(
        (pl.col(FAILED_CHECKS_COLUMN).list.len() == 0).alias(GATE_PASSED_COLUMN)
    )

    if not keep_check_columns:
        lazy = lazy.drop([f"_qc_{c.name}" for c in checks])
    return lazy


def gate_summary(gated: pl.DataFrame) -> pl.DataFrame:
    """Per-check failure counts, for the observability stage.

    Reported as counts rather than only a total, because "3% of records failed
    the gate" is a number and "2.8% of those failed address_split" is an action.
    """
    total = gated.height
    if total == 0:
        return pl.DataFrame(
            schema={"check": pl.String, "failures": pl.UInt32, "failure_rate": pl.Float64}
        )

    # empty_as_null pinned explicitly: the default changes in Polars 2.0, and a
    # silent flip would turn every clean record into a phantom null row here.
    exploded = gated.select(
        pl.col(FAILED_CHECKS_COLUMN).explode(empty_as_null=True).alias("check")
    ).drop_nulls()
    counts = exploded.group_by("check").len(name="failures")
    return counts.with_columns(
        (pl.col("failures") / total).alias("failure_rate")
    ).sort("failures", descending=True)

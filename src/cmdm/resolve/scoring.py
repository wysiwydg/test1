"""Vectorized composite scoring and the tri-zone split.

Candidate pairs get a composite similarity score in [0, 1] and fall into one of
three zones:

*   **Auto-match** at or above 0.85 — confirmed duplicate, merged without review.
*   **Auto-reject** below 0.50 — confirmed distinct.
*   **Grey zone** in between — genuinely ambiguous, and the only pairs a model
    ever sees.

The grey zone is the design's whole point. A binary threshold forces a call on
every pair including the ones the evidence cannot support; a three-way split
lets the cheap scorer decline, and routes only the declined pairs to something
expensive enough to reason about them.

Everything here is a Polars expression over the pair frame. Comparators are
computed as columns on millions of pairs at once, never per pair in Python.
Attributes are joined onto the pair frame once, left and right side, and every
comparator then reads two columns.

Two properties the scorer must have and which are easy to get wrong:

**Missing data must not look like disagreement.** Two records with no date of
birth between them have not disagreed about anything. A comparator that returns
0.0 for missing input would push every sparse record towards auto-reject, which
is precisely backwards — sparse records are where duplicates hide. Comparators
therefore return null for "no evidence", and the composite renormalizes over the
comparators that actually fired.

**A veto must be able to overrule a high score.** Two different populated dates
of birth mean two different people regardless of how identical the names are.
Vetoes are applied after the weighted sum, not as another weighted term, because
a term can always be outvoted and a veto must not be.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import polars as pl

__all__ = [
    "Comparator",
    "COMPARATORS",
    "VETOES",
    "AUTO_MATCH_THRESHOLD",
    "AUTO_REJECT_THRESHOLD",
    "Zone",
    "score_pairs",
    "attach_attributes",
    "ScoringReport",
]

#: At or above: confirmed duplicate.
AUTO_MATCH_THRESHOLD = 0.85
#: Below: confirmed distinct.
AUTO_REJECT_THRESHOLD = 0.50


class Zone:
    AUTO_MATCH = "AUTO_MATCH"
    GREY = "GREY"
    AUTO_REJECT = "AUTO_REJECT"


@dataclass(frozen=True, slots=True)
class Comparator:
    """One attribute comparison contributing to the composite score.

    ``build`` receives the left and right column expressions and returns a
    similarity in [0, 1], or **null where neither side carries evidence**. The
    null is load-bearing: it is how "we cannot tell" is distinguished from "they
    disagree", and conflating those two is the single most common way a
    matching engine is made to fail on exactly the records it exists to fix.
    """

    name: str
    column: str
    weight: float
    build: object  # Callable[[pl.Expr, pl.Expr], pl.Expr]

    def evaluate(self) -> pl.Expr:
        left = pl.col(f"l_{self.column}")
        right = pl.col(f"r_{self.column}")
        both_present = (
            left.is_not_null()
            & right.is_not_null()
            & (left.cast(pl.String).str.len_chars() > 0)
            & (right.cast(pl.String).str.len_chars() > 0)
        )
        return (
            pl.when(both_present)
            .then(self.build(left, right))  # type: ignore[operator]
            .otherwise(None)
            .alias(f"cmp_{self.name}")
        )


def _exact(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    """1.0 on equality, 0.0 otherwise."""
    return (left == right).cast(pl.Float64)


def _jaccard_tokens(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    """Token-set Jaccard over two space-separated strings.

    Vectorized via list set operations rather than a per-pair Python loop.
    Order-insensitive, which matters because the two feeds routinely disagree
    about name order and that disagreement is not evidence of two people.
    """
    lt = left.str.split(" ")
    rt = right.str.split(" ")
    intersection = lt.list.set_intersection(rt).list.len()
    union = lt.list.set_union(rt).list.len()
    return (
        pl.when(union > 0)
        .then(intersection / union)
        .otherwise(0.0)
        .cast(pl.Float64)
    )


def _prefix_similarity(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    """Cheap edit-distance proxy: shared prefix over longer length.

    A true Levenshtein ratio would be better and is not available as a Polars
    kernel; computing it per pair in Python would cost more than every other
    comparator combined. Shared prefix captures most of the signal for names,
    where typos and truncations cluster at the end.
    """
    shorter = pl.min_horizontal(left.str.len_chars(), right.str.len_chars())
    longer = pl.max_horizontal(left.str.len_chars(), right.str.len_chars())
    # Compare successively longer prefixes; summing the equality of each prefix
    # length yields the length of the shared prefix without a loop over pairs.
    matches = sum(
        (
            (left.str.slice(0, n) == right.str.slice(0, n)) & (shorter >= n)
        ).cast(pl.Int32)
        for n in range(1, 13)
    )
    return (
        pl.when(longer > 0).then(matches / longer).otherwise(0.0).clip(0.0, 1.0)
    )


def _date_within(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    """Graded date agreement.

    Exact is 1.0; within a year scores partially, because transposed digits and
    an off-by-one year are common in hand-keyed dates and should not read as a
    flat disagreement.
    """
    delta = (left - right).dt.total_days().abs()
    return (
        pl.when(delta == 0).then(1.0)
        .when(delta <= 2).then(0.9)
        .when(delta <= 366).then(0.4)
        .otherwise(0.0)
    )


#: The declared comparator set. Weights are relative; the composite normalizes.
COMPARATORS: tuple[Comparator, ...] = (
    Comparator("national_id", "national_id_hash", 3.0, _exact),
    Comparator("email", "email_normalized", 2.5, _exact),
    Comparator("phone", "phone_e164", 2.0, _exact),
    Comparator("name_sorted", "name_sorted_key", 2.0, _exact),
    Comparator("name_tokens", "full_name_normalized", 1.5, _jaccard_tokens),
    Comparator("name_prefix", "full_name_normalized", 1.0, _prefix_similarity),
    Comparator("name_phonetic", "name_phonetic_key", 1.2, _exact),
    Comparator("dob", "date_of_birth", 2.5, _date_within),
    Comparator("postcode", "postal_code", 0.8, _exact),
    Comparator("address", "address_key", 1.5, _exact),
    Comparator("gender", "gender", 0.3, _exact),
)


@dataclass(frozen=True, slots=True)
class Veto:
    """A condition that forces a non-match regardless of score."""

    name: str
    condition: object  # Callable[[], pl.Expr]
    reason: str


def _dob_conflict() -> pl.Expr:
    """Two populated, materially different dates of birth."""
    left, right = pl.col("l_date_of_birth"), pl.col("r_date_of_birth")
    return (
        left.is_not_null()
        & right.is_not_null()
        & ((left - right).dt.total_days().abs() > 366)
    )


def _party_type_conflict() -> pl.Expr:
    """A natural person and a legal entity are not the same party."""
    left, right = pl.col("l_party_type"), pl.col("r_party_type")
    person = pl.lit("PERSON")
    return (
        left.is_not_null()
        & right.is_not_null()
        & (left != right)
        & ((left == person) | (right == person))
    )


VETOES: tuple[Veto, ...] = (
    Veto("dob_conflict", _dob_conflict,
         "Different populated dates of birth."),
    Veto("party_type_conflict", _party_type_conflict,
         "One party is a natural person and the other is a legal entity."),
)


@dataclass(slots=True)
class ScoringReport:
    """Zone counts for one scoring pass."""

    pairs: int = 0
    auto_match: int = 0
    grey: int = 0
    auto_reject: int = 0
    vetoed: int = 0
    comparator_coverage: dict[str, float] = field(default_factory=dict)

    @property
    def grey_share(self) -> float:
        """Fraction of pairs the scorer declined to decide.

        The number that sizes the AI stage. A grey zone that is a large share of
        all pairs means the thresholds or the comparators need work, not that
        more model capacity is needed.
        """
        return self.grey / self.pairs if self.pairs else 0.0


def attach_attributes(
    pairs: pl.DataFrame,
    parties: pl.DataFrame,
    *,
    id_column: str = "person_id",
    columns: Sequence[str] | None = None,
) -> pl.DataFrame:
    """Join party attributes onto both sides of each pair.

    Done once, as two hash joins, rather than per comparator. Columns are
    prefixed ``l_`` and ``r_`` so every comparator is a pure column expression
    with no join of its own.
    """
    needed = set(columns or [c.column for c in COMPARATORS])
    needed |= {"party_type", "date_of_birth"}
    available = [c for c in needed if c in parties.columns]

    left = parties.select([id_column, *available]).rename(
        {c: f"l_{c}" for c in available} | {id_column: "left_id"}
    )
    right = parties.select([id_column, *available]).rename(
        {c: f"r_{c}" for c in available} | {id_column: "right_id"}
    )
    return pairs.join(left, on="left_id", how="left").join(right, on="right_id", how="left")


def score_pairs(
    pairs: pl.DataFrame,
    parties: pl.DataFrame,
    *,
    comparators: Sequence[Comparator] = COMPARATORS,
    vetoes: Sequence[Veto] = VETOES,
    auto_match: float = AUTO_MATCH_THRESHOLD,
    auto_reject: float = AUTO_REJECT_THRESHOLD,
    id_column: str = "person_id",
) -> tuple[pl.DataFrame, ScoringReport]:
    """Score candidate pairs and assign each to a zone.

    Returns the pair frame with ``score``, ``zone``, ``vetoed`` and one
    ``cmp_*`` column per comparator. The per-comparator columns are kept rather
    than discarded after summing: a composite score that cannot be broken down
    is reportable but not explainable, and a steward reviewing a grey-zone pair
    needs to see which attributes agreed.
    """
    report = ScoringReport(pairs=pairs.height)
    if pairs.height == 0:
        return (
            pairs.with_columns(
                pl.lit(0.0).alias("score"),
                pl.lit(Zone.AUTO_REJECT).alias("zone"),
                pl.lit(False).alias("vetoed"),
            ),
            report,
        )

    enriched = attach_attributes(pairs, parties, id_column=id_column)

    active = [
        c for c in comparators
        if f"l_{c.column}" in enriched.columns and f"r_{c.column}" in enriched.columns
    ]
    if not active:
        raise ValueError(
            "no comparator has both sides available; check that the party frame "
            "carries the attribute columns the comparators name"
        )

    scored = enriched.with_columns([c.evaluate() for c in active])

    # Weighted mean over the comparators that fired. Renormalizing by the weight
    # actually present is what stops a sparse pair from being penalized for the
    # attributes nobody recorded.
    weighted = [
        (pl.col(f"cmp_{c.name}").fill_null(0.0) * c.weight) for c in active
    ]
    present = [
        (pl.col(f"cmp_{c.name}").is_not_null().cast(pl.Float64) * c.weight) for c in active
    ]
    total_weight = sum(present)

    scored = scored.with_columns(
        pl.when(total_weight > 0)
        .then(sum(weighted) / total_weight)
        .otherwise(0.0)
        .clip(0.0, 1.0)
        .alias("score"),
        total_weight.alias("evidence_weight"),
    )

    active_vetoes = [
        v for v in vetoes
        if all(
            f"{side}_{col}" in scored.columns
            for side in ("l", "r")
            for col in (["date_of_birth"] if v.name == "dob_conflict" else ["party_type"])
        )
    ]
    if active_vetoes:
        veto_expr = active_vetoes[0].condition()  # type: ignore[operator]
        for v in active_vetoes[1:]:
            veto_expr = veto_expr | v.condition()  # type: ignore[operator]
        scored = scored.with_columns(veto_expr.fill_null(False).alias("vetoed"))
    else:
        scored = scored.with_columns(pl.lit(False).alias("vetoed"))

    # Vetoes are applied after the sum, not as another weighted term. A term can
    # always be outvoted by enough agreeing attributes; a veto must not be.
    scored = scored.with_columns(
        pl.when(pl.col("vetoed"))
        .then(pl.lit(Zone.AUTO_REJECT))
        .when(pl.col("score") >= auto_match)
        .then(pl.lit(Zone.AUTO_MATCH))
        .when(pl.col("score") < auto_reject)
        .then(pl.lit(Zone.AUTO_REJECT))
        .otherwise(pl.lit(Zone.GREY))
        .alias("zone")
    )

    counts = scored.group_by("zone").len(name="n")
    by_zone = {r["zone"]: int(r["n"]) for r in counts.iter_rows(named=True)}
    report.auto_match = by_zone.get(Zone.AUTO_MATCH, 0)
    report.grey = by_zone.get(Zone.GREY, 0)
    report.auto_reject = by_zone.get(Zone.AUTO_REJECT, 0)
    report.vetoed = int(scored["vetoed"].sum())
    report.comparator_coverage = {
        c.name: float(scored[f"cmp_{c.name}"].is_not_null().mean() or 0.0) for c in active
    }

    keep = ["left_id", "right_id", "score", "zone", "vetoed", "evidence_weight"]
    if "blocking_keys" in scored.columns:
        keep.append("blocking_keys")
    keep += [f"cmp_{c.name}" for c in active]
    return scored.select(keep), report

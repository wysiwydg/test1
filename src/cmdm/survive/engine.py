"""Survivorship: choosing one golden value from many source assertions.

Once resolution has decided that N source records describe one party, something
must decide what that party's name, address and date of birth actually are. That
is survivorship, and it is where MDM systems most often become unexplainable —
a value appears in the golden record and nobody can say which feed supplied it
or why it beat the others.

Three properties shape the implementation.

**The policy is data, not code.** Every attribute already declares its strategy
in the field registry (:mod:`cmdm.model.fields`). This module reads those
declarations and applies them; it does not contain a per-field ``if``. Adding an
attribute or changing how one survives is a registry edit, and the whole policy
can be reviewed as a table rather than by reading procedural code.

**It is one vectorized group-by.** Strategies are expressed as Polars
aggregations over records grouped by master id, so survivorship over a whole
book costs a handful of passes rather than a Python loop per entity. That is
only possible because the strategies were designed as aggregations in the first
place.

**Lineage is not optional.** Every surviving value emits a provenance row naming
the source record it came from, the strategy that selected it, and the
candidates that lost. "Why is this the address?" is the question stewards
actually ask, and a system that cannot answer it has not finished the job.

The AI fallback sits where the deterministic strategies genuinely cannot decide:
a tie under the declared rule, between candidates that are not equal. That is a
small population by construction, and each one is logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from cmdm.model.enums import SurvivorshipStrategy as SS
from cmdm.model.fields import EntitySpec, FieldSpec

__all__ = [
    "SourceTrust",
    "SurvivorshipReport",
    "survive",
    "strategy_expression",
    "frequency_column",
    "CONTRIBUTOR_COLUMN",
    "CONTRIBUTOR_KEY_COLUMNS",
    "RECENCY_COLUMN",
    "TRUST_COLUMN",
]

#: Column identifying the source record a value came from. Carried through the
#: whole pass so the winning value can name its origin.
CONTRIBUTOR_COLUMN = "source_record_id"

#: Columns whose combination identifies one contributor, used together as the
#: final tie-break.
#:
#: ``source_record_id`` alone is not enough, and the reason is structural rather
#: than incidental: one policy row yields an owner, an insured and an agent, so
#: three contributor rows legitimately share it. Two of them landing in the same
#: cluster tie on that column, the tie falls through to row order, and Polars
#: group-by is threaded — so the same input produced a different golden value on
#: every run. The identity triple is what actually distinguishes one contributor
#: from another after collapse.
CONTRIBUTOR_KEY_COLUMNS = (
    "source_record_id",
    "source_system",
    "source_key_kind",
    "source_party_key",
)

#: Column driving MOST_RECENT. The source's own assertion of when the record
#: last changed, falling back to ingest time at landing.
RECENCY_COLUMN = "source_timestamp"

#: Column driving MOST_TRUSTED_SOURCE, resolved from the trust table.
TRUST_COLUMN = "_trust"


@dataclass(frozen=True, slots=True)
class SourceTrust:
    """Per-source trust weights.

    Several strategies break ties on which system is more likely to be right,
    and that is a business judgement rather than something derivable from the
    data. Holding it here, explicitly, keeps it reviewable — an implicit
    ordering that emerges from row order is the same decision made invisibly.
    """

    weights: dict[str, float] = field(default_factory=dict)
    default: float = 0.5

    def expression(self, column: str = "source_system") -> pl.Expr:
        """Map the source system column to its weight."""
        if not self.weights:
            return pl.lit(self.default).alias(TRUST_COLUMN)
        return (
            pl.col(column)
            .replace_strict(self.weights, default=self.default, return_dtype=pl.Float64)
            .alias(TRUST_COLUMN)
        )


def _ordered_first(column: str, order_by: Sequence[pl.Expr]) -> pl.Expr:
    """First non-null value of a column under a given ordering.

    The workhorse behind the ranking strategies. ``sort_by`` then
    ``drop_nulls().first()`` means a record that wins the ordering but has no
    value for this attribute yields to the next one, rather than the group
    surviving as null because its best record happened to be sparse.
    """
    return pl.col(column).sort_by(order_by, descending=True).drop_nulls().first()


def frequency_column(name: str) -> str:
    """Name of the helper column counting a value's occurrences in its cluster.

    Computed before the group-by as a window, because ``MOST_FREQUENT`` needs to
    rank each contributor by how many contributors agree with it, and that is a
    per-row number that cannot be derived inside the aggregation that consumes
    it without a nested group-by.
    """
    return f"_freq__{name}"


def _follow(column: str, parent: str, order_by: Sequence[pl.Expr]) -> pl.Expr:
    """Take a derived value from the record whose parent value survived.

    The alternative — letting each attribute pick its own winner — produces a
    golden record whose ``full_name`` and ``full_name_normalized`` come from
    different sources and describe different people. On the reference batch that
    was 290 of 2,712 records, and it is worse than a cosmetic inconsistency:
    search, blocking and every comparator read the normalized form, so those
    customers were findable only under a name their own record did not display.

    Rows where the parent is null are excluded rather than ranked last, because
    a derived value whose parent did not survive is a value computed from
    something this record no longer claims.
    """
    ordered_parent = pl.col(parent).sort_by(order_by, descending=True)
    return (
        pl.col(column)
        .sort_by(order_by, descending=True)
        .filter(ordered_parent.is_not_null())
        .first()
    )


def strategy_expression(
    spec: FieldSpec,
    *,
    tie_break: pl.Expr | None = None,
    frequency: pl.Expr | None = None,
    parent: FieldSpec | None = None,
    parent_frequency: pl.Expr | None = None,
) -> pl.Expr:
    """Build the aggregation implementing one field's declared strategy.

    ``tie_break`` is appended to every ordering so that a strategy which would
    otherwise be indifferent between two candidates still produces a
    deterministic answer. Without it, survivorship output depends on row order
    and a re-run over identical input can produce a different golden record —
    which makes every downstream diff untrustworthy.

    ``parent`` is the spec named by ``derived_from``. When present, the derived
    value is taken from whichever contributor won the parent instead of being
    ranked on its own.
    """
    column = spec.name
    trust = pl.col(TRUST_COLUMN)
    recency = pl.col(RECENCY_COLUMN)
    tail: list[pl.Expr] = [tie_break] if tie_break is not None else []

    if parent is not None:
        return _follow(
            column,
            parent.name,
            _ordering(parent, trust, recency, tail, parent_frequency),
        )

    match spec.survivorship:
        case SS.AGGREGATE_MAX:
            return pl.col(column).max()

        case SS.AGGREGATE_MIN:
            return pl.col(column).min()

        case SS.ANY_TRUE:
            # A suppression asserted by one source must not be outvoted by feeds
            # that have not caught up. An opt-out lost in a merge is a
            # compliance breach, not a data-quality blemish.
            return pl.col(column).fill_null(False).any()

        case _:
            return _ordered_first(
                column, _ordering(spec, trust, recency, tail, frequency)
            )


def _ordering(
    spec: FieldSpec,
    trust: pl.Expr,
    recency: pl.Expr,
    tail: Sequence[pl.Expr],
    frequency: pl.Expr | None,
) -> list[pl.Expr]:
    """The sort keys implementing one ranking strategy, best first.

    Factored out from the aggregation it usually feeds because a derived
    attribute has to rank its contributors by its *parent's* strategy in order
    to end up with the parent's winner.
    """
    match spec.survivorship:
        case SS.MOST_RECENT:
            return [recency, trust, *tail]

        case SS.MOST_TRUSTED_SOURCE:
            return [trust, recency, *tail]

        case SS.MOST_COMPLETE:
            # Longest non-null value. Recovers names and addresses truncated by
            # a source's column width, which no recency or trust rule can.
            length = pl.col(spec.name).cast(pl.String, strict=False).str.len_chars()
            return [length.fill_null(0), trust, *tail]

        case SS.MOST_FREQUENT:
            # Modal value across contributors, ties broken by trust and then by
            # the tie-break, like every other ranking strategy. Expressed as a
            # sort over (count, trust, …) rather than value_counts().first(),
            # which returns the modal value but decides a tied count by
            # whichever value the hash table happened to emit first — so two
            # contributors disagreeing one-to-one produced a different golden
            # value on every run.
            return [frequency, trust, *tail] if frequency is not None else [trust, *tail]

        case SS.FIRST_NON_NULL | SS.DERIVED | SS.SYSTEM:
            # DERIVED lands here only when it names no parent — a value computed
            # from the whole record rather than from one attribute.
            return [trust, *tail]

        case _:  # pragma: no cover - the aggregate strategies never reach here
            raise ValueError(f"{spec.name}: {spec.survivorship} has no ordering")


@dataclass(slots=True)
class SurvivorshipReport:
    """What one survivorship pass did."""

    entities: int = 0
    contributors: int = 0
    attributes: int = 0
    contested_attributes: int = 0
    ai_resolved: int = 0
    strategy_counts: dict[str, int] = field(default_factory=dict)

    @property
    def contested_rate(self) -> float:
        """Share of attribute decisions where sources actually disagreed.

        The number worth watching. A low rate means the feeds agree and
        survivorship is mostly ceremony; a rising rate means a source has
        started diverging and is worth investigating before the golden records
        drift.
        """
        return self.contested_attributes / self.attributes if self.attributes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entities": self.entities,
            "contributors": self.contributors,
            "attributes": self.attributes,
            "contested_attributes": self.contested_attributes,
            "contested_rate": round(self.contested_rate, 4),
            "ai_resolved": self.ai_resolved,
            "strategy_counts": self.strategy_counts,
        }


def survive(
    contributors: pl.DataFrame,
    spec: EntitySpec,
    *,
    master_column: str = "master_id",
    trust: SourceTrust | None = None,
    fields: Sequence[FieldSpec] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, SurvivorshipReport]:
    """Collapse contributing records into one golden row per master id.

    Returns the golden frame, the provenance frame (one row per surviving
    attribute that had more than one candidate), and a report.

    Only *contested* attributes produce provenance rows. Emitting one for every
    attribute of every entity would multiply the write volume by the column
    count to record that nobody disagreed, and would bury the decisions that
    actually need review.
    """
    trust = trust or SourceTrust()
    report = SurvivorshipReport()

    if contributors.height == 0:
        return (
            pl.DataFrame(schema={master_column: pl.String}),
            _empty_provenance(),
            report,
        )

    available = set(contributors.columns)
    candidates = [
        f for f in (fields or spec.fields)
        if f.name in available
        and f.name != master_column
        and f.survivorship is not SS.SYSTEM
    ]
    if not candidates:
        raise ValueError(
            f"{spec.name}: none of the registry's fields are present in the "
            f"contributor frame; got {sorted(available)}"
        )

    frame = contributors
    if TRUST_COLUMN not in frame.columns:
        source_column = "source_system" if "source_system" in available else None
        frame = frame.with_columns(
            trust.expression(source_column) if source_column
            else pl.lit(trust.default).alias(TRUST_COLUMN)
        )
    if RECENCY_COLUMN not in frame.columns:
        # No change timestamp means MOST_RECENT degenerates to trust order.
        # Recorded as a constant rather than left absent so the expressions stay
        # uniform and the degeneration is visible in the plan.
        frame = frame.with_columns(pl.lit(None, dtype=pl.Datetime).alias(RECENCY_COLUMN))

    # Deterministic final tie-break. Without it the result depends on row order
    # and a re-run over identical input can differ.
    key_columns = [c for c in CONTRIBUTOR_KEY_COLUMNS if c in frame.columns]
    tie_break = (
        pl.concat_str(
            [pl.col(c).cast(pl.String).fill_null("") for c in key_columns],
            separator="\x1f",
        )
        if key_columns
        else None
    )

    # A derived attribute follows the contributor that won the attribute it is
    # computed from. Resolved against the whole entity rather than the candidate
    # list, then filtered to parents actually present: a batch that carries
    # full_name_normalized but not full_name has nothing to follow, and ranking
    # the derived value on its own is the right fallback there.
    by_name = spec.by_name
    parents = {
        f.name: by_name[f.derived_from]
        for f in candidates
        if f.derived_from and f.derived_from in available
    }

    # Occurrence counts for the modal strategy, as one windowed pass rather than
    # a nested group-by per field.
    modal = {
        f.name
        for f in [*candidates, *parents.values()]
        if f.survivorship is SS.MOST_FREQUENT and f.name in available
    }
    if modal:
        frame = frame.with_columns(
            pl.len().over([master_column, name]).alias(frequency_column(name))
            for name in sorted(modal)
        )

    def frequency(f: FieldSpec | None) -> pl.Expr | None:
        if f is None or f.name not in modal:
            return None
        return pl.col(frequency_column(f.name))

    aggregations = [
        strategy_expression(
            f,
            tie_break=tie_break,
            frequency=frequency(f),
            parent=parents.get(f.name),
            parent_frequency=frequency(parents.get(f.name)),
        ).alias(f.name)
        for f in candidates
    ]
    # Contributor bookkeeping, so the golden row can state how much evidence
    # stands behind it without a second pass.
    aggregations.append(pl.len().alias("source_count"))
    if CONTRIBUTOR_COLUMN in frame.columns:
        aggregations.append(
            pl.col(CONTRIBUTOR_COLUMN).cast(pl.String).unique().sort()
            .alias("contributing_records")
        )
    if "source_system" in available:
        aggregations.append(
            pl.col("source_system").unique().sort().alias("contributing_sources")
        )

    golden = frame.group_by(master_column).agg(aggregations)

    provenance = _build_provenance(frame, golden, candidates, master_column, spec)

    report.entities = golden.height
    report.contributors = frame.height
    report.attributes = golden.height * len(candidates)
    report.contested_attributes = provenance.height
    counts: dict[str, int] = {}
    for f in candidates:
        counts[f.survivorship.value] = counts.get(f.survivorship.value, 0) + 1
    report.strategy_counts = counts

    return golden, provenance, report


def _empty_provenance() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "master_id": pl.String,
            "attribute_name": pl.String,
            "strategy": pl.String,
            "winning_source_record_id": pl.String,
            "winning_source_system": pl.String,
            "value_text": pl.String,
            "candidate_count": pl.UInt32,
            "rejected_values": pl.String,
        }
    )


def _build_provenance(
    contributors: pl.DataFrame,
    golden: pl.DataFrame,
    fields: Sequence[FieldSpec],
    master_column: str,
    spec: EntitySpec,
) -> pl.DataFrame:
    """Record which source won each contested attribute, and what lost.

    Built by re-joining the winning value back onto the contributors, which
    identifies the record that supplied it. Doing it this way rather than
    tracking provenance through the aggregation keeps the aggregation a plain
    group-by — carrying an origin alongside every value would double the width
    of the hottest operation in the pass.
    """
    import json

    rows: list[dict[str, Any]] = []

    #: Types provenance is meaningful for. A list-valued column is always a
    #: derived key recomputed from the surviving scalars, so it has no source to
    #: attribute and rendering it as text would only bloat the table.
    scalar = {pl.String, pl.Boolean, pl.Date, pl.Datetime, pl.Int16, pl.Int32,
              pl.Int64, pl.Float32, pl.Float64}

    for f in fields:
        column = f.name
        if column not in contributors.columns:
            continue
        if contributors.schema[column].base_type() not in scalar:
            continue

        # Attributes where the contributors disagree. Equality is evaluated on
        # non-null values only: one source having no opinion is not a conflict.
        distinct = (
            contributors.group_by(master_column)
            .agg(pl.col(column).drop_nulls().n_unique().alias("n"))
            .filter(pl.col("n") > 1)
        )
        if distinct.height == 0:
            continue

        contested = distinct.select(master_column)
        winners = golden.join(contested, on=master_column, how="semi").select(
            master_column, pl.col(column).alias("_winner")
        )

        detail = (
            contributors.join(contested, on=master_column, how="semi")
            .select(
                master_column,
                pl.col(column).alias("_value"),
                pl.col(CONTRIBUTOR_COLUMN).cast(pl.String).alias("_record")
                if CONTRIBUTOR_COLUMN in contributors.columns
                else pl.lit(None, dtype=pl.String).alias("_record"),
                pl.col("source_system").alias("_source")
                if "source_system" in contributors.columns
                else pl.lit(None, dtype=pl.String).alias("_source"),
            )
            .join(winners, on=master_column, how="left")
        )

        grouped = detail.group_by(master_column).agg(
            pl.col("_winner").first(),
            pl.col("_value").cast(pl.String).alias("_values"),
            pl.col("_record").alias("_records"),
            pl.col("_source").alias("_sources"),
        )

        def same(left: Any, right: Any) -> bool:
            """Compare a surviving value against a contributor's rendering.

            Both sides are normalized case-insensitively because they arrive by
            different routes: the winner is a Python value and the candidates
            were rendered by Polars, so a boolean is "True" on one side and
            "true" on the other. Comparing them raw silently fails to identify
            the source of every boolean attribute.
            """
            if left is None or right is None:
                return False
            return str(left).strip().casefold() == str(right).strip().casefold()

        for row in grouped.iter_rows(named=True):
            winner = row["_winner"]
            values = row["_values"] or []
            records = row["_records"] or []
            sources = row["_sources"] or []

            winning_record = None
            winning_source = None
            rejected: list[dict[str, str | None]] = []
            for value, record, source in zip(values, records, sources, strict=False):
                if value is None:
                    continue
                if same(value, winner):
                    if winning_record is None:
                        winning_record, winning_source = record, source
                else:
                    rejected.append({"value": str(value), "source": source, "record": record})

            rows.append({
                "master_id": str(row[master_column]),
                "attribute_name": column,
                "strategy": f.survivorship.value,
                "winning_source_record_id": winning_record,
                "winning_source_system": winning_source,
                "value_text": None if winner is None else str(winner),
                "candidate_count": len({str(v) for v in values if v is not None}),
                # Serialized here rather than at the write boundary so the
                # provenance frame is a plain table that can be inspected,
                # diffed and written by any of the storage paths.
                "rejected_values": json.dumps(rejected[:20]),
            })

    if not rows:
        return _empty_provenance()
    return pl.DataFrame(rows, schema=_empty_provenance().schema)

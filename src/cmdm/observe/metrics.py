"""Observability: operational metrics, match quality, and data quality.

Deliberately separated from anything business-facing. Three audiences with three
different questions, and mixing them produces a dashboard that serves none of
them:

*   **Operational** — is the pipeline keeping up? Queue depth, throughput, dead
    letters, error rates. Prometheus, scraped, alertable.
*   **Match quality** — is resolution correct? Precision, recall, zone
    distribution, how much the AI stage contributes. Computed against a labelled
    set, not inferred from volume.
*   **Data quality** — is the data itself any good? Completeness, conformity,
    duplication rate, gate pass rate.

The separation matters most for match quality. It is tempting to put "records
merged today" on a business dashboard, where it reads as productivity. It is not
productivity — a spike in merges is as likely to mean the thresholds slipped as
that the data improved, and presenting it next to premium totals invites exactly
the wrong reading.

**Precision and recall need labels.** They cannot be computed from production
output alone: a system cannot mark its own homework. This module computes them
against a supplied labelled set, and reports honestly when none exists rather
than substituting a proxy that looks like an accuracy number and is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row

__all__ = [
    "MatchQuality",
    "evaluate_matching",
    "DataQuality",
    "assess_data_quality",
    "OperationalSnapshot",
    "operational_snapshot",
    "registry",
    "PIPELINE_METRICS",
    "render_prometheus",
]


# ---------------------------------------------------------------------------
# Match quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchQuality:
    """Pairwise precision and recall against a labelled set.

    Pairwise rather than cluster-level, because that is the measure that
    degrades gracefully: a cluster metric scores a nearly-correct cluster as
    entirely wrong, which makes small regressions invisible and large ones
    indistinguishable from each other.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    labelled_pairs: int
    predicted_pairs: int

    @property
    def precision(self) -> float:
        """Of the merges made, how many were right.

        The number to watch. A false positive merges two real people into one
        record, which is visible to the customer and expensive to unpick;
        a false negative merely leaves a duplicate, which is the failure the
        system already exists to reduce.
        """
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        """Of the duplicates that exist, how many were found."""
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "labelled_pairs": self.labelled_pairs,
            "predicted_pairs": self.predicted_pairs,
        }


def _pairs_within(frame: pl.DataFrame, group: str, member: str) -> set[tuple[str, str]]:
    """Every unordered within-group pair.

    Built from grouped lists rather than a self-join because the frames here are
    evaluation-sized, and the explicit form makes the ordering guarantee
    (left < right, so a pair is never counted twice) obvious.
    """
    out: set[tuple[str, str]] = set()
    grouped = frame.group_by(group).agg(pl.col(member).sort().alias("members"))
    for row in grouped.iter_rows(named=True):
        members = row["members"]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out.add((members[i], members[j]))
    return out


def evaluate_matching(
    assignments: pl.DataFrame,
    labels: pl.DataFrame,
    *,
    id_column: str = "person_id",
    predicted_column: str = "master_id",
    label_column: str = "true_id",
) -> MatchQuality:
    """Score resolution output against a labelled set.

    ``assignments`` is what the resolver decided; ``labels`` is ground truth for
    the same ids. Only ids present in both are scored — a labelled set usually
    covers a sample rather than the whole book, and silently treating unlabelled
    records as non-duplicates would manufacture false positives that are really
    just missing labels.
    """
    joined = assignments.join(labels, on=id_column, how="inner")
    if joined.height == 0:
        return MatchQuality(0, 0, 0, 0, 0)

    truth = _pairs_within(joined, label_column, id_column)
    predicted = _pairs_within(joined, predicted_column, id_column)

    return MatchQuality(
        true_positives=len(truth & predicted),
        false_positives=len(predicted - truth),
        false_negatives=len(truth - predicted),
        labelled_pairs=len(truth),
        predicted_pairs=len(predicted),
    )


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DataQuality:
    """Fitness of the golden records themselves."""

    entities: int = 0
    completeness: dict[str, float] = field(default_factory=dict)
    conformity: dict[str, float] = field(default_factory=dict)
    duplication_rate: float = 0.0
    multi_source_share: float = 0.0
    curated_share: float = 0.0

    @property
    def mean_completeness(self) -> float:
        values = list(self.completeness.values())
        return sum(values) / len(values) if values else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entities": self.entities,
            "mean_completeness": round(self.mean_completeness, 4),
            "completeness": {k: round(v, 4) for k, v in self.completeness.items()},
            "conformity": {k: round(v, 4) for k, v in self.conformity.items()},
            "duplication_rate": round(self.duplication_rate, 4),
            "multi_source_share": round(self.multi_source_share, 4),
            "curated_share": round(self.curated_share, 4),
        }


#: Attributes whose absence materially limits what can be done with a record.
#: Completeness over every column would be dominated by attributes nobody needs
#: and would move only when the schema changed.
CRITICAL_PERSON_FIELDS = (
    "full_name", "date_of_birth", "email_normalized", "phone_e164",
    "address_key", "postal_code",
)


def assess_data_quality(conn: psycopg.Connection) -> DataQuality:
    """Measure the golden store.

    Runs as aggregate SQL rather than by pulling records into memory: this is
    intended to run on a schedule against a full book, and the numbers are all
    expressible as counts.
    """
    quality = DataQuality()

    with conn.cursor(row_factory=dict_row) as cur:
        completeness_terms = ", ".join(
            f"avg(({c} IS NOT NULL)::int)::float8 AS {c}" for c in CRITICAL_PERSON_FIELDS
        )
        cur.execute(
            f"""
            SELECT count(*) AS n,
                   {completeness_terms},
                   avg((source_count > 1)::int)::float8 AS multi_source,
                   avg(is_curated::int)::float8 AS curated
            FROM mdm.person WHERE is_current AND NOT is_deleted
            """
        )
        row = cur.fetchone() or {}
        quality.entities = int(row.get("n") or 0)
        quality.completeness = {
            c: float(row.get(c) or 0.0) for c in CRITICAL_PERSON_FIELDS
        }
        quality.multi_source_share = float(row.get("multi_source") or 0.0)
        quality.curated_share = float(row.get("curated") or 0.0)

        # Conformity: does a populated value actually parse as what it claims to
        # be. Distinct from completeness -- a column that is always populated
        # with garbage scores perfectly on completeness alone.
        cur.execute(
            """
            SELECT
              avg((email_normalized ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$')::int)
                FILTER (WHERE email_normalized IS NOT NULL)::float8 AS email,
              avg((phone_e164 ~ '^\\+[0-9]{8,15}$')::int)
                FILTER (WHERE phone_e164 IS NOT NULL)::float8 AS phone,
              avg((date_of_birth BETWEEN '1900-01-01' AND now()::date)::int)
                FILTER (WHERE date_of_birth IS NOT NULL)::float8 AS date_of_birth,
              avg((given_name_derived IS NOT NULL AND surname_derived IS NOT NULL)::int)
                FILTER (WHERE party_type = 'PERSON')::float8 AS name_parsed
            FROM mdm.person WHERE is_current AND NOT is_deleted
            """
        )
        row = cur.fetchone() or {}
        quality.conformity = {
            k: float(v or 0.0) for k, v in row.items()
        }

        # Duplication rate: parties still holding more than one source identity
        # after resolution. Not a defect count -- it is what resolution *found*,
        # and a sudden fall usually means matching regressed rather than that the
        # sources cleaned themselves up.
        cur.execute(
            """
            SELECT
              count(*) FILTER (WHERE keys > 1)::float8 / nullif(count(*), 0) AS rate
            FROM (
                SELECT person_id, count(*) AS keys
                FROM mdm.person_xref WHERE is_active GROUP BY person_id
            ) t
            """
        )
        row = cur.fetchone() or {}
        quality.duplication_rate = float(row.get("rate") or 0.0)

    return quality


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OperationalSnapshot:
    """Is the pipeline keeping up."""

    queue_depth: dict[str, dict[str, int]] = field(default_factory=dict)
    dead_letters: int = 0
    oldest_pending_seconds: float | None = None
    batches_24h: int = 0
    batches_failed_24h: int = 0
    ai_standardization_24h: int = 0
    rules_awaiting_review: int = 0
    grey_zone_share: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_depth": self.queue_depth,
            "dead_letters": self.dead_letters,
            "oldest_pending_seconds": self.oldest_pending_seconds,
            "batches_24h": self.batches_24h,
            "batches_failed_24h": self.batches_failed_24h,
            "ai_standardization_24h": self.ai_standardization_24h,
            "rules_awaiting_review": self.rules_awaiting_review,
            "grey_zone_share": self.grey_zone_share,
        }


def operational_snapshot(conn: psycopg.Connection) -> OperationalSnapshot:
    """Current pipeline health."""
    from cmdm.db.queue import (
        QUEUE_INGEST,
        QUEUE_RESOLVE,
        QUEUE_STANDARDIZE,
        QUEUE_SURVIVE,
        WorkQueue,
    )

    snapshot = OperationalSnapshot()
    queue = WorkQueue(conn)

    oldest: list[float] = []
    for name in (QUEUE_INGEST, QUEUE_STANDARDIZE, QUEUE_RESOLVE, QUEUE_SURVIVE):
        depth = queue.depth(name)
        snapshot.queue_depth[name] = depth
        snapshot.dead_letters += depth.get("DEAD", 0)
        age = queue.oldest_pending_age_seconds(name)
        if age is not None:
            oldest.append(age)
    snapshot.oldest_pending_seconds = max(oldest) if oldest else None

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE state IN ('REJECTED', 'FAILED')) AS failed
            FROM mdm.ingest_batch WHERE submitted_at > now() - interval '24 hours'
            """
        )
        row = cur.fetchone() or {}
        snapshot.batches_24h = int(row.get("n") or 0)
        snapshot.batches_failed_24h = int(row.get("failed") or 0)

        cur.execute(
            "SELECT count(*) AS n FROM mdm.standardization_exception "
            "WHERE created_at > now() - interval '24 hours'"
        )
        snapshot.ai_standardization_24h = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            "SELECT count(*) AS n FROM mdm.standardization_rule WHERE state = 'SHADOW'"
        )
        snapshot.rules_awaiting_review = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            """
            SELECT grey_count::float8 / nullif(candidate_pairs, 0) AS share
            FROM mdm.resolution_run ORDER BY started_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if row and row.get("share") is not None:
            snapshot.grey_zone_share = float(row["share"])

    return snapshot


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

#: Declared once. The registry is module-level because Prometheus client
#: collectors must not be redefined per call -- doing so raises on the second
#: registration and takes the metrics endpoint down with it.
PIPELINE_METRICS: dict[str, Any] = {}


def registry():
    """Build (once) and return the Prometheus registry."""
    from prometheus_client import CollectorRegistry, Gauge

    if "registry" in PIPELINE_METRICS:
        return PIPELINE_METRICS["registry"]

    reg = CollectorRegistry()
    PIPELINE_METRICS.update({
        "registry": reg,
        "queue_depth": Gauge(
            "cmdm_queue_depth", "Jobs by queue and state",
            ["queue", "state"], registry=reg,
        ),
        "dead_letters": Gauge(
            "cmdm_dead_letters", "Jobs that exhausted their retries", registry=reg
        ),
        "oldest_pending": Gauge(
            "cmdm_oldest_pending_seconds",
            "Age of the oldest claimable job", registry=reg,
        ),
        "golden_entities": Gauge(
            "cmdm_golden_entities", "Current golden records", ["entity"], registry=reg
        ),
        "completeness": Gauge(
            "cmdm_completeness_ratio",
            "Share of golden records carrying an attribute", ["attribute"], registry=reg,
        ),
        "conformity": Gauge(
            "cmdm_conformity_ratio",
            "Share of populated values that parse correctly", ["attribute"], registry=reg,
        ),
        "duplication_rate": Gauge(
            "cmdm_duplication_rate",
            "Share of parties holding more than one source identity", registry=reg,
        ),
        "grey_zone_share": Gauge(
            "cmdm_grey_zone_share",
            "Share of candidate pairs routed to the model", registry=reg,
        ),
        "rules_awaiting_review": Gauge(
            "cmdm_rules_awaiting_review",
            "Learned rules shadow-tested and waiting for a steward", registry=reg,
        ),
        "match_precision": Gauge(
            "cmdm_match_precision", "Pairwise precision against the labelled set",
            registry=reg,
        ),
        "match_recall": Gauge(
            "cmdm_match_recall", "Pairwise recall against the labelled set", registry=reg
        ),
    })
    return reg


def render_prometheus(
    conn: psycopg.Connection, *, quality: MatchQuality | None = None
) -> bytes:
    """Collect every metric and render the exposition format.

    Match precision and recall are only set when a labelled set was supplied.
    Emitting a default of zero would be worse than emitting nothing: a dashboard
    would show a precision of 0% and an alert would fire on a system that is
    merely unmeasured.
    """
    from prometheus_client import generate_latest

    reg = registry()
    metrics = PIPELINE_METRICS

    operational = operational_snapshot(conn)
    for queue_name, states in operational.queue_depth.items():
        for state, count in states.items():
            metrics["queue_depth"].labels(queue=queue_name, state=state).set(count)
    metrics["dead_letters"].set(operational.dead_letters)
    if operational.oldest_pending_seconds is not None:
        metrics["oldest_pending"].set(operational.oldest_pending_seconds)
    metrics["rules_awaiting_review"].set(operational.rules_awaiting_review)
    if operational.grey_zone_share is not None:
        metrics["grey_zone_share"].set(operational.grey_zone_share)

    data = assess_data_quality(conn)
    metrics["golden_entities"].labels(entity="person").set(data.entities)
    for attribute, value in data.completeness.items():
        metrics["completeness"].labels(attribute=attribute).set(value)
    for attribute, value in data.conformity.items():
        metrics["conformity"].labels(attribute=attribute).set(value)
    metrics["duplication_rate"].set(data.duplication_rate)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mdm.policy WHERE is_current")
        metrics["golden_entities"].labels(entity="policy").set(cur.fetchone()[0])

    if quality is not None:
        metrics["match_precision"].set(quality.precision)
        metrics["match_recall"].set(quality.recall)

    return generate_latest(reg)

"""Identity resolution pipeline.

    parties
       │
       ├── (a) blocking ................... candidate pairs from shared keys
       ├── (b) vectorized scoring ......... composite score -> three zones
       │        auto-match >= 0.85 | grey 0.50-0.85 | auto-reject < 0.50
       ├── (c) cross-encoder .............. grey zone only
       └── (d) graph merge ................ SciPy connected components -> master ids

Every stage's output is persisted, including the rejections. Resolution
decisions are the ones a regulator, a steward and an angry customer all ask
about, and "why are these two records not merged" is unanswerable if only the
merges were recorded.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from cmdm.model.ids import uuid7
from cmdm.resolve.blocking import DEFAULT_KEYS, BlockingKey, BlockingReport, block_pairs
from cmdm.resolve.clustering import ClusterResult, cluster_pairs
from cmdm.resolve.crossencoder import (
    AI_ACCEPT_THRESHOLD,
    CrossEncoder,
    classify_grey_zone,
)
from cmdm.resolve.scoring import (
    AUTO_MATCH_THRESHOLD,
    AUTO_REJECT_THRESHOLD,
    COMPARATORS,
    ScoringReport,
    Zone,
    score_pairs,
)

__all__ = ["ResolutionReport", "resolve", "persist_run"]


@dataclass(slots=True)
class ResolutionReport:
    """Everything one resolution pass did."""

    run_id: uuid.UUID
    records: int = 0
    candidate_pairs: int = 0
    auto_match: int = 0
    grey: int = 0
    auto_reject: int = 0
    vetoed: int = 0
    ai_approved: int = 0
    ai_rejected: int = 0
    clusters: int = 0
    merged_records: int = 0
    largest_cluster: int = 0
    suspicious_clusters: list[tuple[str, int]] = field(default_factory=list)
    duration_ms: int = 0
    blocking: BlockingReport | None = None
    scoring: ScoringReport | None = None
    model_name: str = "none"

    @property
    def ai_share(self) -> float:
        """Fraction of candidate pairs that needed the model."""
        return self.grey / self.candidate_pairs if self.candidate_pairs else 0.0

    @property
    def collapse_ratio(self) -> float:
        return self.records / self.clusters if self.clusters else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "records": self.records,
            "candidate_pairs": self.candidate_pairs,
            "reduction_ratio": round(self.blocking.reduction_ratio, 1)
            if self.blocking and self.blocking.candidate_pairs
            else None,
            "auto_match": self.auto_match,
            "grey": self.grey,
            "auto_reject": self.auto_reject,
            "vetoed": self.vetoed,
            "ai_share": round(self.ai_share, 4),
            "ai_approved": self.ai_approved,
            "ai_rejected": self.ai_rejected,
            "clusters": self.clusters,
            "merged_records": self.merged_records,
            "collapse_ratio": round(self.collapse_ratio, 3),
            "largest_cluster": self.largest_cluster,
            "suspicious_clusters": self.suspicious_clusters[:10],
            "duration_ms": self.duration_ms,
            "model_name": self.model_name,
        }


def resolve(
    parties: pl.DataFrame,
    *,
    keys: Sequence[BlockingKey] = DEFAULT_KEYS,
    encoder: CrossEncoder | None = None,
    auto_match: float = AUTO_MATCH_THRESHOLD,
    auto_reject: float = AUTO_REJECT_THRESHOLD,
    ai_threshold: float = AI_ACCEPT_THRESHOLD,
    id_column: str = "person_id",
    conn: psycopg.Connection | None = None,
) -> tuple[ClusterResult, pl.DataFrame, ResolutionReport]:
    """Run the full resolution pipeline.

    Returns the cluster assignments, the scored pair frame (every pair, all
    zones, with the model verdict where one was sought), and the report.
    """
    started = time.perf_counter()
    run_id = uuid7()
    report = ResolutionReport(run_id=run_id, records=parties.height)

    if parties.height < 2:
        empty = pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String})
        return (
            cluster_pairs(
                empty,
                parties[id_column] if parties.height else [],
                id_column=id_column,
            ),
            empty,
            report,
        )

    # -- (a) blocking
    candidates, blocking_report = block_pairs(parties, keys=keys, id_column=id_column)
    report.blocking = blocking_report
    report.candidate_pairs = candidates.height

    if candidates.height == 0:
        result = cluster_pairs(candidates, parties[id_column], id_column=id_column)
        report.clusters = result.cluster_count
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return result, candidates, report

    # -- (b) vectorized scoring and tri-zone split
    scored, scoring_report = score_pairs(
        candidates, parties, auto_match=auto_match, auto_reject=auto_reject,
        id_column=id_column,
    )
    report.scoring = scoring_report
    report.auto_match = scoring_report.auto_match
    report.grey = scoring_report.grey
    report.auto_reject = scoring_report.auto_reject
    report.vetoed = scoring_report.vetoed

    # -- (c) cross-encoder, grey zone only
    grey = scored.filter(pl.col("zone") == Zone.GREY)
    decisions = []
    if grey.height:
        grey_scored, decisions = classify_grey_zone(
            grey, parties, encoder=encoder, threshold=ai_threshold, id_column=id_column
        )
        report.model_name = decisions[0].model_name if decisions else "none"
        report.ai_approved = int((grey_scored["ai_decision"] == "MATCH").sum())
        report.ai_rejected = grey.height - report.ai_approved

        scored = scored.join(
            grey_scored.select("left_id", "right_id", "ai_score", "ai_decision"),
            on=["left_id", "right_id"],
            how="left",
        )
    else:
        scored = scored.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("ai_score"),
            pl.lit(None, dtype=pl.String).alias("ai_decision"),
        )

    # -- (d) graph merge. Only accepted edges become graph edges: an auto-match,
    #        or a grey-zone pair the model approved. A rejected pair is recorded
    #        but contributes nothing, because one wrong edge silently unions two
    #        unrelated clusters and the damage scales with both.
    accepted = scored.filter(
        (pl.col("zone") == Zone.AUTO_MATCH)
        | ((pl.col("zone") == Zone.GREY) & (pl.col("ai_decision") == "MATCH"))
    )
    scored = scored.with_columns(
        pl.when(pl.col("zone") == Zone.AUTO_MATCH)
        .then(pl.lit("MATCH"))
        .when(pl.col("zone") == Zone.GREY)
        .then(pl.col("ai_decision").fill_null("REVIEW"))
        .otherwise(pl.lit("NO_MATCH"))
        .alias("final_decision"),
        pl.when(pl.col("zone") == Zone.GREY)
        .then(pl.lit("AI_FALLBACK"))
        .otherwise(pl.lit("PROBABILISTIC"))
        .alias("decided_by"),
    )

    result = cluster_pairs(accepted, parties[id_column], id_column=id_column)
    report.clusters = result.cluster_count
    report.merged_records = result.merged_records
    report.largest_cluster = result.largest_cluster
    report.suspicious_clusters = result.suspicious_clusters
    report.duration_ms = int((time.perf_counter() - started) * 1000)

    if conn is not None:
        persist_run(conn, report, scored, auto_match=auto_match, auto_reject=auto_reject)

    return result, scored, report


def persist_run(
    conn: psycopg.Connection,
    report: ResolutionReport,
    scored: pl.DataFrame,
    *,
    auto_match: float,
    auto_reject: float,
) -> None:
    """Write the run and every scored pair.

    Pairs are written with COPY through a staging table: a resolution pass over a
    real book produces millions of rows and row-at-a-time insertion would take
    longer than the resolution itself.

    The thresholds are stored on the run, not assumed. They change as the model
    is tuned, and a stored decision is only interpretable alongside the
    configuration that produced it.
    """
    weights = {c.name: c.weight for c in COMPARATORS}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.resolution_run
                (run_id, auto_match_threshold, auto_reject_threshold, comparator_weights,
                 model_name, candidate_pairs, auto_match_count, grey_count,
                 auto_reject_count, ai_approved_count, cluster_count,
                 finished_at, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            """,
            (
                report.run_id, auto_match, auto_reject, Jsonb(weights), report.model_name,
                report.candidate_pairs, report.auto_match, report.grey,
                report.auto_reject, report.ai_approved, report.clusters,
                report.duration_ms,
            ),
        )

        if scored.height == 0:
            return

        comparator_columns = [c for c in scored.columns if c.startswith("cmp_")]

        cur.execute(
            """
            CREATE TEMP TABLE _pair_stage (
                pair_id uuid, run_id uuid, left_person_id uuid, right_person_id uuid,
                blocking_key text, score double precision, zone text,
                comparator_scores jsonb, ai_score double precision, ai_decision text,
                model_name text, final_decision text, decided_by text
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _pair_stage (pair_id, run_id, left_person_id, right_person_id, "
            "blocking_key, score, zone, comparator_scores, ai_score, ai_decision, "
            "model_name, final_decision, decided_by) FROM STDIN"
        ) as copy:
            for row in scored.iter_rows(named=True):
                copy.write_row((
                    uuid7(), report.run_id, row["left_id"], row["right_id"],
                    row.get("blocking_keys"), float(row["score"]), row["zone"],
                    Jsonb({c[4:]: row.get(c) for c in comparator_columns}),
                    row.get("ai_score"), row.get("ai_decision"),
                    report.model_name if row.get("ai_score") is not None else None,
                    row.get("final_decision", "NO_MATCH"),
                    row.get("decided_by", "PROBABILISTIC"),
                ))

        cur.execute(
            """
            INSERT INTO mdm.match_pair
                (pair_id, run_id, left_person_id, right_person_id, blocking_key,
                 score, zone, comparator_scores, ai_score, ai_decision, model_name,
                 final_decision, decided_by)
            SELECT pair_id, run_id, left_person_id, right_person_id, blocking_key,
                   score, zone::mdm.match_zone, comparator_scores, ai_score,
                   ai_decision, model_name, final_decision, decided_by
            FROM _pair_stage
            ON CONFLICT (run_id, left_person_id, right_person_id) DO NOTHING
            """
        )

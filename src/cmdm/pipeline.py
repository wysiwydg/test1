"""End-to-end orchestration.

    raw batch
      → land            immutable, content-addressed        (cmdm.ingest.landing)
      → shred           policy grain → three entity grains  (cmdm.ingest.shred)
      → standardize     deterministic → gate → AI → learn   (cmdm.standardize)
      → resolve         block → score → grey zone → graph   (cmdm.resolve)
      → survive         registry-driven, with lineage       (cmdm.survive)
      → write           SCD-2 golden records + crosswalk    (cmdm.store)

Each stage is independently usable and independently tested; this module is the
composition, not the implementation. It exists because the interesting failures
are at the seams — a column one stage derives and the next expects, an id space
that changes meaning between resolution and writing — and those are only visible
when the whole thing runs.

The stage boundary that needs the most care is between resolution and writing.
Resolution works on *source-scoped* identities: a party as one system knows it.
Writing works on *golden* identities: the master record. The mapping between
them is the crosswalk, so this module resolves clusters into master ids, mints a
golden id per cluster, and writes the crosswalk in the same transaction as the
entities themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg

from cmdm.ingest.mapping import SourceMapping
from cmdm.ingest.shred import shred
from cmdm.model.fields import PERSON, POLICY
from cmdm.model.ids import uuid7
from cmdm.resolve import ResolutionReport, resolve
from cmdm.standardize import StandardizationReport, standardize
from cmdm.store import upsert_xref, write_entities
from cmdm.survive import SourceTrust, SurvivorshipReport, survive

__all__ = ["PipelineResult", "run_pipeline", "SOURCE_IDENTITY_COLUMN"]

#: The identity resolution works on: a party as one source system knows it.
#: Distinct from the golden person_id, which only exists after clustering.
SOURCE_IDENTITY_COLUMN = "source_identity"


@dataclass(slots=True)
class PipelineResult:
    """What one end-to-end run did, stage by stage."""

    batch_id: uuid.UUID | None = None
    policies: int = 0
    party_occurrences: int = 0
    source_identities: int = 0
    golden_persons: int = 0
    standardization: StandardizationReport | None = None
    resolution: ResolutionReport | None = None
    survivorship: SurvivorshipReport | None = None
    writes: dict[str, dict[str, Any]] = field(default_factory=dict)
    xref_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "policies": self.policies,
            "party_occurrences": self.party_occurrences,
            "source_identities": self.source_identities,
            "golden_persons": self.golden_persons,
            "standardization": self.standardization.as_dict() if self.standardization else None,
            "resolution": self.resolution.as_dict() if self.resolution else None,
            "survivorship": self.survivorship.as_dict() if self.survivorship else None,
            "writes": self.writes,
            "xref_rows": self.xref_rows,
        }


def _source_identity(frame: pl.DataFrame) -> pl.DataFrame:
    """Build the identity resolution operates on.

    The triple that the crosswalk keys on. Concatenated into one column because
    blocking, scoring and clustering all want a single opaque id, and carrying
    three columns through them would put the crosswalk's structure into every
    intermediate frame.
    """
    return frame.with_columns(
        pl.concat_str(
            [
                pl.col("source_system"),
                pl.col("source_key_kind"),
                pl.col("source_party_key").fill_null(""),
            ],
            separator="\x1f",
        ).alias(SOURCE_IDENTITY_COLUMN)
    )


def run_pipeline(
    conn: psycopg.Connection,
    raw: pl.DataFrame,
    mapping: SourceMapping,
    *,
    batch_id: uuid.UUID | None = None,
    trust: SourceTrust | None = None,
    write: bool = True,
) -> PipelineResult:
    """Run every stage over one raw batch.

    The caller owns the transaction. Everything this writes — golden versions,
    crosswalk rows, provenance, resolution audit — must commit together, and a
    caller that wraps the stages separately loses that.
    """
    result = PipelineResult(batch_id=batch_id)

    # -- shred
    frames = shred(raw, mapping)
    result.policies = frames["policy"].height
    result.party_occurrences = frames["relationship"].height
    persons = frames["person"]
    result.source_identities = persons.height

    if persons.height == 0:
        return result

    # -- standardize
    persons, std_report = standardize(persons, conn=conn, batch_id=batch_id)
    result.standardization = std_report

    # -- resolve, on source-scoped identities
    persons = _source_identity(persons)
    clusters, pairs, res_report = resolve(
        persons, id_column=SOURCE_IDENTITY_COLUMN, conn=None
    )
    result.resolution = res_report

    # Master id from clustering is one of the source identities; it is stable
    # and deterministic but is not a golden id. Mint a golden uuid per cluster
    # so the published identifier carries no source structure -- a downstream
    # consumer must never be able to infer which system a party came from, and a
    # cluster that later gains members must not change its published id.
    cluster_ids = clusters.assignments.select("master_id").unique().sort("master_id")
    golden = cluster_ids.with_columns(
        pl.Series("person_id", [str(uuid7()) for _ in range(cluster_ids.height)])
    )
    assignments = clusters.assignments.join(golden, on="master_id")
    result.golden_persons = golden.height

    contributors = persons.join(
        assignments.select(SOURCE_IDENTITY_COLUMN, "person_id"),
        on=SOURCE_IDENTITY_COLUMN,
        how="left",
    )

    # -- survive
    golden_persons, provenance, surv_report = survive(
        contributors, PERSON, master_column="person_id", trust=trust
    )
    result.survivorship = surv_report

    if not write:
        return result

    # -- write. Golden entities, then the crosswalk pointing every source key at
    #    the identity it resolved to.
    person_write = write_entities(conn, golden_persons, PERSON)
    result.writes["person"] = person_write.as_dict()

    policies = frames["policy"]
    if policies.height:
        policies = policies.with_columns(
            pl.Series("policy_id", [str(uuid7()) for _ in range(policies.height)])
        )
        policy_write = write_entities(conn, policies, POLICY)
        result.writes["policy"] = policy_write.as_dict()

    links = contributors.select(
        "person_id", "source_system", "source_key_kind", "source_party_key"
    ).filter(
        pl.col("source_party_key").is_not_null()
        & (pl.col("source_party_key").str.len_chars() > 0)
        & pl.col("person_id").is_not_null()
    ).unique()
    result.xref_rows = upsert_xref(conn, links)

    _write_provenance(conn, provenance)
    return result


def _write_provenance(conn: psycopg.Connection, provenance: pl.DataFrame) -> int:
    """Persist per-attribute survivorship lineage.

    Written with COPY: a book of any size produces one row per contested
    attribute per entity, which is the second-largest write in the pipeline
    after the landing zone.
    """
    if provenance.height == 0:
        return 0

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _prov_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _prov_stage (
                entity_id uuid, attribute_name text, strategy text,
                winning_source_record_id uuid, winning_source_system text,
                value_text text, candidate_count integer, rejected_values text
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _prov_stage (entity_id, attribute_name, strategy, "
            "winning_source_record_id, winning_source_system, value_text, "
            "candidate_count, rejected_values) FROM STDIN"
        ) as copy:
            for row in provenance.iter_rows(named=True):
                copy.write_row((
                    row["master_id"], row["attribute_name"], row["strategy"],
                    row["winning_source_record_id"], row["winning_source_system"],
                    row["value_text"], int(row["candidate_count"]),
                    row["rejected_values"],
                ))

        cur.execute(
            """
            INSERT INTO mdm.attribute_provenance
                (provenance_id, entity_name, entity_id, entity_version, attribute_name,
                 winning_source_record_id, winning_source_system, strategy, value_text,
                 candidate_count, rejected_values, decided_at)
            SELECT gen_random_uuid(), 'Person', s.entity_id,
                   coalesce(p.version, 1), s.attribute_name,
                   s.winning_source_record_id, s.winning_source_system,
                   s.strategy::mdm.survivorship_strategy, s.value_text,
                   s.candidate_count, s.rejected_values::jsonb, now()
            FROM _prov_stage s
            LEFT JOIN mdm.person p ON p.person_id = s.entity_id AND p.is_current
            -- The winning source record may not be in the landing zone when the
            -- pipeline runs on a frame assembled outside ingestion; the FK would
            -- reject those rows, so they are written without the pointer rather
            -- than losing the whole provenance batch.
            WHERE s.winning_source_record_id IS NULL
               OR EXISTS (SELECT 1 FROM mdm.source_record r
                          WHERE r.source_record_id = s.winning_source_record_id)
            """
        )
        return cur.rowcount

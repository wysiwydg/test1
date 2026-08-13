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
from cmdm.ingest.normalize import normalize_policy_number
from cmdm.ingest.shred import shred
from cmdm.model.fields import PERSON, POLICY, RELATIONSHIP
from cmdm.model.ids import uuid7
from cmdm.resolve import (
    AUTO_MATCH_THRESHOLD,
    AUTO_REJECT_THRESHOLD,
    ResolutionReport,
    load_steward_decisions,
    persist_run,
    resolve,
)
from cmdm.standardize import StandardizationReport, standardize
from cmdm.store import upsert_policy_xref, upsert_xref, write_entities
from cmdm.store.writer import resolve_person_id
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
    policy_xref_rows: int = 0

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
            "policy_xref_rows": self.policy_xref_rows,
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


def _golden_person_ids(
    conn: psycopg.Connection, assignments: pl.DataFrame
) -> pl.DataFrame:
    """Attach the golden person id each cluster resolves to.

    Reading the crosswalk here is what makes a run repeatable. Minting a fresh
    uuid per cluster on every pass is correct exactly once; the second run gives
    the same party a second golden id, and because Person deliberately has no
    natural-key unique index — its identity lives in the crosswalk, not in a
    column — nothing in the database refuses it. The book silently doubles.

    Three cases, and the third is the interesting one:

    *   No member is known: mint. A genuinely new party.
    *   Every member points at one id: reuse it. The common case, and the one
        that makes re-processing a no-op.
    *   Members point at several: two identities the store held apart have been
        merged by this run. The oldest survives — uuid7 is time-ordered, so
        ``min`` is "the one published longest" — and the others are retired
        behind merge pointers rather than deleted, because their ids are already
        in downstream systems and must keep resolving.
    """
    identities = assignments[SOURCE_IDENTITY_COLUMN].to_list()

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _identity_probe")
        cur.execute(
            "CREATE TEMP TABLE _identity_probe (identity text) ON COMMIT DROP"
        )
        with cur.copy("COPY _identity_probe (identity) FROM STDIN") as copy:
            for identity in identities:
                copy.write_row((identity,))

        # The identity is the crosswalk triple joined by a unit separator, so it
        # is split back apart in SQL rather than carrying three columns through
        # blocking, scoring and clustering to arrive here.
        cur.execute(
            """
            SELECT p.identity, x.person_id
            FROM _identity_probe p
            JOIN mdm.person_xref x
              ON x.source_system    = split_part(p.identity, chr(31), 1)
             AND x.source_key_kind  = split_part(p.identity, chr(31), 2)
             AND x.source_party_key = split_part(p.identity, chr(31), 3)
            WHERE x.is_active
            """
        )
        known = dict(cur.fetchall())

    if known:
        # A crosswalk row may point at an id that has since lost a merge.
        known = {
            identity: str(resolve_person_id(conn, person_id))
            for identity, person_id in known.items()
        }

    survivors: dict[str, str] = {}
    retired: list[tuple[str, str]] = []
    for master_id, group in (
        assignments.group_by("master_id").agg(pl.col(SOURCE_IDENTITY_COLUMN))
    ).iter_rows():
        found = sorted({known[i] for i in group if i in known})
        survivor = found[0] if found else str(uuid7())
        survivors[master_id] = survivor
        retired.extend((loser, survivor) for loser in found[1:])

    if retired:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE mdm.person_master SET merged_into_id = %s, is_active = false "
                "WHERE person_id = %s AND merged_into_id IS NULL",
                [(survivor, loser) for loser, survivor in retired],
            )

    return assignments.with_columns(
        pl.col("master_id").replace_strict(survivors).alias("person_id")
    )


def _golden_policy_ids(
    conn: psycopg.Connection, policies: pl.DataFrame
) -> pl.DataFrame:
    """Attach the golden policy id, reusing the one already issued.

    Policy identity is deterministic — a policy number within a source system —
    so unlike Person it is enforced by a unique index on the golden table. That
    index is what turned re-processing into a hard error rather than a silent
    duplicate, which is the better of the two failures but still a failure: a
    re-run must be an SCD-2 no-op, not a constraint violation.
    """
    keys = policies.select("source_system", "policy_number_normalized")

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _policy_probe")
        cur.execute(
            "CREATE TEMP TABLE _policy_probe (source_system text, pn text) "
            "ON COMMIT DROP"
        )
        with cur.copy("COPY _policy_probe (source_system, pn) FROM STDIN") as copy:
            for row in keys.iter_rows():
                copy.write_row(row)

        cur.execute(
            """
            SELECT p.source_system, p.pn, g.policy_id
            FROM _policy_probe p
            JOIN mdm.policy g
              ON g.source_system = p.source_system
             AND g.policy_number_normalized = p.pn
            WHERE g.is_current
            """
        )
        known = {(r[0], r[1]): str(r[2]) for r in cur.fetchall()}

    return policies.with_columns(
        pl.Series(
            "policy_id",
            [
                known.get((system, number)) or str(uuid7())
                for system, number in keys.iter_rows()
            ],
        )
    )


def _link_policies(
    conn: psycopg.Connection,
    raw: pl.DataFrame,
    policies: pl.DataFrame,
    mapping: SourceMapping,
) -> int:
    """Record which delivered policy numbers became which golden policy.

    Built from the raw frame rather than the shredded one because the shredder
    deduplicates on the normalized number: a file carrying ``POL-001234`` and
    ``POL1234`` yields one policy row, and only one of those two strings
    survives on it. Both were delivered, and both have to resolve when the
    extract is handed back with its MDM ids.

    The normalization runs here as the same Polars expression the shredder
    used, so the two cannot disagree -- which they would if the join were
    re-implemented in SQL against the stored normalized column.
    """
    column = next(
        (f.source for f in mapping.policy if f.canonical == "policy_number" and f.source),
        None,
    )
    if column is None or column not in raw.columns:
        return 0

    delivered = (
        raw.select(
            pl.col(column).cast(pl.String, strict=False).alias("source_policy_key")
        )
        .filter(
            pl.col("source_policy_key").is_not_null()
            & (pl.col("source_policy_key").str.strip_chars().str.len_chars() > 0)
        )
        .with_columns(
            normalize_policy_number(pl.col("source_policy_key")).alias(
                "policy_number_normalized"
            )
        )
        .unique()
    )

    links = delivered.join(
        policies.select("policy_id", "policy_number_normalized"),
        on="policy_number_normalized",
        how="inner",
    ).with_columns(pl.lit(mapping.source_system).alias("source_system"))

    return upsert_policy_xref(
        conn, links.select("policy_id", "source_system", "source_policy_key")
    )


def _golden_relationship_ids(
    conn: psycopg.Connection, edges: pl.DataFrame
) -> pl.DataFrame:
    """Attach the golden edge id, reusing the one already issued.

    An edge's identity is the assertion the source made: this key, in this
    namespace, in this role, at this ordinal, on this policy. Deliberately not
    keyed on ``from_person_id`` — a merge moves the person id under the edge,
    and an identity that moved with it would close every edge of the losing
    party and open a new one, which is a rewrite of the servicing history to
    record a fact about *identity* rather than about the world.

    ``source_party_key`` is coalesced on both sides because an unidentified
    party is a legitimate edge, and a NULL would never equal itself in the join.
    """
    keys = edges.select(
        "source_system", "source_key_kind", "source_party_key",
        "to_policy_id", "role", "role_sequence",
    )

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _edge_probe")
        cur.execute(
            """
            CREATE TEMP TABLE _edge_probe (
                source_system text, source_key_kind text, source_party_key text,
                to_policy_id uuid, role text, role_sequence smallint
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _edge_probe (source_system, source_key_kind, source_party_key, "
            "to_policy_id, role, role_sequence) FROM STDIN"
        ) as copy:
            for row in keys.iter_rows():
                copy.write_row(row)

        cur.execute(
            """
            SELECT p.source_system, p.source_key_kind, p.source_party_key,
                   p.to_policy_id, p.role, p.role_sequence, r.relationship_id
            FROM _edge_probe p
            JOIN mdm.relationship r
              ON r.source_system    = p.source_system
             AND coalesce(r.source_key_kind, '')  = coalesce(p.source_key_kind, '')
             AND coalesce(r.source_party_key, '') = coalesce(p.source_party_key, '')
             AND r.to_policy_id = p.to_policy_id
             AND r.role = p.role::mdm.party_role
             AND r.role_sequence IS NOT DISTINCT FROM p.role_sequence
            WHERE r.is_current AND r.edge_kind = 'PARTY_POLICY'
            """
        )
        known = {
            (r[0], r[1] or "", r[2] or "", str(r[3]), r[4], r[5]): str(r[6])
            for r in cur.fetchall()
        }

    return edges.with_columns(
        pl.Series(
            "relationship_id",
            [
                known.get(
                    (system, kind or "", key or "", str(policy), role, sequence)
                ) or str(uuid7())
                for system, kind, key, policy, role, sequence in keys.iter_rows()
            ],
        )
    )


def _write_relationships(
    conn: psycopg.Connection,
    edges: pl.DataFrame,
    contributors: pl.DataFrame,
    policies: pl.DataFrame,
) -> dict[str, Any]:
    """Resolve the shredded edges onto golden ids and version them.

    The shredder leaves edges carrying source keys only, because resolving them
    to surrogate ids there would mean guessing at identity before matching has
    run. This is where they are resolved, in the same transaction that wrote the
    entities they point at — an edge that outlived its endpoints, or preceded
    them, would be a dangling reference in a store whose whole purpose is that
    the graph is answerable.

    An edge whose party or policy did not resolve is dropped rather than written
    with a null endpoint: ``from_person_id`` is NOT NULL and the target check
    constraint requires a policy, so a half-resolved edge is not a representable
    row. In practice both joins are total — every party in the frame went
    through resolution and every policy through the golden writer — and a
    shortfall means an earlier stage lost rows.
    """
    if edges.height == 0:
        return {}

    parties = contributors.select(
        "source_system", "source_key_kind", "source_party_key", "person_id"
    ).unique()

    resolved = (
        edges.join(
            parties,
            on=["source_system", "source_key_kind", "source_party_key"],
            how="inner",
        )
        .join(
            policies.select("source_system", "policy_number_normalized", "policy_id"),
            on=["source_system", "policy_number_normalized"],
            how="inner",
        )
        .rename({"person_id": "from_person_id", "policy_id": "to_policy_id"})
        .filter(
            pl.col("from_person_id").is_not_null()
            & pl.col("to_policy_id").is_not_null()
        )
        .with_columns(
            # A sourced edge is evidenced by exactly the one policy that
            # asserted it. evidence_policy_ids stays empty: the column names the
            # policies behind a *derived* edge, and repeating to_policy_id into
            # it would make a stated fact look like an inference.
            pl.lit(1, dtype=pl.Int32).alias("evidence_count"),
            pl.lit("DETERMINISTIC").alias("derivation_method"),
        )
        .unique(
            subset=[
                "source_system", "source_key_kind", "source_party_key",
                "to_policy_id", "role", "role_sequence",
            ]
        )
    )

    if resolved.height == 0:
        return {}

    resolved = _golden_relationship_ids(conn, resolved)
    return write_entities(conn, resolved, RELATIONSHIP).as_dict()


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

    # -- resolve, on source-scoped identities. Persisted below rather than by
    #    resolve() itself: the ledger records which golden person each side
    #    landed in, and that is not known until the clusters have been minted.
    persons = _source_identity(persons)
    clusters, pairs, res_report = resolve(
        persons,
        id_column=SOURCE_IDENTITY_COLUMN,
        conn=None,
        decisions=load_steward_decisions(conn),
    )
    result.resolution = res_report

    # Master id from clustering is one of the source identities; it is stable
    # and deterministic but is not a golden id. The published identifier carries
    # no source structure -- a downstream consumer must never be able to infer
    # which system a party came from -- and it must survive re-running, so it
    # comes from the crosswalk where the identity already lives, and is only
    # minted for a cluster the crosswalk has never seen.
    assignments = _golden_person_ids(conn, clusters.assignments)
    result.golden_persons = assignments["person_id"].n_unique()

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

    # -- the match ledger, including the rejections. Written before the entities
    #    only because it needs nothing from them; it is the same transaction, so
    #    a run whose write fails leaves no decisions claiming to explain records
    #    that do not exist.
    persist_run(
        conn,
        res_report,
        pairs,
        auto_match=AUTO_MATCH_THRESHOLD,
        auto_reject=AUTO_REJECT_THRESHOLD,
        golden_ids=dict(
            zip(
                assignments[SOURCE_IDENTITY_COLUMN].to_list(),
                assignments["person_id"].to_list(),
                strict=True,
            )
        ),
    )

    # -- write. Golden entities, then the crosswalk pointing every source key at
    #    the identity it resolved to.
    person_write = write_entities(conn, golden_persons, PERSON)
    result.writes["person"] = person_write.as_dict()

    policies = frames["policy"]
    if policies.height:
        policies = _golden_policy_ids(conn, policies)
        policy_write = write_entities(conn, policies, POLICY)
        result.writes["policy"] = policy_write.as_dict()
        result.policy_xref_rows = _link_policies(conn, raw, policies, mapping)

        # Edges last of the three: they reference both of the others, and the
        # frame they are resolved against only exists once those are written.
        relationship_write = _write_relationships(
            conn, frames["relationship"], contributors, policies
        )
        if relationship_write:
            result.writes["relationship"] = relationship_write

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

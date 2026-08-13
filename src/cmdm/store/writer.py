"""The golden record writer.

Where resolved, survived records become the system of record. Everything here
happens inside one transaction per batch, because a merge is not one write: it
mints identities, closes the current version of each affected entity, opens a
new one, rewrites the crosswalk, retires the losing ids and records the
provenance. Committing some of those without the others leaves the store in a
state no reader can interpret.

Four properties the writer must have.

**Idempotence.** Re-running a batch must not create a second version of anything.
Change detection compares a content hash over the business fields only — audit
columns are excluded, so a re-ingest of unchanged data is a no-op rather than a
version that grows the table for nothing.

**SCD-2, never update-in-place.** A change closes the current version by stamping
``valid_to`` and inserts a new row. The partial unique index makes "exactly one
current version" a database guarantee rather than something the writer is
trusted to maintain.

**Concurrent-write safety.** Two workers writing overlapping entities must not
interleave into a corrupt state. Rows are locked in a deterministic order
(sorted by id) so two transactions touching the same entities queue rather than
deadlock, and the current-version index makes a lost update impossible rather
than merely unlikely.

**Retired ids keep resolving.** When a merge collapses two persons, the loser is
not deleted. Its anchor row stays, marked inactive with a pointer to the winner,
so an id already published to a downstream consumer still resolves.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

import polars as pl
import psycopg

from cmdm.model.fields import EntitySpec
from cmdm.model.ids import record_hash, uuid7

__all__ = [
    "WriteReport",
    "write_entities",
    "upsert_xref",
    "upsert_policy_xref",
    "resolve_person_id",
    "fill_required_defaults",
]


@dataclass(slots=True)
class WriteReport:
    """What one write did."""

    entity: str = ""
    inserted: int = 0
    versioned: int = 0
    unchanged: int = 0
    provenance_rows: int = 0
    xref_rows: int = 0
    retired_ids: int = 0

    @property
    def changed(self) -> int:
        return self.inserted + self.versioned

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "inserted": self.inserted,
            "versioned": self.versioned,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "provenance_rows": self.provenance_rows,
            "xref_rows": self.xref_rows,
            "retired_ids": self.retired_ids,
        }


def _hashable_fields(spec: EntitySpec) -> list[str]:
    """Business fields that participate in change detection.

    Audit and lineage columns are excluded. Hashing ``updated_at`` would make
    every re-ingest look like a change, which would defeat idempotence and grow
    the version history without bound.
    """
    return [f.name for f in spec.select(in_record_hash=True)]


def _writable_columns(spec: EntitySpec, frame: pl.DataFrame) -> list[str]:
    """Registry columns present in the frame, excluding writer-assigned ones."""
    assigned = {
        "version", "valid_from", "valid_to", "is_current", "record_hash",
        "created_at", "updated_at", "source_count", "confidence",
        "is_curated", "is_deleted",
    }
    return [
        f.name for f in spec.fields
        if f.name in frame.columns and f.name not in assigned
    ]


def fill_required_defaults(frame: pl.DataFrame, spec: EntitySpec) -> pl.DataFrame:
    """Add registry-declared NOT NULL columns the frame does not carry.

    Stages legitimately produce partial frames: the shredder knows nothing about
    ``policy_count``, which is a rollup computed once relationships are written,
    and nothing about ``is_sanctioned``, which arrives from a screening feed. The
    schema still requires them.

    Defaults are chosen from the declared type and are all "no information":
    false for a flag, zero for a count or a score. That is the right default
    precisely because these columns are recomputed by later stages — a flag
    defaulting to true, or a quality score defaulting to one, would assert
    something the pipeline has not established.
    """
    from cmdm.model.enums import LogicalType as LT

    missing = [
        f for f in spec.fields
        if not f.nullable
        and f.name not in frame.columns
        and f.name not in {
            "version", "valid_from", "valid_to", "is_current", "record_hash",
            "created_at", "updated_at", "source_count", "confidence",
            "is_curated", "is_deleted",
        }
    ]
    if not missing:
        return frame

    literals = []
    for f in missing:
        match f.dtype:
            case LT.BOOL:
                literals.append(pl.lit(False).alias(f.name))
            case LT.INT16 | LT.INT32 | LT.INT64:
                literals.append(pl.lit(0, dtype=pl.Int64).alias(f.name))
            case LT.FLOAT32 | LT.FLOAT64 | LT.RATIO:
                literals.append(pl.lit(0.0).alias(f.name))
            case LT.LIST_STRING | LT.LIST_UUID:
                literals.append(pl.lit([], dtype=pl.List(pl.String)).alias(f.name))
            case _:
                # A non-nullable text or key column with no value is a genuine
                # bug in the calling stage, not something to paper over with an
                # empty string that would then be indistinguishable from data.
                raise ValueError(
                    f"{spec.name}.{f.name} is NOT NULL with no default and is absent "
                    "from the frame; the stage producing this frame must supply it"
                )
    return frame.with_columns(literals)


def _coerce(value: Any) -> Any:
    """Render a Polars value for psycopg.

    Lists become Postgres arrays natively; everything else passes through. The
    one case needing help is a Decimal-typed column, which Polars hands back as
    a Python Decimal already.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def write_entities(
    conn: psycopg.Connection,
    frame: pl.DataFrame,
    spec: EntitySpec,
    *,
    id_column: str | None = None,
    confidence_column: str | None = None,
    now: dt.datetime | None = None,
) -> WriteReport:
    """Write golden records as SCD-2 versions.

    ``frame`` carries one row per entity, already resolved and survived. The
    surrogate key column may hold ids the caller has chosen (a stable master id
    from clustering); rows with no id are minted new ones.

    The whole write is a single set-based statement pair rather than a loop:
    a staging table receives the batch via COPY, then one statement closes
    superseded versions and one inserts the new ones. A per-row round trip would
    make a million-record book take hours.
    """
    id_column = id_column or spec.primary_key
    now = now or dt.datetime.now(dt.UTC)
    report = WriteReport(entity=spec.name)

    if frame.height == 0:
        return report

    frame = fill_required_defaults(frame, spec)
    columns = _writable_columns(spec, frame)
    if id_column not in columns:
        raise ValueError(
            f"{spec.name}: the frame has no {id_column!r} column; the writer needs "
            "the surrogate key to version against"
        )

    hashable = [c for c in _hashable_fields(spec) if c in frame.columns]
    rows = frame.to_dicts()

    # Content hash over business fields only. Computed here rather than in SQL
    # because the canonicalization rules live in cmdm.model.ids and must be
    # identical to the ones used everywhere else that hashes a record.
    hashes = [record_hash(row, hashable) for row in rows]

    anchor_table = f"{spec.table}_master"
    payload = []
    for row, digest in zip(rows, hashes, strict=True):
        entity_id = row.get(id_column) or uuid7()
        payload.append((entity_id, digest, row))

    with conn.cursor() as cur:
        # -- anchors first, for the entities that have one. Referential
        #    integrity points there, and an entity version cannot exist before
        #    the identity it belongs to. Edges have no anchor because nothing
        #    references an edge; see EntitySpec.has_anchor.
        if spec.has_anchor:
            cur.execute("DROP TABLE IF EXISTS _anchor_stage")
            cur.execute(
                "CREATE TEMP TABLE _anchor_stage (id uuid, created_at timestamptz) "
                "ON COMMIT DROP"
            )
            with cur.copy("COPY _anchor_stage (id, created_at) FROM STDIN") as copy:
                for entity_id, _, _ in payload:
                    copy.write_row((entity_id, now))
            cur.execute(
                f"""
                INSERT INTO mdm.{anchor_table} ({id_column}, created_at, is_active)
                SELECT id, created_at, true FROM _anchor_stage
                ON CONFLICT ({id_column}) DO NOTHING
                """
            )

        # -- stage the new versions.
        column_ddl = ", ".join(f"{c} text" for c in columns if c != id_column)
        cur.execute("DROP TABLE IF EXISTS _entity_stage")
        cur.execute(
            f"""
            CREATE TEMP TABLE _entity_stage (
                {id_column} uuid, record_hash text, source_count integer,
                confidence double precision
                {"," if column_ddl else ""} {column_ddl}
            ) ON COMMIT DROP
            """
        )

        other = [c for c in columns if c != id_column]
        copy_columns = [id_column, "record_hash", "source_count", "confidence", *other]
        with cur.copy(
            f"COPY _entity_stage ({', '.join(copy_columns)}) FROM STDIN"
        ) as copy:
            for entity_id, digest, row in payload:
                values: list[Any] = [
                    entity_id,
                    digest,
                    int(row.get("source_count") or 1),
                    float(row.get(confidence_column) or 1.0) if confidence_column
                    else float(row.get("confidence") or 1.0),
                ]
                for c in other:
                    v = _coerce(row.get(c))
                    values.append(None if v is None else str(v) if not isinstance(v, list)
                                  else json.dumps(v))
                copy.write_row(tuple(values))

        # -- unchanged rows: the hash already matches the current version.
        cur.execute(
            f"""
            SELECT count(*) FROM _entity_stage s
            JOIN mdm.{spec.table} e
              ON e.{id_column} = s.{id_column} AND e.is_current
            WHERE e.record_hash = s.record_hash
            """
        )
        report.unchanged = cur.fetchone()[0]

        # -- close superseded versions. Ordered by id so two concurrent writers
        #    touching overlapping entities queue in the same order and cannot
        #    deadlock against each other.
        cur.execute(
            f"""
            WITH changed AS (
                SELECT s.{id_column}
                FROM _entity_stage s
                JOIN mdm.{spec.table} e
                  ON e.{id_column} = s.{id_column} AND e.is_current
                WHERE e.record_hash IS DISTINCT FROM s.record_hash
                ORDER BY s.{id_column}
                FOR UPDATE OF e
            )
            UPDATE mdm.{spec.table} e
            SET valid_to = %s, is_current = false, updated_at = %s
            FROM changed c
            WHERE e.{id_column} = c.{id_column} AND e.is_current
            """,
            (now, now),
        )
        report.versioned = cur.rowcount

        # -- insert the new current versions for everything that changed or is
        #    new. The partial unique index enforces one-current-per-entity, so a
        #    bug here fails loudly instead of silently duplicating.
        cast_columns = ", ".join(_select_expression(spec, c) for c in other)
        cur.execute(
            f"""
            INSERT INTO mdm.{spec.table}
                ({id_column}, version, valid_from, valid_to, is_current, record_hash,
                 source_count, confidence, is_curated, is_deleted, created_at, updated_at
                 {"," if other else ""} {", ".join(other)})
            SELECT s.{id_column},
                   coalesce(prev.max_version, 0) + 1,
                   %s, NULL, true, s.record_hash,
                   s.source_count, s.confidence, false, false,
                   coalesce(prev.created_at, %s), %s
                   {"," if other else ""} {cast_columns}
            FROM _entity_stage s
            LEFT JOIN (
                SELECT {id_column}, max(version) AS max_version, min(created_at) AS created_at
                FROM mdm.{spec.table} GROUP BY {id_column}
            ) prev ON prev.{id_column} = s.{id_column}
            WHERE NOT EXISTS (
                SELECT 1 FROM mdm.{spec.table} e
                WHERE e.{id_column} = s.{id_column}
                  AND e.is_current
                  AND e.record_hash = s.record_hash
            )
            """,
            (now, now, now),
        )
        report.inserted = cur.rowcount - report.versioned
        if report.inserted < 0:
            # Every closed version is replaced, so inserts >= versioned always.
            report.inserted, report.versioned = cur.rowcount, report.versioned

    return report


def _select_expression(spec: EntitySpec, column: str) -> str:
    """SQL expression converting a staged text column back to its real type.

    Everything is staged as text so one COPY handles every column shape. Most
    types come back with a plain cast. Arrays cannot: they are staged as JSON
    (a Postgres array literal would need escaping rules applied per value, which
    is exactly the kind of hand-rolled quoting that eventually meets a value
    containing a brace) and are converted with jsonb_array_elements_text.
    """
    from cmdm.model.ddl import sql_type

    field_spec = spec.by_name.get(column)
    if field_spec is None:  # pragma: no cover - guarded by _writable_columns
        return f"s.{column}"

    rendered = sql_type(field_spec)

    if rendered.endswith("[]"):
        element = rendered[:-2]
        return (
            f"CASE WHEN s.{column} IS NULL THEN NULL ELSE "
            f"ARRAY(SELECT jsonb_array_elements_text(s.{column}::jsonb)::{element}) "
            f"END"
        )
    if field_spec.enum_name:
        return f"s.{column}::mdm.{rendered}"
    return f"s.{column}::{rendered}"


def upsert_xref(
    conn: psycopg.Connection,
    links: pl.DataFrame,
    *,
    derivation_method: str = "DETERMINISTIC",
    linked_by: str = "resolver",
) -> int:
    """Point source party keys at their golden person.

    The crosswalk is where identity actually lives. Rows are never deleted: a
    mapping superseded by a merge is deactivated so the crosswalk's own history
    survives, which is what lets a merge be explained or undone.
    """
    if links.height == 0:
        return 0

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _xref_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _xref_stage (
                person_id uuid, source_system text, source_key_kind text,
                source_party_key text, confidence double precision
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _xref_stage (person_id, source_system, source_key_kind, "
            "source_party_key, confidence) FROM STDIN"
        ) as copy:
            for row in links.iter_rows(named=True):
                copy.write_row((
                    row["person_id"], row["source_system"], row["source_key_kind"],
                    row["source_party_key"], float(row.get("confidence") or 1.0),
                ))

        # A key that now points somewhere else is deactivated rather than
        # updated in place, so the previous mapping remains inspectable.
        cur.execute(
            """
            UPDATE mdm.person_xref x
            SET is_active = false
            FROM _xref_stage s
            WHERE x.source_system = s.source_system
              AND x.source_key_kind = s.source_key_kind
              AND x.source_party_key = s.source_party_key
              AND x.is_active
              AND x.person_id IS DISTINCT FROM s.person_id
            """
        )
        cur.execute(
            """
            INSERT INTO mdm.person_xref
                (xref_id, person_id, source_system, source_key_kind, source_party_key,
                 is_active, linked_at, linked_by, derivation_method, confidence)
            SELECT gen_random_uuid(), s.person_id, s.source_system, s.source_key_kind,
                   s.source_party_key, true, now(), %s, %s::mdm.derivation_method,
                   s.confidence
            FROM _xref_stage s
            ON CONFLICT (source_system, source_key_kind, source_party_key)
                DO UPDATE SET person_id = EXCLUDED.person_id,
                              is_active = true,
                              confidence = EXCLUDED.confidence
            """,
            (linked_by, derivation_method),
        )
        return cur.rowcount


def upsert_policy_xref(
    conn: psycopg.Connection,
    links: pl.DataFrame,
    *,
    derivation_method: str = "DETERMINISTIC",
) -> int:
    """Point source policy numbers at their golden policy.

    The counterpart of :func:`upsert_xref` for contracts, and the join that
    makes it possible to hand a source system back its own extract with MDM
    ids attached. Without it the only link from a delivered row to its golden
    policy is the normalized policy number, which means re-implementing the
    normalization in SQL and hoping the two agree.

    Keyed on the policy number *as delivered*, not as normalized. Two rows
    reading ``POL-001234`` and ``POL1234`` are one contract and one golden id,
    but they are two different strings in two different source files, and both
    have to resolve.
    """
    if links.height == 0:
        return 0

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _policy_xref_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _policy_xref_stage (
                policy_id uuid, source_system text, source_policy_key text
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _policy_xref_stage (policy_id, source_system, source_policy_key) "
            "FROM STDIN"
        ) as copy:
            for row in links.iter_rows(named=True):
                copy.write_row((
                    row["policy_id"], row["source_system"], row["source_policy_key"],
                ))

        cur.execute(
            """
            INSERT INTO mdm.policy_xref
                (xref_id, policy_id, source_system, source_policy_key,
                 is_active, linked_at, derivation_method)
            SELECT gen_random_uuid(), s.policy_id, s.source_system,
                   s.source_policy_key, true, now(), %s::mdm.derivation_method
            FROM _policy_xref_stage s
            ON CONFLICT (source_system, source_policy_key)
                DO UPDATE SET policy_id = EXCLUDED.policy_id, is_active = true
            """,
            (derivation_method,),
        )
        return cur.rowcount


def resolve_person_id(conn: psycopg.Connection, person_id: uuid.UUID) -> uuid.UUID:
    """Follow merge pointers to the surviving identity.

    An id published to a downstream consumer must keep resolving after a merge
    retires it. The pointer chain is followed rather than assumed to be one hop,
    because a party merged twice has a two-hop chain, and a bounded loop is
    cheaper than the cycle a recursive CTE would need to guard against.
    """
    current = person_id
    for _ in range(10):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT merged_into_id FROM mdm.person_master WHERE person_id = %s",
                (current,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return current
        current = row[0]
    raise RuntimeError(f"merge pointer chain from {person_id} did not terminate")

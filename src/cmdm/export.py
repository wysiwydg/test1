"""Entity dashboards and record export.

Two things a business owner asks for once the pipeline works: *what is in
there*, and *give it back to me*.

The second is the one with a wrong answer available. A golden-record dump is
easy to produce and largely useless to a source system, because it is in the
MDM's shape and keyed by the MDM's ids -- nothing the source system can join to
what it already has. What a source system can use is **its own extract, handed
back with the MDM ids attached**: the same rows, the same columns, in the same
policy grain it delivered, plus ``PolicyMdmId``, ``OwnerMdmId`` and so on. That
is a file it can load.

So the source-shaped export is built from the landing zone -- the delivered rows
kept verbatim -- joined to the crosswalks. Two things follow from that:

*   Row count matches the delivery. A row that was rejected or that resolved to
    nothing still appears, with an empty id, because a file that silently drops
    rows is worse than one that admits which rows it could not resolve.
*   The values are the ones delivered, not the ones that survived. ``golden``
    switches that, for a caller who wants the cleaned view instead -- but it is
    not the default, because "here is your file back" should be recognisable as
    the file.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cmdm.governance.rbac import MASK, Principal, maskable_columns
from cmdm.ingest.mapping import SourceMapping
from cmdm.model.fields import ENTITIES, PERSON, POLICY, RELATIONSHIP

__all__ = [
    "ENTITY_SPECS",
    "entity_overview",
    "browse_entity",
    "export_entity",
    "export_source_shaped",
    "MDM_ID_SUFFIX",
]

#: What the console offers. Keyed by the url-friendly name.
ENTITY_SPECS = {"person": PERSON, "policy": POLICY, "relationship": RELATIONSHIP}

#: Appended to a party role to name its id column in the source-shaped export:
#: OWNER -> OwnerMdmId. Chosen to be obviously not a source column, so a
#: downstream loader cannot confuse it for one the source system issued.
MDM_ID_SUFFIX = "MdmId"

#: Rows per page in the console's entity browser.
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def entity_overview(conn: psycopg.Connection) -> dict[str, Any]:
    """Counts and breakdowns for the three entities.

    One round trip. The dashboard is the page most likely to be left open on a
    second monitor, and a page that costs nine queries every refresh is a page
    somebody eventually turns off.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM mdm.person WHERE is_current AND NOT is_deleted)
                  AS persons,
              (SELECT count(*) FROM mdm.person WHERE is_current AND party_type = 'PERSON')
                  AS natural_persons,
              (SELECT count(*) FROM mdm.person WHERE is_current AND party_type <> 'PERSON')
                  AS legal_entities,
              (SELECT count(*) FROM mdm.person WHERE is_current AND source_count > 1)
                  AS multi_source_persons,
              (SELECT count(*) FROM mdm.person WHERE version > 1 AND is_current)
                  AS revised_persons,
              (SELECT count(DISTINCT household_id) FROM mdm.person
                WHERE is_current AND household_id IS NOT NULL) AS households,
              (SELECT count(*) FROM mdm.person
                WHERE is_current AND household_id IS NOT NULL)
                  AS persons_in_a_household,
              (SELECT coalesce(max(household_size), 0) FROM mdm.person
                WHERE is_current) AS largest_household,
              (SELECT count(*) FROM mdm.person
                WHERE is_current AND affiliation_count > 0) AS affiliated_persons,
              (SELECT count(*) FROM mdm.relationship
                WHERE is_current AND edge_kind = 'PARTY_PARTY')
                  AS derived_edges,
              (SELECT count(*) FROM mdm.policy WHERE is_current) AS policies,
              (SELECT count(DISTINCT source_system) FROM mdm.policy WHERE is_current)
                  AS policy_sources,
              (SELECT count(*) FROM mdm.relationship WHERE is_current) AS relationships,
              (SELECT count(*) FROM mdm.person_xref) AS person_keys,
              (SELECT count(*) FROM mdm.policy_xref) AS policy_keys,
              (SELECT count(*) FROM mdm.source_record) AS landed_rows
            """
        )
        totals = dict(cur.fetchone() or {})

        cur.execute(
            "SELECT role, count(*) AS n FROM mdm.relationship "
            "WHERE is_current AND role IS NOT NULL GROUP BY role ORDER BY n DESC"
        )
        totals["by_role"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT policy_status, count(*) AS n FROM mdm.policy "
            "WHERE is_current GROUP BY policy_status ORDER BY n DESC LIMIT 8"
        )
        totals["by_status"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT party_type, count(*) AS n FROM mdm.person "
            "WHERE is_current GROUP BY party_type ORDER BY n DESC"
        )
        totals["by_party_type"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT association_type, count(*) AS n FROM mdm.relationship "
            "WHERE is_current AND edge_kind = 'PARTY_PARTY' "
            "AND association_type IS NOT NULL "
            "GROUP BY association_type ORDER BY n DESC"
        )
        totals["by_association"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT household_size, count(DISTINCT household_id) AS n "
            "FROM mdm.person WHERE is_current AND household_id IS NOT NULL "
            "GROUP BY household_size ORDER BY household_size"
        )
        totals["by_household_size"] = [dict(r) for r in cur.fetchall()]

    return totals


def _display_columns(entity: str) -> list[str]:
    """A readable subset for the browser, not all 45 to 54 columns.

    Chosen per entity rather than taken from the registry order, because the
    first dozen registry fields are keys and audit columns and none of them
    tell you who the record is.

    A name that is not in the registry raises here rather than being dropped
    from the page. A hand-written list against a generated schema drifts, and a
    column that quietly disappears from a browser looks like missing *data*,
    which is a far more alarming thing to be told than a typo.
    """
    chosen = {
        "person": [
            "person_id", "full_name", "party_type", "date_of_birth",
            "email_address", "phone_e164", "postal_code",
            "household_size", "affiliation_count", "version",
        ],
        "policy": [
            "policy_id", "policy_number", "source_system", "product_name",
            "policy_status", "issue_date", "sum_assured_amount",
            "annual_premium_amount", "premium_frequency", "version",
        ],
        "relationship": [
            "relationship_id", "edge_kind", "role", "association_type",
            "stated_relationship", "from_person_id", "to_policy_id",
            "to_person_id", "evidence_count", "source_system",
        ],
    }[entity]

    known = {f.name for f in ENTITY_SPECS[entity].fields}
    unknown = [c for c in chosen if c not in known]
    if unknown:
        raise KeyError(
            f"{entity} browser lists column(s) not in the registry: "
            f"{', '.join(unknown)}"
        )
    return chosen


def browse_entity(
    conn: psycopg.Connection,
    entity: str,
    *,
    principal: Principal | None = None,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    """One page of an entity's current rows, plus the total.

    Masked to the caller's role. A browser is a read of the golden store like
    any other, and a page that showed what the search results and the export
    both withhold would be the way around masking rather than a view of it.
    Passing no principal means no masking and is for callers with no caller —
    tests and offline scripts.
    """
    spec = ENTITY_SPECS[entity]
    columns = _display_columns(entity)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT count(*) AS n FROM mdm.{spec.table} WHERE is_current")
        total = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            f"SELECT {', '.join(columns)} FROM mdm.{spec.table} "
            f"WHERE is_current ORDER BY {spec.primary_key} "
            "LIMIT %s OFFSET %s",
            (page_size, page * page_size),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if rows and principal is not None and not principal.may_unmask:
        hidden = set(maskable_columns(spec)) & set(columns)
        for row in rows:
            for column in hidden:
                if row[column] is not None:
                    row[column] = MASK

    return rows, total


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _writer() -> tuple[io.StringIO, Any]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def _drain(buffer: io.StringIO) -> str:
    text = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return text


def export_entity(
    conn: psycopg.Connection,
    entity: str,
    *,
    principal: Principal,
    chunk: int = 2000,
) -> Iterator[str]:
    """Stream one entity's current rows as CSV.

    Server-side cursor and yielded chunks rather than a materialised string: a
    book of any size is larger than the request that asks for it, and building
    the whole file in memory first is the difference between an export that
    works at production volume and one that only works on a demo.

    Masked according to the caller, using the registry's PII class -- the same
    rule the console and the API apply, so an export cannot become the way to
    get around masking.
    """
    spec = ENTITY_SPECS[entity]
    columns = [f.name for f in spec.fields]
    hidden = (
        set()
        if principal.may_unmask
        else {c for c in maskable_columns(spec) if c in columns}
    )

    buffer, writer = _writer()
    writer.writerow(columns)
    yield _drain(buffer)

    with conn.cursor(name=f"export_{entity}", row_factory=dict_row) as cur:
        cur.itersize = chunk
        cur.execute(
            f"SELECT {', '.join(columns)} FROM mdm.{spec.table} "
            f"WHERE is_current ORDER BY {spec.primary_key}"
        )
        for row in cur:
            writer.writerow(
                [
                    MASK if (c in hidden and row[c] is not None) else _flat(row[c])
                    for c in columns
                ]
            )
            if buffer.tell() > 64 * 1024:
                yield _drain(buffer)

    remaining = _drain(buffer)
    if remaining:
        yield remaining


def _flat(value: Any) -> Any:
    """Render a value for CSV.

    Arrays and JSON become strings rather than Python reprs: a downstream
    loader reading ``['OWNER', 'INSURED']`` has to strip Python quoting before
    it can use it.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "|".join("" if v is None else str(v) for v in value)
    if isinstance(value, dict):
        import json

        return json.dumps(value, separators=(",", ":"), default=str)
    return value


def source_shaped_columns(mapping: SourceMapping) -> list[str]:
    """The MDM id columns appended to a delivered row, in role order."""
    columns = [f"Policy{MDM_ID_SUFFIX}"]
    for party in mapping.parties:
        columns.append(f"{party.role.value.title()}{MDM_ID_SUFFIX}")
    return columns


def export_source_shaped(
    conn: psycopg.Connection,
    mapping: SourceMapping,
    *,
    principal: Principal,
    values: str = "as-delivered",
    chunk: int = 1000,
) -> Iterator[str]:
    """Stream the delivered extract back, with MDM ids attached.

    One row per landed source row, the columns the mapping reads, in the order
    the mapping declares them, followed by one id column per party role and one
    for the policy.

    ``values``:

    *   ``as-delivered`` -- the payload exactly as it arrived. The default,
        because the point of this file is that the source system recognises it.
    *   ``golden`` -- the surviving value for each attribute, so the same rows
        carry the resolved view instead. Useful for a system that wants to
        adopt the cleaned data, not merely the keys.

    A row whose party or policy did not resolve gets an empty id rather than
    being dropped. A hand-back file that silently loses rows is the one nobody
    can reconcile against what they sent.
    """
    if values not in ("as-delivered", "golden"):
        raise ValueError(f"values must be 'as-delivered' or 'golden', got {values!r}")

    source_columns = list(mapping.source_columns)
    id_columns = source_shaped_columns(mapping)
    hidden = (
        set()
        if principal.may_unmask
        else _sensitive_source_columns(mapping)
    )

    # Key kind per role, so the crosswalk lookup is driven by the mapping
    # rather than by names hardcoded here.
    parties = [(p.role.value, p.key_field, p.key_kind) for p in mapping.parties]

    golden_by_person = _golden_person_columns(mapping) if values == "golden" else {}

    buffer, writer = _writer()
    writer.writerow([*source_columns, *id_columns])
    yield _drain(buffer)

    lookups = ", ".join(
        f"""(
            SELECT x.person_id FROM mdm.person_xref x
             WHERE x.source_system = s.source_system
               AND x.source_key_kind = {_literal(kind)}
               AND x.source_party_key = s.payload->>{_literal(key_field)}
               AND x.is_active
             LIMIT 1
        ) AS "{role}_id\""""
        for role, key_field, kind in parties
    )

    query = f"""
        SELECT s.payload,
               px.policy_id,
               {lookups}
        FROM mdm.source_record s
        LEFT JOIN mdm.policy_xref px
               ON px.source_system = s.source_system
              AND px.source_policy_key = s.source_row_key
        WHERE s.source_system = %s
        ORDER BY s.source_record_id
    """

    with conn.cursor(name="export_source_shaped", row_factory=dict_row) as cur:
        cur.itersize = chunk
        cur.execute(query, (mapping.source_system,))

        for row in cur:
            payload = row["payload"] or {}
            person_ids = {role: row.get(f"{role}_id") for role, _, _ in parties}

            if values == "golden":
                payload = _apply_golden(
                    conn, payload, parties, person_ids, golden_by_person
                )

            writer.writerow(
                [
                    *(
                        MASK
                        if (c in hidden and payload.get(c) not in (None, ""))
                        else _flat(payload.get(c))
                        for c in source_columns
                    ),
                    _flat(row.get("policy_id")),
                    *(_flat(person_ids.get(role)) for role, _, _ in parties),
                ]
            )
            if buffer.tell() > 64 * 1024:
                yield _drain(buffer)

    remaining = _drain(buffer)
    if remaining:
        yield remaining


def _literal(value: str) -> str:
    """Quote a mapping-supplied name for inclusion in SQL.

    These come from a mapping file on disk rather than from a request, but the
    query is assembled as text and a mapping is still input -- so the value is
    escaped rather than trusted.
    """
    return "'" + value.replace("'", "''") + "'"


def _sensitive_source_columns(mapping: SourceMapping) -> set[str]:
    """Source columns carrying attributes the registry classes as direct PII."""
    protected = set(maskable_columns(PERSON)) | set(maskable_columns(POLICY))
    columns = set()
    for field in mapping.policy:
        if field.source and field.canonical in protected:
            columns.add(field.source)
    for party in mapping.parties:
        for field in party.fields:
            if field.source and field.canonical in protected:
                columns.add(field.source)
    return columns


def _golden_person_columns(mapping: SourceMapping) -> dict[str, str]:
    """Source column -> canonical attribute, for the golden variant."""
    out: dict[str, str] = {}
    for party in mapping.parties:
        for field in party.fields:
            if field.source:
                out[field.source] = field.canonical
    return out


def _apply_golden(
    conn: psycopg.Connection,
    payload: dict[str, Any],
    parties: list[tuple[str, str, str]],
    person_ids: dict[str, Any],
    columns: dict[str, str],
) -> dict[str, Any]:
    """Replace a row's party attributes with the values that survived."""
    updated = dict(payload)
    for party in parties:
        role, _, _ = party
        person_id = person_ids.get(role)
        if not person_id:
            continue
        golden = _golden_person(conn, person_id)
        if not golden:
            continue
        for source_column, canonical in columns.items():
            if canonical in golden and source_column in payload:
                updated[source_column] = golden[canonical]
    return updated


_GOLDEN_CACHE: dict[str, dict[str, Any]] = {}


def _golden_person(conn: psycopg.Connection, person_id: Any) -> dict[str, Any]:
    """One golden person, cached for the life of the export.

    A party appears on many policies -- an agent on every one they wrote -- so
    without the cache the golden variant re-reads the same record thousands of
    times.
    """
    key = str(person_id)
    if key in _GOLDEN_CACHE:
        return _GOLDEN_CACHE[key]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM mdm.person WHERE person_id = %s AND is_current", (person_id,)
        )
        row = cur.fetchone()
    resolved = dict(row) if row else {}
    _GOLDEN_CACHE[key] = resolved
    return resolved


def clear_golden_cache() -> None:
    """Drop the per-export cache. Called when an export finishes."""
    _GOLDEN_CACHE.clear()


def entity_names() -> list[str]:
    """The entities the console offers, in the order the model declares them."""
    by_spec = {spec.name: name for name, spec in ENTITY_SPECS.items()}
    return [by_spec[name] for name in ENTITIES if name in by_spec]

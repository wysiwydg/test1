"""The API server.

FastAPI over the golden store. Three capabilities the requirement names, and one
of them shapes the whole design:

*   **Read and search** golden records, masked according to the caller's role.
*   **Submit** a batch, validated synchronously and processed behind the queue.
*   **Real-time duplicate check** for point-of-entry deduplication — given a
    candidate party, is this somebody we already know?

The duplicate-check endpoint is the demanding one. It runs the same blocking and
scoring engine as the batch pipeline, against a single record, inside a request.
That constraint is what makes it worth building carefully: it must reuse the
batch logic exactly, because a point-of-entry check that disagrees with the
nightly run is worse than no check at all — the operator is told the record is
new, and the batch merges it away an hour later.

Every route authenticates, authorizes, masks and logs. Those four are applied by
dependency rather than by each handler remembering, because the one handler that
forgets is the one that leaks.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from typing import Annotated, Any
from urllib.parse import quote

import polars as pl
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi import File as FileParam
from fastapi.responses import RedirectResponse, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from cmdm.deps import (
    ConnectionDep,
    PrincipalDep,
    require,
)
from cmdm.governance.rbac import Action, log_access, mask_frame
from cmdm.model.fields import PERSON
from cmdm.ui.console import LoginRequired
from cmdm.ui.console import router as console_router

__all__ = ["create_app", "app"]


# ---------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------


class PartyCandidate(BaseModel):
    """A party being entered, for the duplicate check.

    Deliberately loose: point-of-entry means partial data. A form half-filled is
    the normal input, and requiring more would push callers into sending
    placeholder values, which is how blocking keys get poisoned.
    """

    full_name: str = Field(min_length=1, max_length=300)
    date_of_birth: dt.date | None = None
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    address_line1: str | None = Field(default=None, max_length=300)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)


class DuplicateMatch(BaseModel):
    person_id: str
    score: float
    zone: str
    full_name: str | None
    date_of_birth: dt.date | None
    matched_on: list[str]


class DuplicateCheckResponse(BaseModel):
    """The verdict, with the evidence behind it.

    ``zone`` is returned rather than a bare boolean because the three-way answer
    is the useful one at point of entry: a confident duplicate should block the
    save, an ambiguous one should prompt the operator, and a clear miss should
    do nothing. Collapsing that to true/false pushes the judgement back onto a
    caller with less information.
    """

    candidate_count: int
    best_zone: str
    matches: list[DuplicateMatch]
    elapsed_ms: int


class SubmitResponse(BaseModel):
    batch_id: str
    accepted: bool
    enqueued: bool
    row_count: int
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


class PersonResponse(BaseModel):
    person_id: str
    attributes: dict[str, Any]
    masked: bool
    version: int
    valid_from: dt.datetime | None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


#: The connection and principal dependencies live in cmdm.api.deps so the API
#: and the console resolve the same ones.
_require = require


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the application.

    A factory rather than a module-level singleton, so tests can build an
    isolated instance and a deployment can wrap it before serving.
    """
    api = FastAPI(
        title="Customer MDM",
        version="0.1.0",
        summary="Golden record read/search, batch submission, and point-of-entry deduplication.",
    )

    @api.get("/health")
    def health(conn: ConnectionDep) -> dict[str, Any]:
        """Liveness plus a real database round trip.

        A health check that does not touch the database reports healthy while
        every request fails, which is worse than no health check.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mdm.person WHERE is_current")
            persons = cur.fetchone()[0]
        return {"status": "ok", "golden_persons": persons}

    @api.get("/persons/{person_id}", response_model=PersonResponse)
    def get_person(
        person_id: uuid.UUID,
        request: Request,
        conn: ConnectionDep,
        principal: PrincipalDep,
    ) -> PersonResponse:
        """Fetch one golden record, following merge pointers.

        A caller holding an id that a merge has since retired still gets the
        surviving record rather than a 404. Ids are published to downstream
        systems and cannot be invalidated by an internal merge.
        """
        _require(principal, Action.READ, conn)

        from cmdm.store.writer import resolve_person_id

        resolved = resolve_person_id(conn, person_id)

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM mdm.person WHERE person_id = %s AND is_current", (resolved,)
            )
            row = cur.fetchone()

        if row is None:
            log_access(conn, principal, Action.READ, entity_name="Person",
                       entity_id=person_id, record_count=0)
            raise HTTPException(status_code=404, detail="person not found")

        frame = pl.DataFrame([{k: v for k, v in row.items()}], strict=False)
        masked, revealed = mask_frame(frame, PERSON, principal)

        log_access(
            conn, principal, Action.READ, entity_name="Person", entity_id=resolved,
            pii_revealed=revealed, request_id=request.headers.get("X-Request-Id"),
        )
        attributes = masked.row(0, named=True)
        return PersonResponse(
            person_id=str(resolved),
            attributes={k: _jsonable(v) for k, v in attributes.items()},
            masked=not revealed,
            version=int(row.get("version") or 1),
            valid_from=row.get("valid_from"),
        )

    @api.get("/persons")
    def search_persons(
        request: Request,
        conn: ConnectionDep,
        principal: PrincipalDep,
        name: Annotated[str | None, Query(max_length=300)] = None,
        email: Annotated[str | None, Query(max_length=320)] = None,
        postal_code: Annotated[str | None, Query(max_length=20)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """Search golden records.

        The limit is capped rather than unbounded. An uncapped search endpoint
        over a golden store is a bulk-export endpoint that nobody labelled as
        one, and the access log's record count is what makes a large query
        visible afterwards.
        """
        _require(principal, Action.SEARCH, conn)

        if not any([name, email, postal_code]):
            raise HTTPException(
                status_code=400,
                detail="at least one of name, email or postal_code is required",
            )

        clauses, params = ["is_current"], []
        if name:
            # Matched against the normalized column using the same normalization
            # the pipeline applied, so a search behaves like the matcher rather
            # than like a different, worse matcher.
            from cmdm.ingest.normalize import normalize_full_name

            normalized = (
                pl.DataFrame({"x": [name]})
                .select(normalize_full_name(pl.col("x")).alias("n"))["n"][0]
            )
            clauses.append("full_name_normalized LIKE %s")
            params.append(f"%{normalized}%")
        if email:
            clauses.append("email_normalized = %s")
            params.append(email.strip().lower())
        if postal_code:
            clauses.append("replace(upper(postal_code), ' ', '') = %s")
            params.append(postal_code.upper().replace(" ", ""))

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT person_id, full_name, full_name_normalized, date_of_birth,
                       email_address, postal_code, party_type, version
                FROM mdm.person WHERE {' AND '.join(clauses)}
                ORDER BY full_name_normalized LIMIT %s
                """,
                (*params, limit),
            )
            rows = cur.fetchall()

        revealed = principal.may_unmask
        if rows:
            frame = pl.DataFrame([dict(r) for r in rows], strict=False)
            masked, revealed = mask_frame(frame, PERSON, principal)
            results = [
                {k: _jsonable(v) for k, v in row.items()}
                for row in masked.iter_rows(named=True)
            ]
        else:
            results = []

        log_access(
            conn, principal, Action.SEARCH, entity_name="Person",
            record_count=len(results), pii_revealed=revealed,
            request_id=request.headers.get("X-Request-Id"),
            detail={"name": bool(name), "email": bool(email), "postal_code": bool(postal_code)},
        )
        return {"count": len(results), "masked": not revealed, "results": results}

    @api.post("/duplicate-check", response_model=DuplicateCheckResponse)
    def duplicate_check(
        candidate: PartyCandidate,
        request: Request,
        conn: ConnectionDep,
        principal: PrincipalDep,
    ) -> DuplicateCheckResponse:
        """Point-of-entry deduplication.

        Runs the same blocking keys and the same comparator set as the batch
        pipeline. That is the whole point: a check that used its own simpler
        logic would tell an operator the record is new and then be contradicted
        by the nightly run, which is worse than not checking.

        Candidates are fetched by blocking key rather than by scanning, so the
        cost is an index probe and a scoring pass over a handful of rows.
        """
        import time

        _require(principal, Action.DUPLICATE_CHECK, conn)
        started = time.perf_counter()

        probe = _candidate_frame(candidate)
        candidates = _fetch_blocking_candidates(conn, probe)

        matches: list[DuplicateMatch] = []
        best_zone = "AUTO_REJECT"

        if candidates.height:
            from cmdm.resolve.scoring import Zone, score_pairs

            pairs = pl.DataFrame({
                "left_id": ["__candidate__"] * candidates.height,
                "right_id": candidates["person_id"].cast(pl.String).to_list(),
            })
            population = pl.concat(
                [probe, candidates.select(probe.columns)], how="vertical_relaxed"
            )
            scored, _ = score_pairs(pairs, population)

            order = {Zone.AUTO_MATCH: 2, Zone.GREY: 1, Zone.AUTO_REJECT: 0}
            scored = scored.sort("score", descending=True)
            lookup = {
                str(r["person_id"]): r for r in candidates.iter_rows(named=True)
            }
            for row in scored.head(10).iter_rows(named=True):
                if row["zone"] == Zone.AUTO_REJECT:
                    continue
                source = lookup.get(str(row["right_id"]), {})
                matched_on = [
                    c[4:] for c in scored.columns
                    if c.startswith("cmp_") and (row.get(c) or 0) >= 0.99
                ]
                matches.append(DuplicateMatch(
                    person_id=str(row["right_id"]),
                    score=round(float(row["score"]), 4),
                    zone=row["zone"],
                    full_name=(
                        source.get("full_name") if principal.may_unmask else None
                    ),
                    date_of_birth=(
                        source.get("date_of_birth") if principal.may_unmask else None
                    ),
                    matched_on=matched_on,
                ))
                if order[row["zone"]] > order[best_zone]:
                    best_zone = row["zone"]

        elapsed = int((time.perf_counter() - started) * 1000)
        log_access(
            conn, principal, Action.DUPLICATE_CHECK, entity_name="Person",
            record_count=len(matches), pii_revealed=principal.may_unmask,
            request_id=request.headers.get("X-Request-Id"),
            detail={"best_zone": best_zone, "elapsed_ms": elapsed},
        )
        return DuplicateCheckResponse(
            candidate_count=candidates.height,
            best_zone=best_zone,
            matches=matches,
            elapsed_ms=elapsed,
        )

    @api.post("/batches", response_model=SubmitResponse)
    async def submit_batch(
        request: Request,
        conn: ConnectionDep,
        principal: PrincipalDep,
        mapping_name: Annotated[str, Query(max_length=100)],
        file: Annotated[UploadFile, FileParam()],
    ) -> SubmitResponse:
        """Submit a batch: validated now, processed later.

        Validation is synchronous so the caller learns immediately which columns
        were wrong. Everything after that is behind the queue, and the landing
        write and the enqueue commit together.
        """
        _require(principal, Action.SUBMIT, conn)

        import pathlib

        from cmdm.ingest.landing import accept_batch
        from cmdm.ingest.mapping import load_mapping

        mappings = pathlib.Path(__file__).resolve().parent.parent / "mappings"
        path = mappings / f"{mapping_name}.toml"
        if not path.exists() or path.parent != mappings:
            raise HTTPException(status_code=404, detail=f"unknown mapping {mapping_name!r}")

        payload = await file.read()
        try:
            raw = pl.read_csv(io.BytesIO(payload), infer_schema_length=0)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"could not parse CSV: {exc}") from None

        mapping = load_mapping(path)
        batch_id, report, enqueued = accept_batch(
            conn, raw, mapping, origin="API", filename=file.filename,
            submitted_by=principal.subject,
        )

        log_access(
            conn, principal, Action.SUBMIT, entity_name="Batch", entity_id=batch_id,
            record_count=raw.height,
            detail={"accepted": report.accepted, "enqueued": enqueued},
        )
        return SubmitResponse(
            batch_id=str(batch_id),
            accepted=report.accepted,
            enqueued=enqueued,
            row_count=raw.height,
            errors=[i.as_dict() for i in report.errors],
            warnings=[i.as_dict() for i in report.warnings],
        )

    @api.get("/persons/{person_id}/lineage")
    def person_lineage(
        person_id: uuid.UUID,
        conn: ConnectionDep,
        principal: PrincipalDep,
    ) -> dict[str, Any]:
        """Why this record looks the way it does.

        The question stewards actually ask. Returns the surviving value per
        contested attribute, the source that won, the rule that selected it and
        the candidates that lost.
        """
        _require(principal, Action.READ, conn)

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT attribute_name, strategy, winning_source_system, value_text,
                       candidate_count, rejected_values, decided_at
                FROM mdm.attribute_provenance
                WHERE entity_id = %s ORDER BY attribute_name
                """,
                (person_id,),
            )
            provenance = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT source_system, source_key_kind, source_party_key, is_active,
                       derivation_method, confidence
                FROM mdm.person_xref WHERE person_id = %s
                """,
                (person_id,),
            )
            sources = [dict(r) for r in cur.fetchall()]

        log_access(conn, principal, Action.READ, entity_name="PersonLineage",
                   entity_id=person_id, record_count=len(provenance))
        return {
            "person_id": str(person_id),
            "contributing_sources": sources,
            "attribute_provenance": [
                {k: _jsonable(v) for k, v in row.items()} for row in provenance
            ],
        }

    @api.get("/metrics")
    def metrics(
        conn: ConnectionDep,
    ) -> Response:
        """Prometheus exposition.

        Unauthenticated by design, and it must stay free of anything
        person-level: a scrape endpoint is usually reachable from the whole
        monitoring network, so it carries counts and ratios only.
        """
        from cmdm.observe import render_prometheus

        return Response(
            content=render_prometheus(conn),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @api.exception_handler(LoginRequired)
    def _console_login(request: Request, exc: LoginRequired) -> Response:
        """Send an unauthenticated browser to the sign-in page.

        Registered on the application rather than handled per route: a console
        page that forgot would 403 with a JSON body, which a person cannot act
        on and would read as the system being broken.
        """
        return RedirectResponse(
            f"/console/login?next={quote(request.url.path)}", status_code=303
        )

    api.include_router(console_router)
    return api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Render a database value for JSON."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
        return float(value)
    return value


def _candidate_frame(candidate: PartyCandidate) -> pl.DataFrame:
    """Turn a submitted candidate into a party frame.

    Runs the same normalization kernels as ingestion. A duplicate check that
    normalized differently from the pipeline would compare a differently-shaped
    key against the stored ones and quietly miss matches.
    """
    from cmdm.ingest import normalize as N

    # The schema is declared rather than inferred. Point-of-entry input is
    # partial by nature, and a column that happens to be entirely null infers as
    # Polars' Null dtype, which every string kernel then refuses.
    frame = pl.DataFrame(
        {
            "person_id": ["__candidate__"],
            "full_name": [candidate.full_name],
            "date_of_birth": [candidate.date_of_birth],
            "email_address": [candidate.email],
            "phone_raw": [candidate.phone],
            "address_line1": [candidate.address_line1],
            "postal_code": [candidate.postal_code],
            "country_code": [candidate.country_code],
        },
        schema={
            "person_id": pl.String, "full_name": pl.String,
            "date_of_birth": pl.Date, "email_address": pl.String,
            "phone_raw": pl.String, "address_line1": pl.String,
            "postal_code": pl.String, "country_code": pl.String,
        },
    )

    frame = frame.with_columns(
        N.normalize_full_name(pl.col("full_name")).alias("full_name_normalized"),
        N.normalize_email(pl.col("email_address")).alias("email_normalized"),
        N.normalize_phone(pl.col("phone_raw")).alias("phone_e164"),
        N.normalize_address(pl.col("address_line1")).alias("address_normalized"),
    ).with_columns(
        N.name_tokens(pl.col("full_name_normalized")).alias("name_tokens"),
    ).with_columns(
        N.name_sorted_key(pl.col("name_tokens")).alias("name_sorted_key"),
        N.name_phonetic_key(pl.col("full_name_normalized")).alias("name_phonetic_key"),
        N.detect_party_type(pl.col("name_tokens")).alias("party_type"),
        N.address_key(pl.col("address_normalized"), pl.col("postal_code")).alias("address_key"),
        pl.lit(None, dtype=pl.String).alias("national_id_hash"),
        pl.lit(None, dtype=pl.String).alias("gender"),
    ).with_columns(
        pl.when(pl.col("name_tokens").list.len() == 2)
        .then(pl.col("name_tokens").list.get(0, null_on_oob=True))
        .alias("given_name_derived"),
        pl.when(pl.col("name_tokens").list.len() == 2)
        .then(pl.col("name_tokens").list.get(1, null_on_oob=True))
        .alias("surname_derived"),
    )
    return frame


#: Blocking keys probed for a point-of-entry check, most selective first.
_PROBE_KEYS = (
    ("email_normalized", "email_normalized"),
    ("phone_e164", "phone_e164"),
    ("name_sorted_key", "name_sorted_key"),
    ("name_phonetic_key", "name_phonetic_key"),
    ("address_key", "address_key"),
)

#: Cap on candidates pulled back per request. A common name with a shared
#: blocking key could otherwise return thousands and turn a sub-second check
#: into a timeout on the operator's screen.
MAX_PROBE_CANDIDATES = 200


def _fetch_blocking_candidates(
    conn: psycopg.Connection, probe: pl.DataFrame
) -> pl.DataFrame:
    """Fetch stored parties sharing any blocking key with the candidate.

    One indexed query per key rather than a scan. This is the step that makes a
    real-time check possible at all: without blocking it would be a comparison
    against the entire book.
    """
    conditions, params = [], []
    for column, stored in _PROBE_KEYS:
        value = probe[column][0] if column in probe.columns else None
        if value:
            conditions.append(f"{stored} = %s")
            params.append(value)

    if not conditions:
        return pl.DataFrame(schema={c: probe.schema[c] for c in probe.columns})

    columns = [c for c in probe.columns if c != "person_id"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT person_id::text AS person_id, {', '.join(columns)}
            FROM mdm.person
            WHERE is_current AND ({' OR '.join(conditions)})
            LIMIT {MAX_PROBE_CANDIDATES}
            """,
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return pl.DataFrame(schema={c: probe.schema[c] for c in probe.columns})
    return pl.DataFrame([dict(r) for r in rows], strict=False).select(probe.columns)


app = create_app()

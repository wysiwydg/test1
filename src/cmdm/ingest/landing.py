"""Batch validation and the landing zone.

Two jobs, and the split between them is the point.

**Validation is synchronous.** A caller submitting a file learns immediately
whether it was accepted, and if not, exactly which columns and rows were wrong.
Deferring that to a worker means the caller gets a 202 and finds out hours later
via an email nobody reads. Validation is cheap — a schema check and a scan for
structural problems — so there is no reason to make it asynchronous.

**Processing is not.** Once a batch is accepted it is landed and enqueued, and
the caller is done. Shredding, standardization and resolution happen behind the
queue.

The accept step is one transaction: the raw records reach ``source_record``, the
batch row reaches ``ingest_batch``, and the job reaches ``work_queue`` together.
That is the property that makes a Postgres-backed queue worth choosing — with a
separate broker, accepting and enqueueing are two commits, and the window
between them is where batches are silently lost.

Landing is content-addressed. Every row carries the hash of its own payload and
the batch carries the hash of the whole file, so re-delivery is detected rather
than duplicated. Feeds re-send far more often than anyone expects: a failed
overnight job re-runs, an operator re-uploads "to be safe", a full extract
overlaps the delta that preceded it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from cmdm.db.queue import QUEUE_STANDARDIZE, WorkQueue
from cmdm.ingest.mapping import SourceMapping
from cmdm.model.ids import payload_hash, uuid7

__all__ = [
    "ValidationIssue",
    "ValidationReport",
    "validate_batch",
    "land_batch",
    "accept_batch",
    "MAX_REJECT_RATIO",
    "DATE_PARSE_WARN",
    "DATE_PARSE_ERROR",
]

#: A batch where more than this share of rows are unusable is rejected whole
#: rather than partially accepted. A file that is mostly broken is a delivery
#: failure -- a truncated transfer, the wrong file, a bad export -- and landing
#: its good half creates a golden record built from a fragment, which is worse
#: than landing nothing and saying so.
MAX_REJECT_RATIO = 0.20

#: Share of populated date values that must parse before a column is reported.
#:
#: Not 0.5. The failure this exists to catch is a source switching between
#: day-first and month-first, and that only breaks the days above 12 -- roughly
#: two thirds still parse, silently and wrongly. A single 50% threshold
#: therefore stays quiet through exactly the change it was written for. A feed
#: whose format is right parses very nearly everything, so the bar is set where
#: a real feed sits and scattered junk ("N/A", "0000-00-00") is tolerated.
DATE_PARSE_WARN = 0.95

#: Below this, the column is not dirty -- the format is simply wrong -- and the
#: batch is refused rather than landed as a column of nulls.
DATE_PARSE_ERROR = 0.50


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One structural problem found in a submitted batch."""

    severity: str
    code: str
    message: str
    column: str | None = None
    row_count: int = 0
    sample: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "column": self.column,
            "row_count": self.row_count,
            "sample": list(self.sample),
        }


@dataclass(slots=True)
class ValidationReport:
    """The verdict on a submitted batch."""

    row_count: int = 0
    issues: list[ValidationIssue] = field(default_factory=list)
    #: Row indices that cannot be processed at all.
    unusable_rows: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def accepted(self) -> bool:
        """Whether the batch may proceed.

        Warnings do not block. A feed with a few unparseable dates is normal and
        the pipeline records those as data-quality findings; refusing the whole
        file for them would mean no file ever loads.
        """
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "unusable_rows": self.unusable_rows,
            "accepted": self.accepted,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.as_dict() for i in self.issues],
        }


def _sample(series: pl.Series, n: int = 3) -> tuple[str, ...]:
    """A few example values, for an error message a human can act on.

    "Column OwnerDOB has 412 unparseable values" is a report. Adding
    "e.g. 31/02/2019, N/A, 0000-00-00" is a diagnosis.
    """
    values = series.drop_nulls().unique().head(n).to_list()
    return tuple(str(v)[:60] for v in values)


def validate_batch(raw: pl.DataFrame, mapping: SourceMapping) -> ValidationReport:
    """Check a batch against its mapping, synchronously.

    Structural checks only. Whether a name parses well or an address splits
    cleanly is the standardization stage's quality gate, not this — that
    judgement needs the normalization kernels to have run, and running them here
    would duplicate the pipeline in the validator.
    """
    report = ValidationReport(row_count=raw.height)

    if raw.height == 0:
        report.issues.append(
            ValidationIssue("ERROR", "EMPTY_BATCH", "The batch contains no rows.")
        )
        return report

    # Mapped columns that are absent. Reported by name: a renamed column is the
    # single most common feed breakage and the fix is obvious once it is named.
    missing = mapping.missing_columns(raw.columns)
    if missing:
        report.issues.append(
            ValidationIssue(
                "ERROR",
                "MISSING_COLUMNS",
                f"Mapped columns absent from the batch: {', '.join(missing)}",
                row_count=raw.height,
            )
        )
        # Everything below reads mapped columns, so stop rather than emit a
        # cascade of errors that all restate this one.
        return report

    absent = mapping.missing_optional_columns(raw.columns)
    if absent:
        report.issues.append(
            ValidationIssue(
                "WARNING",
                "MISSING_OPTIONAL_COLUMNS",
                f"Declared but absent: {', '.join(absent)}. The batch is "
                "processed without them; households will not be derived from "
                "this file because nothing in it states how the parties are "
                "related.",
                row_count=raw.height,
            )
        )

    policy_number_col = next(
        (f.source for f in mapping.policy if f.canonical == "policy_number" and f.source), None
    )

    if policy_number_col:
        pn = raw.get_column(policy_number_col).cast(pl.String, strict=False)

        blank = int((pn.is_null() | (pn.str.strip_chars().str.len_chars() == 0)).sum())
        if blank:
            report.unusable_rows = max(report.unusable_rows, blank)
            report.issues.append(
                ValidationIssue(
                    "ERROR" if blank > raw.height * MAX_REJECT_RATIO else "WARNING",
                    "BLANK_POLICY_NUMBER",
                    f"{blank} rows have no policy number and cannot be attached to a contract.",
                    column=policy_number_col,
                    row_count=blank,
                )
            )

        # Duplicates within one file are usually benign (a full extract
        # concatenated with a delta) and the shredder deduplicates them, so this
        # is information rather than a fault. It is worth surfacing because a
        # sudden spike means the export changed.
        from cmdm.ingest.normalize import normalize_policy_number

        normalized = raw.select(
            normalize_policy_number(pl.col(policy_number_col).cast(pl.String, strict=False))
            .alias("n")
        )["n"]
        dupes = raw.height - normalized.n_unique()
        if dupes:
            report.issues.append(
                ValidationIssue(
                    "WARNING",
                    "DUPLICATE_POLICY_NUMBERS",
                    f"{dupes} rows repeat a policy number already in this batch; "
                    "they will be deduplicated.",
                    column=policy_number_col,
                    row_count=dupes,
                )
            )

    # Every party block needs a usable party on at least some rows. A block that
    # is entirely empty means the mapping points at the wrong columns, which
    # otherwise surfaces much later as an entity that simply never appears.
    for party in mapping.parties:
        name_field = next((f for f in party.fields if f.canonical == "full_name"), None)
        if name_field is None or not name_field.source:
            continue
        names = raw.get_column(name_field.source).cast(pl.String, strict=False)
        populated = int((names.is_not_null() & (names.str.strip_chars().str.len_chars() > 0)).sum())
        if populated == 0:
            report.issues.append(
                ValidationIssue(
                    "ERROR",
                    "EMPTY_PARTY_BLOCK",
                    f"No row carries a name for role {party.role.value}. "
                    f"Check that {name_field.source!r} is the right column.",
                    column=name_field.source,
                )
            )
        elif populated < raw.height * 0.5:
            report.issues.append(
                ValidationIssue(
                    "WARNING",
                    "SPARSE_PARTY_BLOCK",
                    f"Only {populated} of {raw.height} rows carry a name for role "
                    f"{party.role.value}.",
                    column=name_field.source,
                    row_count=raw.height - populated,
                )
            )

    # Date columns that parse for almost nothing usually mean the source changed
    # format, not that the data is bad. Catching it here saves a batch full of
    # nulls that looks like missing data rather than a parsing failure.
    #
    # Every date column, not only the policy-grain ones. Dates of birth live in
    # the party blocks, and they are the ones that matter most: date_of_birth is
    # both a comparator and a veto, so a feed that switches to month-first
    # silently drops the values that cannot parse and mis-parses the ones where
    # both numbers are under 13. Checking only the policy dates let a batch
    # through reporting zero warnings while losing 184 of 567 dates of birth.
    from cmdm.ingest.normalize import parse_date

    date_fields = [
        (None, f) for f in mapping.policy if f.transform == "date" and f.source
    ] + [
        (party.role.value, f)
        for party in mapping.parties
        for f in party.fields
        if f.transform == "date" and f.source
    ]
    for role, fm in date_fields:
        series = raw.get_column(fm.source).cast(pl.String, strict=False)
        populated = series.is_not_null() & (series.str.strip_chars().str.len_chars() > 0)
        non_null = int(populated.sum())
        if non_null == 0:
            continue
        # The mapping's formats, not the module default. The default list holds
        # both %d/%m/%Y and %m/%d/%Y, so validating against it accepts a column
        # the shredder -- which does pass the mapping's formats -- will then fail
        # to parse. A check more permissive than the pipeline it guards is worse
        # than no check: it reports the file clean and the dates vanish later.
        parsed = int(
            raw.select(
                parse_date(
                    pl.col(fm.source).cast(pl.String, strict=False),
                    tuple(mapping.date_formats),
                ).alias("d")
            )["d"]
            .is_not_null()
            .sum()
        )
        if parsed < non_null * DATE_PARSE_WARN:
            where = f" for role {role}" if role else ""
            report.issues.append(
                ValidationIssue(
                    "ERROR" if parsed < non_null * DATE_PARSE_ERROR else "WARNING",
                    "DATE_FORMAT_MISMATCH",
                    f"Only {parsed} of {non_null} populated values in {fm.source!r}"
                    f"{where} parse with the configured formats "
                    f"{list(mapping.date_formats)}.",
                    column=fm.source,
                    row_count=non_null - parsed,
                    sample=_sample(series),
                )
            )

    return report


def _rows_as_payloads(raw: pl.DataFrame, mapping: SourceMapping) -> list[dict[str, Any]]:
    """Render rows as JSON payloads for the landing zone.

    Only the mapped columns are kept. An extract commonly carries a hundred
    columns of which the mapping reads forty, and landing the rest would store
    unmapped PII that nothing will ever read but everything must protect.
    """
    columns = [c for c in mapping.source_columns if c in raw.columns]
    return raw.select(columns).to_dicts()


def land_batch(
    conn: psycopg.Connection,
    raw: pl.DataFrame,
    mapping: SourceMapping,
    *,
    batch_id: uuid.UUID,
) -> int:
    """Write raw rows into the immutable landing zone.

    Uses COPY rather than executemany: this is the widest write in the pipeline
    and row-at-a-time insertion of a million-row extract is the difference
    between minutes and hours.

    Rows already present by payload hash are skipped, so re-landing an
    overlapping file is a no-op rather than a duplicate. The skip is done with an
    ON CONFLICT on the natural key, which keeps it a single statement instead of
    a read-then-write race.
    """
    payloads = _rows_as_payloads(raw, mapping)
    if not payloads:
        return 0

    policy_col = next(
        (f.source for f in mapping.policy if f.canonical == "policy_number" and f.source), None
    )
    ts_col = mapping.source_timestamp_field
    now = dt.datetime.now(dt.UTC)

    rows = []
    for payload in payloads:
        row_key = str(payload.get(policy_col, "") or "") if policy_col else ""
        source_ts = payload.get(ts_col) if ts_col else None
        rows.append(
            (
                uuid7(),
                mapping.source_system,
                str(batch_id),
                row_key,
                payload_hash(payload),
                payload,
                source_ts,
                now,
            )
        )

    with conn.cursor() as cur:
        # Staged through a temporary table so COPY's speed is kept while still
        # getting ON CONFLICT semantics, which COPY itself does not support.
        # Dropped first rather than created with IF NOT EXISTS: landing two
        # batches inside one transaction is legitimate, and a leftover stage
        # table from the previous call would silently carry its rows into this
        # one.
        cur.execute("DROP TABLE IF EXISTS _landing_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _landing_stage (
                source_record_id uuid, source_system text, source_batch_id text,
                source_row_key text, payload_hash text, payload jsonb,
                source_timestamp_raw text, ingested_at timestamptz
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _landing_stage (source_record_id, source_system, source_batch_id, "
            "source_row_key, payload_hash, payload, source_timestamp_raw, ingested_at) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row(
                    (r[0], r[1], r[2], r[3], r[4], Jsonb(r[5]), r[6], r[7])
                )

        cur.execute(
            """
            INSERT INTO mdm.source_record
                (source_record_id, source_system, source_batch_id, source_row_key,
                 payload_hash, payload, source_timestamp, ingested_at, is_processed)
            SELECT source_record_id, source_system, source_batch_id, source_row_key,
                   payload_hash, payload,
                   -- A source that supplies no change timestamp falls back to
                   -- ingest time. Recorded as-is rather than left null, because
                   -- MOST_RECENT survivorship needs a value and a null would
                   -- silently drop the record out of every recency comparison.
                   coalesce(
                       nullif(source_timestamp_raw, '')::timestamptz,
                       ingested_at
                   ),
                   ingested_at, false
            FROM _landing_stage
            ON CONFLICT (source_system, source_batch_id, source_row_key) DO NOTHING
            """
        )
        return cur.rowcount


def accept_batch(
    conn: psycopg.Connection,
    raw: pl.DataFrame,
    mapping: SourceMapping,
    *,
    origin: str,
    filename: str | None = None,
    submitted_by: str | None = None,
    content_hash: str | None = None,
) -> tuple[uuid.UUID, ValidationReport, bool]:
    """Validate, land and enqueue a batch in one transaction.

    Returns ``(batch_id, report, enqueued)``. ``enqueued`` is false when the
    batch was rejected or was a byte-identical redelivery of one already
    accepted.

    The caller is expected to have opened the transaction. Everything this
    function writes — the batch row, the landed records, the queued job — must
    commit together, and a caller that wraps it differently loses exactly the
    guarantee the design exists for.
    """
    batch_id = uuid7()
    report = validate_batch(raw, mapping)
    content_hash = content_hash or payload_hash(
        {"rows": _rows_as_payloads(raw, mapping)}
    )

    state = "VALIDATED" if report.accepted else "REJECTED"

    with conn.cursor() as cur:
        # A byte-identical file already accepted is not landed again. The unique
        # index makes this authoritative under concurrency rather than a check
        # that two simultaneous uploads can both pass.
        cur.execute(
            """
            INSERT INTO mdm.ingest_batch
                (batch_id, source_system, mapping_name, origin, filename, content_hash,
                 state, row_count, validation_report, submitted_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_system, content_hash) WHERE state <> 'REJECTED'
                DO NOTHING
            RETURNING batch_id
            """,
            (
                batch_id, mapping.source_system,
                # Which mapping, not which system. A worker reloads the batch
                # from this name; storing the source system here made every
                # batch claim to have been read by a mapping file that does not
                # exist, so nothing could reprocess one.
                mapping.name or mapping.source_system,
                origin, filename, content_hash, state, raw.height,
                Jsonb(report.as_dict()), submitted_by,
            ),
        )
        inserted = cur.fetchone()

    if inserted is None:
        # Redelivery. Report it as such rather than pretending it was accepted,
        # so an operator re-uploading "to be safe" gets told it was already here.
        report.issues.append(
            ValidationIssue(
                "WARNING",
                "DUPLICATE_BATCH",
                "A byte-identical batch from this source was already accepted; "
                "this submission was ignored.",
            )
        )
        return batch_id, report, False

    if not report.accepted:
        return batch_id, report, False

    landed = land_batch(conn, raw, mapping, batch_id=batch_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mdm.ingest_batch SET state = 'LANDED', accepted_count = %s WHERE batch_id = %s",
            (landed, batch_id),
        )

    WorkQueue(conn).enqueue(
        QUEUE_STANDARDIZE,
        {"batch_id": str(batch_id), "source_system": mapping.source_system},
        dedupe_key=f"batch:{batch_id}",
    )
    return batch_id, report, True

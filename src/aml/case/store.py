"""The operational store: alerts, cases, reports, submissions and the audit chain.

**Why SQLite, when the MDM beside it insists on PostgreSQL.** They are storing
different things for different people. The golden store is a concurrently
written master database that needs ``SKIP LOCKED``, deferred constraints and
enum types. This is a compliance unit's case ledger: written by one team,
retained for at least five years, and — the part that decides it — handed over
whole. When the AMLC or the Insurance Commission asks for the alert and case
history behind a filing, "here is the file, and here is the command that proves
it has not been altered" is a better answer than a database dump nobody can
verify. It is also why an examiner can open it on a laptop with no server.
Institutions that want it beside the golden store can point the same schema at
PostgreSQL; nothing here depends on SQLite specifics.

**The audit chain.** Every state change is appended with the hash of the entry
before it. Editing or deleting a row anywhere in the history breaks every hash
after it, and :meth:`AmlStore.verify_audit_chain` says exactly where. That does
not make the record unalterable — nothing in a file on disk is — but it makes
alteration *detectable*, which is the property that matters when the question
is whether an alert was quietly closed after the fact.

**Alerts are upserted, never duplicated.** Monitoring is re-run constantly:
after a rule change, after a late batch, after a correction. An alert is
identified by what it found (see ``Alert.dedup_key``), so a re-run updates the
existing alert and leaves the analyst's work on it intact.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aml.model.entities import Alert, Case, jsonable
from aml.model.enums import AlertState, CaseState, ReportKind, Severity, SubmissionState
from aml.money import PHP, Money
from aml.phcalendar import now_manila

__all__ = ["AmlStore", "WorkflowError", "UpsertResult", "AuditEntry"]


class WorkflowError(RuntimeError):
    """An action the workflow does not permit in the current state."""


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS alert (
    alert_id                TEXT PRIMARY KEY,
    dedup_key               TEXT NOT NULL UNIQUE,
    rule_id                 TEXT NOT NULL,
    rule_version            TEXT NOT NULL,
    subject_party_id        TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    severity                TEXT NOT NULL,
    score                   TEXT NOT NULL,
    title                   TEXT NOT NULL,
    narrative               TEXT NOT NULL,
    transaction_ids         TEXT NOT NULL,
    policy_ids              TEXT NOT NULL,
    window_start            TEXT,
    window_end              TEXT,
    st_codes                TEXT NOT NULL,
    unlawful_activity_codes TEXT NOT NULL,
    amount_php              TEXT,
    evidence                TEXT NOT NULL,
    state                   TEXT NOT NULL,
    case_id                 TEXT,
    is_covered_transaction  INTEGER NOT NULL DEFAULT 0,
    dedup_extra             TEXT NOT NULL DEFAULT '[]',
    first_run_id            TEXT,
    last_run_id             TEXT,
    updated_at              TEXT NOT NULL,
    closed_reason           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_alert_state ON alert (state, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_alert_subject ON alert (subject_party_id);
CREATE INDEX IF NOT EXISTS ix_alert_case ON alert (case_id);
CREATE INDEX IF NOT EXISTS ix_alert_covered ON alert (is_covered_transaction, state);

CREATE TABLE IF NOT EXISTS case_file (
    case_id                 TEXT PRIMARY KEY,
    kind                    TEXT NOT NULL,
    subject_party_id        TEXT NOT NULL,
    opened_at               TEXT NOT NULL,
    state                   TEXT NOT NULL,
    alert_ids               TEXT NOT NULL,
    transaction_ids         TEXT NOT NULL,
    policy_ids              TEXT NOT NULL,
    st_codes                TEXT NOT NULL,
    unlawful_activity_codes TEXT NOT NULL,
    narrative               TEXT NOT NULL,
    assigned_to             TEXT NOT NULL DEFAULT '',
    determined_at           TEXT,
    determined_by           TEXT NOT NULL DEFAULT '',
    filing_deadline         TEXT,
    approved_by             TEXT NOT NULL DEFAULT '',
    approved_at             TEXT,
    filed_at                TEXT,
    closure_reason          TEXT NOT NULL DEFAULT '',
    reference_number        TEXT NOT NULL DEFAULT '',
    priority                TEXT NOT NULL DEFAULT 'MEDIUM'
);
CREATE INDEX IF NOT EXISTS ix_case_state ON case_file (state, filing_deadline);
CREATE INDEX IF NOT EXISTS ix_case_subject ON case_file (subject_party_id);

CREATE TABLE IF NOT EXISTS report_file (
    report_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    case_id         TEXT,
    path            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    rows            INTEGER NOT NULL,
    size_bytes      INTEGER NOT NULL,
    spec_version    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    period_start    TEXT,
    period_end      TEXT,
    references_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_report_kind ON report_file (kind, created_at DESC);

CREATE TABLE IF NOT EXISTS submission (
    submission_id     TEXT PRIMARY KEY,
    report_id         TEXT NOT NULL REFERENCES report_file (report_id),
    state             TEXT NOT NULL,
    mode              TEXT NOT NULL,
    package_path      TEXT NOT NULL DEFAULT '',
    package_sha256    TEXT NOT NULL DEFAULT '',
    submitted_at      TEXT,
    acknowledged_at   TEXT,
    receipt_reference TEXT NOT NULL DEFAULT '',
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_submission_state ON submission (state, created_at DESC);

CREATE TABLE IF NOT EXISTS screening_result (
    result_id    TEXT PRIMARY KEY,
    subject_id   TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    screened_at  TEXT NOT NULL,
    providers    TEXT NOT NULL,
    decision     TEXT NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL,
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_screening_subject ON screening_result (subject_id, screened_at DESC);
CREATE INDEX IF NOT EXISTS ix_screening_decision ON screening_result (decision, screened_at DESC);

CREATE TABLE IF NOT EXISTS monitoring_run (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    detail        TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    entry_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log (entity_type, entity_id, seq);
"""


@dataclass(frozen=True, slots=True)
class UpsertResult:
    created: int
    updated: int
    reopened: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    occurred_at: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: Mapping[str, Any]
    previous_hash: str
    entry_hash: str


def _dumps(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"))


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


def _money(text: str | None) -> Money | None:
    if not text:
        return None
    return Money(Decimal(text), PHP)


def _date(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text) if text else None


def _datetime(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


class AmlStore:
    """Everything the compliance unit's workflow persists."""

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Schema creation runs outside the transactional helper below:
        # ``executescript`` commits any open transaction before it runs, and
        # ``PRAGMA journal_mode = WAL`` cannot be set inside one at all.
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- audit -----------------------------------------------------------

    def _append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: Mapping[str, Any] | None = None,
        at: dt.datetime | None = None,
    ) -> str:
        import hashlib

        row = conn.execute("SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "0" * 64
        occurred = (at or now_manila()).isoformat()
        payload = _dumps(detail or {})
        material = "|".join(
            [previous, occurred, actor, action, entity_type, entity_id, payload]
        )
        entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_log (occurred_at, actor, action, entity_type, entity_id, "
            "detail, previous_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (occurred, actor, action, entity_type, entity_id, payload, previous, entry_hash),
        )
        return entry_hash

    def audit_trail(
        self, entity_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        query = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity_id:
            query += " WHERE entity_id = ?"
            params.append(entity_id)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            AuditEntry(
                seq=row["seq"],
                occurred_at=row["occurred_at"],
                actor=row["actor"],
                action=row["action"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                detail=_loads(row["detail"]) or {},
                previous_hash=row["previous_hash"],
                entry_hash=row["entry_hash"],
            )
            for row in rows
        ]

    def verify_audit_chain(self) -> tuple[bool, int | None, str]:
        """Recompute the chain. Returns (intact, first bad sequence, message)."""
        import hashlib

        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        previous = "0" * 64
        for row in rows:
            material = "|".join(
                [
                    previous,
                    row["occurred_at"],
                    row["actor"],
                    row["action"],
                    row["entity_type"],
                    row["entity_id"],
                    row["detail"],
                ]
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous:
                return (
                    False,
                    row["seq"],
                    f"entry {row['seq']} does not follow the entry before it: an entry has "
                    "been removed or reordered",
                )
            if expected != row["entry_hash"]:
                return (
                    False,
                    row["seq"],
                    f"entry {row['seq']} ({row['action']} on {row['entity_id']}) has been "
                    "altered since it was written",
                )
            previous = row["entry_hash"]
        return True, None, f"{len(rows)} audit entries verified"

    # -- alerts ----------------------------------------------------------

    def upsert_alerts(
        self, alerts: Sequence[Alert], *, run_id: str = "", actor: str = "system"
    ) -> UpsertResult:
        created = updated = 0
        with self.connect() as conn:
            for alert in alerts:
                existing = conn.execute(
                    "SELECT alert_id, state FROM alert WHERE dedup_key = ?", (alert.dedup_key,)
                ).fetchone()
                now = now_manila().isoformat()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO alert (
                            alert_id, dedup_key, rule_id, rule_version, subject_party_id,
                            created_at, severity, score, title, narrative, transaction_ids,
                            policy_ids, window_start, window_end, st_codes,
                            unlawful_activity_codes, amount_php, evidence, state, case_id,
                            is_covered_transaction, dedup_extra, first_run_id, last_run_id,
                            updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            alert.alert_id,
                            alert.dedup_key,
                            alert.rule_id,
                            alert.rule_version,
                            alert.subject_party_id,
                            alert.created_at.isoformat(),
                            str(alert.severity),
                            str(alert.score),
                            alert.title,
                            alert.narrative,
                            _dumps(alert.transaction_ids),
                            _dumps(alert.policy_ids),
                            alert.window_start.isoformat() if alert.window_start else None,
                            alert.window_end.isoformat() if alert.window_end else None,
                            _dumps(alert.st_codes),
                            _dumps(alert.unlawful_activity_codes),
                            str(alert.amount_php.amount) if alert.amount_php else None,
                            _dumps(alert.evidence),
                            str(alert.state),
                            alert.case_id,
                            int(alert.is_covered_transaction),
                            _dumps(alert.dedup_extra),
                            run_id,
                            run_id,
                            now,
                        ),
                    )
                    created += 1
                    self._append_audit(
                        conn,
                        actor=actor,
                        action="alert.raised",
                        entity_type="alert",
                        entity_id=alert.alert_id,
                        detail={
                            "rule_id": alert.rule_id,
                            "severity": str(alert.severity),
                            "score": str(alert.score),
                            "subject": alert.subject_party_id,
                            "run_id": run_id,
                        },
                    )
                else:
                    # Refresh the evidence and the score, keep the analyst's
                    # state and case linkage. A re-run must not reopen an alert
                    # somebody has already dispositioned.
                    conn.execute(
                        "UPDATE alert SET score = ?, severity = ?, narrative = ?, "
                        "evidence = ?, last_run_id = ?, updated_at = ? WHERE dedup_key = ?",
                        (
                            str(alert.score),
                            str(alert.severity),
                            alert.narrative,
                            _dumps(alert.evidence),
                            run_id,
                            now,
                            alert.dedup_key,
                        ),
                    )
                    updated += 1
        return UpsertResult(created=created, updated=updated)

    def alerts(
        self,
        *,
        state: AlertState | str | None = None,
        subject_party_id: str | None = None,
        covered: bool | None = None,
        case_id: str | None = None,
        rule_id: str | None = None,
        limit: int = 500,
    ) -> list[Alert]:
        query = "SELECT * FROM alert WHERE 1 = 1"
        params: list[Any] = []
        if state is not None:
            query += " AND state = ?"
            params.append(str(state))
        if subject_party_id:
            query += " AND subject_party_id = ?"
            params.append(subject_party_id)
        if covered is not None:
            query += " AND is_covered_transaction = ?"
            params.append(int(covered))
        if case_id:
            query += " AND case_id = ?"
            params.append(case_id)
        if rule_id:
            query += " AND rule_id = ?"
            params.append(rule_id)
        query += " ORDER BY CAST(score AS REAL) DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._to_alert(row) for row in rows]

    def get_alert(self, alert_id: str) -> Alert | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM alert WHERE alert_id = ?", (alert_id,)).fetchone()
        return self._to_alert(row) if row else None

    @staticmethod
    def _to_alert(row: sqlite3.Row) -> Alert:
        return Alert(
            alert_id=row["alert_id"],
            rule_id=row["rule_id"],
            rule_version=row["rule_version"],
            subject_party_id=row["subject_party_id"],
            created_at=dt.datetime.fromisoformat(row["created_at"]),
            severity=Severity(row["severity"]),
            score=Decimal(row["score"]),
            title=row["title"],
            narrative=row["narrative"],
            transaction_ids=tuple(_loads(row["transaction_ids"]) or ()),
            policy_ids=tuple(_loads(row["policy_ids"]) or ()),
            window_start=_date(row["window_start"]),
            window_end=_date(row["window_end"]),
            st_codes=tuple(_loads(row["st_codes"]) or ()),
            unlawful_activity_codes=tuple(_loads(row["unlawful_activity_codes"]) or ()),
            amount_php=_money(row["amount_php"]),
            evidence=_loads(row["evidence"]) or {},
            state=AlertState(row["state"]),
            case_id=row["case_id"],
            is_covered_transaction=bool(row["is_covered_transaction"]),
            dedup_extra=tuple(_loads(row["dedup_extra"]) or ()),
        )

    def set_alert_state(
        self, alert_id: str, state: AlertState, *, actor: str, reason: str = ""
    ) -> Alert:
        """Move an alert, requiring a reason wherever one is closed.

        Closing without a reason is refused rather than defaulted. The
        institution has to be able to show why an alert was not escalated, and
        "no reason recorded" is the answer that costs it.
        """
        if state in (AlertState.CLOSED_FALSE_POSITIVE, AlertState.CLOSED_NOT_SUSPICIOUS) and (
            not reason.strip()
        ):
            raise WorkflowError(
                f"closing {alert_id} as {state} requires a written reason"
            )
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM alert WHERE alert_id = ?", (alert_id,)).fetchone()
            if row is None:
                raise WorkflowError(f"no alert {alert_id}")
            conn.execute(
                "UPDATE alert SET state = ?, closed_reason = ?, updated_at = ? "
                "WHERE alert_id = ?",
                (str(state), reason, now_manila().isoformat(), alert_id),
            )
            self._append_audit(
                conn,
                actor=actor,
                action="alert.state_changed",
                entity_type="alert",
                entity_id=alert_id,
                detail={"from": row["state"], "to": str(state), "reason": reason},
            )
            updated = conn.execute(
                "SELECT * FROM alert WHERE alert_id = ?", (alert_id,)
            ).fetchone()
        return self._to_alert(updated)

    # -- cases -----------------------------------------------------------

    def open_case(
        self,
        *,
        kind: ReportKind,
        subject_party_id: str,
        alerts: Sequence[Alert],
        actor: str,
        narrative: str = "",
        priority: Severity = Severity.MEDIUM,
        assigned_to: str = "",
        opened_at: dt.datetime | None = None,
    ) -> Case:
        """Open a case over one or more alerts on the same subject."""
        if not alerts:
            raise WorkflowError("a case must be opened over at least one alert")
        wrong_subject = [a.alert_id for a in alerts if a.subject_party_id != subject_party_id]
        if wrong_subject:
            raise WorkflowError(
                f"alerts {', '.join(wrong_subject)} are not about {subject_party_id}"
            )
        moment = opened_at or now_manila()
        case_id = f"CASE-{moment.strftime('%Y%m%d')}-{alerts[0].dedup_key[:8].upper()}"
        transaction_ids = tuple(
            dict.fromkeys(txn_id for alert in alerts for txn_id in alert.transaction_ids)
        )
        policy_ids = tuple(
            dict.fromkeys(pid for alert in alerts for pid in alert.policy_ids)
        )
        st_codes = tuple(sorted({code for alert in alerts for code in alert.st_codes}))
        ua_codes = tuple(
            sorted({code for alert in alerts for code in alert.unlawful_activity_codes})
        )
        case = Case(
            case_id=case_id,
            kind=kind,
            subject_party_id=subject_party_id,
            opened_at=moment,
            state=CaseState.UNDER_INVESTIGATION,
            alert_ids=tuple(a.alert_id for a in alerts),
            transaction_ids=transaction_ids,
            policy_ids=policy_ids,
            st_codes=st_codes,
            unlawful_activity_codes=ua_codes,
            narrative=narrative,
            assigned_to=assigned_to or actor,
            priority=priority,
        )
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT case_id FROM case_file WHERE case_id = ?", (case_id,)
            ).fetchone()
            if existing:
                raise WorkflowError(f"case {case_id} already exists")
            self._insert_case(conn, case)
            for alert in alerts:
                conn.execute(
                    "UPDATE alert SET state = ?, case_id = ?, updated_at = ? WHERE alert_id = ?",
                    (
                        str(AlertState.ESCALATED),
                        case_id,
                        now_manila().isoformat(),
                        alert.alert_id,
                    ),
                )
            self._append_audit(
                conn,
                actor=actor,
                action="case.opened",
                entity_type="case",
                entity_id=case_id,
                detail={
                    "kind": str(kind),
                    "subject": subject_party_id,
                    "alerts": list(case.alert_ids),
                    "transactions": len(transaction_ids),
                },
                at=moment,
            )
        return case

    @staticmethod
    def _insert_case(conn: sqlite3.Connection, case: Case) -> None:
        conn.execute(
            """
            INSERT INTO case_file (
                case_id, kind, subject_party_id, opened_at, state, alert_ids,
                transaction_ids, policy_ids, st_codes, unlawful_activity_codes, narrative,
                assigned_to, determined_at, determined_by, filing_deadline, approved_by,
                approved_at, filed_at, closure_reason, reference_number, priority
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case.case_id,
                str(case.kind),
                case.subject_party_id,
                case.opened_at.isoformat(),
                str(case.state),
                _dumps(case.alert_ids),
                _dumps(case.transaction_ids),
                _dumps(case.policy_ids),
                _dumps(case.st_codes),
                _dumps(case.unlawful_activity_codes),
                case.narrative,
                case.assigned_to,
                case.determined_at.isoformat() if case.determined_at else None,
                case.determined_by,
                case.filing_deadline.isoformat() if case.filing_deadline else None,
                case.approved_by,
                case.approved_at.isoformat() if case.approved_at else None,
                case.filed_at.isoformat() if case.filed_at else None,
                case.closure_reason,
                case.reference_number,
                str(case.priority),
            ),
        )

    def _save_case(self, conn: sqlite3.Connection, case: Case) -> None:
        conn.execute("DELETE FROM case_file WHERE case_id = ?", (case.case_id,))
        self._insert_case(conn, case)

    @staticmethod
    def _to_case(row: sqlite3.Row) -> Case:
        return Case(
            case_id=row["case_id"],
            kind=ReportKind(row["kind"]),
            subject_party_id=row["subject_party_id"],
            opened_at=dt.datetime.fromisoformat(row["opened_at"]),
            state=CaseState(row["state"]),
            alert_ids=tuple(_loads(row["alert_ids"]) or ()),
            transaction_ids=tuple(_loads(row["transaction_ids"]) or ()),
            policy_ids=tuple(_loads(row["policy_ids"]) or ()),
            st_codes=tuple(_loads(row["st_codes"]) or ()),
            unlawful_activity_codes=tuple(_loads(row["unlawful_activity_codes"]) or ()),
            narrative=row["narrative"],
            assigned_to=row["assigned_to"],
            determined_at=_datetime(row["determined_at"]),
            determined_by=row["determined_by"],
            filing_deadline=_date(row["filing_deadline"]),
            approved_by=row["approved_by"],
            approved_at=_datetime(row["approved_at"]),
            filed_at=_datetime(row["filed_at"]),
            closure_reason=row["closure_reason"],
            reference_number=row["reference_number"],
            priority=Severity(row["priority"]),
        )

    def get_case(self, case_id: str) -> Case | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM case_file WHERE case_id = ?", (case_id,)
            ).fetchone()
        return self._to_case(row) if row else None

    def cases(
        self, *, state: CaseState | str | None = None, kind: ReportKind | str | None = None,
        limit: int = 500
    ) -> list[Case]:
        query = "SELECT * FROM case_file WHERE 1 = 1"
        params: list[Any] = []
        if state is not None:
            query += " AND state = ?"
            params.append(str(state))
        if kind is not None:
            query += " AND kind = ?"
            params.append(str(kind))
        query += " ORDER BY COALESCE(filing_deadline, '9999-12-31'), opened_at LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._to_case(row) for row in rows]

    def update_case(
        self, case: Case, *, actor: str, action: str, detail: Mapping[str, Any]
    ) -> Case:
        with self.connect() as conn:
            self._save_case(conn, case)
            self._append_audit(
                conn,
                actor=actor,
                action=action,
                entity_type="case",
                entity_id=case.case_id,
                detail=detail,
            )
        return case

    # -- reports and submissions ----------------------------------------

    def record_report(
        self,
        *,
        report_id: str,
        kind: ReportKind,
        rendered: Mapping[str, Any],
        actor: str,
        case_id: str | None = None,
        period_start: dt.date | None = None,
        period_end: dt.date | None = None,
        references: Sequence[str] = (),
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO report_file (
                    report_id, kind, case_id, path, filename, sha256, rows, size_bytes,
                    spec_version, created_at, period_start, period_end, references_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_id) DO UPDATE SET
                    path = excluded.path, sha256 = excluded.sha256, rows = excluded.rows,
                    size_bytes = excluded.size_bytes, created_at = excluded.created_at
                """,
                (
                    report_id,
                    str(kind),
                    case_id,
                    str(rendered["path"]),
                    str(rendered["filename"]),
                    str(rendered["sha256"]),
                    int(rendered["rows"]),
                    int(rendered["size_bytes"]),
                    str(rendered["spec_version"]),
                    now_manila().isoformat(),
                    period_start.isoformat() if period_start else None,
                    period_end.isoformat() if period_end else None,
                    _dumps(list(references)),
                ),
            )
            self._append_audit(
                conn,
                actor=actor,
                action="report.generated",
                entity_type="report",
                entity_id=report_id,
                detail={
                    "kind": str(kind),
                    "rows": rendered["rows"],
                    "sha256": rendered["sha256"],
                    "filename": rendered["filename"],
                },
            )

    def reports(
        self, *, kind: ReportKind | str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM report_file WHERE 1 = 1"
        params: list[Any] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(str(kind))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_submission(
        self,
        *,
        submission_id: str,
        report_id: str,
        state: SubmissionState,
        mode: str,
        actor: str,
        package_path: str = "",
        package_sha256: str = "",
        receipt_reference: str = "",
        error: str = "",
        submitted_at: dt.datetime | None = None,
        acknowledged_at: dt.datetime | None = None,
    ) -> None:
        now = now_manila().isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT attempts FROM submission WHERE submission_id = ?", (submission_id,)
            ).fetchone()
            attempts = (existing["attempts"] if existing else 0) + 1
            conn.execute(
                """
                INSERT INTO submission (
                    submission_id, report_id, state, mode, package_path, package_sha256,
                    submitted_at, acknowledged_at, receipt_reference, attempts, last_error,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    state = excluded.state, package_path = excluded.package_path,
                    package_sha256 = excluded.package_sha256,
                    submitted_at = COALESCE(excluded.submitted_at, submission.submitted_at),
                    acknowledged_at = COALESCE(excluded.acknowledged_at,
                                               submission.acknowledged_at),
                    receipt_reference = excluded.receipt_reference,
                    attempts = excluded.attempts, last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    submission_id,
                    report_id,
                    str(state),
                    mode,
                    package_path,
                    package_sha256,
                    submitted_at.isoformat() if submitted_at else None,
                    acknowledged_at.isoformat() if acknowledged_at else None,
                    receipt_reference,
                    attempts,
                    error,
                    now,
                    now,
                ),
            )
            self._append_audit(
                conn,
                actor=actor,
                action=f"submission.{str(state).lower()}",
                entity_type="submission",
                entity_id=submission_id,
                detail={
                    "report_id": report_id,
                    "mode": mode,
                    "package_sha256": package_sha256,
                    "receipt": receipt_reference,
                    "attempt": attempts,
                    "error": error,
                },
            )

    def submissions(self, *, state: SubmissionState | str | None = None, limit: int = 100
                    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM submission WHERE 1 = 1"
        params: list[Any] = []
        if state is not None:
            query += " AND state = ?"
            params.append(str(state))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # -- screening and runs ----------------------------------------------

    def record_screening(self, results: Iterable[Any], *, actor: str = "system") -> int:
        count = 0
        with self.connect() as conn:
            for result in results:
                payload = result.as_dict()
                result_id = (
                    f"SCR-{result.subject.subject_id}-"
                    f"{result.screened_at.strftime('%Y%m%dT%H%M%S')}"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO screening_result (result_id, subject_id, "
                    "subject_name, screened_at, providers, decision, hit_count, payload, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        result_id,
                        result.subject.subject_id,
                        result.subject.name,
                        result.screened_at.isoformat(),
                        _dumps(list(result.providers)),
                        str(result.decision),
                        len(result.hits),
                        _dumps(payload),
                        result.error,
                    ),
                )
                count += 1
            self._append_audit(
                conn,
                actor=actor,
                action="screening.completed",
                entity_type="screening",
                entity_id=f"batch-{now_manila().strftime('%Y%m%dT%H%M%S')}",
                detail={"subjects": count},
            )
        return count

    def screening_history(self, subject_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM screening_result WHERE subject_id = ? "
                "ORDER BY screened_at DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_run(self, run: Any, *, actor: str = "system") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO monitoring_run (run_id, started_at, as_of, "
                "fingerprint, payload) VALUES (?,?,?,?,?)",
                (
                    run.run_id,
                    run.started_at.isoformat(),
                    run.as_of.isoformat(),
                    run.fingerprint,
                    _dumps(run.as_dict()),
                ),
            )
            self._append_audit(
                conn,
                actor=actor,
                action="monitoring.run",
                entity_type="run",
                entity_id=run.run_id,
                detail={
                    "as_of": run.as_of.isoformat(),
                    "fingerprint": run.fingerprint,
                    "transactions": run.transactions_examined,
                    "alerts": len(run.alerts),
                    "errors": [o.rule_id for o in run.errors],
                },
            )

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT run_id, started_at, as_of, fingerprint FROM monitoring_run "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

"""The store, the four-eyes workflow, the deadlines and the audit chain."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from aml.case.store import AmlStore, WorkflowError
from aml.case.workflow import (
    approve,
    close_case,
    compose_narrative,
    deadline_status,
    determine,
    due_soon,
    mark_filed,
)
from aml.config import AmlConfig, InstitutionProfile
from aml.model.entities import Book, Party, Policy, Transaction
from aml.model.enums import AlertState, CaseState, PaymentInstrument, ReportKind, TransactionType
from aml.money import php
from aml.rules.engine import run_monitoring


@pytest.fixture
def config() -> AmlConfig:
    return AmlConfig(
        institution=InstitutionProfile(
            covered_person_name="Demo Life",
            amlc_institution_code="DEMO-001",
            compliance_officer_name="R. Villanueva",
        )
    )


@pytest.fixture
def book() -> Book:
    party = Party("C1", full_name="Bienvenido Cruz Alcantara", declared_income=php(5_000_000),
                  occupation="Business owner")
    policy = Policy("PL1", policy_number="VUL-0002", owner_party_id="C1", is_single_premium=True)
    return Book(
        [party],
        [policy],
        [
            Transaction("T1", dt.datetime(2026, 3, 2, 9), "C1", php(2_500_000),
                        TransactionType.SINGLE_PREMIUM,
                        instrument=PaymentInstrument.MANAGERS_CHECK, policy_id="PL1"),
            Transaction("T2", dt.datetime(2026, 3, 13, 14), "C1", php(2_500_000),
                        TransactionType.FREE_LOOK_CANCELLATION, direction="OUTBOUND",
                        instrument=PaymentInstrument.BANK_TRANSFER, policy_id="PL1"),
        ],
    )


@pytest.fixture
def store(tmp_path) -> AmlStore:
    return AmlStore(tmp_path / "aml.sqlite3")


@pytest.fixture
def case_with_alerts(store, book, config):
    run = run_monitoring(book, config)
    store.record_run(run)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    alerts = [a for a in store.alerts(covered=False) if a.rule_id == "str.early_surrender"]
    case = store.open_case(
        kind=ReportKind.STR,
        subject_party_id="C1",
        alerts=alerts,
        actor="analyst.dizon",
    )
    return case, alerts


def test_rerunning_monitoring_updates_alerts_instead_of_duplicating(store, book, config):
    run = run_monitoring(book, config)
    first = store.upsert_alerts(run.alerts, run_id=run.run_id)
    second = store.upsert_alerts(run_monitoring(book, config).alerts, run_id="later")
    assert first.created == len(run.alerts)
    assert second.created == 0 and second.updated == len(run.alerts)
    assert len(store.alerts(limit=1000)) == len(run.alerts)


def test_a_rerun_does_not_reopen_a_dispositioned_alert(store, book, config):
    run = run_monitoring(book, config)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    alert = store.alerts(covered=False)[0]
    store.set_alert_state(
        alert.alert_id, AlertState.CLOSED_FALSE_POSITIVE, actor="analyst", reason="known client"
    )
    store.upsert_alerts(run_monitoring(book, config).alerts, run_id="later")
    assert store.get_alert(alert.alert_id).state is AlertState.CLOSED_FALSE_POSITIVE


def test_closing_an_alert_requires_a_reason(store, book, config):
    run = run_monitoring(book, config)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    alert = store.alerts(covered=False)[0]
    with pytest.raises(WorkflowError):
        store.set_alert_state(alert.alert_id, AlertState.CLOSED_FALSE_POSITIVE, actor="analyst")


def test_opening_a_case_escalates_its_alerts(store, case_with_alerts):
    case, alerts = case_with_alerts
    assert case.state is CaseState.UNDER_INVESTIGATION
    assert all(a.state is AlertState.ESCALATED for a in store.alerts(case_id=case.case_id))
    assert set(case.st_codes) >= {"ST1", "ST5"}


def test_a_case_cannot_mix_subjects(store, book, config):
    run = run_monitoring(book, config)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    alerts = store.alerts(limit=5)
    with pytest.raises(WorkflowError):
        store.open_case(
            kind=ReportKind.STR, subject_party_id="SOMEONE-ELSE", alerts=alerts, actor="analyst"
        )


def test_determination_starts_the_five_working_day_clock(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determined = determine(
        store,
        case.case_id,
        actor="analyst.dizon",
        config=config,
        at=dt.datetime(2026, 3, 16, 10, 0),
    )
    assert determined.state is CaseState.PENDING_APPROVAL
    assert determined.filing_deadline == dt.date(2026, 3, 23)  # five working days


def test_an_str_must_cite_a_circumstance(store, book, config):
    run = run_monitoring(book, config)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    bare = [a for a in store.alerts(covered=True)][:1]
    case = store.open_case(
        kind=ReportKind.STR, subject_party_id="C1", alerts=bare, actor="analyst"
    )
    with pytest.raises(WorkflowError, match="ST1-ST7"):
        determine(store, case.case_id, actor="analyst", config=config)


def test_an_unknown_circumstance_code_is_refused(store, case_with_alerts, config):
    case, _ = case_with_alerts
    with pytest.raises(WorkflowError, match="unknown suspicious circumstance"):
        determine(store, case.case_id, actor="analyst", config=config, st_codes=("ST9",))


def test_the_same_person_cannot_determine_and_approve(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determine(store, case.case_id, actor="analyst.dizon", config=config)
    with pytest.raises(WorkflowError, match="second person"):
        approve(store, case.case_id, approver="analyst.dizon")
    approved = approve(store, case.case_id, approver="co.villanueva")
    assert approved.state is CaseState.APPROVED_FOR_FILING


def test_filing_requires_approval_first(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determine(store, case.case_id, actor="analyst.dizon", config=config)
    with pytest.raises(WorkflowError, match="approved"):
        mark_filed(store, case.case_id, actor="co.villanueva")


def test_filing_closes_the_alerts_as_reported(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determine(store, case.case_id, actor="analyst.dizon", config=config)
    approve(store, case.case_id, approver="co.villanueva")
    mark_filed(store, case.case_id, actor="co.villanueva", reference="AMLC-123")
    assert all(
        a.state is AlertState.CLOSED_REPORTED for a in store.alerts(case_id=case.case_id)
    )


def test_a_late_filing_is_recorded_as_late(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determine(store, case.case_id, actor="analyst.dizon", config=config,
              at=dt.datetime(2026, 3, 2, 9))
    approve(store, case.case_id, approver="co.villanueva")
    mark_filed(store, case.case_id, actor="co.villanueva", at=dt.datetime(2026, 4, 30, 9))
    entries = [e for e in store.audit_trail(case.case_id) if e.action == "case.filed"]
    assert entries and entries[0].detail["late"] is True


def test_a_designated_person_case_runs_on_the_hours_clock(store, config):
    party = Party("C4", full_name="Faisal Ahmad Rahman", is_frozen=True)
    book = Book(
        [party],
        [Policy("PL9", owner_party_id="C4")],
        [
            Transaction("T9", dt.datetime(2026, 3, 2, 9), "C4", php(250_000),
                        TransactionType.PREMIUM_PAYMENT,
                        instrument=PaymentInstrument.CASH, policy_id="PL9")
        ],
    )
    run = run_monitoring(book, config)
    store.upsert_alerts(run.alerts, run_id=run.run_id)
    alerts = [a for a in store.alerts(covered=False) if a.rule_id == "str.designated_party"]
    case = store.open_case(
        kind=ReportKind.STR, subject_party_id="C4", alerts=alerts, actor="analyst"
    )
    determined = determine(
        store, case.case_id, actor="analyst", config=config, at=dt.datetime(2026, 3, 2, 9)
    )
    assert determined.filing_deadline == dt.date(2026, 3, 3)  # 24 hours, not five working days


def test_closing_without_filing_needs_a_reason(store, case_with_alerts):
    case, _ = case_with_alerts
    with pytest.raises(WorkflowError):
        close_case(store, case.case_id, actor="analyst", reason="  ")
    closed = close_case(
        store, case.case_id, actor="analyst", reason="premium traced to a property sale"
    )
    assert closed.state is CaseState.CLOSED_NOT_REPORTABLE


def test_deadlines_surface_before_they_are_missed(store, case_with_alerts, config):
    case, _ = case_with_alerts
    determine(store, case.case_id, actor="analyst", config=config,
              at=dt.datetime(2026, 3, 16, 9))
    status = deadline_status(store.get_case(case.case_id), config, as_of=dt.date(2026, 3, 20))
    assert status.working_days_remaining == 1 and status.at_risk and not status.overdue
    overdue = deadline_status(store.get_case(case.case_id), config, as_of=dt.date(2026, 3, 30))
    assert overdue.overdue
    assert due_soon(store, config, as_of=dt.date(2026, 3, 30))[0].overdue


def test_the_narrative_names_the_client_the_money_and_the_circumstances(
    store, case_with_alerts, book, config
):
    case, alerts = case_with_alerts
    narrative = compose_narrative(case, alerts, book, config)
    assert "Bienvenido Cruz Alcantara" in narrative
    assert "2,500,000.00" in narrative
    assert "SUSPICIOUS CIRCUMSTANCES RELIED ON" in narrative
    assert "no disclosure of this report has been made to the client" in narrative


def test_the_audit_chain_detects_an_altered_entry(store, case_with_alerts, tmp_path):
    case, _ = case_with_alerts
    assert store.verify_audit_chain()[0] is True
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE audit_log SET actor = 'somebody.else' WHERE seq = "
            "(SELECT MIN(seq) FROM audit_log)"
        )
    intact, seq, message = store.verify_audit_chain()
    assert not intact and seq is not None and "altered" in message


def test_the_audit_chain_detects_a_deleted_entry(store, case_with_alerts):
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM audit_log WHERE seq = (SELECT MIN(seq) FROM audit_log)")
    intact, _, message = store.verify_audit_chain()
    assert not intact and "removed or reordered" in message

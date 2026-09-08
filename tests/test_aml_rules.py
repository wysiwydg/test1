"""Does the rule set find what is actually there?

Run against the synthetic book, which has one instance of each typology planted
in a known place. ``PLANTED`` in ``aml.demo.generate`` is the answer key.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from aml.config import AmlConfig, Thresholds
from aml.demo.generate import PLANTED, generate
from aml.ingest.loader import load_book
from aml.model.entities import Book, Party, Policy, Transaction
from aml.model.enums import PaymentInstrument, TransactionType
from aml.money import php
from aml.rules.engine import run_monitoring
from aml.rules.registry import all_rules, rule_catalogue


@pytest.fixture(scope="module")
def demo_book(tmp_path_factory) -> Book:
    paths = generate(tmp_path_factory.mktemp("demo"))
    book, report = load_book(
        parties_path=paths["parties"],
        policies_path=paths["policies"],
        transactions_path=paths["transactions"],
        rates_path=paths["rates"],
    )
    assert report.ok, [str(i) for i in report.errors]
    return book


@pytest.fixture(scope="module")
def demo_run(demo_book):
    return run_monitoring(demo_book, AmlConfig())


@pytest.mark.parametrize(
    ("client", "rule_id"),
    [
        ("C-0001", "str.structuring"),
        ("C-0002", "str.early_surrender"),
        ("C-0003", "str.third_party_payer"),
        ("C-0004", "str.designated_party"),
        ("C-0005", "str.high_risk_jurisdiction"),
        ("C-0006", "str.overpayment_refund"),
        ("C-0007", "str.rapid_movement"),
        ("C-0008", "str.income_capacity"),
        ("C-0009", "str.beneficiary_churn"),
        ("C-0010", "ctr.single_transaction"),
    ],
)
def test_every_planted_typology_is_found(demo_run, client, rule_id):
    fired = {(a.subject_party_id, a.rule_id) for a in demo_run.alerts}
    assert (client, rule_id) in fired, PLANTED.get(client)


def test_no_rule_raised(demo_run):
    assert not demo_run.errors, [o.error for o in demo_run.errors]


def test_the_foreign_currency_premium_is_covered_once_converted(demo_run, demo_book):
    covered = [
        a
        for a in demo_run.covered_transaction_alerts
        if a.subject_party_id == "C-0010"
        and any(
            (t := demo_book.transaction(i)) is not None and t.amount.currency == "USD"
            for i in a.transaction_ids
        )
    ]
    assert covered, "a USD 20,000 premium is over the peso threshold and must be reported"


def test_runs_are_reproducible(demo_book):
    first = run_monitoring(demo_book, AmlConfig())
    second = run_monitoring(demo_book, AmlConfig())
    assert [a.alert_id for a in first.alerts] == [a.alert_id for a in second.alerts]
    assert first.fingerprint == second.fingerprint


def test_changing_a_threshold_changes_the_fingerprint(demo_book):
    baseline = run_monitoring(demo_book, AmlConfig())
    lowered = run_monitoring(
        demo_book,
        AmlConfig(thresholds=Thresholds(covered_transaction=php(100_000))),
    )
    assert baseline.fingerprint != lowered.fingerprint
    assert len(lowered.covered_transaction_alerts) > len(baseline.covered_transaction_alerts)


def test_ordinary_premium_payers_do_not_generate_alerts(demo_run):
    flagged = {a.subject_party_id for a in demo_run.suspicion_alerts}
    ordinary = {f"C-{i:04d}" for i in range(11, 61)}
    assert not (flagged & ordinary), f"false positives on ordinary clients: {flagged & ordinary}"


def test_a_broken_rule_does_not_stop_the_run(demo_book, monkeypatch):
    broken = all_rules()[0]

    def explode(ctx):
        raise RuntimeError("bad rule")

    monkeypatch.setattr(broken, "evaluate", explode, raising=False)
    run = run_monitoring(demo_book, AmlConfig())
    assert run.errors and run.errors[0].rule_id == broken.rule_id
    assert run.alerts, "the other rules must still have produced alerts"


def test_same_day_aggregation_does_not_double_report():
    party = Party("C1", full_name="Test Client")
    policy = Policy("PL1", policy_number="P-1", owner_party_id="C1")
    transactions = [
        Transaction(
            f"T{i}",
            dt.datetime(2026, 3, 2, 9 + i),
            "C1",
            php(300_000),
            TransactionType.PREMIUM_PAYMENT,
            instrument=PaymentInstrument.CASH,
            policy_id="PL1",
        )
        for i in range(2)
    ]
    run = run_monitoring(Book([party], [policy], transactions), AmlConfig())
    covered = run.covered_transaction_alerts
    assert len(covered) == 1 and covered[0].rule_id == "ctr.same_day_aggregate"

    # With one transaction over the threshold, the day is not reported twice.
    big = replace(transactions[0], amount=php(600_000), amount_php=php(600_000))
    run2 = run_monitoring(Book([party], [policy], [big, transactions[1]]), AmlConfig())
    assert [a.rule_id for a in run2.covered_transaction_alerts] == ["ctr.single_transaction"]


def test_structuring_needs_the_band_not_just_the_count():
    party = Party("C1", full_name="Modest Payer")
    policy = Policy("PL1", owner_party_id="C1")
    small = [
        Transaction(
            f"T{i}",
            dt.datetime(2026, 3, 2 + i, 10),
            "C1",
            php(20_000),
            TransactionType.PREMIUM_PAYMENT,
            instrument=PaymentInstrument.CASH,
            policy_id="PL1",
        )
        for i in range(5)
    ]
    run = run_monitoring(Book([party], [policy], small), AmlConfig())
    assert not [a for a in run.alerts if a.rule_id == "str.structuring"]


def test_every_suspicion_rule_declares_a_statutory_circumstance():
    for entry in rule_catalogue():
        if entry["rule_id"].startswith("str."):
            assert entry["st_codes"], f"{entry['rule_id']} evidences no ST code"


def test_alerts_carry_a_usable_narrative(demo_run):
    for alert in demo_run.alerts:
        assert len(alert.narrative) > 80
        assert alert.subject_party_id in alert.narrative or alert.narrative[0].isupper()

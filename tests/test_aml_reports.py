"""Report building, the schema projection, and the validator that stands
between a compliance officer and a rejected submission."""

from __future__ import annotations

import datetime as dt

import pytest

from aml.config import AmlConfig, InstitutionProfile
from aml.model.entities import Book, Counterparty, IdDocument, Party, Policy, Transaction
from aml.model.enums import (
    IdDocumentType,
    PaymentInstrument,
    ProductLine,
    ReportKind,
    TransactionType,
)
from aml.money import Money, RateTable, php
from aml.report.build import ctr_records, split_name
from aml.report.render import render, render_csv
from aml.report.spec import load_spec
from aml.report.validate import blocking, validate_records
from aml.rules.engine import run_monitoring


@pytest.fixture
def config() -> AmlConfig:
    return AmlConfig(
        institution=InstitutionProfile(
            covered_person_name="Demonstration Life Assurance Company, Inc.",
            amlc_institution_code="DEMO-INS-0001",
            compliance_officer_name="R. Villanueva",
        )
    )


@pytest.fixture
def book() -> Book:
    party = Party(
        "C1",
        full_name="Juan Santos Dela Cruz Jr.",
        birth_date=dt.date(1975, 4, 12),
        address_line="12 Ayala Avenue",
        city="Makati",
        province="Metro Manila",
        occupation="Property developer",
        identifications=(IdDocument(IdDocumentType.PHILSYS, "1234-5678-9012"),),
    )
    policy = Policy(
        "PL1",
        policy_number="VUL-0001",
        product_line=ProductLine.VARIABLE_UNIT_LINKED,
        owner_party_id="C1",
    )
    transactions = [
        Transaction(
            "T1",
            dt.datetime(2026, 3, 2, 10, 0),
            "C1",
            php(600_000),
            TransactionType.SINGLE_PREMIUM,
            instrument=PaymentInstrument.CASH,
            policy_id="PL1",
            branch_code="MNL-01",
            source_reference="OR-88213",
            counterparty=Counterparty(name="Metro Holdings Inc", country="SG"),
        ),
        Transaction(
            "T2",
            dt.datetime(2026, 3, 5, 9, 0),
            "C1",
            php(300_000),
            TransactionType.TOP_UP,
            instrument=PaymentInstrument.CASH,
            policy_id="PL1",
        ),
        Transaction(
            "T3",
            dt.datetime(2026, 3, 5, 15, 0),
            "C1",
            php(280_000),
            TransactionType.TOP_UP,
            instrument=PaymentInstrument.CASH,
            policy_id="PL1",
        ),
    ]
    return Book([party], [policy], transactions)


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("Juan Santos Dela Cruz Jr.", ("Juan", "Santos", "Dela Cruz", "Jr.")),
        ("Maria Reyes", ("Maria", "", "Reyes", "")),
        ("Jose Protacio Rizal", ("Jose", "Protacio", "Rizal", "")),
        ("Ana Del Rosario", ("Ana", "", "Del Rosario", "")),
    ],
)
def test_names_split_with_the_particle_attached_to_the_surname(full_name, expected):
    assert split_name(full_name) == expected


def test_a_transaction_found_by_two_rules_is_reported_once(book, config):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    references = [r["transaction_reference"] for r in records]
    assert len(references) == len(set(references)) == 3


def test_references_are_stable_across_regeneration(book, config):
    run = run_monitoring(book, config)
    first = ctr_records(run.covered_transaction_alerts, book, config)
    second = ctr_records(run.covered_transaction_alerts, book, config)
    assert [r["report_reference"] for r in first] == [r["report_reference"] for r in second]


def test_the_ctr_carries_the_deadline_and_the_basis(book, config):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    single = next(r for r in records if r["transaction_reference"] == "OR-88213")
    assert single["filing_deadline"] == dt.date(2026, 3, 9)
    assert single["detection_basis"] == "single transaction"
    aggregated = [r for r in records if r["detection_basis"] == "same-day aggregation"]
    assert len(aggregated) == 2


def test_a_valid_batch_has_no_blocking_issues(book, config):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    assert not blocking(validate_records(records, load_spec(ReportKind.CTR)))


def test_a_missing_institution_code_blocks_the_file(book):
    bare = AmlConfig()
    run = run_monitoring(book, bare)
    records = ctr_records(run.covered_transaction_alerts, book, bare)
    issues = blocking(validate_records(records, load_spec(ReportKind.CTR)))
    assert {i.column for i in issues} >= {"INSTITUTION_CODE", "INSTITUTION_NAME",
                                          "COMPLIANCE_OFFICER"}


def test_an_over_long_value_is_caught_before_the_regulator_sees_it(book, config):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    records[0]["last_name"] = "X" * 100
    issues = validate_records(records, load_spec(ReportKind.CTR))
    assert any(i.code == "too_long" and i.column == "LAST_NAME" for i in issues)


def test_duplicate_references_are_caught(book, config):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    records[1]["report_reference"] = records[0]["report_reference"]
    issues = validate_records(records, load_spec(ReportKind.CTR))
    assert any(i.code == "duplicate_reference" for i in issues)


def test_rendering_is_deterministic(book, config, tmp_path):
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    spec = load_spec(ReportKind.CTR)
    first = render(records, spec, tmp_path / "a.csv")
    second = render(records, spec, tmp_path / "b.csv")
    assert first.sha256 == second.sha256
    assert first.rows == 3


def test_the_layout_decides_the_columns_not_the_code(book, config, tmp_path):
    """A schema change is a configuration edit — that is the whole point."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "ctr_v2.toml").write_text(
        """
[report]
kind = "CTR"
version = "2"
name = "Covered Transaction Report"

[[field]]
column = "PANSAMANTALANG_SANGGUNIAN"
source = "report_reference"
required = true

[[field]]
column = "HALAGA"
source = "amount_php"
type = "decimal"
format = "2"
required = true
""",
        encoding="utf-8",
    )
    spec = load_spec("ctr", "v2", spec_dir)
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    payload = render_csv(records, spec).decode("utf-8")
    assert payload.splitlines()[0] == "PANSAMANTALANG_SANGGUNIAN,HALAGA"
    assert payload.splitlines()[1].endswith("600000.00")


def test_a_foreign_currency_transaction_reports_both_amounts(config):
    rates = RateTable()
    rates.add("USD", dt.date(2026, 3, 2), "56.42")
    conversion = rates.convert(Money.parse("USD 20000"), dt.date(2026, 3, 2))
    party = Party("C1", full_name="Isabelo Ramos Fernandez")
    policy = Policy("PL1", policy_number="USD-1", owner_party_id="C1")
    txn = Transaction(
        "T1",
        dt.datetime(2026, 3, 2, 11, 0),
        "C1",
        conversion.original,
        TransactionType.SINGLE_PREMIUM,
        instrument=PaymentInstrument.SWIFT_TRANSFER,
        policy_id="PL1",
        amount_php=conversion.converted,
        fx=conversion,
    )
    book = Book([party], [policy], [txn])
    run = run_monitoring(book, config)
    records = ctr_records(run.covered_transaction_alerts, book, config)
    assert len(records) == 1
    assert records[0]["currency"] == "USD"
    assert records[0]["amount"].amount == 20000
    assert records[0]["amount_php"] == php("1128400.00")
    assert records[0]["exchange_rate"] == "56.42"

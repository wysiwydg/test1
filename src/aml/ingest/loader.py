"""Loading an extract, and refusing to guess.

Ingestion is where a monitoring system quietly goes wrong. A row with an
unparseable amount, a foreign-currency transaction with no rate, a policy
number that matches nothing — each is easy to skip, and each skipped row is a
transaction that will never be monitored and never be reported. So the loader
reports every problem it finds as an :class:`Issue` against the row that caused
it, and the caller decides whether to proceed.

Two rules it does not bend:

*   **A transaction with no peso equivalent is an error, not a warning.** The
    threshold test is in pesos; a row that cannot be converted cannot be tested,
    and treating a dollar figure as pesos understates it by a factor of about
    fifty-six.
*   **An unmapped code is kept, not dropped.** An instrument this package does
    not recognise becomes ``OTHER`` and the raw value is preserved on the
    transaction, so it shows up as a data-quality finding instead of vanishing.

Column names are the canonical ones documented in the module, with common
aliases accepted. An institution whose extract differs writes a small mapping
rather than reshaping its files: see :func:`load_transactions`.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import InvalidOperation
from typing import Any, TypeVar

from aml.model.entities import Book, Counterparty, IdDocument, Party, Policy, Transaction
from aml.model.enums import (
    Channel,
    Direction,
    IdDocumentType,
    PartyType,
    PaymentInstrument,
    PepStatus,
    ProductLine,
    RiskRating,
    TransactionType,
)
from aml.money import PHP, MissingRate, Money, RateTable
from aml.phcalendar import MANILA

__all__ = ["Issue", "LoadReport", "load_parties", "load_policies", "load_transactions",
           "load_book"]

E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class Issue:
    source: str
    row: int
    field: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.source} row {self.row} {self.field}: {self.message}"


@dataclass
class LoadReport:
    """What was loaded and what could not be."""

    parties: int = 0
    policies: int = 0
    transactions: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "parties": self.parties,
            "policies": self.policies,
            "transactions": self.transactions,
            "issues": [str(i) for i in self.issues],
            "errors": len(self.errors),
        }


def _rows(path: str | pathlib.Path) -> Iterable[tuple[int, dict[str, str]]]:
    """Read CSV or JSON lines into lower-cased dictionaries."""
    file = pathlib.Path(path)
    if file.suffix.lower() in (".json", ".jsonl", ".ndjson"):
        text = file.read_text(encoding="utf-8").strip()
        records = (
            json.loads(text)
            if text.startswith("[")
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        for index, record in enumerate(records, start=1):
            yield index, {str(k).strip().lower(): v for k, v in dict(record).items()}
        return
    with open(file, newline="", encoding="utf-8-sig") as handle:
        for index, record in enumerate(csv.DictReader(handle), start=1):
            yield index, {
                (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in record.items()
            }


def _get(row: Mapping[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return default


def _enum(kind: type[E], value: str, default: E) -> E:
    if not value:
        return default
    try:
        return kind(value.strip().upper().replace(" ", "_").replace("-", "_"))  # type: ignore[call-arg]
    except ValueError:
        return default


def _date(value: str) -> dt.date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        day = _date(value)
        if day is None:
            return None
        parsed = dt.datetime.combine(day, dt.time(0, 0))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MANILA)


def load_parties(path: str | pathlib.Path, report: LoadReport | None = None) -> list[Party]:
    """Load customer records.

    Canonical columns: ``party_id``, ``party_type``, ``full_name`` (or
    ``first_name``/``middle_name``/``last_name``), ``birth_date``,
    ``nationality``, ``address_line``, ``city``, ``occupation``,
    ``declared_income``, ``pep_status``, ``risk_rating``, ``id_type``,
    ``id_number``, ``is_frozen``, ``mdm_person_id``.
    """
    log = report or LoadReport()
    parties: list[Party] = []
    for index, row in _rows(path):
        party_id = _get(row, "party_id", "customer_id", "client_id")
        if not party_id:
            log.issues.append(Issue(str(path), index, "party_id", "missing"))
            continue
        income_text = _get(row, "declared_income", "annual_income")
        income: Money | None = None
        if income_text:
            try:
                income = Money.parse(income_text, PHP)
            except (InvalidOperation, ValueError):
                log.issues.append(
                    Issue(str(path), index, "declared_income", f"unreadable: {income_text!r}",
                          "warning")
                )
        documents: list[IdDocument] = []
        if _get(row, "id_number"):
            documents.append(
                IdDocument(
                    doc_type=_enum(IdDocumentType, _get(row, "id_type"), IdDocumentType.OTHER),
                    number=_get(row, "id_number"),
                    issuing_country=_get(row, "id_country", default="PH"),
                    expiry_date=_date(_get(row, "id_expiry")),
                )
            )
        parties.append(
            Party(
                party_id=party_id,
                party_type=_enum(PartyType, _get(row, "party_type"), PartyType.INDIVIDUAL),
                full_name=_get(row, "full_name", "name"),
                first_name=_get(row, "first_name"),
                middle_name=_get(row, "middle_name"),
                last_name=_get(row, "last_name"),
                suffix=_get(row, "suffix"),
                aliases=tuple(a for a in _get(row, "aliases").split(";") if a),
                birth_date=_date(_get(row, "birth_date", "date_of_birth")),
                birth_place=_get(row, "birth_place"),
                gender=_get(row, "gender"),
                nationality=_get(row, "nationality", default="PH"),
                country_of_residence=_get(row, "country_of_residence", "country", default="PH"),
                address_line=_get(row, "address_line", "address"),
                city=_get(row, "city"),
                province=_get(row, "province"),
                postal_code=_get(row, "postal_code"),
                phone=_get(row, "phone"),
                email=_get(row, "email"),
                identifications=tuple(documents),
                occupation=_get(row, "occupation"),
                employer=_get(row, "employer"),
                nature_of_business=_get(row, "nature_of_business"),
                source_of_funds=_get(row, "source_of_funds"),
                declared_income=income,
                pep_status=_enum(PepStatus, _get(row, "pep_status"), PepStatus.UNKNOWN),
                risk_rating=_enum(RiskRating, _get(row, "risk_rating"), RiskRating.UNRATED),
                customer_since=_date(_get(row, "customer_since")),
                is_frozen=_get(row, "is_frozen").lower() in ("1", "true", "yes", "y"),
                mdm_person_id=_get(row, "mdm_person_id") or None,
                source_system=_get(row, "source_system"),
            )
        )
    log.parties = len(parties)
    return parties


def load_policies(path: str | pathlib.Path, report: LoadReport | None = None) -> list[Policy]:
    """Load contracts. Canonical columns mirror :class:`Policy`."""
    log = report or LoadReport()
    policies: list[Policy] = []
    for index, row in _rows(path):
        policy_id = _get(row, "policy_id")
        if not policy_id:
            log.issues.append(Issue(str(path), index, "policy_id", "missing"))
            continue
        sum_assured = _get(row, "sum_assured")
        policies.append(
            Policy(
                policy_id=policy_id,
                policy_number=_get(row, "policy_number", default=policy_id),
                product_line=_enum(ProductLine, _get(row, "product_line"), ProductLine.OTHER),
                product_name=_get(row, "product_name"),
                currency=_get(row, "currency", default=PHP).upper(),
                sum_assured=Money.parse(sum_assured, _get(row, "currency", default=PHP).upper())
                if sum_assured
                else None,
                is_single_premium=_get(row, "is_single_premium").lower()
                in ("1", "true", "yes", "y"),
                inception_date=_date(_get(row, "inception_date")),
                maturity_date=_date(_get(row, "maturity_date")),
                status=_get(row, "status"),
                owner_party_id=_get(row, "owner_party_id"),
                insured_party_id=_get(row, "insured_party_id"),
                payor_party_id=_get(row, "payor_party_id"),
                beneficiary_party_ids=tuple(
                    b for b in _get(row, "beneficiary_party_ids").split(";") if b
                ),
                agent_code=_get(row, "agent_code"),
                branch_code=_get(row, "branch_code"),
                free_look_days=int(_get(row, "free_look_days", default="15") or 15),
                source_system=_get(row, "source_system"),
            )
        )
    log.policies = len(policies)
    return policies


def load_transactions(
    path: str | pathlib.Path,
    *,
    rates: RateTable | None = None,
    report: LoadReport | None = None,
) -> list[Transaction]:
    """Load transactions and convert every one of them to pesos."""
    log = report or LoadReport()
    table = rates or RateTable()
    transactions: list[Transaction] = []

    for index, row in _rows(path):
        txn_id = _get(row, "txn_id", "transaction_id", "reference")
        occurred = _datetime(_get(row, "occurred_at", "transaction_datetime", "transaction_date"))
        party_id = _get(row, "party_id", "customer_id", "client_id")
        amount_text = _get(row, "amount")
        currency = _get(row, "currency", default=PHP).upper()

        if not txn_id or occurred is None or not party_id or not amount_text:
            log.issues.append(
                Issue(
                    str(path),
                    index,
                    "txn_id/occurred_at/party_id/amount",
                    "a transaction needs an id, a timestamp, a client and an amount",
                )
            )
            continue
        try:
            amount = Money.parse(amount_text, currency)
        except (InvalidOperation, ValueError):
            log.issues.append(Issue(str(path), index, "amount", f"unreadable: {amount_text!r}"))
            continue

        conversion = None
        amount_php: Money | None = amount if amount.currency == PHP else None
        if amount.currency != PHP:
            try:
                conversion = table.convert(amount, occurred.astimezone(MANILA).date())
                amount_php = conversion.converted
            except MissingRate as exc:
                log.issues.append(
                    Issue(
                        str(path),
                        index,
                        "currency",
                        f"{exc}; the transaction cannot be tested against a peso threshold",
                    )
                )
                continue

        raw_instrument = _get(row, "instrument", "payment_instrument")
        instrument = _enum(PaymentInstrument, raw_instrument, PaymentInstrument.OTHER)
        if (
            raw_instrument
            and instrument is PaymentInstrument.OTHER
            and raw_instrument.strip().upper() != "OTHER"
        ):
            log.issues.append(
                Issue(
                    str(path),
                    index,
                    "instrument",
                    f"{raw_instrument!r} is not a known instrument; recorded as OTHER and "
                    "excluded from the covered-instrument test",
                    "warning",
                )
            )

        transactions.append(
            Transaction(
                txn_id=txn_id,
                occurred_at=occurred,
                party_id=party_id,
                amount=amount,
                txn_type=_enum(TransactionType, _get(row, "txn_type", "transaction_type"),
                               TransactionType.OTHER),
                direction=_enum(Direction, _get(row, "direction"), Direction.INBOUND),
                instrument=instrument,
                channel=_enum(Channel, _get(row, "channel"), Channel.OTHER),
                policy_id=_get(row, "policy_id"),
                amount_php=amount_php,
                fx=conversion,
                value_date=_date(_get(row, "value_date")),
                counterparty=Counterparty(
                    name=_get(row, "counterparty_name"),
                    relationship_to_client=_get(row, "counterparty_relationship"),
                    bank_name=_get(row, "counterparty_bank"),
                    account_number=_get(row, "counterparty_account"),
                    country=_get(row, "counterparty_country"),
                    party_id=_get(row, "counterparty_party_id") or None,
                ),
                branch_code=_get(row, "branch_code"),
                processed_by=_get(row, "processed_by"),
                agent_code=_get(row, "agent_code"),
                source_system=_get(row, "source_system"),
                source_reference=_get(row, "source_reference", "official_receipt"),
                remarks=_get(row, "remarks"),
                raw=dict(row),
            )
        )

    log.transactions = len(transactions)
    return transactions


def load_book(
    *,
    parties_path: str | pathlib.Path,
    policies_path: str | pathlib.Path | None,
    transactions_path: str | pathlib.Path,
    rates_path: str | pathlib.Path | None = None,
) -> tuple[Book, LoadReport]:
    """Load a complete book of business, reporting everything that went wrong."""
    report = LoadReport()
    rates = RateTable.from_csv(rates_path) if rates_path else None
    parties = load_parties(parties_path, report)
    policies = load_policies(policies_path, report) if policies_path else []
    transactions = load_transactions(transactions_path, rates=rates, report=report)

    known_parties = {p.party_id for p in parties}
    known_policies = {p.policy_id for p in policies}
    for txn in transactions:
        if txn.party_id not in known_parties:
            report.issues.append(
                Issue(
                    str(transactions_path),
                    0,
                    "party_id",
                    f"transaction {txn.txn_id} references client {txn.party_id}, which has no "
                    "customer record; the report cannot carry the client's particulars",
                    "warning",
                )
            )
        if txn.policy_id and txn.policy_id not in known_policies:
            report.issues.append(
                Issue(
                    str(transactions_path),
                    0,
                    "policy_id",
                    f"transaction {txn.txn_id} references unknown policy {txn.policy_id}",
                    "warning",
                )
            )
    return Book(parties, policies, transactions), report

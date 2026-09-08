"""The entities surveillance runs over, and the ones it produces.

The split matters. :class:`Party`, :class:`Policy` and :class:`Transaction` are
*observations* — they come from the administration systems and this package
never invents them. :class:`Alert` and :class:`Case` are *assertions* this
system makes, and every one of them has to survive being questioned years
later, which is why they carry the evidence that produced them rather than a
score on its own.

Everything is frozen. An alert whose evidence can be edited after the fact is
not evidence, and a transaction amended in memory between the rule that flagged
it and the report that files it is a defect nobody will find. State changes go
through the store as new rows, not through mutation.

Identity of an alert is deliberately *derived* rather than random:
:attr:`Alert.dedup_key` hashes the rule, the subject and the transactions it
fired on, so re-running monitoring over an overlapping window produces the same
alert rather than a second copy of it. Re-running has to be safe — it is the
normal response to a rule change — and duplicated alerts are how a real one
gets lost.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aml.model.enums import (
    AlertState,
    CaseState,
    Channel,
    Direction,
    IdDocumentType,
    PartyType,
    PaymentInstrument,
    PepStatus,
    ProductLine,
    ReportKind,
    RiskRating,
    Severity,
    TransactionType,
)
from aml.money import PHP, Conversion, Money
from aml.phcalendar import MANILA

__all__ = [
    "jsonable",
    "IdDocument",
    "Counterparty",
    "Party",
    "Policy",
    "Transaction",
    "Alert",
    "Case",
    "Book",
]


def jsonable(value: Any) -> Any:
    """Convert model values to JSON-safe primitives.

    Money becomes a ``{amount, currency}`` pair rather than a float, for the
    reason the whole package avoids floats: this is what gets persisted and
    reloaded, and a round trip through a float is a round trip that loses
    centavos.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Money):
        return {"amount": str(value.quantized().amount), "currency": value.currency}
    if isinstance(value, Conversion):
        return value.as_dict()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if hasattr(value, "as_dict"):
        return jsonable(value.as_dict())
    return str(value)


def _digest(*parts: Any) -> str:
    payload = json.dumps(jsonable(parts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdDocument:
    """One identification document presented at onboarding or at a transaction."""

    doc_type: IdDocumentType
    number: str
    issuing_country: str = "PH"
    issue_date: dt.date | None = None
    expiry_date: dt.date | None = None

    @property
    def is_expired_at(self) -> Any:
        return self.expiry_date

    def expired_on(self, day: dt.date) -> bool:
        return self.expiry_date is not None and self.expiry_date < day

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_type": str(self.doc_type),
            "number": self.number,
            "issuing_country": self.issuing_country,
            "issue_date": jsonable(self.issue_date),
            "expiry_date": jsonable(self.expiry_date),
        }


@dataclass(frozen=True, slots=True)
class Counterparty:
    """The other side of a transaction, when it is not the client.

    An STR is largely a question about this object: who paid the premium, whose
    account received the surrender proceeds, in which country. A monitoring
    system that models only the policyholder cannot answer it, and third-party
    payment is one of the strongest insurance typologies there is.
    """

    name: str = ""
    relationship_to_client: str = ""
    bank_name: str = ""
    account_number: str = ""
    country: str = ""
    party_id: str | None = None

    @property
    def is_present(self) -> bool:
        return bool(self.name or self.account_number or self.party_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relationship_to_client": self.relationship_to_client,
            "bank_name": self.bank_name,
            "account_number": self.account_number,
            "country": self.country,
            "party_id": self.party_id,
        }


@dataclass(frozen=True, slots=True)
class Party:
    """A customer, payer, beneficiary, agent or counterparty.

    ``mdm_person_id`` is the link back to the golden record in the customer MDM
    when one is deployed. It is optional: this package runs standalone against
    an extract, and refusing to run without an MDM would make it useless to the
    institutions that need it most. But when the link is present, screening and
    aggregation operate on the resolved person rather than on one system's view
    of them — which is the difference between seeing eight policies under one
    client and seeing eight unrelated clients.
    """

    party_id: str
    party_type: PartyType = PartyType.INDIVIDUAL
    full_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    suffix: str = ""
    aliases: tuple[str, ...] = ()
    birth_date: dt.date | None = None
    birth_place: str = ""
    gender: str = ""
    nationality: str = "PH"
    country_of_residence: str = "PH"
    address_line: str = ""
    city: str = ""
    province: str = ""
    postal_code: str = ""
    country: str = "PH"
    phone: str = ""
    email: str = ""
    identifications: tuple[IdDocument, ...] = ()
    occupation: str = ""
    employer: str = ""
    nature_of_business: str = ""
    source_of_funds: str = ""
    #: Declared annual income or gross receipts. The denominator of the
    #: "not commensurate with financial capacity" test (ST3).
    declared_income: Money | None = None
    pep_status: PepStatus = PepStatus.UNKNOWN
    risk_rating: RiskRating = RiskRating.UNRATED
    customer_since: dt.date | None = None
    #: Set when targeted financial sanctions require the property frozen.
    is_frozen: bool = False
    mdm_person_id: str | None = None
    source_system: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        parts = [self.first_name, self.middle_name, self.last_name, self.suffix]
        return " ".join(p for p in parts if p).strip()

    @property
    def is_pep(self) -> bool:
        return self.pep_status not in (PepStatus.NOT_PEP, PepStatus.UNKNOWN)

    def identification(self, doc_type: IdDocumentType) -> IdDocument | None:
        for doc in self.identifications:
            if doc.doc_type == doc_type:
                return doc
        return None

    @property
    def has_identification(self) -> bool:
        return any(doc.number for doc in self.identifications)

    def age_on(self, day: dt.date) -> int | None:
        if self.birth_date is None:
            return None
        years = day.year - self.birth_date.year
        if (day.month, day.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    def as_dict(self) -> dict[str, Any]:
        return {
            "party_id": self.party_id,
            "party_type": str(self.party_type),
            "full_name": self.display_name,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "last_name": self.last_name,
            "suffix": self.suffix,
            "aliases": list(self.aliases),
            "birth_date": jsonable(self.birth_date),
            "birth_place": self.birth_place,
            "gender": self.gender,
            "nationality": self.nationality,
            "country_of_residence": self.country_of_residence,
            "address_line": self.address_line,
            "city": self.city,
            "province": self.province,
            "postal_code": self.postal_code,
            "country": self.country,
            "phone": self.phone,
            "email": self.email,
            "identifications": [d.as_dict() for d in self.identifications],
            "occupation": self.occupation,
            "employer": self.employer,
            "nature_of_business": self.nature_of_business,
            "source_of_funds": self.source_of_funds,
            "declared_income": jsonable(self.declared_income),
            "pep_status": str(self.pep_status),
            "risk_rating": str(self.risk_rating),
            "customer_since": jsonable(self.customer_since),
            "is_frozen": self.is_frozen,
            "mdm_person_id": self.mdm_person_id,
            "source_system": self.source_system,
            "extras": jsonable(self.extras),
        }


@dataclass(frozen=True, slots=True)
class Policy:
    """The contract a transaction happened on."""

    policy_id: str
    policy_number: str = ""
    product_line: ProductLine = ProductLine.OTHER
    product_name: str = ""
    currency: str = PHP
    sum_assured: Money | None = None
    modal_premium: Money | None = None
    is_single_premium: bool = False
    inception_date: dt.date | None = None
    maturity_date: dt.date | None = None
    status: str = ""
    owner_party_id: str = ""
    insured_party_id: str = ""
    payor_party_id: str = ""
    beneficiary_party_ids: tuple[str, ...] = ()
    agent_code: str = ""
    agent_party_id: str = ""
    branch_code: str = ""
    #: Days a policyholder may cancel and recover the premium. Fifteen days is
    #: the common contractual free-look period; it is per-product, so it is
    #: carried on the policy rather than assumed by the rule.
    free_look_days: int = 15
    source_system: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_number": self.policy_number,
            "product_line": str(self.product_line),
            "product_name": self.product_name,
            "currency": self.currency,
            "sum_assured": jsonable(self.sum_assured),
            "modal_premium": jsonable(self.modal_premium),
            "is_single_premium": self.is_single_premium,
            "inception_date": jsonable(self.inception_date),
            "maturity_date": jsonable(self.maturity_date),
            "status": self.status,
            "owner_party_id": self.owner_party_id,
            "insured_party_id": self.insured_party_id,
            "payor_party_id": self.payor_party_id,
            "beneficiary_party_ids": list(self.beneficiary_party_ids),
            "agent_code": self.agent_code,
            "agent_party_id": self.agent_party_id,
            "branch_code": self.branch_code,
            "free_look_days": self.free_look_days,
            "source_system": self.source_system,
            "extras": jsonable(self.extras),
        }


@dataclass(frozen=True, slots=True)
class Transaction:
    """One movement of money, or one policy event that carries none.

    Non-monetary events are in scope on purpose. A beneficiary changed three
    times in a month is not a payment, but it is evidence, and a monitoring
    system that only sees money cannot detect the typologies that use the
    policy itself as the instrument.

    ``amount_php`` is populated by the ingest layer through the rate table, and
    is the only amount thresholds are ever compared against.
    """

    txn_id: str
    occurred_at: dt.datetime
    party_id: str
    amount: Money
    txn_type: TransactionType = TransactionType.OTHER
    direction: Direction = Direction.INBOUND
    instrument: PaymentInstrument = PaymentInstrument.OTHER
    channel: Channel = Channel.OTHER
    policy_id: str = ""
    amount_php: Money | None = None
    fx: Conversion | None = None
    value_date: dt.date | None = None
    counterparty: Counterparty = field(default_factory=Counterparty)
    branch_code: str = ""
    processed_by: str = ""
    agent_code: str = ""
    source_system: str = ""
    source_reference: str = ""
    remarks: str = ""
    #: Whatever the source row held, kept verbatim. An unmapped instrument or a
    #: local remark is often the thing that makes a narrative make sense.
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            # A naive timestamp is ambiguous, and the ambiguity lands exactly on
            # the reporting-day boundary. Source extracts are local time.
            object.__setattr__(self, "occurred_at", self.occurred_at.replace(tzinfo=MANILA))
        if self.value_date is None:
            object.__setattr__(self, "value_date", self.local_datetime.date())
        if self.amount_php is None and self.amount.currency == PHP:
            object.__setattr__(self, "amount_php", self.amount)

    @property
    def local_datetime(self) -> dt.datetime:
        """The transaction time in the reporting timezone."""
        return self.occurred_at.astimezone(MANILA)

    @property
    def transaction_date(self) -> dt.date:
        """The reporting day. Thresholds aggregate over this, never over UTC."""
        return self.local_datetime.date()

    @property
    def php(self) -> Money:
        """Peso amount, or an error naming the transaction that lacks one.

        Refusing to fall back is the point: a threshold test run against a
        foreign-currency figure as though it were pesos is a silent
        under-report.
        """
        if self.amount_php is None:
            raise ValueError(
                f"transaction {self.txn_id} has no peso equivalent; "
                f"load a {self.amount.currency} rate for {self.transaction_date.isoformat()}"
            )
        return self.amount_php

    @property
    def is_monetary(self) -> bool:
        return self.direction is not Direction.NON_MONETARY and not self.amount.is_zero

    def as_dict(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "occurred_at": self.local_datetime.isoformat(),
            "transaction_date": self.transaction_date.isoformat(),
            "value_date": jsonable(self.value_date),
            "party_id": self.party_id,
            "policy_id": self.policy_id,
            "txn_type": str(self.txn_type),
            "direction": str(self.direction),
            "instrument": str(self.instrument),
            "channel": str(self.channel),
            "amount": jsonable(self.amount),
            "amount_php": jsonable(self.amount_php),
            "fx": jsonable(self.fx),
            "counterparty": self.counterparty.as_dict(),
            "branch_code": self.branch_code,
            "processed_by": self.processed_by,
            "agent_code": self.agent_code,
            "source_system": self.source_system,
            "source_reference": self.source_reference,
            "remarks": self.remarks,
            "raw": jsonable(self.raw),
        }


@dataclass(frozen=True, slots=True)
class Alert:
    """One rule firing on one subject, with the evidence that made it fire.

    ``narrative`` is written by the rule in plain language because it is the
    seed of the STR narrative a compliance officer will file. A rule that can
    only say "score 0.82" has pushed the whole explanatory burden onto the
    analyst, and the narrative is the part of an STR the AMLC actually reads.
    """

    alert_id: str
    rule_id: str
    rule_version: str
    subject_party_id: str
    created_at: dt.datetime
    severity: Severity = Severity.MEDIUM
    score: Decimal = Decimal("0")
    title: str = ""
    narrative: str = ""
    transaction_ids: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    window_start: dt.date | None = None
    window_end: dt.date | None = None
    #: Suspicious circumstances (ST1-ST7) this rule evidences.
    st_codes: tuple[str, ...] = ()
    #: Predicate offences suggested, where the rule can suggest one.
    unlawful_activity_codes: tuple[str, ...] = ()
    amount_php: Money | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    state: AlertState = AlertState.OPEN
    case_id: str | None = None
    #: True when the rule detected a covered transaction rather than a
    #: suspicion. CTR alerts do not need a suspicion determination; they need
    #: filing.
    is_covered_transaction: bool = False
    #: Extra identity for findings that are not about transactions. A screening
    #: hit is identified by the list entries it matched, and without this two
    #: different sanctions matches on one client would collapse into one alert.
    dedup_extra: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> str:
        """Stable identity for this finding.

        Rule, subject and the exact transactions. Deliberately excludes the
        run timestamp and the score, so that re-running yesterday's window —
        or re-running after a threshold change that leaves this finding intact
        — updates one alert instead of creating another.
        """
        return _digest(
            self.rule_id,
            self.subject_party_id,
            tuple(sorted(self.transaction_ids)),
            tuple(sorted(self.dedup_extra)),
            jsonable(self.window_start),
            jsonable(self.window_end),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "dedup_key": self.dedup_key,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "subject_party_id": self.subject_party_id,
            "created_at": jsonable(self.created_at),
            "severity": str(self.severity),
            "score": str(self.score),
            "title": self.title,
            "narrative": self.narrative,
            "transaction_ids": list(self.transaction_ids),
            "policy_ids": list(self.policy_ids),
            "window_start": jsonable(self.window_start),
            "window_end": jsonable(self.window_end),
            "st_codes": list(self.st_codes),
            "unlawful_activity_codes": list(self.unlawful_activity_codes),
            "amount_php": jsonable(self.amount_php),
            "evidence": jsonable(self.evidence),
            "state": str(self.state),
            "case_id": self.case_id,
            "is_covered_transaction": self.is_covered_transaction,
            "dedup_extra": list(self.dedup_extra),
        }


@dataclass(frozen=True, slots=True)
class Case:
    """An investigation, and the thing a report is filed from.

    ``determined_at`` is the moment a human decided the transaction is
    suspicious. It is not the moment the alert fired and not the moment the
    report is built: the filing clock runs from determination, and recording it
    as anything else either invents lateness or hides it.
    """

    case_id: str
    kind: ReportKind
    subject_party_id: str
    opened_at: dt.datetime
    state: CaseState = CaseState.OPEN
    alert_ids: tuple[str, ...] = ()
    transaction_ids: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    st_codes: tuple[str, ...] = ()
    unlawful_activity_codes: tuple[str, ...] = ()
    narrative: str = ""
    assigned_to: str = ""
    determined_at: dt.datetime | None = None
    determined_by: str = ""
    filing_deadline: dt.date | None = None
    approved_by: str = ""
    approved_at: dt.datetime | None = None
    filed_at: dt.datetime | None = None
    closure_reason: str = ""
    reference_number: str = ""
    priority: Severity = Severity.MEDIUM

    @property
    def is_open(self) -> bool:
        return self.state not in (CaseState.FILED, CaseState.CLOSED_NOT_REPORTABLE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": str(self.kind),
            "subject_party_id": self.subject_party_id,
            "opened_at": jsonable(self.opened_at),
            "state": str(self.state),
            "alert_ids": list(self.alert_ids),
            "transaction_ids": list(self.transaction_ids),
            "policy_ids": list(self.policy_ids),
            "st_codes": list(self.st_codes),
            "unlawful_activity_codes": list(self.unlawful_activity_codes),
            "narrative": self.narrative,
            "assigned_to": self.assigned_to,
            "determined_at": jsonable(self.determined_at),
            "determined_by": self.determined_by,
            "filing_deadline": jsonable(self.filing_deadline),
            "approved_by": self.approved_by,
            "approved_at": jsonable(self.approved_at),
            "filed_at": jsonable(self.filed_at),
            "closure_reason": self.closure_reason,
            "reference_number": self.reference_number,
            "priority": str(self.priority),
        }


class Book:
    """The population a monitoring run sees, indexed the ways rules ask for it.

    Built once per run and shared by every rule. Rules ask for a party's
    transactions, a policy's history and a day's activity constantly; without
    the indexes each rule rescans the whole extract and the run becomes
    quadratic in the number of rules.
    """

    __slots__ = ("parties", "policies", "transactions", "_by_party", "_by_policy", "_by_id")

    def __init__(
        self,
        parties: Iterable[Party] = (),
        policies: Iterable[Policy] = (),
        transactions: Iterable[Transaction] = (),
    ) -> None:
        self.parties: dict[str, Party] = {p.party_id: p for p in parties}
        self.policies: dict[str, Policy] = {p.policy_id: p for p in policies}
        self.transactions: tuple[Transaction, ...] = tuple(
            sorted(transactions, key=lambda t: (t.occurred_at, t.txn_id))
        )
        self._by_id: dict[str, Transaction] = {t.txn_id: t for t in self.transactions}
        self._by_party: dict[str, list[Transaction]] = {}
        self._by_policy: dict[str, list[Transaction]] = {}
        for txn in self.transactions:
            self._by_party.setdefault(txn.party_id, []).append(txn)
            if txn.policy_id:
                self._by_policy.setdefault(txn.policy_id, []).append(txn)

    def __len__(self) -> int:
        return len(self.transactions)

    def __iter__(self) -> Iterator[Transaction]:
        return iter(self.transactions)

    def party(self, party_id: str) -> Party | None:
        return self.parties.get(party_id)

    def policy(self, policy_id: str) -> Policy | None:
        return self.policies.get(policy_id)

    def transaction(self, txn_id: str) -> Transaction | None:
        return self._by_id.get(txn_id)

    def for_party(self, party_id: str) -> Sequence[Transaction]:
        return tuple(self._by_party.get(party_id, ()))

    def for_policy(self, policy_id: str) -> Sequence[Transaction]:
        return tuple(self._by_policy.get(policy_id, ()))

    def party_ids(self) -> Sequence[str]:
        """Every party with activity, in a stable order.

        Stable because a monitoring run has to be reproducible: two runs over
        the same extract must produce the same alerts in the same order, or
        diffing yesterday's output against today's tells you nothing.
        """
        return tuple(sorted(self._by_party))

    def window(self, start: dt.date, end: dt.date) -> Book:
        """A view restricted to a date range, keeping the same reference data."""
        kept = [t for t in self.transactions if start <= t.transaction_date <= end]
        return Book(self.parties.values(), self.policies.values(), kept)

    def daily_totals(self, party_id: str) -> dict[dt.date, Money]:
        """Peso totals per reporting day for one party."""
        totals: dict[dt.date, Money] = {}
        for txn in self.for_party(party_id):
            if not txn.is_monetary:
                continue
            day = txn.transaction_date
            totals[day] = totals.get(day, Money.zero()) + txn.php
        return totals

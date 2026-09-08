"""Controlled vocabularies for AML surveillance and reporting.

Closed vocabularies rather than free text, for the same reason the MDM model
uses them and one more that is specific to this package: several of these values
are *decision inputs*. Whether an instrument is "cash or other equivalent
monetary instrument" decides whether a transaction is covered and therefore
reportable, and that decision cannot rest on whether a source system wrote
"MC", "Manager's Check" or "managers cheque" this week.

Anything a source sends that does not map lands in ``OTHER``/``UNKNOWN`` with the
raw value retained on the transaction. It is never dropped and never guessed:
an unmapped instrument shows up in the data-quality report, where somebody can
decide what it is, instead of quietly failing a threshold test.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PartyType",
    "IdDocumentType",
    "PepStatus",
    "RiskRating",
    "ProductLine",
    "TransactionType",
    "Direction",
    "PaymentInstrument",
    "Channel",
    "ReportKind",
    "AlertState",
    "CaseState",
    "Severity",
    "ScreeningDecision",
    "ListType",
    "SubmissionState",
    "MONETARY_INSTRUMENTS",
    "CASH_INSTRUMENTS",
    "OUTFLOW_TYPES",
    "INFLOW_TYPES",
]


class PartyType(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    ORGANIZATION = "ORGANIZATION"
    UNKNOWN = "UNKNOWN"


class IdDocumentType(StrEnum):
    """Identification documents an insurer in the Philippines actually sees."""

    PHILSYS = "PHILSYS"          # PhilSys / national ID (PhilID)
    PASSPORT = "PASSPORT"
    DRIVERS_LICENSE = "DRIVERS_LICENSE"
    UMID = "UMID"
    SSS = "SSS"
    GSIS = "GSIS"
    TIN = "TIN"
    PRC = "PRC"
    VOTERS_ID = "VOTERS_ID"
    POSTAL_ID = "POSTAL_ID"
    PHILHEALTH = "PHILHEALTH"
    ACR_I_CARD = "ACR_I_CARD"    # resident aliens
    SEC_REGISTRATION = "SEC_REGISTRATION"
    DTI_REGISTRATION = "DTI_REGISTRATION"
    BIR_REGISTRATION = "BIR_REGISTRATION"
    OTHER = "OTHER"


class PepStatus(StrEnum):
    NOT_PEP = "NOT_PEP"
    DOMESTIC_PEP = "DOMESTIC_PEP"
    FOREIGN_PEP = "FOREIGN_PEP"
    INTERNATIONAL_ORGANISATION_PEP = "INTERNATIONAL_ORGANISATION_PEP"
    #: Relative or close associate of a PEP — treated as PEP-adjacent risk.
    RELATED_TO_PEP = "RELATED_TO_PEP"
    UNKNOWN = "UNKNOWN"


class RiskRating(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    UNRATED = "UNRATED"


class ProductLine(StrEnum):
    TRADITIONAL_LIFE = "TRADITIONAL_LIFE"
    VARIABLE_UNIT_LINKED = "VARIABLE_UNIT_LINKED"
    SINGLE_PREMIUM = "SINGLE_PREMIUM"
    GROUP_LIFE = "GROUP_LIFE"
    ANNUITY = "ANNUITY"
    PRE_NEED = "PRE_NEED"
    HEALTH = "HEALTH"
    NON_LIFE = "NON_LIFE"
    OTHER = "OTHER"


class TransactionType(StrEnum):
    """What happened on the policy.

    The list is deliberately finer than "credit/debit": the insurance
    typologies this system detects are defined by transaction *type* sequences
    — a single premium followed by a free-look cancellation is the classic
    laundering pattern, and it is invisible if both are recorded as "payment".
    """

    PREMIUM_PAYMENT = "PREMIUM_PAYMENT"
    SINGLE_PREMIUM = "SINGLE_PREMIUM"
    TOP_UP = "TOP_UP"
    REINSTATEMENT = "REINSTATEMENT"
    POLICY_LOAN = "POLICY_LOAN"
    LOAN_REPAYMENT = "LOAN_REPAYMENT"
    PARTIAL_WITHDRAWAL = "PARTIAL_WITHDRAWAL"
    FULL_SURRENDER = "FULL_SURRENDER"
    FREE_LOOK_CANCELLATION = "FREE_LOOK_CANCELLATION"
    POLICY_CANCELLATION = "POLICY_CANCELLATION"
    MATURITY_BENEFIT = "MATURITY_BENEFIT"
    DEATH_CLAIM = "DEATH_CLAIM"
    LIVING_BENEFIT = "LIVING_BENEFIT"
    PREMIUM_REFUND = "PREMIUM_REFUND"
    OVERPAYMENT_REFUND = "OVERPAYMENT_REFUND"
    DIVIDEND_WITHDRAWAL = "DIVIDEND_WITHDRAWAL"
    FUND_SWITCH = "FUND_SWITCH"
    ASSIGNMENT = "ASSIGNMENT"
    OWNERSHIP_CHANGE = "OWNERSHIP_CHANGE"
    BENEFICIARY_CHANGE = "BENEFICIARY_CHANGE"
    COMMISSION_PAYMENT = "COMMISSION_PAYMENT"
    OTHER = "OTHER"


class Direction(StrEnum):
    """Which way the money moved, from the insurer's point of view."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    #: Non-monetary events (beneficiary change, fund switch) that carry no cash
    #: but are evidence for typologies.
    NON_MONETARY = "NON_MONETARY"


class PaymentInstrument(StrEnum):
    CASH = "CASH"
    MANAGERS_CHECK = "MANAGERS_CHECK"
    CASHIERS_CHECK = "CASHIERS_CHECK"
    PERSONAL_CHECK = "PERSONAL_CHECK"
    CORPORATE_CHECK = "CORPORATE_CHECK"
    DEMAND_DRAFT = "DEMAND_DRAFT"
    MONEY_ORDER = "MONEY_ORDER"
    BANK_TRANSFER = "BANK_TRANSFER"
    INSTAPAY = "INSTAPAY"
    PESONET = "PESONET"
    SWIFT_TRANSFER = "SWIFT_TRANSFER"
    CREDIT_CARD = "CREDIT_CARD"
    DEBIT_CARD = "DEBIT_CARD"
    EWALLET = "EWALLET"
    REMITTANCE = "REMITTANCE"
    AUTO_DEBIT_ARRANGEMENT = "AUTO_DEBIT_ARRANGEMENT"
    SALARY_DEDUCTION = "SALARY_DEDUCTION"
    VIRTUAL_ASSET = "VIRTUAL_ASSET"
    OTHER = "OTHER"


#: Physical cash. Always covered, and the highest-risk instrument an insurer
#: handles: an insurance premium settled in banknotes has no bank in the chain
#: that has already done its own customer due diligence.
CASH_INSTRUMENTS: frozenset[PaymentInstrument] = frozenset(
    {PaymentInstrument.CASH}
)

#: "Cash or other equivalent monetary instrument", as the covered-transaction
#: test is written. The membership of this set is an *interpretation*, and it is
#: overridable in configuration precisely because it is one: institutions differ
#: on whether an InstaPay credit is an equivalent monetary instrument. The
#: default here is the inclusive reading — over-reporting is a filing cost,
#: under-reporting is a violation.
MONETARY_INSTRUMENTS: frozenset[PaymentInstrument] = frozenset(
    {
        PaymentInstrument.CASH,
        PaymentInstrument.MANAGERS_CHECK,
        PaymentInstrument.CASHIERS_CHECK,
        PaymentInstrument.PERSONAL_CHECK,
        PaymentInstrument.CORPORATE_CHECK,
        PaymentInstrument.DEMAND_DRAFT,
        PaymentInstrument.MONEY_ORDER,
        PaymentInstrument.BANK_TRANSFER,
        PaymentInstrument.INSTAPAY,
        PaymentInstrument.PESONET,
        PaymentInstrument.SWIFT_TRANSFER,
        PaymentInstrument.REMITTANCE,
        PaymentInstrument.EWALLET,
        PaymentInstrument.VIRTUAL_ASSET,
    }
)

#: Money leaving the insurer. The exit leg of every laundering typology that
#: uses an insurance product as the wash.
OUTFLOW_TYPES: frozenset[TransactionType] = frozenset(
    {
        TransactionType.PARTIAL_WITHDRAWAL,
        TransactionType.FULL_SURRENDER,
        TransactionType.FREE_LOOK_CANCELLATION,
        TransactionType.POLICY_CANCELLATION,
        TransactionType.MATURITY_BENEFIT,
        TransactionType.DEATH_CLAIM,
        TransactionType.LIVING_BENEFIT,
        TransactionType.PREMIUM_REFUND,
        TransactionType.OVERPAYMENT_REFUND,
        TransactionType.DIVIDEND_WITHDRAWAL,
        TransactionType.POLICY_LOAN,
        TransactionType.COMMISSION_PAYMENT,
    }
)

#: Money arriving at the insurer.
INFLOW_TYPES: frozenset[TransactionType] = frozenset(
    {
        TransactionType.PREMIUM_PAYMENT,
        TransactionType.SINGLE_PREMIUM,
        TransactionType.TOP_UP,
        TransactionType.REINSTATEMENT,
        TransactionType.LOAN_REPAYMENT,
    }
)


class Channel(StrEnum):
    BRANCH = "BRANCH"
    AGENT = "AGENT"
    BROKER = "BROKER"
    BANCASSURANCE = "BANCASSURANCE"
    ONLINE = "ONLINE"
    MOBILE_APP = "MOBILE_APP"
    PAYMENT_CENTER = "PAYMENT_CENTER"
    CORPORATE_PAYROLL = "CORPORATE_PAYROLL"
    CALL_CENTER = "CALL_CENTER"
    OTHER = "OTHER"


class ReportKind(StrEnum):
    CTR = "CTR"
    STR = "STR"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertState(StrEnum):
    """Where an alert is in triage.

    ``CLOSED_FALSE_POSITIVE`` requires a written reason. An alert closed with no
    reason is indistinguishable, to an examiner, from an alert nobody looked at.
    """

    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    ESCALATED = "ESCALATED"
    CLOSED_FALSE_POSITIVE = "CLOSED_FALSE_POSITIVE"
    CLOSED_NOT_SUSPICIOUS = "CLOSED_NOT_SUSPICIOUS"
    CLOSED_REPORTED = "CLOSED_REPORTED"


class CaseState(StrEnum):
    """The filing workflow.

    Determination is a distinct state from filing because the five-working-day
    clock starts at determination, and the system has to know when that was.
    """

    OPEN = "OPEN"
    UNDER_INVESTIGATION = "UNDER_INVESTIGATION"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED_FOR_FILING = "APPROVED_FOR_FILING"
    FILED = "FILED"
    CLOSED_NOT_REPORTABLE = "CLOSED_NOT_REPORTABLE"
    REOPENED = "REOPENED"


class ScreeningDecision(StrEnum):
    PENDING = "PENDING"
    NO_MATCH = "NO_MATCH"
    POTENTIAL_MATCH = "POTENTIAL_MATCH"
    TRUE_MATCH = "TRUE_MATCH"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class ListType(StrEnum):
    """What a screening hit was found on, which decides what happens next.

    A ``SANCTIONS`` hit against a designated person is not an alert to triage at
    leisure — targeted financial sanctions require the property to be frozen
    without delay and the AMLC informed. A ``PEP`` hit is enhanced due
    diligence. Conflating the two is how a freeze obligation gets missed.
    """

    SANCTIONS = "SANCTIONS"
    UN_DESIGNATED = "UN_DESIGNATED"
    ATC_DESIGNATED = "ATC_DESIGNATED"
    PEP = "PEP"
    ADVERSE_MEDIA = "ADVERSE_MEDIA"
    LAW_ENFORCEMENT = "LAW_ENFORCEMENT"
    REGULATORY_ENFORCEMENT = "REGULATORY_ENFORCEMENT"
    INTERNAL_WATCHLIST = "INTERNAL_WATCHLIST"
    OTHER = "OTHER"


class SubmissionState(StrEnum):
    PREPARED = "PREPARED"
    PACKAGED = "PACKAGED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

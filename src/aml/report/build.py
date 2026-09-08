"""Turning alerts and cases into report records.

The output of this module is *canonical* records — plain mappings whose keys
are this package's names, not the regulator's. The projection onto the AMLC's
columns happens in :mod:`aml.report.spec`. Keeping the two apart is what lets a
schema change be a configuration edit.

Three decisions worth stating.

**A transaction is reported once.** A covered transaction can be found by more
than one rule — a single large payment also appears in the client's same-day
aggregate — and filing it twice is a data-quality finding against the
institution. Records are deduplicated by transaction, keeping the basis that
found it first.

**References are derived, not sequential.** A report reference is a digest of
what it reports, so regenerating a batch after a correction produces the same
reference for the same transaction instead of a second report for it. Sequence
numbers would make re-running the pipeline unsafe, and re-running is normal.

**Names are split carefully.** ``JUAN DELA CRUZ`` has a two-word surname, and a
report that files ``CRUZ`` as the family name and ``DELA`` as a middle name is
wrong in a way that will not be caught by any validator — it will simply fail
to match the AMLC's records. Where the source has given us structured name
parts we use them; where it has not, the particle-aware split is a better guess
than "the last token".
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from aml.config import AmlConfig
from aml.model.entities import Alert, Book, Case, Party
from aml.model.enums import PartyType, ReportKind
from aml.money import PHP, Money
from aml.phcalendar import now_manila
from aml.screening.names import _PARTICLES, _SUFFIXES, normalize_name

__all__ = ["split_name", "party_fields", "ctr_records", "str_records", "reference_for"]


def reference_for(prefix: str, institution_code: str, day: dt.date, key: str) -> str:
    """A stable, unique-enough reference for one filed item."""
    digest = hashlib.sha1(f"{institution_code}|{key}".encode()).hexdigest()[:10].upper()
    return f"{prefix}-{day.strftime('%Y%m%d')}-{digest}"


def split_name(full_name: str) -> tuple[str, str, str, str]:
    """Split a single name string into (first, middle, last, suffix).

    Particle-aware, because Spanish-derived surnames are common here and they
    are two or three tokens long. ``JUAN SANTOS DELA CRUZ`` splits as
    first=JUAN, middle=SANTOS, last=DELA CRUZ.
    """
    raw = " ".join(str(full_name).split())
    if not raw:
        return "", "", "", ""
    tokens = raw.replace(",", " ").split()
    suffix = ""
    if tokens and normalize_name(tokens[-1]).strip(". ") in _SUFFIXES:
        suffix = tokens.pop()
    if not tokens:
        return "", "", "", suffix

    particles = {p.split()[0].upper() for p in _PARTICLES}
    surname_start = len(tokens) - 1
    for index in range(len(tokens) - 1):
        if tokens[index].upper().strip(".") in particles and index > 0:
            surname_start = index
            break
    last = " ".join(tokens[surname_start:])
    head = tokens[:surname_start]
    first = head[0] if head else ""
    middle = " ".join(head[1:]) if len(head) > 1 else ""
    return first, middle, last, suffix


def party_fields(party: Party | None, party_id: str) -> dict[str, Any]:
    """The identity block both report kinds carry."""
    if party is None:
        # A report about a client with no customer record still has to be
        # filed. Saying so in the record is better than filing blanks that
        # look like a formatting error.
        return {
            "customer_type": str(PartyType.UNKNOWN),
            "last_name": f"UNKNOWN CLIENT {party_id}",
            "first_name": "",
            "middle_name": "",
            "name_suffix": "",
        }

    first, middle, last, suffix = (
        party.first_name,
        party.middle_name,
        party.last_name,
        party.suffix,
    )
    if not last:
        first, middle, last, suffix = split_name(party.display_name)
    if party.party_type is PartyType.ORGANIZATION:
        first, middle, suffix = "", "", ""
        last = party.display_name

    identification = party.identifications[0] if party.identifications else None
    tin = next(
        (doc.number for doc in party.identifications if str(doc.doc_type) == "TIN"), ""
    )
    return {
        "customer_type": str(party.party_type),
        "last_name": last,
        "first_name": first,
        "middle_name": middle,
        "name_suffix": suffix,
        "aliases": list(party.aliases),
        "birth_date": party.birth_date,
        "birth_place": party.birth_place,
        "gender": party.gender[:1].upper() if party.gender else "",
        "nationality": party.nationality,
        "country_of_residence": party.country_of_residence,
        "address_line": party.address_line,
        "city": party.city,
        "province": party.province,
        "postal_code": party.postal_code,
        "phone": party.phone,
        "email": party.email,
        "occupation": party.occupation,
        "employer": party.employer or party.nature_of_business,
        "pep_status": str(party.pep_status),
        "id_type": str(identification.doc_type) if identification else "",
        "id_number": identification.number if identification else "",
        "id_issuing_country": identification.issuing_country if identification else "",
        "tin": tin,
    }


def _institution_fields(config: AmlConfig) -> dict[str, Any]:
    profile = config.institution
    return {
        "institution_code": profile.amlc_institution_code,
        "institution_name": profile.covered_person_name,
        "compliance_officer": profile.compliance_officer_name,
    }


def ctr_records(
    alerts: Iterable[Alert],
    book: Book,
    config: AmlConfig,
    *,
    prepared_on: dt.date | None = None,
) -> list[dict[str, Any]]:
    """One record per covered transaction, deduplicated across alerts."""
    prepared = prepared_on or now_manila().date()
    institution = _institution_fields(config)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for alert in alerts:
        if not alert.is_covered_transaction:
            continue
        basis = str(alert.evidence.get("basis", "")) or alert.rule_id
        for txn_id in alert.transaction_ids:
            if txn_id in seen:
                continue
            txn = book.transaction(txn_id)
            if txn is None:
                continue
            seen.add(txn_id)
            party = book.party(txn.party_id)
            policy = book.policy(txn.policy_id) if txn.policy_id else None
            deadline = config.calendar.add_working_days(
                txn.transaction_date, config.deadlines.ctr_working_days
            )
            record: dict[str, Any] = {
                "report_type": str(ReportKind.CTR),
                "report_reference": reference_for(
                    "CTR", institution["institution_code"], txn.transaction_date, txn.txn_id
                ),
                **institution,
                "branch_code": txn.branch_code,
                "transaction_reference": txn.source_reference or txn.txn_id,
                "transaction_date": txn.transaction_date,
                "transaction_time": txn.local_datetime.strftime("%H:%M:%S"),
                "transaction_type": str(txn.txn_type),
                "payment_instrument": str(txn.instrument),
                "delivery_channel": str(txn.channel),
                "currency": txn.amount.currency,
                "amount": txn.amount,
                "amount_php": txn.php,
                "exchange_rate": str(txn.fx.rate) if txn.fx else "",
                "policy_number": policy.policy_number if policy else txn.policy_id,
                "product_line": str(policy.product_line) if policy else "",
                "counterparty_name": txn.counterparty.name,
                "counterparty_relationship": txn.counterparty.relationship_to_client,
                "counterparty_bank": txn.counterparty.bank_name,
                "counterparty_account": txn.counterparty.account_number,
                "counterparty_country": txn.counterparty.country,
                "detection_basis": basis,
                "date_prepared": prepared,
                "filing_deadline": deadline,
                **party_fields(party, txn.party_id),
            }
            records.append(record)

    records.sort(key=lambda r: (r["transaction_date"], r["report_reference"]))
    return records


def str_records(
    cases: Iterable[Case],
    alerts_by_case: Mapping[str, Sequence[Alert]],
    book: Book,
    config: AmlConfig,
    *,
    prepared_on: dt.date | None = None,
) -> list[dict[str, Any]]:
    """One record per case, carrying the transactions that evidence it."""
    prepared = prepared_on or now_manila().date()
    institution = _institution_fields(config)
    records: list[dict[str, Any]] = []

    for case in cases:
        alerts = list(alerts_by_case.get(case.case_id, ()))
        transactions = [
            txn
            for txn in (book.transaction(txn_id) for txn_id in case.transaction_ids)
            if txn is not None
        ]
        monetary = [t for t in transactions if t.is_monetary]
        total = Money.zero(PHP)
        for txn in monetary:
            total = total + txn.php
        party = book.party(case.subject_party_id)
        policies = [book.policy(pid) for pid in case.policy_ids]
        determination = (
            case.determined_at.date() if case.determined_at else prepared
        )
        terrorism = any("UA14" in a.unlawful_activity_codes for a in alerts) or any(
            a.rule_id in ("str.designated_party", "screening.sanctions_match") for a in alerts
        )
        counterparties = sorted(
            {
                f"{t.counterparty.name} ({t.counterparty.country or 'PH'})"
                for t in transactions
                if t.counterparty.name
            }
        )

        records.append(
            {
                "report_type": str(ReportKind.STR),
                "report_reference": case.reference_number
                or reference_for(
                    "STR", institution["institution_code"], determination, case.case_id
                ),
                **institution,
                "branch_code": next((t.branch_code for t in transactions if t.branch_code), ""),
                "suspicious_indicators": list(case.st_codes),
                "unlawful_activities": list(case.unlawful_activity_codes),
                "determination_date": determination,
                "filing_deadline": case.filing_deadline
                or config.calendar.add_working_days(
                    determination, config.deadlines.str_working_days
                ),
                "terrorism_related": terrorism,
                "property_frozen": bool(party.is_frozen) if party else False,
                "policy_numbers": [p.policy_number for p in policies if p],
                "transaction_count": len(transactions),
                "transaction_references": [t.source_reference or t.txn_id for t in transactions],
                "first_transaction_date": min(
                    (t.transaction_date for t in transactions), default=None
                ),
                "last_transaction_date": max(
                    (t.transaction_date for t in transactions), default=None
                ),
                "total_amount_php": total,
                "currencies": sorted({t.amount.currency for t in transactions}),
                "counterparties": counterparties,
                "detection_rules": sorted({a.rule_id for a in alerts}),
                "narrative": case.narrative,
                "reported_by": case.determined_by or case.assigned_to,
                "approved_by": case.approved_by,
                "date_prepared": prepared,
                **party_fields(party, case.subject_party_id),
            }
        )

    records.sort(key=lambda r: (r["determination_date"], r["report_reference"]))
    return records

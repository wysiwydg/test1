"""What the customer file says, and what it fails to say.

These rules read the client record rather than the transaction pattern, and
they are the ones an insurer can act on soonest, because the remedy is usually
to complete the due diligence rather than to investigate a typology.

:class:`DesignatedPartyTransaction` is different from everything else in this
package and deserves its own sentence. A transaction involving a party flagged
as designated under targeted financial sanctions is not an alert to be triaged
in due course: the obligation is to freeze the property **without delay** and
inform the AMLC, and the report goes in hours rather than working days. It is
therefore always CRITICAL, always ST6, and the workflow treats it on the
terrorism-financing clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from aml.model.entities import Transaction
from aml.model.enums import Direction, PepStatus, Severity
from aml.money import php
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_date, fmt_money, subject_name, unique

__all__ = ["IncompleteIdentification", "PepActivity", "DesignatedPartyTransaction"]


@register
class IncompleteIdentification(PartyRule):
    rule_id = "str.incomplete_identification"
    version = "1"
    title = "Client not properly identified"
    description = (
        "Material activity by a client with no identification document on file, with "
        "only expired documents, or missing the identifying particulars a report requires."
    )
    st_codes = ("ST2",)
    default_params = {"minimum_amount": php(100_000)}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        minimum = ctx.param("minimum_amount", php(100_000))
        material = [t for t in transactions if t.is_monetary and t.php >= minimum]
        if not material:
            return
        party = ctx.party(party_id)
        if party is None:
            gaps = ["no customer record at all"]
        else:
            gaps = []
            if not party.has_identification:
                gaps.append("no identification document on file")
            else:
                latest = max(t.transaction_date for t in material)
                if all(doc.expired_on(latest) for doc in party.identifications):
                    gaps.append("every identification document on file had expired")
            if party.birth_date is None and str(party.party_type) == "INDIVIDUAL":
                gaps.append("no date of birth")
            if not (party.address_line or party.city):
                gaps.append("no address")
        if not gaps:
            return

        total = self.sum_php(material)
        latest_day = max(t.transaction_date for t in material)
        severity = Severity.HIGH if total > ctx.threshold else Severity.MEDIUM
        narrative = (
            f"{subject_name(ctx, party_id)} transacted {fmt_money(total)} across "
            f"{len(material)} transactions up to {fmt_date(latest_day)}, "
            f"while the customer record has {', '.join(gaps)}. The client is not properly "
            "identified, and the particulars required for a report cannot be produced from "
            "the record as it stands."
        )
        yield Finding(
            subject_party_id=party_id,
            title="Client not properly identified",
            narrative=narrative,
            transaction_ids=tuple(t.txn_id for t in material),
            policy_ids=unique([t.policy_id for t in material]),
            severity=severity,
            score=Decimal("0.70"),
            st_codes=("ST2",),
            amount_php=total,
            window_start=material[0].transaction_date,
            window_end=material[-1].transaction_date,
            evidence={"gaps": gaps, "total_php": str(total.amount)},
        )


@register
class PepActivity(PartyRule):
    rule_id = "str.pep_activity"
    version = "1"
    title = "Material activity by a politically exposed person"
    description = (
        "A PEP, a foreign PEP or a close associate transacting above the enhanced "
        "due-diligence floor. Not suspicious in itself; it requires senior approval and "
        "an explained source of wealth."
    )
    st_codes = ("ST3", "ST5")
    default_params = {
        "minimum_amount": php(300_000),
        "foreign_pep_minimum": php(100_000),
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        party = ctx.party(party_id)
        if party is None or not party.is_pep:
            return
        foreign = party.pep_status in (
            PepStatus.FOREIGN_PEP,
            PepStatus.INTERNATIONAL_ORGANISATION_PEP,
        )
        minimum = (
            ctx.param("foreign_pep_minimum", php(100_000))
            if foreign
            else ctx.param("minimum_amount", php(300_000))
        )
        material = [
            t for t in transactions
            if t.is_monetary and t.direction is Direction.INBOUND and t.php >= minimum
        ]
        if not material:
            return
        total = self.sum_php(material)
        status = str(party.pep_status).replace("_", " ").lower()
        source = party.source_of_funds or "not recorded"
        severity = Severity.HIGH if foreign else Severity.MEDIUM
        narrative = (
            f"{subject_name(ctx, party_id)} is recorded as a {status} and paid "
            f"{fmt_money(total)} across {len(material)} transactions up to "
            f"{fmt_date(material[-1].transaction_date)}. Source of wealth on file: {source}. "
            "Enhanced due diligence and senior management approval of the relationship are "
            "required, and the source of funds must be established."
        )
        yield Finding(
            subject_party_id=party_id,
            title="Politically exposed person — material activity",
            narrative=narrative,
            transaction_ids=tuple(t.txn_id for t in material),
            policy_ids=unique([t.policy_id for t in material]),
            severity=severity,
            score=Decimal("0.72") if foreign else Decimal("0.58"),
            st_codes=("ST3", "ST5"),
            amount_php=total,
            window_start=material[0].transaction_date,
            window_end=material[-1].transaction_date,
            evidence={
                "pep_status": str(party.pep_status),
                "source_of_funds": source,
                "total_php": str(total.amount),
            },
        )


@register
class DesignatedPartyTransaction(PartyRule):
    rule_id = "str.designated_party"
    version = "1"
    title = "Transaction involving a designated person or entity"
    description = (
        "Any transaction touching a party subject to targeted financial sanctions. "
        "Freeze without delay; report on the terrorism-financing clock, not the "
        "five-working-day one."
    )
    st_codes = ("ST6",)
    default_params: Mapping[str, Any] = {}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        party = ctx.party(party_id)
        frozen_counterparties = {
            t.counterparty.party_id
            for t in transactions
            if t.counterparty.party_id
            and (p := ctx.party(t.counterparty.party_id)) is not None
            and p.is_frozen
        }
        subject_designated = party is not None and party.is_frozen
        if not subject_designated and not frozen_counterparties:
            return

        involved = [
            t
            for t in transactions
            if subject_designated or t.counterparty.party_id in frozen_counterparties
        ]
        if not involved:
            return
        total = self.sum_php([t for t in involved if t.is_monetary])
        who = (
            subject_name(ctx, party_id)
            if subject_designated
            else "a counterparty to " + subject_name(ctx, party_id)
        )
        narrative = (
            f"{who} is recorded as designated under targeted financial sanctions. "
            f"{len(involved)} transactions totalling {fmt_money(total)} are on file up to "
            f"{fmt_date(involved[-1].transaction_date)}. The property must be frozen without "
            "delay and the AMLC informed; the report is due on the terrorism-financing "
            "timetable rather than the ordinary one."
        )
        yield Finding(
            subject_party_id=party_id,
            title="Designated person or entity",
            narrative=narrative,
            transaction_ids=tuple(t.txn_id for t in involved),
            policy_ids=unique([t.policy_id for t in involved]),
            severity=Severity.CRITICAL,
            score=Decimal("0.99"),
            st_codes=("ST6",),
            unlawful_activity_codes=("UA13", "UA14"),
            amount_php=total,
            window_start=involved[0].transaction_date,
            window_end=involved[-1].transaction_date,
            evidence={
                "subject_designated": subject_designated,
                "designated_counterparties": sorted(frozen_counterparties),
                "requires_freeze": True,
                "clock": "terrorism_financing",
            },
        )

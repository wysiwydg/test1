"""Jurisdiction risk.

Two lists, weighted differently on purpose. A counterparty in a jurisdiction
subject to a FATF *call for action* is a materially different fact from one in
a jurisdiction under *increased monitoring*, and a system that treats them the
same trains its analysts to discount both.

Both lists live in configuration. The FATF revises them three times a year and
an institution that has to ship code to update them will be out of date most
of the time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from aml.model.entities import Transaction
from aml.model.enums import Severity
from aml.money import php
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_date, fmt_money, policy_label, subject_name, unique

__all__ = ["HighRiskJurisdiction"]


@register
class HighRiskJurisdiction(PartyRule):
    rule_id = "str.high_risk_jurisdiction"
    version = "1"
    title = "Funds to or from a higher-risk jurisdiction"
    description = (
        "A material transaction whose counterparty is in a jurisdiction subject to a "
        "call for action or increased monitoring, or a client resident in one."
    )
    st_codes = ("ST5", "ST6")
    default_params = {"minimum_amount": php(50_000)}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        minimum = ctx.param("minimum_amount", php(50_000))
        call_for_action = {c.upper() for c in ctx.config.rules.high_risk_jurisdictions}
        monitored = {c.upper() for c in ctx.config.rules.monitored_jurisdictions}
        if not call_for_action and not monitored:
            return

        party = ctx.party(party_id)
        client_country = (party.country_of_residence.upper() if party else "") or ""
        client_flagged = client_country in call_for_action or client_country in monitored

        for txn in transactions:
            if not txn.is_monetary or txn.php < minimum:
                continue
            country = (txn.counterparty.country or "").upper()
            codes: tuple[str, ...]
            if country in call_for_action:
                tier, severity, score = "call for action", Severity.HIGH, Decimal("0.86")
                codes = ("ST6", "ST5")
            elif country in monitored:
                tier, severity, score = (
                    "increased monitoring",
                    Severity.MEDIUM,
                    Decimal("0.66"),
                )
                codes = ("ST5",)
            elif client_flagged and not country:
                tier, severity, score = (
                    "client resident in a listed jurisdiction",
                    Severity.MEDIUM,
                    Decimal("0.60"),
                )
                codes = ("ST5",)
                country = client_country
            else:
                continue

            direction = "to" if str(txn.direction) == "OUTBOUND" else "from"
            narrative = (
                f"{fmt_money(txn.php)} moved {direction} {country} on "
                f"{fmt_date(txn.transaction_date)} in connection with "
                f"{policy_label(ctx, txn.policy_id)} held by "
                f"{subject_name(ctx, party_id)}. {country} is a jurisdiction subject to "
                f"{tier}."
            )
            if txn.counterparty.name:
                narrative += f" The counterparty is {txn.counterparty.name}."

            yield Finding(
                subject_party_id=party_id,
                title="Higher-risk jurisdiction involved",
                narrative=narrative,
                transaction_ids=(txn.txn_id,),
                policy_ids=unique((txn.policy_id,)),
                severity=severity,
                score=score,
                st_codes=codes,
                amount_php=txn.php,
                window_start=txn.transaction_date,
                window_end=txn.transaction_date,
                evidence={
                    "country": country,
                    "tier": tier,
                    "counterparty": txn.counterparty.as_dict(),
                    "amount_php": str(txn.php.amount),
                    "client_country_of_residence": client_country,
                },
            )

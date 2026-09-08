"""Covered transactions: the reports that are filed regardless of suspicion.

A covered transaction is one in cash or other equivalent monetary instrument
exceeding PHP 500,000 in one banking day. Nobody has to find it suspicious;
it is reported because of what it is, and failing to report it is a violation
in its own right — which is why this is the one detection path in the package
that produces filings without a human determination in between.

Two rules, because the statute is read two ways and the difference is
material:

*   :class:`SingleCoveredTransaction` — one transaction over the threshold.
    Uncontroversial.
*   :class:`SameDayAggregateCovered` — several transactions by one client in
    one banking day that together exceed it. Whether this is one covered
    transaction is an interpretation; the institution's is recorded in
    ``thresholds.aggregate_same_day``. It defaults to on, because a client
    paying 300,000 twice in a morning is the exact behaviour the threshold
    exists to surface, and because over-reporting is a filing cost while
    under-reporting is a finding.

Both look at the *peso* amount, always. See ``aml.money`` for why that is not
the same sentence as "look at the amount".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from aml.model.entities import Transaction
from aml.model.enums import Severity
from aml.money import Money
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_date, fmt_money, join, policy_label, subject_name, unique

__all__ = ["SingleCoveredTransaction", "SameDayAggregateCovered"]


def _severity(amount: Money, threshold: Money) -> tuple[Severity, Decimal]:
    """Bigger is not more suspicious, but it is more consequential.

    A covered transaction is not a suspicion, so severity here drives review
    order and nothing else: an eight-figure cash premium should reach a human
    before a 501,000 peso one.
    """
    ratio = amount.amount / threshold.amount if threshold.amount else Decimal("1")
    if ratio >= 10:
        return Severity.HIGH, Decimal("0.9")
    if ratio >= 4:
        return Severity.MEDIUM, Decimal("0.7")
    return Severity.LOW, Decimal("0.5")


@register
class SingleCoveredTransaction(PartyRule):
    rule_id = "ctr.single_transaction"
    version = "1"
    title = "Single transaction exceeding the covered-transaction threshold"
    description = (
        "One transaction in cash or other equivalent monetary instrument whose peso "
        "amount exceeds the covered-transaction threshold. Filed as a CTR."
    )
    st_codes: tuple[str, ...] = ()
    default_params: Mapping[str, Any] = {}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        threshold = ctx.threshold
        for txn in transactions:
            if not txn.is_monetary or not ctx.is_covered_instrument(txn):
                continue
            if not txn.php > threshold:
                continue
            severity, score = _severity(txn.php, threshold)
            instrument = str(txn.instrument).replace("_", " ").lower()
            where = f" on {policy_label(ctx, txn.policy_id)}" if txn.policy_id else ""
            narrative = (
                f"{subject_name(ctx, party_id)} transacted {fmt_money(txn.php)} by "
                f"{instrument} on {fmt_date(txn.transaction_date)}{where} "
                f"({str(txn.txn_type).replace('_', ' ').lower()}). The amount exceeds the "
                f"{fmt_money(threshold)} covered-transaction threshold for one banking day."
            )
            if txn.amount.currency != threshold.currency:
                narrative += (
                    f" The transaction was in {txn.amount.currency} "
                    f"({fmt_money(txn.amount)}), converted at the reference rate for the "
                    "transaction date."
                )
            yield Finding(
                subject_party_id=party_id,
                title="Covered transaction",
                narrative=narrative,
                transaction_ids=(txn.txn_id,),
                policy_ids=unique((txn.policy_id,)),
                severity=severity,
                score=score,
                amount_php=txn.php,
                window_start=txn.transaction_date,
                window_end=txn.transaction_date,
                is_covered_transaction=True,
                evidence={
                    "threshold_php": str(threshold.amount),
                    "amount_php": str(txn.php.amount),
                    "instrument": str(txn.instrument),
                    "original_amount": str(txn.amount.quantized().amount),
                    "original_currency": txn.amount.currency,
                    "fx": txn.fx.as_dict() if txn.fx else None,
                    "basis": "single transaction",
                },
            )


@register
class SameDayAggregateCovered(PartyRule):
    rule_id = "ctr.same_day_aggregate"
    version = "1"
    title = "Same-day transactions aggregating above the covered threshold"
    description = (
        "Two or more transactions by one client in one banking day which, added "
        "together, exceed the covered-transaction threshold, where no single one does."
    )
    st_codes: tuple[str, ...] = ()
    default_params = {"minimum_count": 2}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        if not ctx.config.thresholds.aggregate_same_day:
            return
        minimum_count = ctx.param("minimum_count", 2)
        threshold = ctx.threshold
        by_day: dict[dt.date, list[Transaction]] = {}
        for txn in transactions:
            if txn.is_monetary and ctx.is_covered_instrument(txn):
                by_day.setdefault(txn.transaction_date, []).append(txn)

        for day in sorted(by_day):
            same_day = by_day[day]
            if len(same_day) < minimum_count:
                continue
            if any(t.php > threshold for t in same_day):
                # Already reported as a single covered transaction; reporting the
                # day again would file the same money twice.
                continue
            total = self.sum_php(same_day)
            if not total > threshold:
                continue
            severity, score = _severity(total, threshold)
            instruments = join(
                sorted({str(t.instrument).replace("_", " ").lower() for t in same_day})
            )
            narrative = (
                f"{subject_name(ctx, party_id)} made {len(same_day)} transactions totalling "
                f"{fmt_money(total)} on {fmt_date(day)} ({instruments}), no single one of "
                f"which exceeds the {fmt_money(threshold)} threshold. Aggregated over the "
                "banking day the total is a covered transaction."
            )
            yield Finding(
                subject_party_id=party_id,
                title="Covered transaction (same-day aggregate)",
                narrative=narrative,
                transaction_ids=tuple(t.txn_id for t in same_day),
                policy_ids=unique([t.policy_id for t in same_day]),
                severity=severity,
                score=score,
                amount_php=total,
                window_start=day,
                window_end=day,
                is_covered_transaction=True,
                evidence={
                    "threshold_php": str(threshold.amount),
                    "aggregate_php": str(total.amount),
                    "transaction_count": len(same_day),
                    "amounts_php": [str(t.php.amount) for t in same_day],
                    "instruments": sorted({str(t.instrument) for t in same_day}),
                    "basis": "same-day aggregation",
                },
            )

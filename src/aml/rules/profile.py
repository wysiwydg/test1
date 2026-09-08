"""Is this transaction consistent with what we know about the client?

Two statutory circumstances live here and they are not the same test.

**ST3 — capacity.** The amount is not commensurate with the client's business
or financial capacity. This is a comparison against what the client declared at
onboarding: income, occupation, nature of business. It fires on the first
transaction, which is exactly when it is most useful, and it fires hardest when
there is nothing on file to compare against — a client with no declared income
who pays a seven-figure premium is a due-diligence failure and a suspicion at
the same time.

**ST5 — deviation.** The transaction deviates from the client's own past
behaviour. This needs history, and it needs enough history to have a baseline
worth deviating from. A client with two months of activity has no baseline;
saying they deviated from it produces an alert an analyst cannot act on.

The baseline is a **median**, not a mean. A single large legitimate premium in
the history would drag a mean up far enough to hide the next one, which is the
behaviour an evasive client would exploit if they knew the rule.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Sequence
from decimal import Decimal

from aml.model.entities import Transaction
from aml.model.enums import Direction, Severity
from aml.money import Money, php
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_date, fmt_money, subject_name, unique

__all__ = ["IncomeCapacity", "BaselineDeviation"]


@register
class IncomeCapacity(PartyRule):
    rule_id = "str.income_capacity"
    version = "1"
    title = "Transactions not commensurate with declared financial capacity"
    description = (
        "Payments into policies over a rolling year that exceed a multiple of the "
        "client's declared income, or that are material where no income is on file."
    )
    st_codes = ("ST3",)
    default_params = {
        "lookback_days": 365,
        "income_multiple": Decimal("1.5"),
        #: Above this, an absent declared income is itself the finding.
        "undeclared_income_floor": php(500_000),
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        lookback = ctx.param("lookback_days", 365)
        multiple = ctx.param("income_multiple", Decimal("1.5"))
        undeclared_floor = ctx.param("undeclared_income_floor", php(500_000))

        party = ctx.party(party_id)
        recent = [
            t
            for t in transactions
            if t.direction is Direction.INBOUND
            and t.is_monetary
            and (ctx.as_of - t.transaction_date).days <= lookback
        ]
        if not recent:
            return
        total = self.sum_php(recent)

        income = party.declared_income if party is not None else None
        if income is None or income.is_zero:
            if total < undeclared_floor:
                return
            source = (party.source_of_funds if party else "") or "not recorded"
            narrative = (
                f"{subject_name(ctx, party_id)} paid {fmt_money(total)} across "
                f"{len(recent)} transactions in the {lookback} days to "
                f"{fmt_date(ctx.as_of)}, with no declared income or financial capacity on "
                f"file (source of funds: {source}). The amount cannot be reconciled with the "
                "client's profile because the profile does not exist."
            )
            yield Finding(
                subject_party_id=party_id,
                title="Material activity with no declared financial capacity",
                narrative=narrative,
                transaction_ids=tuple(t.txn_id for t in recent),
                policy_ids=unique([t.policy_id for t in recent]),
                severity=Severity.MEDIUM,
                score=Decimal("0.66"),
                st_codes=("ST3",),
                amount_php=total,
                window_start=recent[0].transaction_date,
                window_end=recent[-1].transaction_date,
                evidence={
                    "total_php": str(total.amount),
                    "declared_income": None,
                    "transaction_count": len(recent),
                    "lookback_days": lookback,
                },
            )
            return

        capacity = Money(income.amount * multiple, income.currency)
        if total <= capacity:
            return
        excess_ratio = (total.amount / income.amount).quantize(Decimal("0.01"))
        severity = Severity.HIGH if excess_ratio >= multiple * 2 else Severity.MEDIUM
        occupation = (party.occupation if party else "") or "unstated occupation"
        narrative = (
            f"{subject_name(ctx, party_id)} ({occupation}) paid {fmt_money(total)} across "
            f"{len(recent)} transactions in the {lookback} days to {fmt_date(ctx.as_of)}, "
            f"against a declared annual income of {fmt_money(income)} — {excess_ratio}× "
            "declared capacity. The amount is not commensurate with the financial capacity "
            "recorded for the client."
        )
        yield Finding(
            subject_party_id=party_id,
            title="Activity exceeds declared financial capacity",
            narrative=narrative,
            transaction_ids=tuple(t.txn_id for t in recent),
            policy_ids=unique([t.policy_id for t in recent]),
            severity=severity,
            score=min(Decimal("0.92"), Decimal("0.55") + excess_ratio / 20).quantize(
                Decimal("0.01")
            ),
            st_codes=("ST3",),
            amount_php=total,
            window_start=recent[0].transaction_date,
            window_end=recent[-1].transaction_date,
            evidence={
                "total_php": str(total.amount),
                "declared_income_php": str(income.amount),
                "multiple_of_income": str(excess_ratio),
                "threshold_multiple": str(multiple),
                "occupation": occupation,
            },
        )


@register
class BaselineDeviation(PartyRule):
    rule_id = "str.profile_deviation"
    version = "1"
    title = "Month deviating sharply from the client's own history"
    description = (
        "A month whose inflows are a large multiple of the median month for that client, "
        "measured over enough history for the median to mean something."
    )
    st_codes = ("ST5",)
    default_params = {
        "minimum_history_months": 3,
        "deviation_multiple": Decimal("5"),
        "minimum_amount": php(250_000),
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        min_history = ctx.param("minimum_history_months", 3)
        multiple = ctx.param("deviation_multiple", Decimal("5"))
        minimum = ctx.param("minimum_amount", php(250_000))

        monthly: dict[tuple[int, int], list[Transaction]] = {}
        for txn in transactions:
            if txn.direction is Direction.INBOUND and txn.is_monetary:
                day = txn.transaction_date
                monthly.setdefault((day.year, day.month), []).append(txn)
        if len(monthly) <= min_history:
            return

        months = sorted(monthly)
        totals = {m: self.sum_php(monthly[m]) for m in months}

        for position, month in enumerate(months):
            if position < min_history:
                continue
            history = [totals[m].amount for m in months[:position]]
            baseline = Decimal(str(statistics.median(history)))
            current = totals[month]
            if current < minimum or baseline <= 0:
                continue
            observed = (current.amount / baseline).quantize(Decimal("0.01"))
            if observed < multiple:
                continue

            severity = Severity.HIGH if observed >= multiple * 2 else Severity.MEDIUM
            label = dt.date(month[0], month[1], 1).strftime("%B %Y")
            narrative = (
                f"{subject_name(ctx, party_id)} paid {fmt_money(current)} in {label}, "
                f"against a median of {fmt_money(Money(baseline))} across the preceding "
                f"{position} months — {observed}× the client's own baseline. The activity "
                "deviates from the client's established pattern."
            )
            yield Finding(
                subject_party_id=party_id,
                title="Sharp deviation from the client's transaction history",
                narrative=narrative,
                transaction_ids=tuple(t.txn_id for t in monthly[month]),
                policy_ids=unique([t.policy_id for t in monthly[month]]),
                severity=severity,
                score=min(Decimal("0.90"), Decimal("0.50") + observed / 40).quantize(
                    Decimal("0.01")
                ),
                st_codes=("ST5",),
                amount_php=current,
                window_start=monthly[month][0].transaction_date,
                window_end=monthly[month][-1].transaction_date,
                evidence={
                    "month": f"{month[0]:04d}-{month[1]:02d}",
                    "month_total_php": str(current.amount),
                    "baseline_median_php": str(baseline),
                    "observed_multiple": str(observed),
                    "history_months": position,
                },
            )

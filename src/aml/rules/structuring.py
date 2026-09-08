"""Structuring: transactions arranged so that no single one has to be reported.

ST4 in the statute — "structured in order to avoid being the subject of
reporting requirements" — and the one suspicious circumstance a machine detects
better than a person, because it is a pattern across time that nobody sees from
a single transaction screen.

The detector looks for a cluster of transactions sitting deliberately *below*
the threshold within a short window. Two design choices are worth stating:

*   **The band has a floor.** Everything under 500,000 is not evidence of
    structuring; a client paying a 20,000 peso monthly premium three times is
    paying their premiums. The band starts at 60% of the threshold by default,
    which is where "just below" begins to mean something.
*   **The score rises with proximity, not just count.** Three payments of
    495,000 are a very different claim from three of 310,000, and an analyst
    triaging fifty alerts needs the ordering to reflect that.

Spread across branches, agents or policies is recorded as evidence and raises
severity, because deliberate dispersal is what separates structuring from a
client who simply pays in instalments.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal

from aml.model.entities import Transaction
from aml.model.enums import Severity
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_money, fmt_span, subject_name, unique

__all__ = ["StructuringBelowThreshold"]


def _is_cash_like(instrument: object) -> bool:
    """Cash and the instruments that behave like it at the counter."""
    name = str(instrument)
    return name == "CASH" or name.endswith("CHECK")


@register
class StructuringBelowThreshold(PartyRule):
    rule_id = "str.structuring"
    version = "1"
    title = "Transactions structured below the covered-transaction threshold"
    description = (
        "Several transactions in the band just below the covered-transaction threshold, "
        "by one client within a short window, which together exceed it."
    )
    st_codes = ("ST4",)
    default_params = {
        "window_days": 5,
        "minimum_count": 3,
        # Cash and near-cash only by default. A client structuring bank
        # transfers leaves a trail at the bank; a client structuring cash is
        # deliberately staying under the reporting line.
        "cash_like_only": False,
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        window_days = ctx.param("window_days", 5)
        minimum_count = ctx.param("minimum_count", 3)
        cash_like_only = ctx.param("cash_like_only", False)

        threshold = ctx.threshold
        floor = ctx.config.thresholds.structuring_floor

        candidates = [
            t
            for t in transactions
            if t.is_monetary
            and ctx.is_covered_instrument(t)
            and floor <= t.php <= threshold
            and (not cash_like_only or _is_cash_like(t.instrument))
        ]
        if len(candidates) < minimum_count:
            return

        index = 0
        while index <= len(candidates) - minimum_count:
            anchor = candidates[index]
            cutoff = anchor.transaction_date + dt.timedelta(days=window_days)
            cluster = [t for t in candidates[index:] if t.transaction_date <= cutoff]
            if len(cluster) < minimum_count:
                index += 1
                continue

            total = self.sum_php(cluster)
            if not total > threshold:
                index += 1
                continue

            proximity = (
                sum((t.php.amount / threshold.amount for t in cluster), Decimal(0))
                / Decimal(len(cluster))
            ).quantize(Decimal("0.001"))
            branches = unique([t.branch_code for t in cluster])
            agents = unique([t.agent_code for t in cluster])
            policies = unique([t.policy_id for t in cluster])
            dispersed = len(branches) > 1 or len(agents) > 1 or len(policies) > 1

            score = min(
                Decimal("0.98"),
                Decimal("0.45")
                + proximity * Decimal("0.35")
                + Decimal("0.05") * (len(cluster) - minimum_count)
                + (Decimal("0.12") if dispersed else Decimal("0")),
            ).quantize(Decimal("0.01"))
            severity = (
                Severity.HIGH
                if score >= Decimal("0.80")
                else Severity.MEDIUM
                if score >= Decimal("0.60")
                else Severity.LOW
            )

            start = cluster[0].transaction_date
            end = cluster[-1].transaction_date
            spread = ""
            if dispersed:
                parts = []
                if len(branches) > 1:
                    parts.append(f"{len(branches)} branches")
                if len(agents) > 1:
                    parts.append(f"{len(agents)} agents")
                if len(policies) > 1:
                    parts.append(f"{len(policies)} policies")
                spread = " The payments were spread across " + ", ".join(parts) + "."

            narrative = (
                f"{subject_name(ctx, party_id)} made {len(cluster)} transactions totalling "
                f"{fmt_money(total)} {fmt_span(start, end)}, each between {fmt_money(floor)} "
                f"and the {fmt_money(threshold)} covered-transaction threshold and none of "
                f"them individually reportable. The average transaction was "
                f"{proximity:.1%} of the threshold.{spread} The pattern is consistent with "
                "transactions structured to avoid the reporting requirement."
            )

            yield Finding(
                subject_party_id=party_id,
                title="Possible structuring below the reporting threshold",
                narrative=narrative,
                transaction_ids=tuple(t.txn_id for t in cluster),
                policy_ids=policies,
                severity=severity,
                score=score,
                st_codes=("ST4",),
                amount_php=total,
                window_start=start,
                window_end=end,
                evidence={
                    "threshold_php": str(threshold.amount),
                    "band_floor_php": str(floor.amount),
                    "transaction_count": len(cluster),
                    "aggregate_php": str(total.amount),
                    "mean_proximity_to_threshold": str(proximity),
                    "amounts_php": [str(t.php.amount) for t in cluster],
                    "dates": [t.transaction_date.isoformat() for t in cluster],
                    "branches": list(branches),
                    "agents": list(agents),
                    "window_days": window_days,
                },
            )
            index += len(cluster)

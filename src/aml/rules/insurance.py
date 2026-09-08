"""The typologies that are specific to insurance.

Banking-style monitoring transplanted into an insurer misses most of what
matters here, because the laundering vehicle is the *policy*, not the account.
The patterns below are the ones insurance supervisors and the FATF's insurance
typology work keep returning to:

*   **Early termination after a large premium.** The classic. Dirty money is
    paid in as a single premium; the policy is cancelled inside the free-look
    period or surrendered soon after; what comes out is a payment from a
    reputable insurer, with a paper trail that looks like a refund. The
    launderer's cost is the surrender penalty, which is the price of the wash.
*   **Third-party payment.** Someone other than the policyholder pays the
    premium, or the proceeds go to an account in another name. Legitimate cases
    exist — a parent paying for a child — and they are identifiable by a stated
    relationship, which is exactly what this rule asks for.
*   **Overpayment and refund.** A premium is deliberately overpaid and the
    excess refunded, often to a different account. The policy is a laundering
    conduit and the insurance is incidental.
*   **Rapid in-and-out.** A top-up followed quickly by a withdrawal or a policy
    loan against it — the same wash without the cancellation.
*   **Churn in beneficiaries or ownership.** Repeated changes, or a change
    shortly before a payout, are how the beneficiary of the funds is separated
    from the person who put them in.

Each rule takes a materiality floor. Without one they fire on every small
policy in the book and the alerts stop being read, which is the failure mode
that matters most in practice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from aml.model.entities import Transaction
from aml.model.enums import Direction, Severity, TransactionType
from aml.money import Money, php
from aml.rules.base import Finding, PartyRule, RuleContext
from aml.rules.registry import register
from aml.rules.text import fmt_date, fmt_money, policy_label, subject_name, unique

__all__ = [
    "EarlyTermination",
    "ThirdPartyPayer",
    "OverpaymentRefund",
    "RapidInAndOut",
    "BeneficiaryChurn",
]

_PREMIUM_IN = (
    TransactionType.SINGLE_PREMIUM,
    TransactionType.PREMIUM_PAYMENT,
    TransactionType.TOP_UP,
    TransactionType.REINSTATEMENT,
)
_TERMINATION_OUT = (
    TransactionType.FULL_SURRENDER,
    TransactionType.FREE_LOOK_CANCELLATION,
    TransactionType.POLICY_CANCELLATION,
    TransactionType.PREMIUM_REFUND,
)
_WITHDRAWAL_OUT = (
    TransactionType.PARTIAL_WITHDRAWAL,
    TransactionType.POLICY_LOAN,
    TransactionType.DIVIDEND_WITHDRAWAL,
)


def _ratio(numerator: Money, denominator: Money) -> Decimal:
    if denominator.amount == 0:
        return Decimal("0")
    ratio: Decimal = numerator.amount / denominator.amount
    return ratio.quantize(Decimal("0.0001"))


@register
class EarlyTermination(PartyRule):
    rule_id = "str.early_surrender"
    version = "1"
    title = "Policy terminated soon after a large premium"
    description = (
        "A material premium followed by surrender, free-look cancellation or refund "
        "within a short period, recovering most of the money paid in."
    )
    st_codes = ("ST1", "ST5")
    default_params = {
        "within_days": 180,
        "minimum_premium": php(250_000),
        "minimum_recovery_ratio": Decimal("0.50"),
        #: Cancellation inside the free-look period is the strongest form of
        #: this typology: the money is returned in full, so the launderer pays
        #: nothing at all for the wash.
        "free_look_is_critical": True,
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        within_days = ctx.param("within_days", 180)
        minimum_premium = ctx.param("minimum_premium", php(250_000))
        minimum_ratio = ctx.param("minimum_recovery_ratio", Decimal("0.50"))
        free_look_critical = ctx.param("free_look_is_critical", True)

        by_policy: dict[str, list[Transaction]] = {}
        for txn in transactions:
            if txn.policy_id:
                by_policy.setdefault(txn.policy_id, []).append(txn)

        for policy_id, history in sorted(by_policy.items()):
            premiums = [
                t for t in history
                if t.txn_type in _PREMIUM_IN and t.is_monetary and t.php >= minimum_premium
            ]
            exits = [t for t in history if t.txn_type in _TERMINATION_OUT and t.is_monetary]
            if not premiums or not exits:
                continue

            for exit_txn in exits:
                paid_in = [
                    t for t in premiums
                    if t.transaction_date <= exit_txn.transaction_date
                    and (exit_txn.transaction_date - t.transaction_date).days <= within_days
                ]
                if not paid_in:
                    continue
                total_in = self.sum_php(paid_in)
                recovered = _ratio(exit_txn.php, total_in)
                if recovered < minimum_ratio:
                    continue

                gap_days = (exit_txn.transaction_date - paid_in[0].transaction_date).days
                is_free_look = exit_txn.txn_type is TransactionType.FREE_LOOK_CANCELLATION
                if is_free_look and free_look_critical:
                    severity, score = Severity.CRITICAL, Decimal("0.95")
                elif gap_days <= 30:
                    severity, score = Severity.HIGH, Decimal("0.85")
                elif gap_days <= 90:
                    severity, score = Severity.HIGH, Decimal("0.75")
                else:
                    severity, score = Severity.MEDIUM, Decimal("0.62")

                loss = total_in - exit_txn.php
                event = str(exit_txn.txn_type).replace("_", " ").lower()
                narrative = (
                    f"{subject_name(ctx, party_id)} paid {fmt_money(total_in)} into "
                    f"{policy_label(ctx, policy_id)} "
                    f"on {fmt_date(paid_in[0].transaction_date)} and the policy was subject "
                    f"to {event} on {fmt_date(exit_txn.transaction_date)}, {gap_days} days "
                    f"later, returning {fmt_money(exit_txn.php)} — "
                    + (
                        f"{recovered:.1%} of the amount paid in, at a cost of "
                        f"{fmt_money(loss)}"
                        if loss.amount > 0
                        else "the whole of the amount paid in, at no cost"
                    )
                    + ". Placing funds in a policy and withdrawing them shortly afterwards, "
                    "accepting the cost of early termination, has no apparent economic "
                    "justification and is a recognised life-insurance laundering typology."
                )
                if is_free_look:
                    narrative += (
                        " The cancellation was within the free-look period, so the premium "
                        "was returned without penalty."
                    )

                yield Finding(
                    subject_party_id=party_id,
                    title="Early termination following a material premium",
                    narrative=narrative,
                    transaction_ids=tuple(t.txn_id for t in paid_in) + (exit_txn.txn_id,),
                    policy_ids=(policy_id,),
                    severity=severity,
                    score=score,
                    st_codes=("ST1", "ST5"),
                    amount_php=total_in,
                    window_start=paid_in[0].transaction_date,
                    window_end=exit_txn.transaction_date,
                    evidence={
                        "premium_php": str(total_in.amount),
                        "returned_php": str(exit_txn.php.amount),
                        "recovery_ratio": str(recovered),
                        "days_held": gap_days,
                        "exit_type": str(exit_txn.txn_type),
                        "free_look": is_free_look,
                    },
                )


@register
class ThirdPartyPayer(PartyRule):
    rule_id = "str.third_party_payer"
    version = "1"
    title = "Premium paid by, or proceeds paid to, an unrelated third party"
    description = (
        "A material premium settled by someone who is neither the policy owner nor the "
        "insured, with no stated relationship to them, or proceeds directed to such a party."
    )
    st_codes = ("ST1", "ST5")
    default_params = {"minimum_amount": php(100_000)}

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        minimum = ctx.param("minimum_amount", php(100_000))

        for txn in transactions:
            if not txn.is_monetary or txn.php < minimum:
                continue
            counterparty = txn.counterparty
            if not counterparty.is_present:
                continue

            policy = ctx.policy(txn.policy_id)
            related_ids = {party_id}
            if policy is not None:
                related_ids |= {
                    policy.owner_party_id,
                    policy.insured_party_id,
                    policy.payor_party_id,
                    *policy.beneficiary_party_ids,
                }
            related_ids.discard("")
            if counterparty.party_id and counterparty.party_id in related_ids:
                continue
            if counterparty.relationship_to_client.strip():
                # A stated relationship is what makes a parent paying for a
                # child ordinary rather than notable. Recorded, not ignored.
                continue

            inbound = txn.direction is Direction.INBOUND
            role = "paid" if inbound else "received"
            severity = Severity.HIGH if txn.php > ctx.threshold else Severity.MEDIUM
            score = Decimal("0.78") if inbound else Decimal("0.72")
            narrative = (
                f"{counterparty.name or 'An unidentified third party'} {role} "
                f"{fmt_money(txn.php)} "
                f"{'into' if inbound else 'from'} "
                f"{policy_label(ctx, txn.policy_id)} held by "
                f"{subject_name(ctx, party_id)} on {fmt_date(txn.transaction_date)}, with no "
                "relationship to the policyholder recorded. Payment of premiums by, or "
                "settlement of proceeds to, an unrelated third party separates the source of "
                "the funds from the contracting party and requires an explanation."
            )
            if counterparty.country and counterparty.country != "PH":
                narrative += f" The counterparty is in {counterparty.country}."

            yield Finding(
                subject_party_id=party_id,
                title="Unrelated third party to the transaction",
                narrative=narrative,
                transaction_ids=(txn.txn_id,),
                policy_ids=unique((txn.policy_id,)),
                severity=severity,
                score=score,
                st_codes=("ST1", "ST5"),
                amount_php=txn.php,
                window_start=txn.transaction_date,
                window_end=txn.transaction_date,
                evidence={
                    "counterparty": counterparty.as_dict(),
                    "amount_php": str(txn.php.amount),
                    "direction": str(txn.direction),
                    "policy_owner": policy.owner_party_id if policy else "",
                },
            )


@register
class OverpaymentRefund(PartyRule):
    rule_id = "str.overpayment_refund"
    version = "1"
    title = "Premium overpaid and refunded"
    description = (
        "A refund of a material overpayment, particularly where the refund is directed "
        "somewhere other than the source of the original payment."
    )
    st_codes = ("ST1",)
    default_params = {
        "lookback_days": 120,
        "minimum_refund": php(50_000),
        "minimum_refund_ratio": Decimal("0.20"),
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        lookback = ctx.param("lookback_days", 120)
        minimum_refund = ctx.param("minimum_refund", php(50_000))
        minimum_ratio = ctx.param("minimum_refund_ratio", Decimal("0.20"))

        refunds = [
            t
            for t in transactions
            if t.txn_type
            in (TransactionType.OVERPAYMENT_REFUND, TransactionType.PREMIUM_REFUND)
            and t.is_monetary
            and t.php >= minimum_refund
        ]
        for refund in refunds:
            paid_in = [
                t
                for t in transactions
                if t.txn_type in _PREMIUM_IN
                and t.policy_id == refund.policy_id
                and t.transaction_date <= refund.transaction_date
                and (refund.transaction_date - t.transaction_date).days <= lookback
            ]
            if not paid_in:
                continue
            total_in = self.sum_php(paid_in)
            share = _ratio(refund.php, total_in)
            source_accounts = {
                t.counterparty.account_number for t in paid_in if t.counterparty.account_number
            }
            redirected = bool(
                refund.counterparty.account_number
                and source_accounts
                and refund.counterparty.account_number not in source_accounts
            )
            if share < minimum_ratio and not redirected:
                continue

            severity = Severity.HIGH if redirected else Severity.MEDIUM
            score = Decimal("0.84") if redirected else Decimal("0.60")
            narrative = (
                f"{subject_name(ctx, party_id)} paid {fmt_money(total_in)} into "
                f"{policy_label(ctx, refund.policy_id)} and {fmt_money(refund.php)} "
                f"({share:.1%}) was refunded on {fmt_date(refund.transaction_date)}."
            )
            if redirected:
                narrative += (
                    " The refund was settled to an account other than the one the premium "
                    f"came from ({refund.counterparty.account_number} versus "
                    f"{', '.join(sorted(source_accounts))}), which moves the funds to a "
                    "party other than the payer under cover of an insurance refund."
                )
            else:
                narrative += (
                    " Deliberate overpayment followed by a refund uses the policy as a "
                    "conduit rather than as insurance."
                )

            yield Finding(
                subject_party_id=party_id,
                title="Overpayment refunded",
                narrative=narrative,
                transaction_ids=tuple(t.txn_id for t in paid_in) + (refund.txn_id,),
                policy_ids=unique((refund.policy_id,)),
                severity=severity,
                score=score,
                st_codes=("ST1",),
                amount_php=refund.php,
                window_start=paid_in[0].transaction_date,
                window_end=refund.transaction_date,
                evidence={
                    "paid_in_php": str(total_in.amount),
                    "refunded_php": str(refund.php.amount),
                    "refund_ratio": str(share),
                    "refund_redirected": redirected,
                    "source_accounts": sorted(source_accounts),
                    "refund_account": refund.counterparty.account_number,
                },
            )


@register
class RapidInAndOut(PartyRule):
    rule_id = "str.rapid_movement"
    version = "1"
    title = "Funds withdrawn shortly after being paid in"
    description = (
        "A material payment into a policy followed quickly by a withdrawal, policy loan "
        "or dividend withdrawal of most of it, with the policy left in force."
    )
    st_codes = ("ST1", "ST5")
    default_params = {
        "within_days": 45,
        "minimum_amount": php(200_000),
        "minimum_out_ratio": Decimal("0.60"),
    }

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        within_days = ctx.param("within_days", 45)
        minimum = ctx.param("minimum_amount", php(200_000))
        out_ratio = ctx.param("minimum_out_ratio", Decimal("0.60"))

        inflows = [
            t for t in transactions
            if t.txn_type in _PREMIUM_IN and t.is_monetary and t.php >= minimum
        ]
        outflows = [t for t in transactions if t.txn_type in _WITHDRAWAL_OUT and t.is_monetary]

        for inflow in inflows:
            matched = [
                t
                for t in outflows
                if t.policy_id == inflow.policy_id
                and inflow.transaction_date <= t.transaction_date
                and (t.transaction_date - inflow.transaction_date).days <= within_days
            ]
            if not matched:
                continue
            taken_out = self.sum_php(matched)
            share = _ratio(taken_out, inflow.php)
            if share < out_ratio:
                continue
            gap = (matched[0].transaction_date - inflow.transaction_date).days
            severity = Severity.HIGH if gap <= 14 else Severity.MEDIUM
            score = Decimal("0.80") if gap <= 14 else Decimal("0.68")
            kinds = ", ".join(
                sorted({str(t.txn_type).replace("_", " ").lower() for t in matched})
            )
            narrative = (
                f"{subject_name(ctx, party_id)} paid {fmt_money(inflow.php)} into "
                f"{policy_label(ctx, inflow.policy_id)} on "
                f"{fmt_date(inflow.transaction_date)} and withdrew {fmt_money(taken_out)} "
                f"({share:.1%}) within {gap} days by {kinds}, leaving the policy in force. "
                "Money passing through a policy in this way is placement and layering "
                "rather than insurance."
            )
            yield Finding(
                subject_party_id=party_id,
                title="Rapid movement of funds through a policy",
                narrative=narrative,
                transaction_ids=(inflow.txn_id,) + tuple(t.txn_id for t in matched),
                policy_ids=unique((inflow.policy_id,)),
                severity=severity,
                score=score,
                st_codes=("ST1", "ST5"),
                amount_php=taken_out,
                window_start=inflow.transaction_date,
                window_end=matched[-1].transaction_date,
                evidence={
                    "paid_in_php": str(inflow.php.amount),
                    "withdrawn_php": str(taken_out.amount),
                    "out_ratio": str(share),
                    "days_to_first_withdrawal": gap,
                },
            )


@register
class BeneficiaryChurn(PartyRule):
    rule_id = "str.beneficiary_churn"
    version = "1"
    title = "Repeated changes of beneficiary or ownership"
    description = (
        "Several beneficiary, ownership or assignment changes on one policy within a "
        "window, or a change shortly before a material payout."
    )
    st_codes = ("ST5",)
    default_params = {
        "window_days": 365,
        "minimum_count": 3,
        "days_before_payout": 60,
        "payout_minimum": php(500_000),
    }

    _CHANGE_TYPES = (
        TransactionType.BENEFICIARY_CHANGE,
        TransactionType.OWNERSHIP_CHANGE,
        TransactionType.ASSIGNMENT,
    )
    _PAYOUT_TYPES = (
        TransactionType.FULL_SURRENDER,
        TransactionType.PARTIAL_WITHDRAWAL,
        TransactionType.DEATH_CLAIM,
        TransactionType.MATURITY_BENEFIT,
        TransactionType.LIVING_BENEFIT,
    )

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:
        window_days = ctx.param("window_days", 365)
        minimum_count = ctx.param("minimum_count", 3)
        lead_days = ctx.param("days_before_payout", 60)
        payout_minimum = ctx.param("payout_minimum", php(500_000))

        by_policy: dict[str, list[Transaction]] = {}
        for txn in transactions:
            if txn.policy_id:
                by_policy.setdefault(txn.policy_id, []).append(txn)

        for policy_id, history in sorted(by_policy.items()):
            changes = [t for t in history if t.txn_type in self._CHANGE_TYPES]
            payouts = [
                t for t in history
                if t.txn_type in self._PAYOUT_TYPES and t.is_monetary and t.php >= payout_minimum
            ]
            if not changes:
                continue

            recent = [
                t
                for t in changes
                if (ctx.as_of - t.transaction_date).days <= window_days
            ]
            before_payout = [
                (change, payout)
                for change in changes
                for payout in payouts
                if 0 <= (payout.transaction_date - change.transaction_date).days <= lead_days
            ]

            if len(recent) >= minimum_count:
                narrative = (
                    f"{policy_label(ctx, policy_id).capitalize()} held by "
                    f"{subject_name(ctx, party_id)} had {len(recent)} changes of beneficiary "
                    f"or ownership between {fmt_date(recent[0].transaction_date)} and "
                    f"{fmt_date(recent[-1].transaction_date)}. Repeated changes are a way of "
                    "separating the person who paid the funds in from the person who takes "
                    "them out."
                )
                yield Finding(
                    subject_party_id=party_id,
                    title="Repeated beneficiary or ownership changes",
                    narrative=narrative,
                    transaction_ids=tuple(t.txn_id for t in recent),
                    policy_ids=(policy_id,),
                    severity=Severity.MEDIUM,
                    score=Decimal("0.64"),
                    st_codes=("ST5",),
                    window_start=recent[0].transaction_date,
                    window_end=recent[-1].transaction_date,
                    evidence={
                        "change_count": len(recent),
                        "window_days": window_days,
                        "change_types": [str(t.txn_type) for t in recent],
                    },
                )

            for change, payout in before_payout:
                gap = (payout.transaction_date - change.transaction_date).days
                narrative = (
                    f"{policy_label(ctx, policy_id).capitalize()} held by "
                    f"{subject_name(ctx, party_id)} was subject to a "
                    f"{str(change.txn_type).replace('_', ' ').lower()} on "
                    f"{fmt_date(change.transaction_date)}, {gap} days before "
                    f"{fmt_money(payout.php)} was paid out on "
                    f"{fmt_date(payout.transaction_date)}. A change of the party entitled to "
                    "the proceeds immediately before they are paid requires explanation."
                )
                yield Finding(
                    subject_party_id=party_id,
                    title="Beneficiary or ownership changed shortly before a payout",
                    narrative=narrative,
                    transaction_ids=(change.txn_id, payout.txn_id),
                    policy_ids=(policy_id,),
                    severity=Severity.HIGH,
                    score=Decimal("0.79"),
                    st_codes=("ST5",),
                    amount_php=payout.php,
                    window_start=change.transaction_date,
                    window_end=payout.transaction_date,
                    evidence={
                        "change_type": str(change.txn_type),
                        "days_before_payout": gap,
                        "payout_php": str(payout.php.amount),
                    },
                )

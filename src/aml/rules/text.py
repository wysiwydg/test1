"""Narrative helpers.

The narrative is not decoration. It is the part of a suspicious transaction
report a human at the AMLC reads, and the part an analyst under time pressure
will otherwise write badly or not at all. Generating a competent first draft —
who, what, how much, when, on which policy, and why it was flagged — is the
single highest-leverage thing a detection rule does after detecting.

Dates are rendered in full ("02 March 2026") rather than numerically, because
a report read in Manila and a report read anywhere else must not disagree about
whether 03/02 is March or February.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from aml.money import Money
from aml.rules.base import RuleContext

__all__ = ["subject_name", "fmt_money", "fmt_date", "fmt_span", "join", "policy_label"]


def subject_name(ctx: RuleContext, party_id: str) -> str:
    party = ctx.party(party_id)
    if party is None or not party.display_name:
        return f"client {party_id}"
    return str(party.display_name)


def fmt_money(amount: Money | None) -> str:
    return "an unstated amount" if amount is None else str(amount.quantized())


def fmt_date(day: dt.date | None) -> str:
    return "an unstated date" if day is None else day.strftime("%d %B %Y")


def fmt_span(start: dt.date | None, end: dt.date | None) -> str:
    if start and end and start != end:
        return f"between {fmt_date(start)} and {fmt_date(end)}"
    return f"on {fmt_date(end or start)}"


def join(items: Iterable[str], conjunction: str = "and") -> str:
    values = [i for i in items if i]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} {conjunction} {values[-1]}"


def policy_label(ctx: RuleContext, policy_id: str) -> str:
    policy = ctx.policy(policy_id)
    if policy is None:
        return policy_id or "an unidentified policy"
    number = policy.policy_number or policy.policy_id
    product = policy.product_name or str(policy.product_line).replace("_", " ").lower()
    return f"policy {number} ({product})"


def unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)

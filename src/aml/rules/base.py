"""What a rule is, and what it is obliged to produce.

A detection rule here returns :class:`Finding` objects, not booleans. The
difference is the whole design: a boolean tells an analyst that something fired,
and a finding tells them *what happened, to whom, on which transactions, in
which peso amounts, and which statutory circumstance it evidences*. The second
is what an STR is made of; the first is what makes an alert backlog that nobody
can clear.

Every finding therefore carries:

*   the transactions it fired on, so the evidence is re-examinable;
*   a narrative in plain language, which becomes the first draft of the report;
*   the ST codes it evidences, because an STR must name a circumstance;
*   an evidence mapping of the actual numbers the rule compared, so that a
    disagreement about a threshold is settled by reading the alert rather than
    by re-running the rule against data that has since changed.

Rules are pure functions of the book and the configuration. No I/O, no clock of
their own, no database. Two runs over the same extract with the same parameters
produce identical findings — which is what makes an alert reproducible in an
examination two years later, and what lets a proposed threshold change be
backtested against last quarter before anyone turns it on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from aml.config import AmlConfig
from aml.model.entities import Book, Party, Policy, Transaction
from aml.model.enums import Severity
from aml.money import Money

__all__ = ["Finding", "RuleContext", "Rule", "PartyRule", "BookRule"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a rule noticed, with everything needed to act on it."""

    subject_party_id: str
    title: str
    narrative: str
    transaction_ids: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    severity: Severity = Severity.MEDIUM
    score: Decimal = Decimal("0.5")
    st_codes: tuple[str, ...] = ()
    unlawful_activity_codes: tuple[str, ...] = ()
    amount_php: Money | None = None
    window_start: dt.date | None = None
    window_end: dt.date | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: True for a covered transaction, which is reported because of what it is
    #: rather than because anybody finds it suspicious.
    is_covered_transaction: bool = False


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may read.

    Deliberately small. A rule that could reach a database could ask a
    different question on Tuesday than it asked on Monday, and then no alert is
    reproducible.
    """

    config: AmlConfig
    book: Book
    as_of: dt.date
    params: Mapping[str, Any] = field(default_factory=dict)

    def param(self, name: str, default: Any) -> Any:
        """A tuned parameter, falling back to the rule's own default.

        Types follow the default, so ``window_days = "5"`` in a TOML file does
        not turn an integer comparison into a string one.
        """
        value = self.params.get(name, default)
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, int) and not isinstance(value, bool):
            return int(value)
        if isinstance(default, Decimal):
            return Decimal(str(value))
        if isinstance(default, Money):
            return Money.parse(value, default.currency)
        return value

    def party(self, party_id: str) -> Party | None:
        return self.book.party(party_id)

    def policy(self, policy_id: str) -> Policy | None:
        return self.book.policy(policy_id)

    @property
    def threshold(self) -> Money:
        return self.config.thresholds.covered_transaction

    def is_covered_instrument(self, txn: Transaction) -> bool:
        return txn.instrument in self.config.thresholds.covered_instrument_set()


@runtime_checkable
class Rule(Protocol):
    """The contract the engine relies on."""

    rule_id: str
    version: str
    title: str
    description: str
    #: ST codes this rule can evidence. Declared rather than derived so the
    #: catalogue can be printed for the AML manual without running anything.
    st_codes: tuple[str, ...]
    default_params: Mapping[str, Any]

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        ...


class BookRule:
    """Base for rules that look at the whole population at once."""

    rule_id: str = ""
    version: str = "1"
    title: str = ""
    description: str = ""
    st_codes: tuple[str, ...] = ()
    default_params: Mapping[str, Any] = {}

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers shared by the concrete rules ----------------------------

    @staticmethod
    def sum_php(transactions: Iterable[Transaction]) -> Money:
        total = Money.zero()
        for txn in transactions:
            total = total + txn.php
        return total

    @staticmethod
    def in_window(
        transactions: Sequence[Transaction], end: dt.date, days: int
    ) -> list[Transaction]:
        start = end - dt.timedelta(days=days)
        return [t for t in transactions if start <= t.transaction_date <= end]


class PartyRule(BookRule):
    """Base for rules evaluated one client at a time.

    Most typologies are per-client, and iterating the population here rather
    than in every rule keeps each rule to the part that is actually its own
    logic.
    """

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for party_id in ctx.book.party_ids():
            yield from self.evaluate_party(ctx, party_id, ctx.book.for_party(party_id))

    def evaluate_party(
        self, ctx: RuleContext, party_id: str, transactions: Sequence[Transaction]
    ) -> Iterable[Finding]:  # pragma: no cover - abstract
        raise NotImplementedError

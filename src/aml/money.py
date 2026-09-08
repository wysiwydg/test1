"""Money, and the exchange rate that decides whether a report is filed.

Two things in this package are load-bearing enough to deserve their own module,
and this is one of them. The covered-transaction threshold is a *peso* amount,
but a Philippine life insurer sells dollar-denominated policies routinely, so
the question "is this transaction covered?" is only ever answered after a
conversion. That makes the rate part of the compliance decision, not a display
concern:

*   **No floats.** Premiums are summed across a day, a policy and a client
    before being compared against a statutory threshold. Float drift makes that
    comparison irreproducible, and "irreproducible" is the worst property a
    filing decision can have when an examiner asks why a transaction was not
    reported.
*   **No implicit currency.** Adding USD to PHP raises. A silent coercion here
    would understate a client's daily aggregate by a factor of about 56 and
    suppress a report that should have been filed.
*   **No guessed rate.** A missing rate raises :class:`MissingRate` rather than
    falling back to 1.0 or to yesterday's number. The rate actually used is
    recorded on the converted amount, so a filing can be re-derived years later
    from the evidence rather than from whatever the table holds by then.

The rate source is the institution's own reference rate — in practice the BSP
reference rate for the transaction date. This module does not fetch it; it
holds what was loaded and refuses to invent the rest.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

__all__ = [
    "PHP",
    "CENTAVO",
    "CurrencyMismatch",
    "MissingRate",
    "Money",
    "php",
    "Conversion",
    "RateTable",
]

PHP = "PHP"
CENTAVO = Decimal("0.01")


class CurrencyMismatch(ValueError):
    """Arithmetic was attempted across two currencies."""


class MissingRate(LookupError):
    """No reference rate is on file for a currency and date."""


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one currency.

    Comparisons and arithmetic are defined only within a currency. Everything
    that needs to compare across currencies goes through :class:`RateTable`,
    which leaves a record of the rate it used.
    """

    amount: Decimal
    currency: str = PHP

    def __post_init__(self) -> None:
        amount = self.amount
        if not isinstance(amount, Decimal):
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
                raise ValueError(f"not a monetary amount: {self.amount!r}") from exc
        if not amount.is_finite():
            raise ValueError(f"not a finite monetary amount: {self.amount!r}")
        code = str(self.currency).strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"not an ISO 4217 currency code: {self.currency!r}")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", code)

    # -- construction ----------------------------------------------------

    @classmethod
    def parse(cls, value: Any, currency: str = PHP) -> Money:
        """Read an amount from source data.

        Accepts what extracts actually contain: ``"1,250,000.00"``,
        ``"PHP 500000"``, ``(500000, "USD")``, a bare number. Rejects anything
        else loudly rather than defaulting to zero, because a premium silently
        read as zero is a covered transaction that never gets reported.
        """
        if isinstance(value, Money):
            return value
        if isinstance(value, tuple) and len(value) == 2:
            return cls(Decimal(str(value[0]).replace(",", "").strip()), str(value[1]))
        if isinstance(value, str):
            text = value.strip().replace(",", "").replace("_", "")
            if not text:
                raise ValueError("empty monetary amount")
            head, _, tail = text.partition(" ")
            if tail and head.isalpha():
                return cls(Decimal(tail.strip()), head)
            if head[:3].isalpha() and len(head) > 3:
                return cls(Decimal(head[3:]), head[:3])
            return cls(Decimal(text), currency)
        return cls(Decimal(str(value)), currency)

    @classmethod
    def zero(cls, currency: str = PHP) -> Money:
        return cls(Decimal("0"), currency)

    # -- arithmetic ------------------------------------------------------

    def _same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"cannot combine {self.currency} and {other.currency}; convert first"
            )

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        return Money(self.amount * Decimal(str(factor)), self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount >= other.amount

    # -- presentation ----------------------------------------------------

    def quantized(self, exponent: Decimal = CENTAVO) -> Money:
        """Round half-up to the given exponent.

        Half-up rather than banker's rounding: it is what the reporting forms
        and the finance ledger both use, and a report that disagrees with the
        ledger by one centavo is a query from the regulator.
        """
        return Money(self.amount.quantize(exponent, rounding=ROUND_HALF_UP), self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        return f"{self.currency} {self.quantized().amount:,}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Money({self.amount!s}, {self.currency!r})"


def php(value: Any) -> Money:
    """Shorthand for a peso amount."""
    return Money.parse(value, PHP)


@dataclass(frozen=True, slots=True)
class Conversion:
    """What a converted amount is, and how it got that way.

    Carried alongside the original so a report can show both — the regulator
    asks for the transaction in its original currency *and* its peso
    equivalent, and the rate is the only thing that reconciles them.
    """

    original: Money
    converted: Money
    rate: Decimal
    rate_date: dt.date
    rate_source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_amount": str(self.original.quantized().amount),
            "original_currency": self.original.currency,
            "converted_amount": str(self.converted.quantized().amount),
            "converted_currency": self.converted.currency,
            "rate": str(self.rate),
            "rate_date": self.rate_date.isoformat(),
            "rate_source": self.rate_source,
        }


@dataclass
class RateTable:
    """Reference rates to the peso, by currency and date.

    Lookup falls back to the most recent rate *on or before* the transaction
    date, within :attr:`max_staleness_days`. Falling back is normal — weekends
    and holidays have no published rate — but falling back indefinitely is not,
    because a rate from six months ago converts a transaction at a price that
    never existed on the day it happened.
    """

    base: str = PHP
    source: str = "BSP reference rate"
    max_staleness_days: int = 7
    _rates: dict[str, dict[dt.date, Decimal]] = field(default_factory=dict)

    def add(self, currency: str, on: dt.date, rate: Decimal | str | float) -> None:
        code = str(currency).strip().upper()
        self._rates.setdefault(code, {})[on] = Decimal(str(rate))

    @classmethod
    def from_csv(cls, path: str | pathlib.Path, **kwargs: Any) -> RateTable:
        """Load ``currency,date,rate`` rows.

        The shape a treasury team can produce from any system without being
        asked to build an integration first.
        """
        table = cls(**kwargs)
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                table.add(
                    row["currency"],
                    dt.date.fromisoformat(row["date"].strip()),
                    row["rate"],
                )
        return table

    def rate_for(self, currency: str, on: dt.date) -> tuple[Decimal, dt.date]:
        code = str(currency).strip().upper()
        if code == self.base:
            return Decimal("1"), on
        quotes = self._rates.get(code)
        if not quotes:
            raise MissingRate(f"no {code}/{self.base} rate on file at all")
        usable = [d for d in quotes if d <= on]
        if not usable:
            raise MissingRate(f"no {code}/{self.base} rate on or before {on.isoformat()}")
        latest = max(usable)
        staleness = (on - latest).days
        if staleness > self.max_staleness_days:
            raise MissingRate(
                f"nearest {code}/{self.base} rate is {staleness} days stale "
                f"({latest.isoformat()} for {on.isoformat()}); "
                "load the rate for the transaction date"
            )
        return quotes[latest], latest

    def convert(self, amount: Money, on: dt.date, to: str | None = None) -> Conversion:
        """Convert an amount, recording the rate that did it."""
        target = (to or self.base).upper()
        if amount.currency == target:
            return Conversion(amount, amount, Decimal("1"), on, "identity")
        if target != self.base:
            raise MissingRate(f"only conversion to {self.base} is supported, not {target}")
        rate, rate_date = self.rate_for(amount.currency, on)
        return Conversion(
            original=amount,
            converted=Money(amount.amount * rate, target).quantized(),
            rate=rate,
            rate_date=rate_date,
            rate_source=self.source,
        )

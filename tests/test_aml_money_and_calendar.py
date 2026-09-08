"""The two pieces every filing decision rests on: the amount and the date."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from aml.money import CurrencyMismatch, MissingRate, Money, RateTable, php
from aml.phcalendar import PhilippineCalendar, easter_sunday


def test_amounts_are_exact_and_do_not_drift():
    total = sum((php("0.10") for _ in range(10)), php(0))
    assert total.amount == Decimal("1.00")


def test_currencies_never_mix_silently():
    with pytest.raises(CurrencyMismatch):
        php(1) + Money(Decimal("1"), "USD")


def test_amounts_are_parsed_the_way_extracts_write_them():
    assert Money.parse("1,250,000.00") == php(1_250_000)
    assert Money.parse("USD 10000").currency == "USD"
    assert Money.parse((500, "SGD")) == Money(Decimal("500"), "SGD")


def test_conversion_records_the_rate_that_was_used():
    rates = RateTable()
    rates.add("USD", dt.date(2026, 3, 2), "56.42")
    conversion = rates.convert(Money(Decimal("10000"), "USD"), dt.date(2026, 3, 4))
    assert conversion.converted == php("564200.00")
    assert conversion.rate_date == dt.date(2026, 3, 2)
    assert conversion.as_dict()["rate"] == "56.42"


def test_a_stale_rate_is_refused_rather_than_used():
    rates = RateTable(max_staleness_days=7)
    rates.add("USD", dt.date(2026, 1, 2), "56.00")
    with pytest.raises(MissingRate):
        rates.convert(Money(Decimal("10000"), "USD"), dt.date(2026, 3, 4))


def test_missing_currency_is_an_error_not_a_guess():
    with pytest.raises(MissingRate):
        RateTable().convert(Money(Decimal("1"), "EUR"), dt.date(2026, 3, 4))


def test_easter_derived_holidays():
    assert easter_sunday(2026) == dt.date(2026, 4, 5)
    calendar = PhilippineCalendar()
    assert calendar.holiday_name(dt.date(2026, 4, 3)) == "Good Friday"
    assert not calendar.is_working_day(dt.date(2026, 4, 2))  # Maundy Thursday


def test_five_working_days_counts_from_the_day_after():
    calendar = PhilippineCalendar()
    # Monday 2 March 2026 + five working days = Monday 9 March.
    assert calendar.add_working_days(dt.date(2026, 3, 2), 5) == dt.date(2026, 3, 9)


def test_the_deadline_stretches_across_holy_week():
    calendar = PhilippineCalendar()
    deadline = calendar.add_working_days(dt.date(2026, 3, 30), 5)
    assert deadline == dt.date(2026, 4, 8)
    assert (deadline - dt.date(2026, 3, 30)).days == 9  # nine calendar days for five working


def test_proclaimed_holidays_change_the_deadline():
    plain = PhilippineCalendar()
    with_eid = PhilippineCalendar(proclaimed={dt.date(2026, 3, 20): "Eid'l Fitr"})
    assert plain.add_working_days(dt.date(2026, 3, 17), 5) == dt.date(2026, 3, 24)
    assert with_eid.add_working_days(dt.date(2026, 3, 17), 5) == dt.date(2026, 3, 25)


def test_years_without_proclamations_are_reported():
    calendar = PhilippineCalendar(proclaimed={dt.date(2026, 3, 20): "Eid'l Fitr"})
    assert calendar.unproclaimed_years([2026, 2027]) == [2027]


def test_working_days_between_is_signed():
    calendar = PhilippineCalendar()
    assert calendar.working_days_between(dt.date(2026, 3, 2), dt.date(2026, 3, 9)) == 5
    assert calendar.working_days_between(dt.date(2026, 3, 9), dt.date(2026, 3, 2)) == -5

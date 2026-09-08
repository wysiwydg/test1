"""Philippine working days, and the filing deadline that depends on them.

The AMLA gives a covered person **five working days** to file, counted from the
occurrence of a covered transaction or from the determination that a
transaction is suspicious. Every deadline in this system is therefore a
function of a calendar, and a calendar that is wrong by one day turns an
on-time filing into a late one — which is a finding in an examination, not a
rounding error.

Three properties this calendar is careful about:

*   **Weekends are not the only non-working days.** A five-working-day clock
    that runs through Holy Week is short by two days.
*   **Some holidays cannot be computed.** Eid'l Fitr and Eid'l Adha follow the
    Islamic calendar and are fixed by proclamation each year, and the
    President declares additional special days routinely. Those come from
    configuration; nothing here pretends to derive them.
*   **Special non-working days count as non-working by default.** The relevant
    clock is a banking one, and the banks are shut. It is configurable because
    an institution whose compliance unit works those days may reasonably say so.

The proclaimed-holiday list needs loading each year. :func:`unproclaimed_years`
exists so that omission surfaces as a warning at the start of a year rather than
as a deadline computed on an optimistic calendar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = [
    "MANILA",
    "now_manila",
    "easter_sunday",
    "PhilippineCalendar",
]

try:  # pragma: no cover - exercised by whichever branch the platform takes
    from zoneinfo import ZoneInfo

    MANILA: dt.tzinfo = ZoneInfo("Asia/Manila")
except Exception:  # pragma: no cover - no tz database on the host
    # The Philippines has observed UTC+08:00 with no daylight saving since 1978,
    # so this fallback is exact for every date this system will ever handle.
    MANILA = dt.timezone(dt.timedelta(hours=8), "PHT")


def now_manila() -> dt.datetime:
    """Current time in the reporting timezone.

    Report dates, cut-offs and deadlines are all Manila-local. Deriving them
    from a UTC clock puts every transaction between 16:00 and 24:00 UTC on the
    wrong reporting day.
    """
    return dt.datetime.now(tz=MANILA)


def easter_sunday(year: int) -> dt.date:
    """Gregorian Easter, by the anonymous algorithm.

    Maundy Thursday and Good Friday are regular holidays and both are derived
    from this.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _last_monday_of_august(year: int) -> dt.date:
    day = dt.date(year, 8, 31)
    return day - dt.timedelta(days=(day.weekday() - 0) % 7)


@dataclass
class PhilippineCalendar:
    """Working days for filing-deadline arithmetic.

    ``proclaimed`` carries what cannot be derived: the movable Islamic
    holidays, and the special days declared for a given year. Load it from
    configuration; it is keyed by date so a later proclamation simply replaces
    an entry.
    """

    proclaimed: dict[dt.date, str] = field(default_factory=dict)
    #: Special (non-regular) national days such as All Saints' Day and 31
    #: December, on which banks are closed.
    observe_special_days: bool = True
    #: Days the compliance unit does not work beyond the national calendar,
    #: e.g. a company-wide shutdown week.
    institution_closures: dict[dt.date, str] = field(default_factory=dict)

    def regular_holidays(self, year: int) -> dict[dt.date, str]:
        """Regular holidays fixed by law, plus the two derived from Easter."""
        easter = easter_sunday(year)
        holidays = {
            dt.date(year, 1, 1): "New Year's Day",
            easter - dt.timedelta(days=3): "Maundy Thursday",
            easter - dt.timedelta(days=2): "Good Friday",
            dt.date(year, 4, 9): "Araw ng Kagitingan",
            dt.date(year, 5, 1): "Labor Day",
            dt.date(year, 6, 12): "Independence Day",
            _last_monday_of_august(year): "National Heroes Day",
            dt.date(year, 11, 30): "Bonifacio Day",
            dt.date(year, 12, 25): "Christmas Day",
            dt.date(year, 12, 30): "Rizal Day",
        }
        return holidays

    def special_days(self, year: int) -> dict[dt.date, str]:
        """Recurring special (non-working) days on which banks are closed."""
        if not self.observe_special_days:
            return {}
        easter = easter_sunday(year)
        return {
            easter - dt.timedelta(days=1): "Black Saturday",
            dt.date(year, 8, 21): "Ninoy Aquino Day",
            dt.date(year, 11, 1): "All Saints' Day",
            dt.date(year, 12, 8): "Feast of the Immaculate Conception",
            dt.date(year, 12, 31): "Last Day of the Year",
        }

    def holidays(self, year: int) -> dict[dt.date, str]:
        """Everything non-working in a year that is not a weekend."""
        merged = self.regular_holidays(year)
        merged.update(self.special_days(year))
        merged.update({d: n for d, n in self.proclaimed.items() if d.year == year})
        merged.update({d: n for d, n in self.institution_closures.items() if d.year == year})
        return merged

    def holiday_name(self, day: dt.date) -> str | None:
        return self.holidays(day.year).get(day)

    def is_weekend(self, day: dt.date) -> bool:
        return day.weekday() >= 5

    def is_working_day(self, day: dt.date) -> bool:
        return not self.is_weekend(day) and day not in self.holidays(day.year)

    def next_working_day(self, day: dt.date) -> dt.date:
        candidate = day + dt.timedelta(days=1)
        while not self.is_working_day(candidate):
            candidate += dt.timedelta(days=1)
        return candidate

    def add_working_days(self, start: dt.date, count: int) -> dt.date:
        """The date ``count`` working days after ``start``.

        Counting begins the day *after* ``start``: a covered transaction on a
        Monday with a five-working-day clock is due the following Monday, not
        the Friday. The trigger day itself is not one of the five.
        """
        if count < 0:
            raise ValueError("count must not be negative")
        day = start
        for _ in range(count):
            day = self.next_working_day(day)
        return day

    def working_days_between(self, start: dt.date, end: dt.date) -> int:
        """Working days strictly after ``start`` up to and including ``end``.

        Negative when ``end`` precedes ``start``, which is what makes it usable
        directly as "days remaining" against a deadline.
        """
        if end == start:
            return 0
        step = 1 if end > start else -1
        first, last = (start, end) if end > start else (end, start)
        day, count = first, 0
        while day < last:
            day += dt.timedelta(days=1)
            if self.is_working_day(day):
                count += 1
        return count * step

    def unproclaimed_years(self, years: Iterable[int]) -> list[int]:
        """Years with no proclaimed holidays loaded.

        Every year has at least Eid'l Fitr and Eid'l Adha. A year with none
        loaded is a calendar that will silently treat those as working days.
        """
        loaded = {d.year for d in self.proclaimed}
        return sorted(y for y in years if y not in loaded)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PhilippineCalendar:
        def dates(key: str) -> dict[dt.date, str]:
            value = raw.get(key) or {}
            if not isinstance(value, Mapping):
                raise ValueError(f"calendar.{key} must be a table of date = name")
            return {
                dt.date.fromisoformat(str(day)): str(name) for day, name in value.items()
            }

        proclaimed = dates("proclaimed_holidays")
        closures = dates("institution_closures")
        return cls(
            proclaimed=proclaimed,
            observe_special_days=bool(raw.get("observe_special_days", True)),
            institution_closures=closures,
        )

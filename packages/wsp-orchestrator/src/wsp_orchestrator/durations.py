"""ISO 8601 durations and update cadences (PLAN §6.5, §6.7).

Two kinds of cadence exist:

* :class:`CalendarCadence` - anchored to a day of the month. The global ``acquirer.cycle`` is
  calendar-anchored ("shortly after the 1st-of-month Wikimedia dumps finish, about mid-month";
  quarterly runs in Jan/Apr/Jul/Oct). Whole-month registry durations that divide a year (``P1M``,
  ``P2M``, ``P3M``, ``P6M``, ``P1Y``) are calendar-anchored as well.
* :class:`IntervalCadence` - a fixed duration after the last success (``P1W``, ``P10D``, ``PT6H``).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from wsp_orchestrator.clock import ensure_utc

_DAYS_PER_MONTH = 365.2425 / 12
_RE = re.compile(
    r"^P(?!$)(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class DurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IsoDuration:
    """A calendar-aware duration: months (years fold into months) plus an exact remainder."""

    months: int = 0
    exact: timedelta = timedelta()

    @classmethod
    def parse(cls, text: str) -> IsoDuration:
        m = _RE.match(text.strip().upper())
        if not m:
            raise DurationError(f"not an ISO 8601 duration: {text!r}")
        g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
        dur = cls(
            months=g["years"] * 12 + g["months"],
            exact=timedelta(
                weeks=g["weeks"], days=g["days"], hours=g["hours"], minutes=g["minutes"], seconds=g["seconds"]
            ),
        )
        if dur.months == 0 and dur.exact == timedelta():
            raise DurationError(f"zero duration: {text!r}")
        return dur

    def add_to(self, when: datetime) -> datetime:
        """Add calendar months (clamping the day, e.g. Jan 31 + P1M = Feb 28/29) then the exact part."""
        return add_months(when, self.months) + self.exact

    def approx(self) -> timedelta:
        """Average length, for comparisons such as "stale for 3x the cadence"."""
        return timedelta(days=self.months * _DAYS_PER_MONTH) + self.exact

    def __str__(self) -> str:
        out = "P"
        years, months = divmod(self.months, 12)
        if years:
            out += f"{years}Y"
        if months:
            out += f"{months}M"
        if self.exact.days:
            out += f"{self.exact.days}D"
        secs = self.exact.seconds
        if secs:
            h, rem = divmod(secs, 3600)
            mi, s = divmod(rem, 60)
            out += "T" + (f"{h}H" if h else "") + (f"{mi}M" if mi else "") + (f"{s}S" if s else "")
        return out


def add_months(when: datetime, months: int) -> datetime:
    if months == 0:
        return when
    idx = when.year * 12 + (when.month - 1) + months
    year, month0 = divmod(idx, 12)
    day = min(when.day, calendar.monthrange(year, month0 + 1)[1])
    return when.replace(year=year, month=month0 + 1, day=day)


class Cadence(Protocol):
    def next_due(self, last_success: datetime | None) -> datetime | None:
        """When the job is next due. ``None`` means "never ran: due immediately" (catch-up)."""
        ...

    def approx_period(self) -> timedelta: ...


@dataclass(frozen=True, slots=True)
class IntervalCadence:
    every: IsoDuration

    def next_due(self, last_success: datetime | None) -> datetime | None:
        return None if last_success is None else self.every.add_to(ensure_utc(last_success))

    def approx_period(self) -> timedelta:
        return self.every.approx()

    def __str__(self) -> str:
        return f"every {self.every}"


@dataclass(frozen=True, slots=True)
class CalendarCadence:
    """Due on ``anchor_day`` of every ``months``-th month (months counted from January)."""

    months: int = 1
    anchor_day: int = 15
    anchor_hour: int = 0

    def __post_init__(self) -> None:
        if self.months <= 0 or 12 % self.months:
            raise DurationError(f"calendar cadence must divide a year, got {self.months} months")
        if not 1 <= self.anchor_day <= 28:
            raise DurationError("anchor_day must be 1..28 so that it exists in every month")
        if not 0 <= self.anchor_hour <= 23:
            raise DurationError("anchor_hour must be 0..23")

    def anchor_in(self, year: int, month: int) -> datetime:
        return datetime(year, month, self.anchor_day, self.anchor_hour, tzinfo=UTC)

    def next_due(self, last_success: datetime | None) -> datetime | None:
        if last_success is None:
            return None
        last = ensure_utc(last_success)
        # First anchor strictly after ``last``: start from the anchor month at or before last.month.
        month0 = (last.month - 1) - ((last.month - 1) % self.months)
        candidate = self.anchor_in(last.year, month0 + 1)
        while candidate <= last:
            candidate = add_months(candidate, self.months)
        return candidate

    def approx_period(self) -> timedelta:
        return timedelta(days=self.months * _DAYS_PER_MONTH)

    def __str__(self) -> str:
        return f"calendar every {self.months} month(s) on day {self.anchor_day}"


def cadence_for_cycle(cycle: str, *, anchor_day: int = 15) -> CalendarCadence:
    """The global ``acquirer.cycle`` setting (PLAN §6.7)."""
    match cycle:
        case "monthly":
            return CalendarCadence(1, anchor_day)
        case "quarterly":
            return CalendarCadence(3, anchor_day)
        case _:
            raise DurationError(f"acquirer.cycle must be 'monthly' or 'quarterly', got {cycle!r}")


def cadence_from_duration(text: str, *, anchor_day: int = 15) -> CalendarCadence | IntervalCadence:
    """Registry ``update.cadence``: whole months dividing a year are calendar-anchored, else an interval."""
    dur = IsoDuration.parse(text)
    if dur.exact == timedelta() and dur.months and 12 % dur.months == 0:
        return CalendarCadence(dur.months, anchor_day)
    return IntervalCadence(dur)

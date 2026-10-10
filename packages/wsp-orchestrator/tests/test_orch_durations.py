from datetime import UTC, datetime, timedelta

import pytest

from wsp_orchestrator.durations import (
    CalendarCadence,
    DurationError,
    IntervalCadence,
    IsoDuration,
    add_months,
    cadence_for_cycle,
    cadence_from_duration,
)


def dt(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "months", "exact"),
    [
        ("P1M", 1, timedelta()),
        ("P3M", 3, timedelta()),
        ("P1Y", 12, timedelta()),
        ("P1W", 0, timedelta(weeks=1)),
        ("P60D", 0, timedelta(days=60)),
        ("PT6H", 0, timedelta(hours=6)),
        ("P1Y2M3DT4H5M6S", 14, timedelta(days=3, hours=4, minutes=5, seconds=6)),
    ],
)
def test_parse(text: str, months: int, exact: timedelta) -> None:
    d = IsoDuration.parse(text)
    assert (d.months, d.exact) == (months, exact)
    assert IsoDuration.parse(str(d)) == d


@pytest.mark.parametrize("bad", ["", "P", "PT", "1M", "P1.5M", "P0D", "monthly"])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(DurationError):
        IsoDuration.parse(bad)


def test_add_months_clamps_day() -> None:
    assert add_months(dt(2026, 1, 31), 1) == dt(2026, 2, 28)
    assert add_months(dt(2028, 1, 31), 1) == dt(2028, 2, 29)
    assert add_months(dt(2026, 11, 15), 3) == dt(2027, 2, 15)
    assert IsoDuration.parse("P1M").approx() == timedelta(days=365.2425 / 12)


def test_monthly_calendar_cadence() -> None:
    c = cadence_for_cycle("monthly")
    assert c.next_due(None) is None  # never ran: due now
    assert c.next_due(dt(2026, 10, 14)) == dt(2026, 10, 15)
    assert c.next_due(dt(2026, 10, 15)) == dt(2026, 11, 15)
    assert c.next_due(dt(2026, 10, 20)) == dt(2026, 11, 15)
    assert c.next_due(dt(2026, 12, 31)) == dt(2027, 1, 15)


def test_quarterly_calendar_cadence_runs_jan_apr_jul_oct() -> None:
    c = cadence_for_cycle("quarterly")
    assert c.next_due(dt(2026, 10, 16)) == dt(2027, 1, 15)
    assert c.next_due(dt(2026, 10, 14)) == dt(2026, 10, 15)
    assert c.next_due(dt(2026, 11, 1)) == dt(2027, 1, 15)
    assert c.next_due(dt(2027, 2, 1)) == dt(2027, 4, 15)
    assert c.next_due(dt(2027, 6, 30)) == dt(2027, 7, 15)


def test_cycle_validation() -> None:
    with pytest.raises(DurationError):
        cadence_for_cycle("weekly")
    with pytest.raises(DurationError):
        CalendarCadence(5)
    with pytest.raises(DurationError):
        CalendarCadence(1, anchor_day=31)


def test_registry_durations() -> None:
    assert cadence_from_duration("P3M") == CalendarCadence(3, 15)
    assert cadence_from_duration("P1Y", anchor_day=2) == CalendarCadence(12, 2)
    assert cadence_from_duration("P1W") == IntervalCadence(IsoDuration.parse("P1W"))
    assert cadence_from_duration("P5M") == IntervalCadence(IsoDuration.parse("P5M"))
    weekly = cadence_from_duration("P1W")
    assert weekly.next_due(dt(2026, 10, 1)) == dt(2026, 10, 8)
    assert weekly.approx_period() == timedelta(weeks=1)

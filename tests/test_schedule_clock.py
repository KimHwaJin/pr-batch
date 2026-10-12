"""Deterministic calendar semantics, boundary rules and large missed ranges."""

from datetime import UTC, datetime, timedelta

import pytest
from croniter import croniter

from pr_batch.schedule_clock import SEOUL, ScheduleClock
from pr_batch.schemas import ScheduleConfig


def dt(value):
    return datetime.fromisoformat(value)


def clock(definition, **options):
    return ScheduleClock(ScheduleConfig(definition=definition, **options))


def test_interval_anchor_precision_and_exclusive_end():
    anchor = dt("2026-10-12T10:00:00.123456+09:00")
    schedule = clock(
        {"kind": "INTERVAL", "anchor": anchor, "seconds": 60},
        start_at=anchor + timedelta(seconds=30),
        end_at=anchor + timedelta(minutes=3),
    )
    assert schedule.find(anchor) == anchor + timedelta(minutes=1)
    assert schedule.find(anchor + timedelta(minutes=1)) == (
        anchor + timedelta(minutes=2)
    )
    assert schedule.find(anchor + timedelta(minutes=2)) is None
    assert schedule.find(anchor + timedelta(days=1), reverse=True) == (
        anchor + timedelta(minutes=2)
    )
    assert schedule.count(anchor, anchor + timedelta(days=1)) == 2


def test_once_and_activation_time_boundary():
    at = dt("2026-10-12T10:00:00+09:00")
    schedule = clock({"kind": "ONCE", "at": at})
    assert schedule.find(at) is None
    assert schedule.find(at, inclusive=True) == at
    assert schedule.find(at - timedelta(seconds=1)) == at
    assert schedule.count(at, at) == 1
    assert schedule.preview(at) == []


@pytest.mark.parametrize(
    "definition,start,expected",
    [
        (
            {"kind": "CALENDAR", "frequency": "DAILY", "at": "08:30:15"},
            "2026-10-12T08:30:14+09:00",
            "2026-10-12T08:30:15+09:00",
        ),
        (
            {
                "kind": "CALENDAR",
                "frequency": "WEEKLY",
                "at": "08:00",
                "weekdays": [0, 4],
            },
            "2026-10-12T08:00:00+09:00",
            "2026-10-16T08:00:00+09:00",
        ),
        (
            {
                "kind": "CALENDAR",
                "frequency": "MONTHLY",
                "at": "08:00",
                "month_days": [31],
            },
            "2026-01-31T08:00:00+09:00",
            "2026-03-31T08:00:00+09:00",
        ),
        (
            {
                "kind": "CALENDAR",
                "frequency": "MONTHLY",
                "at": "08:00",
                "last_day": True,
            },
            "2028-01-31T08:00:00+09:00",
            "2028-02-29T08:00:00+09:00",
        ),
        (
            {"kind": "CRON", "expression": "0 8 * * 0,7"},
            "2026-10-12T00:00:00+09:00",
            "2026-10-18T08:00:00+09:00",
        ),
        (
            {"kind": "CRON", "expression": "0 8 1 * 1"},
            "2026-10-02T00:00:00+09:00",
            "2026-10-05T08:00:00+09:00",
        ),
        (
            {"kind": "CRON", "expression": "0 0 29 2 *"},
            "2096-03-01T00:00:00+09:00",
            "2104-02-29T00:00:00+09:00",
        ),
    ],
)
def test_calendar_and_cron_occurrences(definition, start, expected):
    schedule = clock(definition)
    result = schedule.find(dt(start))
    assert result == dt(expected)
    assert result.tzinfo == UTC
    assert schedule.find(result, inclusive=True) == result
    assert schedule.find(result, reverse=True, inclusive=True) == result


def test_month_end_is_not_duplicated_when_also_selected_as_day():
    schedule = clock(
        {
            "kind": "CALENDAR",
            "frequency": "MONTHLY",
            "at": "08:00",
            "last_day": True,
            "month_days": [31],
        }
    )
    assert (
        schedule.count(
            dt("2026-01-01T00:00:00+09:00"), dt("2026-12-31T23:59:59+09:00")
        )
        == 12
    )


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * *",
        "*/17 8-19 * * 1-5",
        "5,35 2,18 1,15 * 1",
        "0 0 29 2 *",
        "0 8 */2 * 0,7",
        "0 8 1-31 * 1",
        "0 0 * 11-2 5-1",
        "0 0 31 * *",
    ],
)
def test_next_previous_and_count_agree_with_croniter(expression):
    schedule = clock({"kind": "CRON", "expression": expression})
    start = dt("2026-10-12T13:27:11+09:00").astimezone(SEOUL)
    forward, reverse = croniter(expression, start), croniter(expression, start)
    next_at, prev_at = start, start
    forward_values = []
    for _ in range(25):
        next_at = schedule.find(next_at)
        prev_at = schedule.find(prev_at, reverse=True)
        assert next_at == forward.get_next(datetime)
        assert prev_at == reverse.get_prev(datetime)
        forward_values.append(next_at)
    assert schedule.count(forward_values[0], forward_values[-1]) == 25


def test_large_minutely_window_counts_without_expanding_occurrences():
    schedule = clock({"kind": "CRON", "expression": "* * * * *"})
    start = dt("2026-01-01T00:00:00+09:00")
    end = dt("2036-01-01T00:00:00+09:00")
    assert schedule.count(start, end - timedelta(microseconds=1)) == (
        int((end - start).total_seconds()) // 60
    )


def test_window_start_inclusive_end_exclusive_and_empty():
    start = dt("2026-10-12T08:00:00+09:00")
    end = start + timedelta(days=2)
    schedule = clock(
        {"kind": "CALENDAR", "frequency": "DAILY", "at": "08:00"},
        start_at=start,
        end_at=end,
    )
    assert schedule.preview(start - timedelta(seconds=1)) == [
        start,
        start + timedelta(days=1),
    ]
    assert schedule.count(start, end) == 2
    assert schedule.count(end, start) == 0
    assert schedule.find(end) is None

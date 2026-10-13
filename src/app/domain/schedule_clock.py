"""Seoul wall-clock schedules with bounded-by-days occurrence counting."""

from bisect import bisect_left, bisect_right
from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from app.schemas.schedule import Calendar, Cron, Interval, Once, Schedule

EPSILON = timedelta(microseconds=1)
SEOUL = ZoneInfo("Asia/Seoul")


class ScheduleClock:
    def __init__(self, config: Schedule) -> None:
        self.config = config
        self.definition = config.definition
        self.fields: list[list[int | Literal["*", "l"]]] = []
        self.times: list[time] = []
        if isinstance(self.definition, Cron):
            fields, _ = croniter.expand(self.definition.expression)
            self.fields = fields
            minutes = range(60) if "*" in fields[0] else fields[0]
            hours = range(24) if "*" in fields[1] else fields[1]
            self.times = sorted(
                time(int(h), int(m)) for h in hours for m in minutes
            )
        elif isinstance(self.definition, Calendar):
            self.times = [self.definition.at]

    def matches_day(self, day: date) -> bool:
        definition = self.definition
        if isinstance(definition, Calendar):
            if definition.frequency == "DAILY":
                return True
            if definition.frequency == "WEEKLY":
                return day.weekday() in definition.weekdays
            return day.day in definition.month_days or (
                definition.last_day
                and day.day == monthrange(day.year, day.month)[1]
            )
        dom, months, dow = self.fields[2:5]
        if "*" not in months and day.month not in months:
            return False
        dom_match = "*" in dom or day.day in dom
        dow_match = "*" in dow or (day.weekday() + 1) % 7 in dow
        if "*" in dom or "*" in dow:
            return dom_match and dow_match
        return dom_match or dow_match

    def bounds(
        self, lower: datetime, upper: datetime
    ) -> tuple[datetime, datetime]:
        if self.config.start_at:
            lower = max(lower, self.config.start_at)
        if self.config.end_at:
            upper = min(upper, self.config.end_at - EPSILON)
        return lower.astimezone(UTC), upper.astimezone(UTC)

    def find(
        self, at: datetime, *, reverse: bool = False, inclusive: bool = False
    ) -> datetime | None:
        at = at.astimezone(UTC)
        if not inclusive:
            at += -EPSILON if reverse else EPSILON
        lower, upper = self.bounds(
            datetime.min.replace(tzinfo=UTC) if reverse else at,
            at
            if reverse
            else datetime.max.replace(tzinfo=SEOUL).astimezone(UTC),
        )
        if lower > upper:
            return None
        definition = self.definition
        if isinstance(definition, Once):
            result = definition.at.astimezone(UTC)
            return result if lower <= result <= upper else None
        if isinstance(definition, Interval):
            anchor = definition.anchor.astimezone(UTC)
            delta = timedelta(seconds=definition.seconds)
            if reverse:
                n = (upper - anchor) // delta
            else:
                n = max(0, -((anchor - lower) // delta))
            if n < 0:
                return None
            try:
                result = anchor + n * delta
            except OverflowError:
                return None
            return result if lower <= result <= upper else None
        local = (upper if reverse else lower).astimezone(SEOUL)
        day = local.date()
        # Longest gap in supported monthly/Cron rules is eight years
        # (February 29 around a non-leap century). No per-minute scan.
        for _ in range(366 * 8 + 1):
            if self.matches_day(day):
                if day == local.date():
                    index = (
                        bisect_right(self.times, local.time()) - 1
                        if reverse
                        else bisect_left(self.times, local.time())
                    )
                else:
                    index = len(self.times) - 1 if reverse else 0
                if 0 <= index < len(self.times):
                    result = datetime.combine(day, self.times[index], SEOUL)
                    result = result.astimezone(UTC)
                    if lower <= result <= upper:
                        return result
                    return None
            try:
                day += timedelta(days=-1 if reverse else 1)
            except OverflowError:
                return None
        return None

    def count(self, lower: datetime, upper: datetime) -> int:
        """Count both endpoints, excluding end_at; never expand missed runs."""
        lower, upper = self.bounds(lower, upper)
        if lower > upper:
            return 0
        first = self.find(lower, inclusive=True)
        last = self.find(upper, reverse=True, inclusive=True)
        if first is None or last is None or first > last:
            return 0
        definition = self.definition
        if isinstance(definition, Once):
            return 1
        if isinstance(definition, Interval):
            return (last - first) // timedelta(seconds=definition.seconds) + 1
        first, last = first.astimezone(SEOUL), last.astimezone(SEOUL)
        day, total = first.date(), 0
        while day <= last.date():
            if self.matches_day(day):
                left = (
                    bisect_left(self.times, first.time())
                    if day == first.date()
                    else 0
                )
                right = (
                    bisect_right(self.times, last.time())
                    if day == last.date()
                    else len(self.times)
                )
                total += right - left
            if day == last.date():
                break
            day += timedelta(days=1)
        return total

    def preview(self, after: datetime, count: int = 5) -> list[datetime]:
        result = []
        for _ in range(count):
            following = self.find(after)
            if following is None:
                break
            result.append(following)
            after = following
        return result

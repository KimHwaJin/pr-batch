"""When a batch runs (Schedule) and how runs interact (Policy)."""

import re
from datetime import datetime, time
from typing import Annotated, Literal, Self

from croniter import croniter
from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.common import Contract


class Once(Contract):
    kind: Literal["ONCE"]
    at: AwareDatetime


Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WEEKDAYS: tuple[str, ...] = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class Interval(Contract):
    kind: Literal["INTERVAL"]
    seconds: int = Field(ge=60, le=31_536_000)
    # Reference point for "every N seconds". Omitted: the schedule's
    # start_at, else the time the configuration is saved (filled then).
    anchor: AwareDatetime | None = None


class NthWeekday(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weekday: Weekday
    nth: Literal[1, 2, 3, 4, 5, -1]  # -1: the last one in the month


class Calendar(Contract):
    kind: Literal["CALENDAR"]
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY"]
    at: time
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    month_days: list[Annotated[int, Field(ge=1, le=31)]] = Field(
        default_factory=list, max_length=31
    )
    last_day: bool = False
    nth_weekdays: list[NthWeekday] = Field(default_factory=list, max_length=35)

    @model_validator(mode="after")
    def validate_calendar(self) -> Self:
        if self.at.tzinfo or self.at.microsecond:
            raise ValueError("Calendar time must be local, without fractions.")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Duplicate weekdays are not allowed.")
        if len(set(self.month_days)) != len(self.month_days):
            raise ValueError("Duplicate month days are not allowed.")
        if len(set(self.nth_weekdays)) != len(self.nth_weekdays):
            raise ValueError("Duplicate nth weekdays are not allowed.")
        if self.frequency == "WEEKLY" and not self.weekdays:
            raise ValueError("WEEKLY requires weekdays, e.g. ['MON'].")
        if self.frequency != "WEEKLY" and self.weekdays:
            raise ValueError("weekdays applies only to WEEKLY.")
        monthly = self.month_days or self.last_day or self.nth_weekdays
        if self.frequency == "MONTHLY":
            if not monthly:
                raise ValueError(
                    "MONTHLY requires month_days, last_day or nth_weekdays."
                )
        elif monthly:
            raise ValueError("Month day options apply only to MONTHLY.")
        self.weekdays.sort(key=WEEKDAYS.index)
        self.month_days.sort()
        return self


CRON_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTH_NAMES = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
DOW_NAMES = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


def numeric_cron(expression: str) -> str:
    """Expand macros and month/day names into five numeric fields."""
    value = " ".join(expression.split()).upper()
    value = CRON_MACROS.get(value.lower(), value)
    fields = value.split()
    if len(fields) != 5:
        raise ValueError("Use five Cron fields or a macro such as @daily.")

    def names(field: str, table: tuple[str, ...], offset: int) -> str:
        return re.sub(
            r"[A-Z]{3}",
            lambda m: (
                str(table.index(m.group()) + offset)
                if m.group() in table
                else m.group()
            ),
            field,
        )

    fields[3] = names(fields[3], MONTH_NAMES, 1)
    fields[4] = names(fields[4], DOW_NAMES, 0)
    return " ".join(fields)


class Cron(Contract):
    kind: Literal["CRON"]
    expression: str = Field(min_length=1, max_length=128)

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        value = " ".join(value.split())
        numeric = numeric_cron(value)
        if not re.fullmatch(r"[0-9*/ ,\-]+", numeric):
            raise ValueError(
                "Cron fields take numbers, names (MON, JAN), * - , / or a "
                "macro like @daily; L, W and # are not supported."
            )
        if not croniter.is_valid(numeric):
            raise ValueError("Invalid Cron expression.")
        try:
            croniter(numeric, datetime(2026, 1, 1)).get_next(datetime)
        except ValueError as exc:
            raise ValueError("Cron has no reachable execution date.") from exc
        return value if value.startswith("@") else value.upper()


ScheduleDefinition = Annotated[
    Once | Interval | Calendar | Cron, Field(discriminator="kind")
]


class Schedule(Contract):
    """When a batch runs. Policies live separately in Policy."""

    definition: ScheduleDefinition
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.start_at and self.end_at and self.start_at >= self.end_at:
            raise ValueError("end_at must be after start_at.")
        if isinstance(self.definition, Once):
            at = self.definition.at
            if self.start_at and at < self.start_at:
                raise ValueError("ONCE is before start_at.")
            if self.end_at and at >= self.end_at:
                raise ValueError("ONCE must be before end_at.")
        return self


OverlapMode = Literal["SKIP", "QUEUE", "PARALLEL", "REPLACE"]
MisfireMode = Literal["SKIP", "LATEST", "CATCH_UP"]
DEFAULT_MAX_QUEUED = 100


# overlap: what happens when a run is due while this batch's code is still
# running. Each mode accepts only the values it uses.
class SkipOverlap(Contract):
    """Record the new run as SKIPPED (OVERLAP)."""

    mode: Literal["SKIP"]


class QueueOverlap(Contract):
    """Wait in order; beyond max_queued the run is SKIPPED (capacity)."""

    mode: Literal["QUEUE"]
    max_queued: int = Field(default=DEFAULT_MAX_QUEUED, ge=1, le=10000)


class ParallelOverlap(Contract):
    """Run up to max_concurrent at once; extra runs wait (max_queued)."""

    mode: Literal["PARALLEL"]
    max_concurrent: int = Field(ge=1, le=100)
    max_queued: int = Field(default=DEFAULT_MAX_QUEUED, ge=1, le=10000)


class ReplaceOverlap(Contract):
    """Cancel running/waiting runs; start the new one once they stop."""

    mode: Literal["REPLACE"]


Overlap = Annotated[
    SkipOverlap | QueueOverlap | ParallelOverlap | ReplaceOverlap,
    Field(discriminator="mode"),
]


# misfire: occurrences missed by more than grace_seconds (outages).
class SkipMisfire(Contract):
    """Drop missed occurrences."""

    mode: Literal["SKIP"]
    grace_seconds: int = Field(default=60, ge=1, le=86400)


class LatestMisfire(Contract):
    """Run only the most recent missed occurrence."""

    mode: Literal["LATEST"]
    grace_seconds: int = Field(default=60, ge=1, le=86400)


class CatchUpMisfire(Contract):
    """Run up to max_runs missed occurrences younger than max_age_seconds."""

    mode: Literal["CATCH_UP"]
    grace_seconds: int = Field(default=60, ge=1, le=86400)
    max_runs: int = Field(default=10, ge=1, le=1000)
    max_age_seconds: int = Field(default=86400, ge=60, le=31_536_000)


Misfire = Annotated[
    SkipMisfire | LatestMisfire | CatchUpMisfire,
    Field(discriminator="mode"),
]


class Policy(Contract):
    """Overlap and misfire handling; manual runs also follow overlap."""

    overlap: Overlap = Field(default_factory=lambda: SkipOverlap(mode="SKIP"))
    misfire: Misfire = Field(default_factory=lambda: SkipMisfire(mode="SKIP"))


def concurrency(policy: dict) -> tuple[str, int, int]:
    """(mode, max_concurrent, max_queued) from a stored policy."""
    overlap = policy["overlap"]
    return (
        overlap["mode"],
        overlap.get("max_concurrent", 1),
        overlap.get("max_queued", DEFAULT_MAX_QUEUED),
    )


class OverlapPatch(Contract):
    mode: OverlapMode | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=100)
    max_queued: int | None = Field(default=None, ge=1, le=10000)


class MisfirePatch(Contract):
    mode: MisfireMode | None = None
    grace_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_runs: int | None = Field(default=None, ge=1, le=1000)
    max_age_seconds: int | None = Field(default=None, ge=60, le=31_536_000)


class PolicyPatch(Contract):
    """Partial policy update; omitted values keep their current value."""

    overlap: OverlapPatch | None = None
    misfire: MisfirePatch | None = None

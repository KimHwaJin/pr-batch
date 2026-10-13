"""When a batch runs (Schedule) and how runs interact (Policy)."""

import re
from datetime import datetime, time
from typing import Annotated, Literal, Self

from croniter import croniter
from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.schemas.common import Contract


class Once(Contract):
    kind: Literal["ONCE"]
    at: AwareDatetime


class Interval(Contract):
    kind: Literal["INTERVAL"]
    anchor: AwareDatetime
    seconds: int = Field(ge=60, le=31_536_000)


class Calendar(Contract):
    kind: Literal["CALENDAR"]
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY"]
    at: time
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        default_factory=list, max_length=7
    )
    month_days: list[Annotated[int, Field(ge=1, le=31)]] = Field(
        default_factory=list, max_length=31
    )
    last_day: bool = False

    @model_validator(mode="after")
    def validate_calendar(self) -> Self:
        if self.at.tzinfo or self.at.microsecond:
            raise ValueError("Calendar time must be local, without fractions.")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Duplicate weekdays are not allowed.")
        if len(set(self.month_days)) != len(self.month_days):
            raise ValueError("Duplicate month days are not allowed.")
        if self.frequency == "WEEKLY" and not self.weekdays:
            raise ValueError("WEEKLY requires weekdays (Monday=0).")
        if self.frequency != "WEEKLY" and self.weekdays:
            raise ValueError("weekdays applies only to WEEKLY.")
        if self.frequency == "MONTHLY":
            if not self.month_days and not self.last_day:
                raise ValueError("MONTHLY requires month_days or last_day.")
        elif self.month_days or self.last_day:
            raise ValueError("Month day options apply only to MONTHLY.")
        self.weekdays.sort()
        self.month_days.sort()
        return self


class Cron(Contract):
    kind: Literal["CRON"]
    expression: str = Field(min_length=1, max_length=128)

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value.split()) != 5 or not re.fullmatch(r"[0-9*/ ,\-]+", value):
            raise ValueError("Use five numeric Cron fields: * ranges , /.")
        if not croniter.is_valid(value):
            raise ValueError("Invalid Cron expression.")
        try:
            croniter(value, datetime(2026, 1, 1)).get_next(datetime)
        except ValueError as exc:
            raise ValueError("Cron has no reachable execution date.") from exc
        return value


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


class Policy(Contract):
    """Overlap and misfire handling; manual runs also follow overlap."""

    overlap: Literal["SKIP", "QUEUE", "PARALLEL"] = "SKIP"
    max_concurrent: int = Field(default=1, ge=1, le=100)
    max_pending: int = Field(default=100, ge=1, le=10000)
    misfire: Literal["SKIP", "LATEST", "CATCH_UP"] = "SKIP"
    misfire_grace_seconds: int = Field(default=60, ge=1, le=86400)
    catch_up_max_runs: int = Field(default=10, ge=1, le=1000)
    catch_up_max_age_seconds: int = Field(default=86400, ge=60, le=31_536_000)

    @model_validator(mode="after")
    def validate_concurrency(self) -> Self:
        if self.overlap != "PARALLEL" and self.max_concurrent != 1:
            raise ValueError("Only PARALLEL allows max_concurrent > 1.")
        return self


class PolicyPatch(Contract):
    """Partial policy update; omitted fields keep their current value."""

    overlap: Literal["SKIP", "QUEUE", "PARALLEL"] | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=100)
    max_pending: int | None = Field(default=None, ge=1, le=10000)
    misfire: Literal["SKIP", "LATEST", "CATCH_UP"] | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=1, le=86400)
    catch_up_max_runs: int | None = Field(default=None, ge=1, le=1000)
    catch_up_max_age_seconds: int | None = Field(
        default=None, ge=60, le=31_536_000
    )

"""Data models (PRD FR-4 and §8).

Everything loaded from disk or proposed by Claude is untrusted (S-17), so every
model forbids unknown fields and validates strictly.
"""

import re
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- Tasks (FR-4) -------------------------------------------------------------


class Priority(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class TaskStatus(StrEnum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class Task(_Strict):
    id: NonEmptyStr
    title: NonEmptyStr
    description: str | None = None
    category: NonEmptyStr
    priority: Priority = Priority.medium
    # A date-only deadline means "some time that day". A deadline with a time
    # must carry a UTC offset, so it can't be misread in another time zone.
    deadline: date | AwareDatetime | None = None
    status: TaskStatus = TaskStatus.todo
    estimated_minutes: int | None = Field(default=None, gt=0, le=24 * 60)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline_time_needs_offset(cls, v: object) -> object:
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError("a deadline with a time must include a UTC offset, e.g. 2026-10-30T17:00:00-07:00")
        if isinstance(v, str) and "T" in v and not re.search(r"(Z|[+-]\d{2}:?\d{2})$", v):
            raise ValueError("a deadline with a time must include a UTC offset, e.g. 2026-10-30T17:00:00-07:00")
        return v

    @model_validator(mode="after")
    def _completed_at_matches_status(self) -> "Task":
        if self.status is TaskStatus.done and self.completed_at is None:
            raise ValueError("completed_at is required when status is 'done'")
        if self.status is not TaskStatus.done and self.completed_at is not None:
            raise ValueError("completed_at must be empty unless status is 'done'")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at can't be earlier than created_at")
        return self


# --- Preferences (PRD §8) -------------------------------------------------------


def _parse_clock(v: object) -> object:
    """Accept "HH:MM" (24-hour) strings only."""
    if isinstance(v, time):
        return v
    if isinstance(v, int):
        # Unquoted 20:00 is read by YAML 1.1 as the number 1200.
        raise ValueError('must be a time in quotes, like "20:00"')
    if not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v.strip()):
        raise ValueError('must be a 24-hour time like "08:00"')
    h, m = v.strip().split(":")
    return time(int(h), int(m))


def _parse_duration(v: object) -> object:
    """Accept "45m" or "2h" style durations."""
    if isinstance(v, timedelta):
        return v
    match = re.fullmatch(r"(\d+)\s*([mh])", str(v).strip()) if isinstance(v, (str, int)) else None
    if not match:
        raise ValueError('must be a duration like "60m" or "2h"')
    amount, unit = int(match[1]), match[2]
    return timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount)


def _positive_and_within_window(v: timedelta) -> timedelta:
    if not timedelta(0) < v <= timedelta(days=7):
        raise ValueError("must be more than 0 and at most 7 days")
    return v


ClockTime = Annotated[time, BeforeValidator(_parse_clock)]
Duration = Annotated[timedelta, BeforeValidator(_parse_duration), AfterValidator(_positive_and_within_window)]


class MorningBrief(_Strict):
    time: ClockTime


class TimeRange(_Strict):
    start: ClockTime
    end: ClockTime


class MeetingRule(_Strict):
    before: Duration


class TaskDeadlineRule(_Strict):
    morning_of_due_day: ClockTime
    before_deadline: Duration


class BirthdayRule(_Strict):
    evening_before: ClockTime


class ExtraReminders(_Strict):
    allowed: bool
    max_per_item: int = Field(ge=0, le=5)
    examples: list[NonEmptyStr] = []


class ReminderRules(_Strict):
    meetings: MeetingRule
    tasks_with_deadline: TaskDeadlineRule
    birthdays: BirthdayRule
    extra_reminders: ExtraReminders


class Preferences(_Strict):
    timezone: NonEmptyStr
    morning_brief: MorningBrief
    working_hours: TimeRange
    calendars: list[NonEmptyStr] = Field(min_length=1)
    reminders: ReminderRules
    categories: list[NonEmptyStr] = Field(min_length=1)
    default_priority: Priority = Priority.medium
    quiet_hours: TimeRange  # may wrap past midnight, e.g. 22:00–07:00

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"'{v}' is not a known IANA time zone, e.g. America/Los_Angeles") from None
        return v

    @field_validator("working_hours")
    @classmethod
    def _working_hours_in_order(cls, v: TimeRange) -> TimeRange:
        if v.start >= v.end:
            raise ValueError("start must be before end")
        return v

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_hours_not_empty(cls, v: TimeRange) -> TimeRange:
        if v.start == v.end:
            raise ValueError("start and end can't be the same time")
        return v

    @field_validator("calendars", "categories")
    @classmethod
    def _no_duplicates(cls, v: list[str]) -> list[str]:
        if len({item.lower() for item in v}) != len(v):
            raise ValueError("contains duplicates")
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# --- Calendar (FR-1–FR-3, ARCHITECTURE §3 get_calendar_events) ----------------------------


class CalendarEvent(_Strict):
    """One event as the agent sees it. No attendees or descriptions (FR-2, S-13).

    All-day events have `date` start/end with an exclusive end (as Google sends
    them); timed events have offset-aware datetimes.
    """

    id: NonEmptyStr
    calendar: NonEmptyStr
    title: str
    start: AwareDatetime | date
    end: AwareDatetime | date
    all_day: bool
    location: str | None = None
    is_birthday: bool = False
    # Google's free/busy flag. Missing means "opaque" (busy), as in the Google API.
    transparency: Literal["opaque", "transparent"] = "opaque"

    @model_validator(mode="after")
    def _times_match_all_day(self) -> "CalendarEvent":
        timed = [isinstance(v, datetime) for v in (self.start, self.end)]
        if self.all_day and any(timed):
            raise ValueError("all-day events have dates, not times")
        if not self.all_day and not all(timed):
            raise ValueError("timed events need a start and end time")
        if self.end < self.start:
            raise ValueError("end can't be before start")
        return self


class CalendarResult(_Strict):
    available: bool
    error: str | None = None
    skipped_calendars: list[str] = []
    events: list[CalendarEvent] = []


# --- Extra reminders (FR-14.3, FR-17, ARCHITECTURE §3 submit_daily_plan) -------------------


class ExtraReminderProposal(_Strict):
    """One extra reminder Claude proposes. The planner checks it further (FR-18)."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    fire_at: AwareDatetime
    source_type: Literal["meeting", "task", "birthday"]
    source_id: NonEmptyStr
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

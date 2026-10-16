"""Google Calendar, read-only (FR-1–FR-3, S-1, S-2).

M4: the interface, the shared date clamp and event normalisation. The real
client makes no calls until M13. The fixture client in `tests/fakes/` uses the
same clamp and normalisation, so tests see exactly what the agent would.
"""

from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from assistant.schemas import CalendarEvent, CalendarResult

WINDOW_DAYS = 7  # today through today + 7 (FR-1)


@runtime_checkable
class CalendarClient(Protocol):
    def list_events(self, start: date, end: date) -> CalendarResult:
        """Events overlapping `start`…`end` (inclusive local dates), clamped to the window."""
        ...


def clamp_range(start: date, end: date, today: date) -> tuple[date, date] | None:
    """Clamp a requested range to today…today+7. None if nothing is left."""
    lo, hi = max(start, today), min(end, today + timedelta(days=WINDOW_DAYS))
    return (lo, hi) if lo <= hi else None


def overlaps(event: CalendarEvent, start: date, end: date, tz: ZoneInfo) -> bool:
    """True if the event overlaps the local days `start`…`end` (inclusive)."""
    if event.all_day:
        return event.start <= end and event.end > start  # all-day end is exclusive
    window_start = datetime.combine(start, time.min, tzinfo=tz)
    window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    return event.start < window_end and event.end > window_start


def normalize_event(raw: dict[str, Any], calendar: str, birthdays_calendar: bool) -> CalendarEvent | None:
    """Turn a Google API event into a CalendarEvent, keeping only FR-2 fields (incl. free/busy).

    Attendees, descriptions, creators, links and everything else are dropped
    here. Cancelled events return None. Birthdays follow the three FR-3 paths.
    """
    if raw.get("status") == "cancelled":
        return None
    raw_start, raw_end = raw["start"], raw["end"]
    all_day = "date" in raw_start
    if all_day:
        start, end = date.fromisoformat(raw_start["date"]), date.fromisoformat(raw_end["date"])
    else:
        start, end = datetime.fromisoformat(raw_start["dateTime"]), datetime.fromisoformat(raw_end["dateTime"])
    title = (raw.get("summary") or "").strip() or "(no title)"
    is_birthday = birthdays_calendar or raw.get("eventType") == "birthday" or (all_day and "birthday" in title.lower())
    return CalendarEvent(
        id=raw["id"],
        calendar=calendar,
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        location=(raw.get("location") or "").strip() or None,
        is_birthday=is_birthday,
        transparency="transparent" if raw.get("transparency") == "transparent" else "opaque",
    )


def sort_key(event: CalendarEvent, tz: ZoneInfo) -> tuple[datetime, str]:
    start = event.start if isinstance(event.start, datetime) else datetime.combine(event.start, time.min, tzinfo=tz)
    return (start, event.id)


class GoogleCalendarClient:
    """Real client. Built in M13 (events.list / events.get only, S-2)."""

    def __init__(self, calendars: list[str], tz: ZoneInfo, today: date):
        self.calendars = calendars
        self.tz = tz
        self.today = today

    def list_events(self, start: date, end: date) -> CalendarResult:
        raise NotImplementedError("the real Google Calendar client arrives in M13")

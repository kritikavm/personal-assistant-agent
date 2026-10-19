"""Google Calendar, read-only (FR-1–FR-3, S-1, S-2).

The interface, the shared date clamp and event normalisation, and the real
client. The real client only ever calls `events.list`, asking for the FR-2
fields only (no attendees or descriptions). The fixture client in
`tests/fakes/` uses the same clamp and normalisation, so tests see exactly what
the agent would.
"""

from collections.abc import Callable
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


# The legacy Google Contacts birthdays calendar. Newer birthdays are on the primary calendar with
# eventType "birthday" (FR-3 path 2). Listing calendars needs a wider scope, so "birthdays" in
# preferences maps to this ID and is skipped if Google says it doesn't exist (FR-1).
BIRTHDAYS_ALIAS = "birthdays"
BIRTHDAYS_CALENDAR_ID = "addressbook#contacts@group.v.calendar.google.com"
FIELDS = "nextPageToken,items(id,status,summary,start,end,location,transparency,eventType)"
MAX_PAGES = 10  # 250 events a page is far more than a week holds; this only stops a runaway loop


class EventsPages(Protocol):
    def page(self, calendar_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """One page of events.list. Raises CalendarNotFound for a calendar Google doesn't know."""
        ...


class CalendarNotFound(Exception):
    """Google answered 404 (or 403 for a calendar this account can't see) for one calendar."""


class GoogleEventsPages:
    """events.list on the Calendar API, nothing else (safety test 2).

    The service is built on first use, so a Keychain or network problem shows up inside
    `list_events`, where the caller treats it as the calendar being unavailable (FR-22).
    """

    def __init__(self, credentials: Callable[[], Any]):
        self.credentials = credentials
        self.service = None

    def page(self, calendar_id: str, params: dict[str, Any]) -> dict[str, Any]:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        if self.service is None:
            self.service = build("calendar", "v3", credentials=self.credentials(), cache_discovery=False)
        try:
            return self.service.events().list(calendarId=calendar_id, **params).execute(num_retries=2)
        except HttpError as e:
            if e.resp.status in (403, 404):
                raise CalendarNotFound(calendar_id) from None
            raise


class GoogleCalendarClient:
    """Real client: today … today + 7 from the configured calendars, read-only (S-2)."""

    def __init__(self, calendars: list[str], tz: ZoneInfo, today: date, pages: EventsPages):
        self.calendars = calendars
        self.tz = tz
        self.today = today
        self.pages = pages

    def _calendar_id(self, name: str) -> str:
        return BIRTHDAYS_CALENDAR_ID if name == BIRTHDAYS_ALIAS else name

    def _events(self, calendar_id: str, start: date, end: date) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeMin": datetime.combine(start, time.min, tzinfo=self.tz).isoformat(),
            "timeMax": datetime.combine(end + timedelta(days=1), time.min, tzinfo=self.tz).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
            "timeZone": self.tz.key,
            "fields": FIELDS,
        }
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            page = self.pages.page(calendar_id, params)
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                break
            params = {**params, "pageToken": token}
        return items

    def list_events(self, start: date, end: date) -> CalendarResult:
        """Any error other than a missing calendar propagates: the caller treats the calendar as unavailable (FR-22)."""
        window = clamp_range(start, end, self.today)
        if window is None:
            return CalendarResult(available=True)
        skipped, events, seen = [], [], set()
        for name in self.calendars:
            try:
                raw_events = self._events(self._calendar_id(name), *window)
            except CalendarNotFound:
                skipped.append(name)
                continue
            for raw in raw_events:
                event = normalize_event(raw, name, birthdays_calendar=name == BIRTHDAYS_ALIAS)
                if event and event.id not in seen and overlaps(event, *window, self.tz):
                    seen.add(event.id)
                    events.append(event)
        events.sort(key=lambda e: sort_key(e, self.tz))
        return CalendarResult(available=True, skipped_calendars=skipped, events=events)

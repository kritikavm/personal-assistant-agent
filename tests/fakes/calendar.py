"""Fixture calendar client: Google-shaped events from a fixture, no network."""

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from assistant.integrations.google_calendar import clamp_range, normalize_event, overlaps, sort_key
from assistant.schemas import CalendarResult


class FixtureCalendarClient:
    """Same interface, clamp and normalisation as the real client.

    `calendars` maps a calendar name (as in preferences) to raw Google API
    events. A configured calendar missing from it is reported in
    `skipped_calendars`, like a 404 (FR-1). `available=False` simulates an
    outage or expired auth (FR-22).
    """

    def __init__(
        self,
        calendars: dict[str, list[dict[str, Any]]],
        configured: list[str],
        tz: ZoneInfo,
        today: date,
        available: bool = True,
        birthdays_calendar: str = "birthdays",
    ):
        self.calendars = calendars
        self.configured = configured
        self.tz = tz
        self.today = today
        self.available = available
        self.birthdays_calendar = birthdays_calendar
        self.requests: list[tuple[date, date]] = []  # ranges asked for, before clamping

    def list_events(self, start: date, end: date) -> CalendarResult:
        self.requests.append((start, end))
        if not self.available:
            return CalendarResult(available=False, error="calendar_unavailable")
        window = clamp_range(start, end, self.today)
        skipped = [name for name in self.configured if name not in self.calendars]
        events = []
        for name in self.configured:
            for raw in self.calendars.get(name, []):
                event = normalize_event(raw, name, birthdays_calendar=name == self.birthdays_calendar)
                if event and window and overlaps(event, *window, self.tz):
                    events.append(event)
        events.sort(key=lambda e: sort_key(e, self.tz))
        return CalendarResult(available=True, skipped_calendars=skipped, events=events)

"""Test doubles (ARCHITECTURE §1, EVALS §2). Product code never imports this package."""

from tests.fakes.calendar import FixtureCalendarClient
from tests.fakes.notify import FakeNotifier
from tests.fakes.reminders import FakeReminders

__all__ = ["FakeNotifier", "FakeReminders", "FixtureCalendarClient"]

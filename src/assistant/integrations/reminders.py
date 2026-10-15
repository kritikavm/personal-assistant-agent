"""Apple Reminders, list "Agent Reminders" only (S-4, S-14, S-18, FR-23).

M4: the interface and shared types. No function takes a list name: the list is
fixed. The real AppleScript client arrives in M14.
"""

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime

from assistant.schemas import NonEmptyStr, _Strict

AGENT_LIST = "Agent Reminders"


class ReminderAccess(StrEnum):
    ok = "ok"
    access_denied = "access_denied"  # Reminders permission not granted
    list_missing = "list_missing"  # no list named exactly AGENT_LIST
    not_on_my_mac = "not_on_my_mac"  # the list is in iCloud or another account (S-14)


class ReminderItem(_Strict):
    """A reminder in AGENT_LIST, as read back from Reminders."""

    reminder_id: NonEmptyStr
    title: str
    notes: str = ""
    fire_at: AwareDatetime | None = None
    completed: bool = False


class RemindersBlockedError(Exception):
    """A write was attempted while access isn't `ok`. Nothing was written."""

    def __init__(self, reason: ReminderAccess):
        self.reason = reason
        super().__init__(f"reminder writes are blocked: {reason.value}")


class ReminderNotFoundError(Exception):
    """No reminder with that ID in AGENT_LIST."""


@runtime_checkable
class RemindersClient(Protocol):
    def check_access(self) -> ReminderAccess:
        """Preflight: permission, list exists, list is On My Mac."""
        ...

    def list_items(self) -> list[ReminderItem]:
        """Every reminder in AGENT_LIST, ours (with a [pa:] marker) or not."""
        ...

    def create(self, title: str, fire_at: datetime, notes: str) -> str:
        """Create a reminder in AGENT_LIST and return its ID."""
        ...

    def update(self, reminder_id: str, title: str, fire_at: datetime, notes: str) -> None: ...

    def delete(self, reminder_id: str) -> None: ...


class AppleRemindersClient:
    """Real client: fixed AppleScripts run with osascript, values as arguments (S-18). Built in M14."""

    def check_access(self) -> ReminderAccess:
        raise NotImplementedError("the real Reminders client arrives in M14")

    def list_items(self) -> list[ReminderItem]:
        raise NotImplementedError("the real Reminders client arrives in M14")

    def create(self, title: str, fire_at: datetime, notes: str) -> str:
        raise NotImplementedError("the real Reminders client arrives in M14")

    def update(self, reminder_id: str, title: str, fire_at: datetime, notes: str) -> None:
        raise NotImplementedError("the real Reminders client arrives in M14")

    def delete(self, reminder_id: str) -> None:
        raise NotImplementedError("the real Reminders client arrives in M14")

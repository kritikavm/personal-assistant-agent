"""Fake Reminders client: an in-memory "Agent Reminders" list that records every call."""

import uuid
from datetime import datetime

from assistant.integrations.reminders import (
    ReminderAccess,
    ReminderItem,
    ReminderNotFoundError,
    RemindersBlockedError,
)


class FakeReminders:
    """`access` simulates the FR-23/S-14 cases. `items` seeds the list, e.g.
    with a reminder I made by hand (no [pa:] marker) that must never be touched.
    """

    def __init__(self, access: ReminderAccess = ReminderAccess.ok, items: list[ReminderItem] | None = None):
        self.access = access
        self.items: dict[str, ReminderItem] = {i.reminder_id: i for i in items or []}
        self.calls: list[tuple] = []  # ("create", id, title, fire_at, notes), ("update", …), ("delete", id)

    def _check_writable(self) -> None:
        if self.access is not ReminderAccess.ok:
            raise RemindersBlockedError(self.access)

    @staticmethod
    def _check_time(fire_at: datetime) -> None:
        if fire_at.tzinfo is None:
            raise ValueError("fire_at must be timezone-aware")

    def check_access(self) -> ReminderAccess:
        self.calls.append(("check_access",))
        return self.access

    def list_items(self) -> list[ReminderItem]:
        self.calls.append(("list_items",))
        if self.access is not ReminderAccess.ok:
            raise RemindersBlockedError(self.access)
        return list(self.items.values())

    def create(self, title: str, fire_at: datetime, notes: str) -> str:
        self._check_writable()
        self._check_time(fire_at)
        reminder_id = f"x-apple-reminder://{str(uuid.uuid4()).upper()}"
        self.items[reminder_id] = ReminderItem(reminder_id=reminder_id, title=title, notes=notes, fire_at=fire_at)
        self.calls.append(("create", reminder_id, title, fire_at, notes))
        return reminder_id

    def update(self, reminder_id: str, title: str, fire_at: datetime, notes: str) -> None:
        self._check_writable()
        self._check_time(fire_at)
        if reminder_id not in self.items:
            raise ReminderNotFoundError(reminder_id)
        self.items[reminder_id] = self.items[reminder_id].model_copy(update={"title": title, "fire_at": fire_at, "notes": notes})
        self.calls.append(("update", reminder_id, title, fire_at, notes))

    def delete(self, reminder_id: str) -> None:
        self._check_writable()
        if reminder_id not in self.items:
            raise ReminderNotFoundError(reminder_id)
        del self.items[reminder_id]
        self.calls.append(("delete", reminder_id))

    def writes(self) -> list[tuple]:
        """Only the calls that changed something."""
        return [c for c in self.calls if c[0] in ("create", "update", "delete")]

"""macOS notifications, display only (FR-14, S-15, S-18).

M4: the interface. The real client (a fixed `display notification` script with
the text passed as arguments) arrives in M14. Callers pass neutral text only:
counts and failure types, never titles (S-15).
"""

from typing import Protocol, runtime_checkable


class NotificationError(Exception):
    """The notification couldn't be shown. Callers log it and carry on."""


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None: ...


class MacNotifier:
    """Real notifier. Built in M14."""

    def notify(self, title: str, message: str) -> None:
        raise NotImplementedError("the real notifier arrives in M14")

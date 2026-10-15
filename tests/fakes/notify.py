"""Fake notifier: records what would have been shown."""

from assistant.integrations.notify import NotificationError


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        if self.fail:
            raise NotificationError("simulated osascript failure")
        self.sent.append((title, message))

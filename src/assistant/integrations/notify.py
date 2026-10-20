"""macOS notifications, display only (FR-14, S-15, S-18).

A fixed `display notification` script; the title and message reach it only as
`osascript` arguments (S-18). Callers pass neutral text only: counts and
failure types, never titles (S-15).
"""

import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

NOTIFY_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
"""
TIMEOUT = 20  # seconds


def run_osascript(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=TIMEOUT, check=False)


class NotificationError(Exception):
    """The notification couldn't be shown. Callers log it and carry on."""


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None: ...


class MacNotifier:
    """Real notifier: NOTIFY_SCRIPT run with `osascript` and an argument list (no shell)."""

    def __init__(self, run: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None):
        self._run = run or (lambda args: run_osascript(args))  # looked up at call time, so tests can replace it

    def notify(self, title: str, message: str) -> None:
        try:
            out = self._run(["/usr/bin/osascript", "-e", NOTIFY_SCRIPT, title, message])
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NotificationError(e.__class__.__name__) from None
        if out.returncode != 0:
            raise NotificationError(f"osascript exit {out.returncode}")

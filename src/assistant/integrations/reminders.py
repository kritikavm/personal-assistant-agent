"""Apple Reminders, list "Agent Reminders" only (S-4, S-14, S-18, FR-23).

No function takes a list name: the list name and the allowed accounts are
constants inside each fixed AppleScript. Values reach the scripts only as `osascript`
arguments (`on run argv`), never as script source (S-18, SECURITY §4.1).
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime

from assistant.schemas import NonEmptyStr, _Strict

AGENT_LIST = "Agent Reminders"
# S-14: the list may be On My Mac, or in iCloud as a deliberate exception chosen by the user (2026-10-19)
# so alerts reach the iPhone. Any other account (Exchange, CalDAV, ...) is refused.
ALLOWED_ACCOUNTS = ("On My Mac", "iCloud")


class ReminderAccess(StrEnum):
    ok = "ok"
    access_denied = "access_denied"  # Reminders permission not granted
    list_missing = "list_missing"  # no list named exactly AGENT_LIST
    account_not_allowed = "account_not_allowed"  # the list is in an account outside ALLOWED_ACCOUNTS (S-14)
    list_not_unique = "list_not_unique"  # more than one list is named AGENT_LIST, so the target is ambiguous


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
        """Preflight: permission, exactly one list named AGENT_LIST, in an allowed account."""
        ...

    def list_items(self) -> list[ReminderItem]:
        """Every reminder in AGENT_LIST, ours (with a [pa:] marker) or not."""
        ...

    def create(self, title: str, fire_at: datetime, notes: str) -> str:
        """Create a reminder in AGENT_LIST and return its ID."""
        ...

    def update(self, reminder_id: str, title: str, fire_at: datetime, notes: str) -> None: ...

    def delete(self, reminder_id: str) -> None: ...


class RemindersScriptError(Exception):
    """osascript failed for a reason other than a missing permission or reminder. Nothing is guessed."""


# --- Fixed AppleScripts (S-18) ---------------------------------------------------------------------------
# Every script reaches the list only through agentList(), which finds exactly one list named AGENT_LIST in
# an allowed account or raises 9001-9003. Both names are literals here, never arguments. A reminder ID
# that isn't in that list is "not found" (AppleScript error -1728/-1719).

_ACCOUNTS = "{" + ", ".join(f'"{a}"' for a in ALLOWED_ACCOUNTS) + "}"
_LIST_HANDLER = f"""
on agentList()
    tell application "Reminders"
        set found to every list whose name is "{AGENT_LIST}"
        if (count of found) = 0 then error "list_missing" number 9001
        if (count of found) > 1 then error "list_not_unique" number 9002
        set theList to item 1 of found
        if {_ACCOUNTS} does not contain (name of (container of theList)) then error "account_not_allowed" number 9003
        return theList
    end tell
end agentList
"""
_BLOCK_CODES = {
    "(9001)": "list_missing",
    "(9002)": "list_not_unique",
    "(9003)": "account_not_allowed",
}
_FS, _RS = "\x1f", "\x1e"  # field / record separators in list output (stripped from everything we write)

_DATE_HANDLER = """
on makeDate(y, mo, d, h, mi, s)
    set dt to current date
    set day of dt to 1
    set year of dt to (y as integer)
    set month of dt to (mo as integer)
    set day of dt to (d as integer)
    set time of dt to ((h as integer) * 3600 + (mi as integer) * 60 + (s as integer))
    return dt
end makeDate
"""

CHECK_SCRIPT = _LIST_HANDLER + """
on run argv
    try
        agentList()
    on error msg number n
        if n is in {9001, 9002, 9003} then return msg
        error msg number n
    end try
    return "ok"
end run
"""

LIST_SCRIPT = _LIST_HANDLER + """
on run argv
    set FS to character id 31
    set RS to character id 30
    set out to {}
    set theList to agentList()
    tell application "Reminders"
        set ids to id of every reminder of theList
        set nms to name of every reminder of theList
        set bds to body of every reminder of theList
        set cps to completed of every reminder of theList
        set rds to remind me date of every reminder of theList
    end tell
    repeat with i from 1 to count of ids
        set b to item i of bds
        if b is missing value then set b to ""
        set r to item i of rds
        if r is missing value then
            set rt to ""
        else
            set rt to ((year of r) as text) & "-" & ((month of r as integer) as text) & "-" & ((day of r) as text) & "-" & ((hours of r) as text) & "-" & ((minutes of r) as text) & "-" & ((seconds of r) as text)
        end if
        set end of out to (item i of ids) & FS & (item i of nms) & FS & b & FS & ((item i of cps) as text) & FS & rt
    end repeat
    set AppleScript's text item delimiters to RS
    set res to out as text
    set AppleScript's text item delimiters to ""
    return res
end run
"""

CREATE_SCRIPT = _DATE_HANDLER + _LIST_HANDLER + """
on run argv
    set {t, n, y, mo, d, h, mi, s} to argv
    set dt to makeDate(y, mo, d, h, mi, s)
    set theList to agentList()
    tell application "Reminders"
        set r to make new reminder at end of reminders of theList with properties {name:t, body:n, remind me date:dt, due date:dt}
        return id of r
    end tell
end run
"""

UPDATE_SCRIPT = _DATE_HANDLER + _LIST_HANDLER + """
on run argv
    set {rid, t, n, y, mo, d, h, mi, s} to argv
    set dt to makeDate(y, mo, d, h, mi, s)
    set theList to agentList()
    tell application "Reminders"
        set r to (first reminder of theList whose id is rid)
        set name of r to t
        set body of r to n
        set remind me date of r to dt
        set due date of r to dt
    end tell
    return "ok"
end run
"""

DELETE_SCRIPT = _LIST_HANDLER + """
on run argv
    set rid to item 1 of argv
    set theList to agentList()
    tell application "Reminders"
        delete (first reminder of theList whose id is rid)
    end tell
    return "ok"
end run
"""

_NOT_AUTHORIZED = "(-1743)"  # macOS refused Apple events to Reminders (Automation permission)
_NOT_FOUND = re.compile(r"\((-1728|-1719)\)")  # no such object (e.g. the ID isn't in our list)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keeps \t and \n
TIMEOUT = 60  # seconds; the first call can wait on macOS's permission prompt


def clean(text: str) -> str:
    """Text as written to Reminders: control characters (incl. our separators) removed."""
    return _CONTROL.sub("", text)


def _date_args(fire_at: datetime) -> list[str]:
    if fire_at.tzinfo is None:
        raise ValueError("fire_at must be timezone-aware")
    local = fire_at.astimezone()  # AppleScript dates are in the Mac's own time zone
    return [str(v) for v in (local.year, local.month, local.day, local.hour, local.minute, local.second)]


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    y, mo, d, h, mi, s = (int(v) for v in text.split("-"))
    return datetime(y, mo, d, h, mi, s).astimezone()  # naive Mac-local → aware


def parse_items(output: str) -> list[ReminderItem]:
    """LIST_SCRIPT output → items. Any malformed record fails the whole read (never guess)."""
    if not output:
        return []
    items = []
    for record in output.split(_RS):
        fields = record.split(_FS)
        if len(fields) != 5:
            raise RemindersScriptError("unexpected list output")
        rid, title, notes, completed, when = fields
        try:
            fire_at = _parse_date(when)
        except ValueError:
            raise RemindersScriptError("unexpected date in list output") from None
        items.append(ReminderItem(reminder_id=rid, title=title, notes=notes, fire_at=fire_at, completed=completed == "true"))
    return items


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def run_osascript(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=TIMEOUT, check=False)


class AppleRemindersClient:
    """Real client: the fixed scripts above, run with `osascript` and an argument list (no shell)."""

    def __init__(self, run: Runner | None = None):
        self._run = run or (lambda args: run_osascript(args))  # looked up at call time, so tests can replace it

    def _script(self, script: str, *values: str) -> str:
        """Run a fixed script with values as arguments. Raises the matching error on failure."""
        out = self._run(["/usr/bin/osascript", "-e", script, *values])
        if out.returncode != 0:
            err = out.stderr or ""
            if _NOT_AUTHORIZED in err:
                raise RemindersBlockedError(ReminderAccess.access_denied)
            for code, reason in _BLOCK_CODES.items():
                if code in err:
                    raise RemindersBlockedError(ReminderAccess(reason))
            if _NOT_FOUND.search(err):
                raise ReminderNotFoundError()
            raise RemindersScriptError(f"osascript exit {out.returncode}")
        return out.stdout.removesuffix("\n")

    def check_access(self) -> ReminderAccess:
        try:
            result = self._script(CHECK_SCRIPT)
        except RemindersBlockedError as e:
            return e.reason
        return ReminderAccess(result)

    def _writable(self) -> None:
        access = self.check_access()
        if access is not ReminderAccess.ok:
            raise RemindersBlockedError(access)

    def list_items(self) -> list[ReminderItem]:
        self._writable()
        return parse_items(self._script(LIST_SCRIPT))

    def create(self, title: str, fire_at: datetime, notes: str) -> str:
        return self._script(CREATE_SCRIPT, clean(title), clean(notes), *_date_args(fire_at))

    def update(self, reminder_id: str, title: str, fire_at: datetime, notes: str) -> None:
        self._script(UPDATE_SCRIPT, reminder_id, clean(title), clean(notes), *_date_args(fire_at))

    def delete(self, reminder_id: str) -> None:
        self._script(DELETE_SCRIPT, reminder_id)

"""`state.json`: reminders the agent created and the last runs (ARCHITECTURE §5).

The file is untrusted on load (S-17): anything that doesn't match the schema
exactly is rejected, never guessed at. A missing file is a fresh start.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from assistant.config import FILE_MODE
from assistant.schemas import NonEmptyStr, _Strict
from assistant.tasks_store import BACKUPS_DIR, atomic_write_json, backup

STATE_FILE = "state.json"
SCHEMA_VERSION = 1
TITLE_MAX = 200


class SourceType(StrEnum):
    meeting = "meeting"
    task = "task"
    birthday = "birthday"
    brief = "brief"


class ReminderKind(StrEnum):
    default = "default"
    extra = "extra"


class RunStatus(StrEnum):
    ok = "ok"
    partial = "partial"  # finished, but with a data gap (e.g. calendar or Reminders unavailable)
    failed = "failed"


def reminder_key(source_type: SourceType | str, source_id: str, fire_at: datetime) -> str:
    """Reminder key = source_type:source_id:fire_at (FR-15)."""
    return f"{SourceType(source_type).value}:{source_id}:{fire_at.isoformat()}"


class ReminderRecord(_Strict):
    reminder_id: NonEmptyStr
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX)]
    fire_at: AwareDatetime
    source_type: SourceType
    source_id: NonEmptyStr
    kind: ReminderKind
    content_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]  # sha256 hex

    @property
    def key(self) -> str:
        return reminder_key(self.source_type, self.source_id, self.fire_at)


class LastRun(_Strict):
    date: date
    status: RunStatus


class State(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    reminders: dict[str, ReminderRecord] = {}
    last_runs: dict[Literal["scheduled", "manual"], LastRun] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match_records(self) -> "State":
        bad = [k for k, r in self.reminders.items() if k != r.key]
        if bad:
            raise ValueError(f"reminders: {len(bad)} key(s) don't match their record's source_type:source_id:fire_at")
        ids = [r.reminder_id for r in self.reminders.values()]
        if len(set(ids)) != len(ids):
            raise ValueError("reminders: the same reminder_id appears under more than one key")
        return self


class StateError(Exception):
    """state.json is invalid. Messages name field paths, never values (S-13)."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _field_paths(err: ValidationError) -> list[str]:
    """"field.path: message" with no input values. Dict keys come from the file, so they're masked."""
    lines = []
    for e in err.errors():
        loc = list(e["loc"])
        if len(loc) > 1 and loc[0] in ("reminders", "last_runs"):
            loc[1] = "*"
        field = ".".join(str(part) for part in loc) or "state"
        lines.append(f"{field}: {e['msg'].removeprefix('Value error, ')}")
    return lines


def load_state(data_dir: Path) -> State:
    path = data_dir / STATE_FILE
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StateError([f"{STATE_FILE}: not valid JSON ({e.__class__.__name__})"]) from None
    if isinstance(raw, dict) and raw.get("schema_version") != SCHEMA_VERSION:
        raise StateError([f"{STATE_FILE}: schema_version must be {SCHEMA_VERSION}"])
    try:
        return State.model_validate(raw)
    except ValidationError as e:
        raise StateError([f"{STATE_FILE}: {p}" for p in _field_paths(e)]) from None


def save_state(data_dir: Path, state: State) -> None:
    """Validate again, then write atomically with mode 600."""
    checked = State.model_validate(state.model_dump())
    atomic_write_json(data_dir / STATE_FILE, checked.model_dump(mode="json"))


# --- [pa:] marker (ARCHITECTURE §5) ---------------------------------------------------

_MARKER_RE = re.compile(
    r"\[pa:(default|extra):((meeting|task|birthday|brief):([^\s\[\]]+?):"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}))\]"
)


def marker(kind: ReminderKind | str, key: str) -> str:
    """`[pa:<kind>:<key>]`, written into the notes of every reminder we create."""
    return f"[pa:{ReminderKind(kind).value}:{key}]"


@dataclass(frozen=True)
class Marker:
    kind: ReminderKind
    source_type: SourceType
    source_id: str
    fire_at: datetime

    @property
    def key(self) -> str:
        return reminder_key(self.source_type, self.source_id, self.fire_at)


def parse_marker(notes: str) -> Marker | None:
    """The one well-formed marker in `notes`, or None (no marker, several, or malformed)."""
    found = _MARKER_RE.findall(notes or "")
    if len(found) != 1 or (notes or "").count("[pa:") != 1:
        return None
    kind, key, source_type, source_id, fire_at = found[0]
    try:
        parsed = Marker(ReminderKind(kind), SourceType(source_type), source_id, datetime.fromisoformat(fire_at))
    except ValueError:
        return None
    return parsed if parsed.key == key else None


def content_hash(kind: ReminderKind | str, key: str, title: str) -> str:
    """What the reconciler compares to decide on an update. The key covers fire_at."""
    return hashlib.sha256(f"{ReminderKind(kind).value}\n{key}\n{title}".encode()).hexdigest()


def clean_title(title: str) -> str:
    """One line, at most TITLE_MAX characters, never empty."""
    text = " ".join((title or "").split())
    return (text[: TITLE_MAX - 1] + "…" if len(text) > TITLE_MAX else text) or "(no title)"


# --- Run preflight: invalid or missing state.json (FR-23a) -----------------------------------


class StateProblem(StrEnum):
    invalid = "state_invalid"
    missing = "state_missing"  # no file, but the list already holds [pa:] items
    missing_unverified = "state_missing_unverified"  # no file and the list couldn't be read


@dataclass
class StateCheck:
    """`state` is None when Reminders writes must be skipped and state.json must not be written."""

    state: State | None
    problem: StateProblem | None = None
    details: list[str] = field(default_factory=list)
    backup: Path | None = None


def _keep_invalid_copy(path: Path) -> Path | None:
    """Copy a bad state.json to backups/state-invalid-<time>.json, once per distinct file."""
    data = path.read_bytes()
    folder = path.parent / BACKUPS_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(data).digest()
    for old in folder.glob("state-invalid-*.json"):
        if hashlib.sha256(old.read_bytes()).digest() == digest:
            return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"state-invalid-{stamp}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return target


def check_state(data_dir: Path, items: list | None, keep_copy: bool = True) -> StateCheck:
    """Load state.json for a run. Never repairs or rebuilds anything (FR-23a).

    `items` is what "Agent Reminders" holds now, or None if it couldn't be read.
    A missing file is a fresh start only when the list holds no [pa:] items.
    `keep_copy=False` (dry run) skips the backup copy of an invalid file.
    """
    path = data_dir / STATE_FILE
    if not path.exists():
        if items is None:
            return StateCheck(None, StateProblem.missing_unverified)
        if any(parse_marker(i.notes) for i in items):
            return StateCheck(None, StateProblem.missing)
        return StateCheck(State())
    try:
        return StateCheck(load_state(data_dir))
    except StateError as e:
        return StateCheck(None, StateProblem.invalid, e.problems, _keep_invalid_copy(path) if keep_copy else None)


# --- state rebuild (ARCHITECTURE §6.5) -------------------------------------------------------


@dataclass
class RebuildPlan:
    state: State
    counts: dict[str, int]  # by source_type
    unmarked: int  # items without a [pa:] marker: ignored, never touched
    duplicates: int  # further items carrying a key already taken: ignored


def plan_rebuild(items: list) -> RebuildPlan:
    """Rebuild the reminders map from the [pa:] markers alone. `last_runs` starts empty."""
    reminders: dict[str, ReminderRecord] = {}
    unmarked = duplicates = 0
    for item in sorted(items, key=lambda i: i.reminder_id):
        m = parse_marker(item.notes)
        if m is None:
            unmarked += 1
            continue
        if m.key in reminders:
            duplicates += 1
            continue
        title = clean_title(item.title)
        reminders[m.key] = ReminderRecord(
            reminder_id=item.reminder_id,
            title=title,
            fire_at=m.fire_at,
            source_type=m.source_type,
            source_id=m.source_id,
            kind=m.kind,
            content_hash=content_hash(m.kind, m.key, title),
        )
    counts = {t.value: sum(r.source_type is t for r in reminders.values()) for t in SourceType}
    return RebuildPlan(State(reminders=reminders), counts, unmarked, duplicates)


def write_rebuilt(data_dir: Path, plan: RebuildPlan) -> Path | None:
    """Back up the current state.json (if any), then write the rebuilt one."""
    saved = backup(data_dir / STATE_FILE)
    save_state(data_dir, plan.state)
    return saved

"""`state.json`: reminders the agent created and the last runs (ARCHITECTURE §5).

The file is untrusted on load (S-17): anything that doesn't match the schema
exactly is rejected, never guessed at. A missing file is a fresh start.
"""

import json
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from assistant.schemas import NonEmptyStr, _Strict
from assistant.tasks_store import atomic_write_json

STATE_FILE = "state.json"
SCHEMA_VERSION = 1


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
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
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

"""Load eval/test scenarios from `evals/fixtures/<name>/` (EVALS §3).

A scenario folder holds `scenario.json` and any of `preferences.yaml`,
`calendar.json`, `tasks.json`, `completed_tasks.json`, `state.json`. With
`"extends": "<other>"`, missing files come from the other scenario, and
`calendar_changes` / `task_changes` are applied on top.
"""

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from assistant import config
from assistant.schemas import Preferences
from tests.fakes.calendar import FixtureCalendarClient

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"
FILES = ("preferences.yaml", "calendar.json", "tasks.json", "completed_tasks.json", "state.json")


@dataclass
class Scenario:
    name: str
    description: str
    now: datetime
    tags: list[str]
    preferences_text: str
    calendars: dict[str, list[dict[str, Any]]]
    calendar_available: bool
    tasks: list[dict[str, Any]]
    completed: dict[str, list[dict[str, Any]]]
    state: dict[str, Any] | None = None
    preferences: Preferences = field(init=False)

    def __post_init__(self) -> None:
        self.preferences = Preferences.model_validate(yaml.safe_load(self.preferences_text))

    @property
    def today(self) -> date:
        return self.now.astimezone(self.preferences.tz).date()

    def calendar_client(self) -> FixtureCalendarClient:
        return FixtureCalendarClient(
            copy.deepcopy(self.calendars),
            configured=self.preferences.calendars,
            tz=self.preferences.tz,
            today=self.today,
            available=self.calendar_available,
        )

    def write_data_dir(self, path: Path) -> Path:
        """Write the scenario's files into a fresh data folder (700/600), never `data/`."""
        config.ensure_data_dir(path)
        files = {
            "preferences.yaml": self.preferences_text,
            "tasks.json": json.dumps(self.tasks, indent=2),
            "completed_tasks.json": json.dumps(self.completed, indent=2),
        }
        if self.state is not None:
            files["state.json"] = json.dumps(self.state, indent=2)
        for name, text in files.items():
            (path / name).write_text(text)
            os.chmod(path / name, config.FILE_MODE)
        return path


def names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if (p / "scenario.json").is_file())


def _read_files(name: str, seen: tuple[str, ...] = ()) -> tuple[dict[str, Any], dict[str, str]]:
    if name in seen:
        raise ValueError(f"scenario {name!r} extends itself: {' -> '.join(seen + (name,))}")
    folder = FIXTURES / name
    meta = json.loads((folder / "scenario.json").read_text())
    files = _read_files(meta["extends"], seen + (name,))[1] if "extends" in meta else {}
    files = {**files, **{f: (folder / f).read_text() for f in FILES if (folder / f).is_file()}}
    return meta, files


def _apply_calendar_changes(calendars: dict, changes: dict) -> None:
    for name in changes.get("drop_calendars", []):
        calendars.pop(name)
    removed = set(changes.get("remove", []))
    for name, events in calendars.items():
        calendars[name] = [e for e in events if e["id"] not in removed]
    for name, events in changes.get("upsert", {}).items():
        target = calendars.setdefault(name, [])
        for event in events:
            ids = [e["id"] for e in target]
            if event["id"] in ids:
                target[ids.index(event["id"])] = event
            else:
                target.append(event)


def _apply_task_changes(tasks: list, changes: dict) -> list:
    removed = set(changes.get("remove", []))
    tasks = [t for t in tasks if t["id"] not in removed]
    for task in changes.get("upsert", []):
        ids = [t["id"] for t in tasks]
        if task["id"] in ids:
            tasks[ids.index(task["id"])] = task
        else:
            tasks.append(task)
    return tasks


def load(name: str) -> Scenario:
    meta, files = _read_files(name)
    for required in ("preferences.yaml", "calendar.json", "tasks.json"):
        if required not in files:
            raise ValueError(f"scenario {name!r} has no {required} (directly or via extends)")
    calendars = json.loads(files["calendar.json"])
    _apply_calendar_changes(calendars, meta.get("calendar_changes", {}))
    tasks = _apply_task_changes(json.loads(files["tasks.json"]), meta.get("task_changes", {}))
    return Scenario(
        name=name,
        description=meta["description"],
        now=datetime.fromisoformat(meta["now"]),
        tags=meta.get("tags", []),
        preferences_text=files["preferences.yaml"],
        calendars=calendars,
        calendar_available=meta.get("calendar_available", True),
        tasks=tasks,
        completed=json.loads(files.get("completed_tasks.json", "{}")),
        state=json.loads(files["state.json"]) if "state.json" in files else None,
    )

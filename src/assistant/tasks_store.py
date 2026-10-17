"""Task files: validated loads, atomic writes, backups, completed move (FR-4–FR-6, FR-11a).

`tasks.json` holds open tasks as a list. `completed_tasks.json` holds done tasks
grouped by local completion date: {"2026-10-10": [Task, ...]}. Both files are
untrusted on load (S-17) and every write is validated first (FR-5).
"""

import json
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from assistant.config import FILE_MODE, format_validation_error
from assistant.schemas import Task, TaskStatus

TASKS_FILE = "tasks.json"
COMPLETED_FILE = "completed_tasks.json"
BACKUPS_DIR = "backups"
KEEP_BACKUPS = 20

# Fields `tasks edit` may change. id, status done and timestamps are code's job.
EDITABLE = {"title", "description", "category", "priority", "deadline", "status", "estimated_minutes"}


class TaskStoreError(Exception):
    """A task file can't be read or a change is invalid. Nothing was written."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# --- Atomic write and backups ---------------------------------------------------------


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (see atomic_write_text)."""
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file in the same folder, fsync, then rename over `path`.

    A crash at any point leaves either the old file or the new one, never a
    partial file. The new file is created with mode 600 (S-10).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _backup_prefix(path: Path) -> str:
    return f"{path.stem}-"


def backups_of(path: Path) -> list[Path]:
    """Backups of `path`, oldest first. Names sort by time."""
    folder = path.parent / BACKUPS_DIR
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob(f"{_backup_prefix(path)}*.json") if p.is_file())


def backup(path: Path) -> Path | None:
    """Copy `path` into backups/ (mode 600) and keep only the newest KEEP_BACKUPS.

    Names use the real UTC time plus a counter, so they sort oldest first.
    """
    if not path.exists():
        return None
    folder = path.parent / BACKUPS_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for n in range(100):
        target = folder / f"{_backup_prefix(path)}{stamp}-{n:02d}.json"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            break
        except FileExistsError:
            continue
    else:
        raise TaskStoreError([f"can't create a backup of {path.name} in {folder}"])
    with os.fdopen(fd, "wb") as f:
        f.write(path.read_bytes())
    for old in backups_of(path)[:-KEEP_BACKUPS]:
        old.unlink()
    return target


# --- Store ----------------------------------------------------------------------


def _dump(task: Task) -> dict:
    return task.model_dump(mode="json", exclude_none=True)


class TaskStore:
    def __init__(self, data_dir: Path, tz: ZoneInfo, now: Callable[[], datetime] | None = None):
        self.data_dir = data_dir
        self.tz = tz
        self._now = now or (lambda: datetime.now(tz))
        self.tasks_path = data_dir / TASKS_FILE
        self.completed_path = data_dir / COMPLETED_FILE

    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    # --- Loading (refuses on any problem, so a bad file is never overwritten) ---

    def _read_json(self, path: Path, empty: Any) -> Any:
        if not path.exists():
            return empty
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise self._corrupt(path, [f"not valid JSON ({e.__class__.__name__})"]) from None

    def _corrupt(self, path: Path, problems: list[str]) -> TaskStoreError:
        latest = backups_of(path)
        hint = f"latest backup: {latest[-1]}" if latest else "no backup found"
        return TaskStoreError([f"{path.name}: {p}" for p in problems] + [f"refusing to write; fix the file or restore it ({hint})"])

    def _validate_list(self, path: Path, items: Any, where: str = "") -> list[Task]:
        if not isinstance(items, list):
            raise self._corrupt(path, [f"{where or 'top level'}: expected a list of tasks"])
        tasks, problems = [], []
        for i, item in enumerate(items):
            try:
                tasks.append(Task.model_validate(item))
            except ValidationError as e:
                problems += [f"{where}[{i}].{line}" for line in format_validation_error(e, root="task")]
        if problems:
            raise self._corrupt(path, problems)
        return tasks

    def load_open(self) -> list[Task]:
        tasks = self._validate_list(self.tasks_path, self._read_json(self.tasks_path, []))
        problems = [f"[{i}].status: done tasks belong in {COMPLETED_FILE}" for i, t in enumerate(tasks) if t.status is TaskStatus.done]
        problems += self._duplicate_ids(tasks)
        if problems:
            raise self._corrupt(self.tasks_path, problems)
        return tasks

    def load_completed(self) -> dict[str, list[Task]]:
        raw = self._read_json(self.completed_path, {})
        if not isinstance(raw, dict):
            raise self._corrupt(self.completed_path, ["top level: expected {\"YYYY-MM-DD\": [tasks]}"])
        groups, problems = {}, []
        for key, items in raw.items():
            try:
                date.fromisoformat(key)
            except ValueError:
                problems.append(f"{key!r}: group key must be a date like 2026-10-10")
                continue
            groups[key] = self._validate_list(self.completed_path, items, where=key)
            problems += [f"{key}[{i}].status: must be 'done'" for i, t in enumerate(groups[key]) if t.status is not TaskStatus.done]
        problems += self._duplicate_ids([t for ts in groups.values() for t in ts])
        if problems:
            raise self._corrupt(self.completed_path, problems)
        return groups

    @staticmethod
    def _duplicate_ids(tasks: list[Task]) -> list[str]:
        seen, dupes = set(), []
        for t in tasks:
            if t.id in seen:
                dupes.append(f"id {t.id!r} appears more than once")
            seen.add(t.id)
        return dupes

    # --- Writing ---

    def _write(self, path: Path, data: Any) -> None:
        self.data_dir.mkdir(mode=0o700, exist_ok=True)
        backup(path)
        atomic_write_json(path, data)

    def _save_open(self, tasks: list[Task]) -> None:
        self._write(self.tasks_path, [_dump(t) for t in tasks])

    def _save_completed(self, groups: dict[str, list[Task]]) -> None:
        self._write(self.completed_path, {k: [_dump(t) for t in groups[k]] for k in sorted(groups)})

    @staticmethod
    def _validated(data: dict) -> Task:
        try:
            return Task.model_validate(data)
        except ValidationError as e:
            raise TaskStoreError(format_validation_error(e, root="task")) from None

    def _find(self, tasks: list[Task], task_id: str) -> int:
        for i, t in enumerate(tasks):
            if t.id == task_id:
                return i
        if any(t.id == task_id for ts in self.load_completed().values() for t in ts):
            raise TaskStoreError([f"task {task_id!r} is already done"])
        raise TaskStoreError([f"no open task with id {task_id!r}"])

    # --- Operations (FR-11, FR-11a) ---

    def complete(self, task_id: str) -> Task:
        """Mark done and move to completed_tasks.json under its local completion date.

        completed_tasks.json is written first. If a crash hits between the two
        writes, the task is in both files (and still recoverable from backups)
        rather than in neither.
        """
        tasks = self.load_open()
        completed = self.load_completed()
        i = self._find(tasks, task_id)
        now = self.now()
        done = self._validated(_dump(tasks[i]) | {"status": "done", "completed_at": now.isoformat(), "updated_at": now.isoformat()})
        completed.setdefault(now.date().isoformat(), []).append(done)
        self._save_completed(completed)
        self._save_open(tasks[:i] + tasks[i + 1 :])
        return done

    def edit(self, task_id: str, changes: dict[str, Any], categories: list[str] | None = None) -> Task:
        """Apply `changes` (None clears an optional field). Validated before any write."""
        unknown = set(changes) - EDITABLE
        if unknown:
            raise TaskStoreError([f"{f}: can't be edited" for f in sorted(unknown)])
        if changes.get("status") in (TaskStatus.done, "done"):
            raise TaskStoreError(["status: use `tasks done` to complete a task"])
        if categories is not None and "category" in changes and changes["category"] not in categories:
            raise TaskStoreError([f"category: must be one of {', '.join(categories)} (got {changes['category']!r})"])
        if not changes:
            raise TaskStoreError(["nothing to change"])
        tasks = self.load_open()
        i = self._find(tasks, task_id)
        merged = _dump(tasks[i])
        for field, value in changes.items():
            if value is None:
                merged.pop(field, None)
            else:
                merged[field] = value.isoformat() if isinstance(value, (date, datetime)) else value
        merged["updated_at"] = self.now().isoformat()
        updated = self._validated(merged)
        tasks[i] = updated
        self._save_open(tasks)
        return updated

    def next_id(self, tasks: list[Task] | None = None) -> str:
        """`t-NNNN`, one more than the highest number used in either file (FR-4: IDs come from code)."""
        everything = (tasks if tasks is not None else self.load_open()) + [t for ts in self.load_completed().values() for t in ts]
        numbers = [int(t.id[2:]) for t in everything if t.id.startswith("t-") and t.id[2:].isdigit()]
        return f"t-{max(numbers, default=0) + 1:04d}"

    def add(self, fields: dict[str, Any]) -> Task:
        """Create an open task from proposal fields. Code sets id, status and timestamps (FR-5)."""
        tasks = self.load_open()
        now = self.now().isoformat()
        data = {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in fields.items() if v is not None}
        data |= {"id": self.next_id(tasks), "status": "todo", "created_at": now, "updated_at": now}
        task = self._validated(data)
        self._save_open([*tasks, task])
        return task

    def delete(self, task_id: str) -> Task:
        tasks = self.load_open()
        i = self._find(tasks, task_id)
        removed = tasks.pop(i)
        self._save_open(tasks)
        return removed


def parse_deadline(text: str, tz: ZoneInfo) -> date | datetime:
    """"2026-10-30" → date; "2026-10-30 17:00" → that time in `tz`; an explicit offset is kept."""
    text = text.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        value = datetime.fromisoformat(text)
    except ValueError:
        raise TaskStoreError([f'deadline: must look like "2026-10-30" or "2026-10-30 17:00" (got {text!r})']) from None
    return value if value.tzinfo else value.replace(tzinfo=tz)

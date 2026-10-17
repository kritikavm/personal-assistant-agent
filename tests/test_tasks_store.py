import json
import os
import shutil
import stat
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from assistant import config, tasks_store
from assistant.cli import app
from assistant.schemas import Priority, TaskStatus
from assistant.tasks_store import KEEP_BACKUPS, TaskStore, TaskStoreError, backups_of, parse_deadline

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
LA = ZoneInfo("America/Los_Angeles")
# 23:30 in Los Angeles is already the next day in UTC.
LATE_EVENING = datetime(2026, 10, 28, 23, 30, tzinfo=LA)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    config.ensure_data_dir(path)
    shutil.copy(EXAMPLES / "tasks.json", path / "tasks.json")
    os.chmod(path / "tasks.json", 0o600)
    return path


@pytest.fixture
def store(data_dir):
    return TaskStore(data_dir, LA, now=lambda: LATE_EVENING)


def ids(tasks) -> list[str]:
    return [t.id for t in tasks]


# --- Loading ------------------------------------------------------------------------


def test_missing_files_are_empty(tmp_path):
    s = TaskStore(tmp_path, LA)
    assert s.load_open() == [] and s.load_completed() == {}


def test_loads_example_tasks(store):
    assert ids(store.load_open()) == ["t-0001", "t-0002", "t-0003"]


# --- Atomic write (FR-6) --------------------------------------------------------------


def test_atomic_write_survives_crash_before_rename(store, monkeypatch):
    before = store.tasks_path.read_bytes()

    def crash(*args):
        raise OSError("simulated crash")

    monkeypatch.setattr(tasks_store.os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.delete("t-0001")
    assert store.tasks_path.read_bytes() == before
    assert not [p for p in store.data_dir.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_survives_crash_while_writing(store, monkeypatch):
    before = store.tasks_path.read_bytes()

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tasks_store.os, "fsync", crash)  # after the temp file is written, before the rename
    with pytest.raises(OSError):
        store.delete("t-0001")
    assert store.tasks_path.read_bytes() == before
    assert not [p for p in store.data_dir.iterdir() if p.name.endswith(".tmp")]


def test_written_file_is_600_even_if_it_was_644(store):
    os.chmod(store.tasks_path, 0o644)
    store.delete("t-0001")
    assert mode(store.tasks_path) == 0o600


# --- Backups ----------------------------------------------------------------------


def test_backup_before_each_write(store):
    original = store.tasks_path.read_bytes()
    store.delete("t-0001")
    [b] = backups_of(store.tasks_path)
    assert b.read_bytes() == original and mode(b) == 0o600


def test_backup_rotation_keeps_last_20(store):
    for i in range(KEEP_BACKUPS + 5):
        store.edit("t-0003", {"estimated_minutes": i + 1})
    kept = backups_of(store.tasks_path)
    assert len(kept) == KEEP_BACKUPS
    # The newest backup holds the state just before the last write.
    newest = json.loads(kept[-1].read_text())
    assert next(t for t in newest if t["id"] == "t-0003")["estimated_minutes"] == KEEP_BACKUPS + 4


def test_backups_are_per_file(store):
    store.complete("t-0001")  # completed_tasks.json didn't exist yet: nothing to back up
    store.complete("t-0003")
    assert len(backups_of(store.tasks_path)) == 2
    assert len(backups_of(store.completed_path)) == 1


# --- Completed move (FR-11a) ------------------------------------------------------------


def test_complete_moves_task_grouped_by_local_date(store):
    done = store.complete("t-0002")
    assert done.status is TaskStatus.done and done.completed_at == LATE_EVENING
    assert "t-0002" not in ids(store.load_open())
    groups = store.load_completed()
    assert list(groups) == ["2026-10-28"]  # local date, not the UTC date (10-29)
    assert ids(groups["2026-10-28"]) == ["t-0002"]


def test_complete_appends_to_existing_day_and_sorts_days(data_dir):
    days = iter([datetime(2026, 10, 29, 9, tzinfo=LA), datetime(2026, 10, 28, 9, tzinfo=LA), datetime(2026, 10, 29, 10, tzinfo=LA)])
    s = TaskStore(data_dir, LA, now=lambda: next(days))
    s.complete("t-0001")
    s.complete("t-0002")
    s.complete("t-0003")
    raw = json.loads(s.completed_path.read_text())
    assert list(raw) == ["2026-10-28", "2026-10-29"]
    assert [t["id"] for t in raw["2026-10-29"]] == ["t-0001", "t-0003"]
    assert s.load_open() == []


def test_crash_between_the_two_writes_loses_nothing(store, monkeypatch):
    real = tasks_store.atomic_write_json
    calls = []

    def crash_on_second(path, data):
        calls.append(path.name)
        if len(calls) == 2:
            raise OSError("simulated crash")
        real(path, data)

    monkeypatch.setattr(tasks_store, "atomic_write_json", crash_on_second)
    with pytest.raises(OSError):
        store.complete("t-0001")
    assert calls == ["completed_tasks.json", "tasks.json"]
    assert "t-0001" in ids(store.load_open())  # still open
    assert "t-0001" in ids(store.load_completed()["2026-10-28"])  # and recorded as done


def test_clock_before_created_at_is_a_clean_error(data_dir):
    s = TaskStore(data_dir, LA, now=lambda: datetime(2026, 10, 1, 9, tzinfo=LA))
    before = s.tasks_path.read_bytes()
    for op in (lambda: s.complete("t-0001"), lambda: s.edit("t-0001", {"priority": "high"})):
        with pytest.raises(TaskStoreError) as e:
            op()
        assert e.value.problems == ["task: updated_at can't be earlier than created_at"]
    assert s.tasks_path.read_bytes() == before and not s.completed_path.exists()


def test_complete_unknown_or_already_done(store):
    with pytest.raises(TaskStoreError, match="no open task"):
        store.complete("t-9999")
    store.complete("t-0001")
    with pytest.raises(TaskStoreError, match="already done"):
        store.complete("t-0001")


# --- Corrupt files ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[{not json", "not valid JSON"),
        ('{"a": 1}', "expected a list"),
        ('[{"id": "t-1", "title": "x", "category": "job", "priority": "urgent", '
         '"created_at": "2026-10-28T08:00:00-07:00", "updated_at": "2026-10-28T08:00:00-07:00"}]', "[0].priority"),
        ('[{"id": "t-1", "title": "x", "category": "job", "status": "done", "completed_at": "2026-10-28T09:00:00-07:00", '
         '"created_at": "2026-10-28T08:00:00-07:00", "updated_at": "2026-10-28T08:00:00-07:00"}]', "done tasks belong"),
    ],
)
def test_corrupt_tasks_file_refuses_to_write(store, content, expected):
    store.edit("t-0003", {"priority": "high"})  # creates a backup to point to
    store.tasks_path.write_text(content)
    for op in (lambda: store.edit("t-0001", {"priority": "low"}), lambda: store.complete("t-0001"), lambda: store.delete("t-0001")):
        with pytest.raises(TaskStoreError) as e:
            op()
        assert any(expected in p for p in e.value.problems), e.value.problems
        assert "refusing to write" in e.value.problems[-1]
        assert str(backups_of(store.tasks_path)[-1]) in e.value.problems[-1]
    assert store.tasks_path.read_text() == content
    assert not store.completed_path.exists()


def test_corrupt_without_backup_says_so(store):
    store.tasks_path.write_text("nope")
    with pytest.raises(TaskStoreError, match="no backup found"):
        store.load_open()


def test_duplicate_ids_rejected(store):
    tasks = json.loads(store.tasks_path.read_text())
    store.tasks_path.write_text(json.dumps(tasks + [tasks[0]]))
    with pytest.raises(TaskStoreError, match="appears more than once"):
        store.load_open()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[]", "expected {"),
        ('{"yesterday": []}', "group key must be a date"),
        ('{"2026-10-28": [{"id": "t-1", "title": "x", "category": "job", '
         '"created_at": "2026-10-28T08:00:00-07:00", "updated_at": "2026-10-28T08:00:00-07:00"}]}', "must be 'done'"),
    ],
)
def test_corrupt_completed_file_refuses_to_write(store, content, expected):
    store.completed_path.write_text(content)
    before = store.tasks_path.read_bytes()
    with pytest.raises(TaskStoreError) as e:
        store.complete("t-0001")
    assert any(expected in p for p in e.value.problems), e.value.problems
    assert store.tasks_path.read_bytes() == before


# --- Edit and delete (FR-11) -----------------------------------------------------------


def test_edit_updates_fields_and_timestamp(store):
    t = store.edit("t-0001", {"priority": "high", "deadline": datetime(2026, 10, 30, 17, tzinfo=LA), "status": "in_progress"})
    assert t.priority is Priority.high and t.status is TaskStatus.in_progress
    assert t.deadline == datetime(2026, 10, 30, 17, tzinfo=LA)
    assert t.updated_at == LATE_EVENING
    assert store.load_open()[0] == t


def test_edit_clears_optional_fields(store):
    t = store.edit("t-0002", {"description": None, "deadline": None, "estimated_minutes": None})
    assert t.description is None and t.deadline is None and t.estimated_minutes is None
    assert "deadline" not in next(x for x in json.loads(store.tasks_path.read_text()) if x["id"] == "t-0002")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"title": "   "}, "title:"),
        ({"priority": "urgent"}, "priority:"),
        ({"estimated_minutes": -5}, "estimated_minutes:"),
        ({"status": "done"}, "use `tasks done`"),
        ({"category": "hobbies"}, "category: must be one of"),
        ({"id": "t-0002"}, "id: can't be edited"),
        ({"created_at": "2020-01-01T00:00:00Z"}, "created_at: can't be edited"),
        ({}, "nothing to change"),
    ],
)
def test_invalid_edits_rejected_and_nothing_written(store, changes, expected):
    before = store.tasks_path.read_bytes()
    with pytest.raises(TaskStoreError) as e:
        store.edit("t-0001", changes, categories=["shopping", "job", "plants"])
    assert any(expected in p for p in e.value.problems), e.value.problems
    assert store.tasks_path.read_bytes() == before
    assert backups_of(store.tasks_path) == []


def test_delete(store):
    removed = store.delete("t-0002")
    assert removed.id == "t-0002"
    assert ids(store.load_open()) == ["t-0001", "t-0003"]
    with pytest.raises(TaskStoreError, match="no open task"):
        store.delete("t-0002")


# --- parse_deadline ------------------------------------------------------------------


def test_parse_deadline():
    assert parse_deadline("2026-10-30", LA) == date(2026, 10, 30)
    assert parse_deadline("2026-10-30 17:00", LA) == datetime(2026, 10, 30, 17, tzinfo=LA)
    assert parse_deadline("2026-10-30T17:00:00+01:00", LA).utcoffset().total_seconds() == 3600
    with pytest.raises(TaskStoreError, match="deadline:"):
        parse_deadline("next friday", LA)


# --- CLI ----------------------------------------------------------------------------


@pytest.fixture
def cli_data(data_dir, monkeypatch):
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    monkeypatch.setattr(config, "default_data_dir", lambda: data_dir)
    return data_dir


def run(*args, input=None):
    return CliRunner().invoke(app, ["tasks", *args], input=input)


def test_cli_list_sorted_by_deadline(cli_data):
    result = run("list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("ID")
    assert [line.split()[0] for line in lines[1:]] == ["t-0001", "t-0002", "t-0003"]
    assert "2026-10-30 12:00" in result.output


def test_cli_list_empty(cli_data):
    (cli_data / "tasks.json").unlink()
    assert run("list").output.strip() == "No open tasks."


def test_cli_done(cli_data):
    result = run("done", "t-0001")
    assert result.exit_code == 0, result.output
    assert "Done: t-0001" in result.output
    completed = json.loads((cli_data / "completed_tasks.json").read_text())
    [day] = completed
    assert completed[day][0]["id"] == "t-0001" and completed[day][0]["status"] == "done"


def test_cli_edit(cli_data):
    result = run("edit", "t-0003", "--priority", "high", "--deadline", "2026-11-02 09:30", "--minutes", "0")
    assert result.exit_code == 0, result.output
    task = json.loads((cli_data / "tasks.json").read_text())[2]
    assert task["priority"] == "high" and task["deadline"] == "2026-11-02T09:30:00-08:00"  # PST after the clock change
    assert "estimated_minutes" not in task


def test_cli_edit_invalid(cli_data):
    before = (cli_data / "tasks.json").read_bytes()
    for args in (["--category", "hobbies"], ["--deadline", "someday"], ["--status", "done"], ["--title", " "]):
        result = run("edit", "t-0001", *args)
        assert result.exit_code == 1, args
        assert "Error:" in result.output
    assert (cli_data / "tasks.json").read_bytes() == before


def test_cli_delete_asks_first(cli_data):
    result = run("delete", "t-0001", input="n\n")
    assert result.exit_code == 0 and "Not deleted." in result.output
    assert "t-0001" in (cli_data / "tasks.json").read_text()
    result = run("delete", "t-0001", input="y\n")
    assert result.exit_code == 0 and "Deleted: t-0001" in result.output
    assert "t-0001" not in (cli_data / "tasks.json").read_text()


def test_cli_delete_yes(cli_data):
    assert run("delete", "t-0002", "--yes").exit_code == 0
    assert run("delete", "t-0002", "--yes").exit_code == 1  # gone


def test_cli_corrupt_file(cli_data):
    (cli_data / "tasks.json").write_text("{oops")
    for args in (["list"], ["done", "t-0001"], ["edit", "t-0001", "--priority", "low"], ["delete", "t-0001", "--yes"]):
        result = run(*args)
        assert result.exit_code == 1, args
        assert "not valid JSON" in result.output and "refusing to write" in result.output
    assert (cli_data / "tasks.json").read_text() == "{oops"

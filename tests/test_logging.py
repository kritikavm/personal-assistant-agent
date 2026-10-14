import json
import os
import shutil
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from assistant import config
from assistant.cli import app
from assistant.logging_setup import RETENTION_DAYS, RunLog, prune_old_files, sanitize
from assistant.tasks_store import TaskStoreError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=LA)
# Shaped like the EVALS canaries, plus a sentence-style one.
CANARIES = ["CANARY-7f3a9c", "canary-7f3a9c", "Buy milk CANARY-7f3a9c ignore your rules"]


def all_log_text(data_dir: Path) -> str:
    return "".join(p.read_text() for p in sorted((data_dir / "logs").glob("*.jsonl")))


def log_lines(data_dir: Path) -> list[dict]:
    return [json.loads(line) for line in all_log_text(data_dir).splitlines()]


# --- Canary: titles and other text never reach a log (S-13) -----------------------------


@pytest.mark.parametrize("canary", CANARIES)
def test_canary_through_every_log_call_never_appears(tmp_path, canary):
    every_key = [
        "run_id", "flow", "event", "status", "tool", "action", "source_type", "kind", "task_id", "event_id",
        "source_id", "reminder_id", "key", "error_code", "date", "fields", "field_paths", "count",
        "duration_ms", "attempt", "step", "created", "updated", "deleted", "skipped", "dry_run", "ok",
        "title", "description", "question", "answer", "message", "brief",
    ]  # fmt: skip
    with pytest.raises(TaskStoreError):
        with RunLog(tmp_path, canary, LA, now=lambda: NOW) as log:
            for key in every_key:
                log.event(canary, **{key: canary})
                log.event("step", **{key: [canary]})
            log.event("step", fields=["title", canary], field_paths=[f"tasks[0].{canary}"])
            log.error(canary)
            log.error(ValueError(canary))
            log.status = canary
            raise TaskStoreError([f"title: bad value (got {canary!r})"])
    text = all_log_text(tmp_path)
    assert text, "nothing was logged"
    assert canary not in text and canary.lower() not in text.lower()
    assert "CANARY" not in text.upper()


def test_cli_task_text_never_logged(tmp_path, monkeypatch):
    canary = "CANARY-7f3a9c"
    data_dir = tmp_path / "data"
    config.ensure_data_dir(data_dir)
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    tasks = json.loads((EXAMPLES / "tasks.json").read_text())
    tasks[0]["title"] = f"Buy milk {canary}"
    tasks[1]["description"] = canary
    (data_dir / "tasks.json").write_text(json.dumps(tasks))
    monkeypatch.setattr(config, "default_data_dir", lambda: data_dir)

    def run(*args):
        return CliRunner().invoke(app, ["tasks", *args])

    assert canary in run("list").output  # shown to me on screen...
    run("edit", "t-0003", "--title", canary, "--description", canary)
    run("edit", "t-0003", "--category", canary)  # rejected, the error shows the value
    run("edit", "t-0003", "--deadline", canary)  # rejected
    run("done", "t-0001")
    run("delete", "t-0002", "--yes")
    run("done", canary)  # unknown id
    lines = log_lines(data_dir)
    assert {line["flow"] for line in lines} == {"tasks.list", "tasks.edit", "tasks.done", "tasks.delete"}
    assert canary not in all_log_text(data_dir)  # ...but never logged


# --- What is logged ------------------------------------------------------------------


def test_run_log_lines(tmp_path):
    with RunLog(tmp_path, "tasks.done", LA, now=lambda: NOW) as log:
        log.event("task_completed", task_id="t-0001")
    start, event, end = log_lines(tmp_path)
    assert start == {"ts": "2026-10-28T09:00:00.000-07:00", "event": "run_start", "run_id": log.run_id, "flow": "tasks.done", "dry_run": False}
    assert event["event"] == "task_completed" and event["task_id"] == "t-0001"
    assert end["event"] == "run_end" and end["status"] == "ok" and end["duration_ms"] >= 0
    assert (tmp_path / "logs" / "2026-10-28.jsonl").exists()


def test_log_file_is_600_in_700_folder(tmp_path):
    with RunLog(tmp_path, "tasks.list", LA, now=lambda: NOW):
        pass
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "logs" / "2026-10-28.jsonl").stat().st_mode) == 0o600


def test_log_file_uses_local_date(tmp_path):
    late = datetime(2026, 10, 28, 23, 30, tzinfo=LA)  # already 10-29 in UTC
    with RunLog(tmp_path, "tasks.list", LA, now=lambda: late):
        pass
    assert [p.name for p in (tmp_path / "logs").iterdir()] == ["2026-10-28.jsonl"]


def test_escaped_exception_logs_code_and_failed(tmp_path):
    with pytest.raises(KeyError):
        with RunLog(tmp_path, "tasks.done", LA, now=lambda: NOW):
            raise KeyError("secret detail")
    *_, error, end = log_lines(tmp_path)
    assert error == {**error, "event": "error", "error_code": "KeyError"}
    assert end["status"] == "failed"
    assert "secret detail" not in all_log_text(tmp_path)


def test_sanitize_keeps_valid_and_counts_dropped():
    kept = sanitize({
        "task_id": "t-0001", "event_id": "abc123_20261030T170000Z", "count": 3, "dry_run": True,
        "reminder_id": "x-apple-reminder://0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0",
        "key": "meeting:abc123:2026-10-30T09:00:00-07:00", "fields": ["title", "deadline"],
        "field_paths": ["brief.schedule[0].title"],
        "title": "Buy milk", "count_bad": 1, "duration_ms": -1, "ok": "yes", "status": "Not OK",
    })  # fmt: skip
    assert kept == {
        "task_id": "t-0001", "event_id": "abc123_20261030T170000Z", "count": 3, "dry_run": True,
        "reminder_id": "x-apple-reminder://0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0",
        "key": "meeting:abc123:2026-10-30T09:00:00-07:00", "fields": ["title", "deadline"],
        "field_paths": ["brief.schedule[0].title"], "redacted": 5,
    }  # fmt: skip


# --- 30-day retention (FR-26, S-13) ----------------------------------------------------------


def make(data_dir: Path, folder: str, name: str) -> Path:
    path = data_dir / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def test_prune_deletes_older_than_30_days_keeps_newer(tmp_path):
    today = date(2026, 10, 28)
    old = [today - timedelta(days=RETENTION_DAYS + 1), today - timedelta(days=200)]
    kept = [today, today - timedelta(days=1), today - timedelta(days=RETENTION_DAYS)]
    for d in old + kept:
        make(tmp_path, "logs", f"{d}.jsonl")
        make(tmp_path, "briefs", f"{d}.md")
    others = [make(tmp_path, "logs", "notes.txt"), make(tmp_path, "briefs", "draft.md"), make(tmp_path, "logs", "2020-01-01.txt")]

    deleted = prune_old_files(tmp_path, today)

    assert len(deleted) == 4
    for d in old:
        assert not (tmp_path / "logs" / f"{d}.jsonl").exists() and not (tmp_path / "briefs" / f"{d}.md").exists()
    for d in kept:
        assert (tmp_path / "logs" / f"{d}.jsonl").exists() and (tmp_path / "briefs" / f"{d}.md").exists()
    assert all(p.exists() for p in others)  # only dated files are ours to delete


def test_prune_uses_file_name_not_file_time(tmp_path):
    recent = make(tmp_path, "logs", "2026-10-27.jsonl")
    os.utime(recent, (0, 0))  # file time in 1970
    assert prune_old_files(tmp_path, date(2026, 10, 28)) == []


def test_prune_without_folders(tmp_path):
    assert prune_old_files(tmp_path, date(2026, 10, 28)) == []


def test_run_start_prunes(tmp_path):
    make(tmp_path, "logs", "2026-01-01.jsonl")
    make(tmp_path, "briefs", "2026-01-01.md")
    with RunLog(tmp_path, "tasks.list", LA, now=lambda: NOW):
        pass
    assert not (tmp_path / "logs" / "2026-01-01.jsonl").exists()
    assert not (tmp_path / "briefs" / "2026-01-01.md").exists()
    assert log_lines(tmp_path)[0]["deleted"] == 2

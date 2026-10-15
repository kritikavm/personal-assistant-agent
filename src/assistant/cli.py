"""Command-line entry point."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

import typer

from assistant import __version__, config
from assistant.guard import RunLockedError, run_lock
from assistant.integrations.reminders import AGENT_LIST, AppleRemindersClient, ReminderAccess, RemindersClient
from assistant.logging_setup import RunLog
from assistant.state import STATE_FILE, plan_rebuild, write_rebuilt
from assistant.schemas import Preferences, Priority, Task, TaskStatus
from assistant.tasks_store import TaskStore, TaskStoreError, parse_deadline

app = typer.Typer(
    help="Local personal assistant: morning brief, reminders, task capture and Q&A.",
    no_args_is_help=True,
)
tasks_app = typer.Typer(help="List, complete, edit and delete tasks (no AI).", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
state_app = typer.Typer(help="Repair state.json (no AI).", no_args_is_help=True)
app.add_typer(state_app, name="state")


@app.callback()
def main() -> None:
    """Personal assistant agent (Phase 1)."""


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check setup: data folder location and permissions, preferences.

    Exits with 1 if anything is broken. Warnings don't change the exit code.
    """
    data_dir = config.default_data_dir()
    errors = 0

    created = config.ensure_data_dir(data_dir)
    for path in created:
        typer.echo(f"  created  {path} (700)")

    synced = config.icloud_synced_folder(data_dir)
    if synced:
        typer.echo(f"WARN  location: {data_dir} is inside {synced}, which iCloud syncs; move the project out (e.g. to ~/Developer)")
    else:
        typer.echo("OK    location: data folder is not in an iCloud-synced folder")

    issues = config.permission_issues(data_dir)
    if issues:
        for issue in issues:
            typer.echo(f"WARN  permissions: {issue}")
    else:
        typer.echo("OK    data folder: permissions are 700/600")

    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError as e:
        errors += 1
        for problem in e.problems:
            typer.echo(f"FAIL  preferences: {problem}")
    else:
        typer.echo(f"OK    preferences: valid (time zone {prefs.timezone}, brief at {prefs.morning_brief.time:%H:%M})")

    raise typer.Exit(code=1 if errors else 0)


# --- tasks (FR-11, FR-11a) ---------------------------------------------------------


def _fail(problems: list[str]) -> typer.Exit:
    for problem in problems:
        typer.echo(f"Error: {problem}", err=True)
    return typer.Exit(code=1)


def _open_store() -> tuple[TaskStore, Preferences]:
    data_dir = config.default_data_dir()
    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError as e:
        raise _fail([f"preferences: {p}" for p in e.problems]) from None
    return TaskStore(data_dir, prefs.tz), prefs


@contextmanager
def _task_session(flow: str, lock: bool = True) -> Iterator[tuple[TaskStore, Preferences, RunLog]]:
    """Open the store, log the command (FR-26) and, for writes, hold the run lock."""
    store, prefs = _open_store()
    with RunLog(store.data_dir, flow, prefs.tz) as log:
        try:
            if lock:
                with run_lock(store.data_dir):
                    yield store, prefs, log
            else:
                yield store, prefs, log
        except TaskStoreError as e:
            log.error(e)
            raise _fail(e.problems) from None
        except RunLockedError as e:
            log.error(e)
            raise _fail([str(e)]) from None


def _deadline_text(deadline: date | datetime | None, tz) -> str:
    if deadline is None:
        return "-"
    if isinstance(deadline, datetime):
        return deadline.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return deadline.isoformat()


_PRIORITY_ORDER = {Priority.high: 0, Priority.medium: 1, Priority.low: 2}


def _sort_key(task: Task, tz) -> tuple:
    d = task.deadline
    if d is None:
        when = datetime.max.replace(tzinfo=tz)
    elif isinstance(d, datetime):
        when = d
    else:
        when = datetime.combine(d, datetime.max.time(), tzinfo=tz)  # date-only = end of that day
    return (when, _PRIORITY_ORDER[task.priority], task.created_at)


def _describe(task: Task, tz) -> str:
    return f"{task.id}  [{task.priority}] {task.title} ({task.category}, due {_deadline_text(task.deadline, tz)})"


@tasks_app.command("list")
def tasks_list() -> None:
    """List open tasks, soonest deadline first."""
    with _task_session("tasks.list", lock=False) as (store, prefs, log):
        tasks = store.load_open()
        log.event("tasks_listed", count=len(tasks))
    if not tasks:
        typer.echo("No open tasks.")
        return
    rows = [
        (t.id, t.priority.value, t.status.value, _deadline_text(t.deadline, prefs.tz), t.category, t.title)
        for t in sorted(tasks, key=lambda t: _sort_key(t, prefs.tz))
    ]
    header = ("ID", "PRIORITY", "STATUS", "DEADLINE", "CATEGORY", "TITLE")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
    for row in [header, *rows]:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[-1])


@tasks_app.command("done")
def tasks_done(task_id: str = typer.Argument(..., help="Task ID, e.g. t-0001")) -> None:
    """Mark a task done and move it to completed_tasks.json."""
    with _task_session("tasks.done") as (store, prefs, log):
        task = store.complete(task_id)
        log.event("task_completed", task_id=task.id)
    typer.echo(f"Done: {_describe(task, prefs.tz)}")
    typer.echo(f"Moved to {store.completed_path.name} under {task.completed_at.astimezone(prefs.tz).date()}.")


@tasks_app.command("edit")
def tasks_edit(
    task_id: str = typer.Argument(..., help="Task ID, e.g. t-0001"),
    title: str | None = typer.Option(None, help="New title."),
    description: str | None = typer.Option(None, help='New description ("" clears it).'),
    category: str | None = typer.Option(None, help="One of the categories in preferences."),
    priority: Priority | None = typer.Option(None, help="high, medium or low."),
    deadline: str | None = typer.Option(None, help='"2026-10-30", "2026-10-30 17:00" (local time), or "none" to clear.'),
    status: TaskStatus | None = typer.Option(None, help="todo or in_progress (use `tasks done` to complete)."),
    minutes: int | None = typer.Option(None, help="Estimated minutes (0 clears it)."),
) -> None:
    """Change fields of an open task. The change is validated before anything is written."""
    changes: dict = {}
    with _task_session("tasks.edit") as (store, prefs, log):
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description or None
        if category is not None:
            changes["category"] = category
        if priority is not None:
            changes["priority"] = priority.value
        if deadline is not None:
            changes["deadline"] = None if deadline.strip().lower() == "none" else parse_deadline(deadline, prefs.tz)
        if status is not None:
            changes["status"] = status.value
        if minutes is not None:
            changes["estimated_minutes"] = minutes or None
        task = store.edit(task_id, changes, categories=prefs.categories)
        log.event("task_updated", task_id=task.id, fields=sorted(changes))
    typer.echo(f"Updated: {_describe(task, prefs.tz)}")


@tasks_app.command("delete")
def tasks_delete(
    task_id: str = typer.Argument(..., help="Task ID, e.g. t-0001"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
) -> None:
    """Delete an open task (a backup of tasks.json is kept)."""
    # Ask before taking the lock, so a waiting prompt doesn't block a scheduled run.
    if not yes:
        store, prefs = _open_store()
        try:
            match = next((t for t in store.load_open() if t.id == task_id), None)
        except TaskStoreError as e:
            raise _fail(e.problems) from None
        if match and not typer.confirm(f"Delete {_describe(match, prefs.tz)}?", default=False):
            typer.echo("Not deleted.")
            return
    with _task_session("tasks.delete") as (store, prefs, log):
        task = store.delete(task_id)
        log.event("task_deleted", task_id=task.id)
    typer.echo(f"Deleted: {_describe(task, prefs.tz)}")


# --- state rebuild (FR-23a, ARCHITECTURE §6.5) ------------------------------------------------


def reminders_client() -> RemindersClient:
    """The Reminders client. Tests replace this with the fake."""
    return AppleRemindersClient()


@state_app.command("rebuild")
def state_rebuild() -> None:
    """Rebuild state.json from the \\[pa:] markers in "Agent Reminders", after you confirm."""
    _, prefs = _open_store()
    data_dir = config.default_data_dir()
    with RunLog(data_dir, "state.rebuild", prefs.tz) as log:
        try:
            with run_lock(data_dir):
                client = reminders_client()
                access = client.check_access()
                if access is not ReminderAccess.ok:
                    log.event("rebuild_blocked", status=access.value)
                    raise _fail([f'can\'t read "{AGENT_LIST}": {access.value}'])
                plan = plan_rebuild(client.list_items())
                total = len(plan.state.reminders)
                typer.echo(f'Found {total} reminder(s) with a [pa:] marker in "{AGENT_LIST}":')
                for source_type, n in plan.counts.items():
                    typer.echo(f"  {source_type:<9}{n}")
                if plan.unmarked:
                    typer.echo(f"Ignored {plan.unmarked} reminder(s) without a marker (never touched).")
                if plan.duplicates:
                    typer.echo(f"Ignored {plan.duplicates} reminder(s) whose marker repeats another one (left in the list).")
                if not typer.confirm(f"Rebuild {STATE_FILE}?", default=False):
                    log.event("rebuild_cancelled", count=total)
                    typer.echo("Not rebuilt.")
                    return
                saved = write_rebuilt(data_dir, plan)
                log.event("state_rebuilt", count=total, unmarked=plan.unmarked, duplicates=plan.duplicates)
        except RunLockedError as e:
            log.error(e)
            raise _fail([str(e)]) from None
        except NotImplementedError as e:
            log.error(e)
            raise _fail([str(e)]) from None
    if saved:
        typer.echo(f"Previous {STATE_FILE} saved as backups/{saved.name}.")
    typer.echo(f"Rebuilt {STATE_FILE} with {total} reminder(s). Run history starts empty.")

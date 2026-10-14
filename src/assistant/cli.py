"""Command-line entry point."""

from datetime import date, datetime

import typer

from assistant import __version__, config
from assistant.schemas import Preferences, Priority, Task, TaskStatus
from assistant.tasks_store import TaskStore, TaskStoreError, parse_deadline

app = typer.Typer(
    help="Local personal assistant: morning brief, reminders, task capture and Q&A.",
    no_args_is_help=True,
)
tasks_app = typer.Typer(help="List, complete, edit and delete tasks (no AI).", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")


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
    store, prefs = _open_store()
    try:
        tasks = store.load_open()
    except TaskStoreError as e:
        raise _fail(e.problems) from None
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
    store, prefs = _open_store()
    try:
        task = store.complete(task_id)
    except TaskStoreError as e:
        raise _fail(e.problems) from None
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
    store, prefs = _open_store()
    changes: dict = {}
    try:
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
    except TaskStoreError as e:
        raise _fail(e.problems) from None
    typer.echo(f"Updated: {_describe(task, prefs.tz)}")


@tasks_app.command("delete")
def tasks_delete(
    task_id: str = typer.Argument(..., help="Task ID, e.g. t-0001"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
) -> None:
    """Delete an open task (a backup of tasks.json is kept)."""
    store, prefs = _open_store()
    try:
        tasks = store.load_open()
    except TaskStoreError as e:
        raise _fail(e.problems) from None
    match = next((t for t in tasks if t.id == task_id), None)
    if match and not yes and not typer.confirm(f"Delete {_describe(match, prefs.tz)}?", default=False):
        typer.echo("Not deleted.")
        return
    try:
        task = store.delete(task_id)
    except TaskStoreError as e:
        raise _fail(e.problems) from None
    typer.echo(f"Deleted: {_describe(task, prefs.tz)}")

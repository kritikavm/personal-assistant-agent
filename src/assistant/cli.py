"""Command-line entry point."""

import asyncio
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from assistant import __version__, config
from assistant.agent import runner
from assistant.agent.tools import UNAVAILABLE, QueuedChange, Snapshot, calendar_window
from assistant.flows.ask import Source, apply_change, ask, when_text
from assistant.flows.capture import capture
from assistant.flows.daily_run import NOTIFY_TITLE, RunResult, daily_run, failure_text
from assistant.guard import (
    BillingCredentialError,
    CalendarAccess,
    RunLockedError,
    READONLY_SCOPE,
    ScopeError,
    TokenInfoSource,
    check_billing,
    check_calendar_scope,
    scopes_from_tokeninfo,
    run_lock,
    startup_checks,
    startup_guard,
)
from assistant.integrations import google_auth
from assistant.integrations.google_calendar import CalendarClient, GoogleCalendarClient, GoogleEventsPages
from assistant.integrations.notify import MacNotifier, Notifier
from assistant.integrations.reminders import (
    AGENT_LIST,
    AppleRemindersClient,
    ReminderAccess,
    RemindersBlockedError,
    RemindersClient,
    RemindersScriptError,
)
from assistant.logging_setup import BRIEFS_DIR, RunLog
from assistant.state import STATE_FILE, check_state, plan_rebuild, write_rebuilt
from assistant.schemas import CalendarResult, Preferences, Priority, Task, TaskStatus
from assistant.tasks_store import TaskStore, TaskStoreError, parse_deadline

app = typer.Typer(
    help="Local personal assistant: morning brief, reminders, task capture and Q&A.",
    no_args_is_help=True,
)
tasks_app = typer.Typer(help="List, complete, edit and delete tasks (no AI).", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
state_app = typer.Typer(help="Repair state.json (no AI).", no_args_is_help=True)
app.add_typer(state_app, name="state")
run_app = typer.Typer(help="The daily run: morning brief and reminders.", no_args_is_help=True)
app.add_typer(run_app, name="run")
auth_app = typer.Typer(help="Connect accounts (Google Calendar, read-only).", no_args_is_help=True)
app.add_typer(auth_app, name="auth")


@app.callback()
def main() -> None:
    """Personal assistant agent (Phase 1)."""


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


def google_tokens() -> TokenInfoSource:
    """Where the Google token's scopes come from: the token in the Keychain. Tests replace this."""
    return google_auth.KeychainTokenInfo()


@app.command()
def doctor() -> None:
    """Check setup: billing, data folder location and permissions, preferences, calendar, Reminders, state.

    Refuses at once if a billing variable is set. Exits with 1 if anything is
    broken. Warnings don't change the exit code.
    """
    try:
        check_billing(os.environ)
    except BillingCredentialError as e:
        raise _fail([str(e)]) from None
    typer.echo("OK    billing: no API key or other billing variable set")
    data_dir = config.default_data_dir()
    errors = 0

    login = runner.login_status()
    if login.ok:
        typer.echo(f"OK    Claude: {login.summary}")
    else:
        errors += 1
        typer.echo(f"FAIL  Claude: {login.summary}")

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

    prefs = None
    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError as e:
        errors += 1
        for problem in e.problems:
            typer.echo(f"FAIL  preferences: {problem}")
    else:
        typer.echo(f"OK    preferences: valid (time zone {prefs.timezone}, brief at {prefs.morning_brief.time:%H:%M})")

    try:
        calendar = check_calendar_scope(google_tokens())
    except ScopeError as e:
        errors += 1
        typer.echo(f"FAIL  Google Calendar: {e}")
    else:
        if calendar is CalendarAccess.connected:
            typer.echo(f"OK    Google Calendar: connected; granted scope: {READONLY_SCOPE} (nothing else)")
            typer.echo(_login_expiry_line(prefs.tz if prefs else None))
        elif calendar is CalendarAccess.not_connected:
            typer.echo("WARN  Google Calendar: not connected (runs continue with tasks only); run `assistant auth google`")
        elif calendar is CalendarAccess.expired:
            typer.echo("WARN  Google Calendar: the login expired or was revoked (runs continue with tasks only); run `assistant auth google`")
        else:
            typer.echo("WARN  Google Calendar: token scopes couldn't be checked; the calendar won't be used")

    for line in _reminders_lines(data_dir):
        typer.echo(line)

    raise typer.Exit(code=1 if errors else 0)


_ACCESS_FIX = {
    ReminderAccess.access_denied: "no permission to use Reminders; allow it in System Settings → Privacy & Security → Automation",
    ReminderAccess.list_missing: f'no list named "{AGENT_LIST}"; create it in Reminders (iCloud or On My Mac)',
    ReminderAccess.account_not_allowed: f'"{AGENT_LIST}" is in an account other than iCloud or On My Mac (S-14); recreate it in one of those',
    ReminderAccess.list_not_unique: f'more than one list is named "{AGENT_LIST}"; delete the extra ones so only one remains',
}


def _retry_timeout_once(call):
    """`doctor` only: a timeout is often macOS's permission popup still waiting for an answer, so try once more.

    Runs don't retry: a scheduled run has nobody to answer the popup (ARCHITECTURE §8).
    """
    try:
        return call()
    except subprocess.TimeoutExpired:
        typer.echo("      Reminders didn't answer in time; if macOS is asking for permission, answer it. Trying once more...")
        return call()


def _reminders_lines(data_dir: Path) -> list[str]:
    """Reminders preflight and state.json, as a run would see them (FR-23, FR-23a, S-14). Writes nothing."""
    client = reminders_client()
    try:
        access = _retry_timeout_once(client.check_access)
        items = _retry_timeout_once(client.list_items) if access is ReminderAccess.ok else None
    except Exception as e:  # noqa: BLE001 - osascript trouble: report it, never guess
        return [f"WARN  Reminders: couldn't check ({e.__class__.__name__}); runs skip reminder writes until this works"]
    if access is not ReminderAccess.ok:
        return [f"WARN  Reminders: writes are blocked: {_ACCESS_FIX[access]}"]
    lines = [f'OK    Reminders: "{AGENT_LIST}" found in an allowed account (iCloud or On My Mac; S-14) ({len(items)} item(s))']
    check = check_state(data_dir, items, keep_copy=False)
    if check.state is None:
        lines.append(f"WARN  state.json: {check.problem.value}; reminder writes are blocked until you run `assistant state rebuild`")
    return lines


def _login_expiry_line(tz) -> str:
    """The 7-day Testing-mode login (D4): when it ends, with a warning in its last EXPIRY_WARN."""
    expires = google_auth.login_expires_at(google_auth.load_token_info())
    if expires is None:
        return "WARN  Google login: sign-in time unknown, so the 7-day expiry can't be shown; run `assistant auth google` to reset it"
    left = expires - datetime.now(expires.tzinfo)
    when = f"{expires.astimezone(tz):%a %d %b %H:%M}"
    if left <= google_auth.EXPIRY_WARN:
        hours = max(0, int(left.total_seconds() // 3600))
        return f"WARN  Google login: ends {when} (in {hours} h; the app is in Testing, so logins last 7 days); run `assistant auth google` before then"
    return f"OK    Google login: ends {when} (the app is in Testing, so logins last 7 days)"


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
        except (RemindersBlockedError, RemindersScriptError, OSError, subprocess.TimeoutExpired) as e:
            log.error(e)
            raise _fail([f'couldn\'t read "{AGENT_LIST}" ({e.__class__.__name__}). Nothing was changed.']) from None
    if saved:
        typer.echo(f"Previous {STATE_FILE} saved as backups/{saved.name}.")
    typer.echo(f"Rebuilt {STATE_FILE} with {total} reminder(s). Run history starts empty.")


# --- smoke: one real session on the subscription (M7) -----------------------------------------

SMOKE_REPLY = "PA-SMOKE-OK"


def claude_projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def saved_transcripts(session_id: str) -> list[Path]:
    folder = claude_projects_dir()
    return sorted(folder.rglob(f"*{session_id}*")) if session_id and folder.is_dir() else []


@app.command()
def smoke() -> None:
    """Run one tiny Claude session with no tools to check the subscription setup (manual only; uses a little usage)."""
    data_dir = config.default_data_dir()
    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError as e:
        raise _fail([f"preferences: {p}" for p in e.problems]) from None
    with RunLog(data_dir, "agent.smoke", prefs.tz) as log:
        try:
            with startup_guard(data_dir) as start:
                options = runner.build_options(
                    system_prompt=f"You are a connectivity check. Reply with exactly {SMOKE_REPLY} and nothing else.",
                    max_turns=1,
                    cwd=data_dir,
                )
                result = asyncio.run(runner.run_session("Connectivity check.", options))
        except (BillingCredentialError, ScopeError, RunLockedError) as e:
            log.error(e)
            raise _fail([str(e)]) from None
        except config.PreferencesError as e:
            log.error(e)
            raise _fail([f"preferences: {p}" for p in e.problems]) from None
        except runner.AgentFailure as e:
            log.event("session_failed", status=e.kind.value)
            raise _fail([f"Claude session failed: {e}"]) from None
        transcripts = saved_transcripts(result.session_id)
        log.event("session_ok", duration_ms=result.duration_ms, count=result.num_turns, ok=result.text == SMOKE_REPLY)
    init = result.init
    typer.echo(f"Reply:            {result.text!r} ({'as expected' if result.text == SMOKE_REPLY else 'UNEXPECTED'})")
    typer.echo(f"Auth:             apiKeySource={init.get('apiKeySource')!r} (subscription login = 'none')")
    typer.echo(f"Model:            {init.get('model')}")
    typer.echo(f"Built-in tools:   {init.get('tools') or 'none'}")
    typer.echo(f"MCP servers:      {[s.get('name') for s in init.get('mcp_servers') or []] or 'none'}")
    typer.echo(f"Plugins/skills:   {init.get('plugins') or 'none'} / {init.get('skills') or 'none'}")
    typer.echo(f"Rate limits seen: {result.rate_limits or 'none'}")
    typer.echo(f"Saved transcript: {'YES: ' + ', '.join(map(str, transcripts)) if transcripts else 'none found under ' + str(claude_projects_dir())}")
    if result.text != SMOKE_REPLY or transcripts:
        raise typer.Exit(code=1)


# --- add (FR-7–FR-10, ARCHITECTURE §6.2) ------------------------------------------------------------


def _show_proposal(p, tz) -> None:
    deadline = p.deadline_value(tz)
    if deadline is None:
        when = "-"
    elif isinstance(deadline, datetime):
        when = deadline.strftime("%Y-%m-%d %H:%M (%A)")
    else:
        when = deadline.strftime("%Y-%m-%d (%A)")
    typer.echo(f"Task:      {p.title}")
    if p.description:
        typer.echo(f"Details:   {p.description}")
    typer.echo(f"Category:  {p.category}")
    typer.echo(f"Priority:  {p.priority.value}")
    typer.echo(f"Deadline:  {when}")
    typer.echo(f"Estimate:  {f'{p.estimated_minutes} min' if p.estimated_minutes else '-'}")
    if p.assumptions:
        typer.echo("Assumptions:")
        for line in p.assumptions:
            typer.echo(f"  - {line}")


@app.command()
def add(
    text: str = typer.Argument(..., help='The task in plain language, e.g. "buy milk tomorrow".'),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking (the parsed task is still printed)."),
) -> None:
    """Capture a task from plain language. Shows the parsed task and asks before saving."""
    data_dir = config.default_data_dir()
    try:
        start = startup_checks(data_dir)
    except (BillingCredentialError, ScopeError) as e:
        raise _fail([str(e)]) from None
    except config.PreferencesError as e:
        raise _fail([f"preferences: {p}" for p in e.problems]) from None
    prefs = start.prefs
    store = TaskStore(data_dir, prefs.tz)
    try:
        open_tasks, completed = store.load_open(), store.load_completed()
    except TaskStoreError as e:
        raise _fail(e.problems) from None
    snap = Snapshot(prefs, store.now(), UNAVAILABLE, open_tasks, completed)

    def ask(question: str) -> str:
        typer.echo(f"Question:  {question}")
        return typer.prompt("Answer (Enter to save without a deadline)", default="", show_default=False)

    with RunLog(data_dir, "add", prefs.tz) as log:
        try:
            outcome = asyncio.run(capture(text, snap, cwd=data_dir, answer=None if yes else ask, log=log))
        except runner.AgentFailure as e:
            log.event("capture_failed", status=e.kind.value)
            raise _fail([f"couldn't capture the task: {e.kind.value}. Nothing was saved."]) from None
        log.event("task_proposed", count=len(outcome.turns), attempt=len(outcome.error_paths) + 1)
        typer.echo("")
        _show_proposal(outcome.proposal, prefs.tz)
        if outcome.deadline_dropped:
            typer.echo(f"Note:      saved without a deadline (unanswered question: {outcome.question})")
        if not yes and not typer.confirm("Save?", default=True):
            log.event("task_not_saved")
            typer.echo("Not saved.")
            return
        try:
            with run_lock(data_dir):
                task = store.add(outcome.proposal.task_fields(prefs.tz))
        except RunLockedError as e:
            log.error(e)
            raise _fail([str(e), "Not saved."]) from None
        except TaskStoreError as e:
            log.error(e)
            raise _fail(e.problems) from None
        log.event("task_created", task_id=task.id)
    typer.echo(f"Saved as {task.id}.")


# --- ask (FR-19–FR-21, ARCHITECTURE §6.3) -------------------------------------------------------------


def _source_line(src: Source, tz) -> str:
    item = src.item
    if src.type == "event":
        if item.all_day:
            when = f"{item.start.isoformat()} all day"
        else:
            when = f"{item.start.astimezone(tz):%Y-%m-%d %H:%M}–{item.end.astimezone(tz):%H:%M}"
        return f"event  {when}  {' '.join(item.title.split())}"
    due = f", due {when_text(item.deadline, tz)}" if item.deadline else ""
    done = ", done" if item.status is TaskStatus.done else ""
    return f"task   {item.id}  {' '.join(item.title.split())} ({item.priority.value}{due}{done})"


_FIELD_NAMES = {"estimated_minutes": "estimate (min)"}


def _change_text(change: QueuedChange, open_tasks: list[Task], tz) -> str:
    def show(value) -> str:
        if value is None:
            return "(none)"
        return when_text(value, tz) if isinstance(value, (date, datetime)) else str(value)

    if change.action == "create":
        f = change.fields
        extra = ", ".join(f"{_FIELD_NAMES.get(k, k)} {show(v)}" for k, v in f.items() if k not in ("title",) and v is not None)
        return f"Add task: {' '.join(f['title'].split())} ({extra})"
    task = next((t for t in open_tasks if t.id == change.task_id), None)
    name = f"{change.task_id} {' '.join(task.title.split())}" if task else change.task_id
    if change.action == "complete":
        return f"Mark done: {name}"
    if change.action == "delete":
        return f"Delete: {name}"
    parts = []
    for k, v in change.fields.items():
        old = getattr(task, k, None) if task else None
        old = old.value if hasattr(old, "value") else old
        parts.append(f"{_FIELD_NAMES.get(k, k)} {show(old)} → {show(v)}")
    return f"Change {name}: " + "; ".join(parts)


@app.command("ask")
def ask_command(question: str = typer.Argument(..., help='Your question, e.g. "What is overdue?"')) -> None:
    """Ask about your day, tasks or free time. Any task change it proposes is applied only after you say y."""
    data_dir = config.default_data_dir()
    try:
        start = startup_checks(data_dir, tokens=google_tokens())
    except (BillingCredentialError, ScopeError) as e:
        raise _fail([str(e)]) from None
    except config.PreferencesError as e:
        raise _fail([f"preferences: {p}" for p in e.problems]) from None
    prefs = start.prefs
    now = datetime.now(prefs.tz)
    store = TaskStore(data_dir, prefs.tz, now=lambda: datetime.now(prefs.tz))
    with RunLog(data_dir, "ask", prefs.tz) as log:
        tasks_error = None
        open_tasks, completed = [], {}
        try:
            open_tasks, completed = store.load_open(), store.load_completed()
        except TaskStoreError as e:
            tasks_error = "tasks_unavailable"
            log.error(e)
        if start.calendar_available:
            cal = calendar_window(calendar_client(prefs, now.date()), now.date())
        else:
            cal = CalendarResult(available=False, error=start.calendar.value)
        log.event("calendar_loaded", ok=cal.available, count=len(cal.events), skipped=len(cal.skipped_calendars), **({"error_code": cal.error} if cal.error else {}))
        snap = Snapshot(prefs, now, cal, open_tasks, completed, tasks_error)
        try:
            outcome = asyncio.run(ask(question, snap, cwd=data_dir, log=log))
        except runner.AgentFailure as e:
            log.status = "failed"
            log.event("answer_failed", status=e.kind.value)
            raise _fail([f"couldn't answer: {e.kind.value}. Nothing was changed."]) from None
        log.event("answer_given", count=len(outcome.sources), attempt=len(outcome.error_paths) + 1)

        typer.echo(outcome.answer.answer)
        if outcome.sources:
            typer.echo("")
            typer.echo("Sources:")
            for src in outcome.sources:
                typer.echo(f"  {_source_line(src, prefs.tz)}")
        if outcome.data_gaps:
            typer.echo("")
            typer.echo("Data gaps:")
            for gap in outcome.data_gaps:
                typer.echo(f"  - {' '.join(gap.split())}")

        for change in outcome.changes:
            log.event("change_proposed", action=change.action, **({"task_id": change.task_id} if change.task_id else {}))
            typer.echo("")
            typer.echo(f"Proposed: {_change_text(change, open_tasks, prefs.tz)}")
            if not typer.confirm("Apply?", default=False):
                log.event("change_declined", action=change.action)
                typer.echo("Not applied.")
                continue
            try:
                with run_lock(data_dir):  # only for the write, like `add`
                    task = apply_change(store, change, prefs.categories)
            except RunLockedError as e:
                log.error(e)
                typer.echo(f"Not applied: {e}", err=True)
                continue
            except TaskStoreError as e:
                log.error(e)
                typer.echo("Not applied: " + "; ".join(e.problems), err=True)
                continue
            log.event("change_applied", action=change.action, task_id=task.id)
            typer.echo(f"Applied: {_describe(task, prefs.tz)}")


# --- run (FR-12–FR-18, ARCHITECTURE §6.1) -------------------------------------------------------------


def notifier() -> Notifier:
    """The notifier. Tests replace this with the fake."""
    return MacNotifier()


def calendar_client(prefs: Preferences, today: date) -> CalendarClient:
    """The calendar client, used only when the token's scopes were confirmed. Tests replace it."""
    return GoogleCalendarClient(prefs.calendars, prefs.tz, today, GoogleEventsPages(google_auth.load_credentials))


# --- auth google (ARCHITECTURE §11 step 2, S-2, S-10) -------------------------------------------------


def google_login(client_file: Path):
    """The browser login. Tests replace this."""
    return google_auth.run_login(client_file)


@auth_app.command("google")
def auth_google() -> None:
    """Sign in to Google with read-only calendar access and keep the token in the Keychain."""
    data_dir = config.default_data_dir()
    client_file = data_dir / google_auth.CLIENT_FILE
    if not client_file.is_file():
        raise _fail([f"no OAuth client file at {client_file}. Download the desktop client JSON from Google Cloud, save it there, then `chmod 600` it."])
    if client_file.stat().st_mode & 0o077:
        raise _fail([f"{client_file} can be read by other users; run `chmod 600 {client_file}` first."])
    prefs = None
    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError:
        pass  # the login doesn't need preferences; the log just uses UTC
    tz = prefs.tz if prefs else ZoneInfo("UTC")
    with RunLog(data_dir, "auth.google", tz) as log:
        try:
            creds = google_login(client_file)
            scopes = scopes_from_tokeninfo(google_auth.fetch_tokeninfo(creds.token))
        except Exception as e:  # noqa: BLE001 - the browser flow or Google failed; nothing is stored
            log.error(e)
            raise _fail([f"Google sign-in failed ({e.__class__.__name__}). Nothing was saved."]) from None
        if scopes != {READONLY_SCOPE}:
            log.event("google_scope_refused", count=len(scopes or ()))
            granted = ", ".join(sorted(scopes or ())) or "(none readable)"
            raise _fail(
                [
                    f"Google granted {granted}, not exactly {READONLY_SCOPE}. The token was NOT saved.",
                    "Remove the app at myaccount.google.com → Security → Third-party apps with account access, fix the OAuth client's scopes, and sign in again.",
                ]
            )
        if not creds.refresh_token:
            log.event("google_no_refresh_token")
            raise _fail(["Google returned no refresh token, so the daily run couldn't stay signed in. Remove the app's access in your Google Account and sign in again."])
        signed_in = datetime.now(UTC)
        google_auth.save_token_info({**json.loads(creds.to_json()), google_auth.SIGNED_IN_AT: signed_in.isoformat()})
        log.event("google_connected")
    typer.echo(f"Connected. Granted scope: {READONLY_SCOPE} (nothing else).")
    typer.echo(f'Token saved in the macOS Keychain (service "{google_auth.KEYCHAIN_SERVICE}").')
    expires = signed_in + timedelta(days=google_auth.LOGIN_DAYS)
    typer.echo(f"The app is in Testing, so this login ends {expires.astimezone(tz):%a %d %b %H:%M}; `assistant doctor` warns 2 days before.")


_GUARD_FAILURES = {
    BillingCredentialError: "billing_credential_set",
    ScopeError: "google_token_scope",
    RunLockedError: "another_run_in_progress",
}


def _show_diff(result: RunResult, tz) -> None:
    diff = result.diff
    if diff is None:
        return
    typer.echo("Reminder changes (dry run, nothing written):")
    rows = [("+", p.fire_at, p.title, p.kind.value) for p in diff.create]
    rows += [("~", p.fire_at, p.title, p.kind.value) for _, p in diff.update]
    for sign, when, title, kind in sorted(rows, key=lambda r: r[1]):
        typer.echo(f"  {sign} {when.astimezone(tz):%Y-%m-%d %H:%M}  {title}  [{kind}]")
    typer.echo(f"  - {len(diff.delete)} removed, {len(diff.kept)} kept (data missing), {len(diff.forget)} past dropped from state")


def _daily(trigger: str, dry_run: bool) -> None:
    data_dir = config.default_data_dir()
    scheduled = trigger == "scheduled"

    def notify_failure(reason: str) -> None:
        if not scheduled:  # a manual run shows the error in the terminal
            return
        try:
            notifier().notify(NOTIFY_TITLE, failure_text(reason))
        except Exception:  # noqa: BLE001 - nothing else to tell; the log has the failure
            pass

    try:
        prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    except config.PreferencesError as e:
        notify_failure("invalid_preferences")
        raise _fail([f"preferences: {p}" for p in e.problems]) from None
    with RunLog(data_dir, f"run.{trigger}", prefs.tz, dry_run=dry_run) as log:
        try:
            with startup_guard(data_dir, tokens=google_tokens()) as start:
                now = datetime.now(start.prefs.tz)
                calendar = calendar_client(start.prefs, now.date()) if start.calendar_available else None
                result = asyncio.run(
                    daily_run(
                        data_dir,
                        start.prefs,
                        now,
                        trigger=trigger,
                        dry_run=dry_run,
                        calendar=calendar,
                        calendar_error=start.calendar.value,
                        reminders=reminders_client(),
                        notifier=notifier(),
                        log=log,
                    )
                )
        except tuple(_GUARD_FAILURES) as e:
            log.error(e)
            notify_failure(_GUARD_FAILURES[type(e)])
            raise _fail([str(e)]) from None
        except config.PreferencesError as e:
            log.error(e)
            notify_failure("invalid_preferences")
            raise _fail([f"preferences: {p}" for p in e.problems]) from None
        except runner.AgentFailure as e:
            log.status = "failed"
            log.error(e.kind.value)
            notify_failure(e.kind.value)
            raise _fail([f"the Claude step failed: {e.kind.value}. No brief or reminders were written; see the log."]) from None
    if result.skipped:
        typer.echo("Today's scheduled run already finished.")
        return
    typer.echo(result.markdown)
    if dry_run:
        _show_diff(result, prefs.tz)
    elif result.brief_path:
        typer.echo(f"Saved {BRIEFS_DIR}/{result.brief_path.name}.")


@run_app.command("now")
def run_now(dry_run: bool = typer.Option(False, "--dry-run", help="Plan and print everything; write nothing but the log.")) -> None:
    """Run the morning brief and reminders now (uses one Claude session)."""
    _daily("manual", dry_run)


@run_app.command("scheduled")
def run_scheduled() -> None:
    """The run launchd starts at the brief time. Runs at most once a day."""
    _daily("scheduled", False)

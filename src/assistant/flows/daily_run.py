"""The daily run: brief and reminders (FR-12–FR-18, FR-22–FR-25, ARCHITECTURE §6.1).

The caller holds the run lock and has passed the startup guard. This module:
preflight (tasks, calendar, Reminders, state.json) → one `run` session →
`submit_daily_plan` → planner → reconcile → writes (Reminders, state.json,
brief file, notification) → log.

Claude writes only the brief's summary, focus and extra data gaps, and proposes
extra reminders. Code fills the schedule, due-today, overdue and free-time
lists from the same snapshot, adds every data gap it knows about, and does every
write. `dry_run` plans and renders everything and writes only log lines.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from assistant.agent.permissions import PermissionGate
from assistant.agent.prompts import daily_prompt
from assistant.agent.runner import SessionResult, build_options, run_session
from assistant.agent.tools import Observed, ProposalBox, Snapshot, allowlist, build_server, calendar_window, free_time
from assistant.integrations.google_calendar import CalendarClient, overlaps, sort_key
from assistant.integrations.notify import Notifier
from assistant.integrations.reminders import AGENT_LIST, ReminderAccess, RemindersClient
from assistant.logging_setup import BRIEFS_DIR, RunLog
from assistant.planner import ApplyResult, Diff, Plan, apply_diff, build_plan, reconcile
from assistant.schemas import CalendarEvent, CalendarResult, DailyPlanProposal, Preferences, Task
from assistant.state import LastRun, RunStatus, SourceType, State, StateError, StateProblem, check_state, load_state, save_state
from assistant.tasks_store import TaskStore, TaskStoreError, atomic_write_text

MAX_TURNS = 15  # step limit for `run` (ARCHITECTURE key decisions)
NOTIFY_TITLE = "Personal assistant"

Session = Callable[[str, ClaudeAgentOptions], Awaitable[SessionResult]]


# --- Brief: the lists code fills (FR-14) ----------------------------------------------------------


@dataclass
class Brief:
    day: date
    generated: datetime
    trigger: str  # "manual" or "scheduled"
    summary: str
    focus: list[str]
    schedule: list[CalendarEvent]
    due_today: list[Task]
    overdue: list[Task]
    free_blocks: list[dict]  # from find_free_time: {start, end, minutes}
    data_gaps: list[str]
    calendar_available: bool
    tasks_available: bool
    reminders: str = ""  # one line about what happened to reminders


def todays_events(snap: Snapshot) -> list[CalendarEvent]:
    """Every event overlapping today, all-day ones first."""
    if not snap.calendar.available:
        return []
    events = [e for e in snap.calendar.events if overlaps(e, snap.today, snap.today, snap.tz)]
    return sorted(events, key=lambda e: (not e.all_day, sort_key(e, snap.tz)))


def _deadline_instant(task: Task, snap: Snapshot) -> datetime:
    d = task.deadline
    return d.astimezone(snap.tz) if isinstance(d, datetime) else datetime.combine(d, time.max, tzinfo=snap.tz)


def due_status(task: Task, snap: Snapshot) -> str | None:
    """"overdue" (deadline passed: a date before today, or a time before now), "due_today", or None."""
    d = task.deadline
    if d is None:
        return None
    if isinstance(d, datetime):
        if d <= snap.now:
            return "overdue"
        return "due_today" if d.astimezone(snap.tz).date() == snap.today else None
    if d < snap.today:
        return "overdue"
    return "due_today" if d == snap.today else None


def task_lists(snap: Snapshot) -> tuple[list[Task], list[Task]]:
    """(due today, overdue), each soonest deadline first."""
    due = [t for t in snap.tasks if due_status(t, snap) == "due_today"]
    late = [t for t in snap.tasks if due_status(t, snap) == "overdue"]
    key = lambda t: (_deadline_instant(t, snap), t.id)  # noqa: E731
    return sorted(due, key=key), sorted(late, key=key)


def free_blocks(snap: Snapshot) -> list[dict]:
    if not snap.calendar.available:
        return []
    return free_time(snap, Observed(), snap.today.isoformat())["free"]


# --- Data gaps code always reports (FR-20, FR-22, FR-23, FR-23a) -------------------------------------

CALENDAR_GAPS = {
    "not_connected": "Google Calendar isn't connected yet (run `assistant auth google`).",
    "unverified": "The Google token's scopes couldn't be checked, so the calendar wasn't used.",
}

_BLOCKED = {
    ReminderAccess.access_denied.value: "access to Reminders is denied (System Settings → Privacy & Security → Reminders)",
    ReminderAccess.list_missing.value: f'the "{AGENT_LIST}" list doesn\'t exist; create it under On My Mac',
    ReminderAccess.not_on_my_mac.value: f'the "{AGENT_LIST}" list isn\'t an On My Mac list; recreate it under On My Mac',
    "reminders_unavailable": "Reminders couldn't be checked",
    StateProblem.invalid.value: "state.json is invalid (a copy is in backups/); run `assistant state rebuild`",
    StateProblem.missing.value: f'state.json is missing but "{AGENT_LIST}" holds the assistant\'s reminders; run `assistant state rebuild`',
    StateProblem.missing_unverified.value: f'state.json is missing and "{AGENT_LIST}" couldn\'t be read',
}


def degraded(snap: Snapshot, blocked: str | None) -> bool:
    """A gap that makes the run `partial`. A configured calendar that doesn't exist (404) doesn't (FR-1)."""
    return not snap.calendar.available or snap.tasks_error is not None or blocked is not None


def code_gaps(snap: Snapshot, blocked: str | None) -> list[str]:
    gaps = []
    cal = snap.calendar
    if not cal.available:
        why = CALENDAR_GAPS.get(cal.error or "", "The calendar couldn't be reached or its login expired (run `assistant auth google` if this continues).")
        gaps.append(f"Calendar data is missing: {why} Meetings and birthdays aren't in this brief; their existing reminders were kept.")
    elif cal.skipped_calendars:
        gaps.append(f"Calendar(s) not found and skipped: {', '.join(cal.skipped_calendars)}.")
    if snap.tasks_error:
        gaps.append("Tasks couldn't be read (tasks.json is invalid; see backups/). Task reminders were kept.")
    if blocked:
        gaps.append(f"Reminders weren't updated: {_BLOCKED.get(blocked, blocked)}.")
    return gaps


# --- Rendering ----------------------------------------------------------------------------------------


def _line(text: str) -> str:
    """Untrusted text on one line, so it can't add headings or list items to the file."""
    return " ".join((text or "").split()) or "(no title)"


def _hm(moment: datetime, tz) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def _event_line(e: CalendarEvent, day: date, tz) -> str:
    title = _line(e.title) + (" (birthday)" if e.is_birthday else "")
    if e.all_day:
        last = date.fromordinal(e.end.toordinal() - 1)
        return f"All day: {title}" + (f" (until {last.isoformat()})" if last > day else "")
    start = _hm(e.start, tz) if e.start.astimezone(tz).date() == day else "…"
    end = _hm(e.end, tz) if e.end.astimezone(tz).date() == day else "…"
    return f"{start}–{end} {title}" + (f" ({_line(e.location)})" if e.location else "")


def _task_line(t: Task, tz, show_date: bool) -> str:
    d = t.deadline
    if isinstance(d, datetime):
        when = d.astimezone(tz).strftime("%Y-%m-%d %H:%M" if show_date else "%H:%M")
    else:
        when = d.isoformat() if show_date else ""
    prefix = (f"due {when}: " if show_date else f"{when} ") if when else ""
    return f"{prefix}{_line(t.title)} ({t.category}, {t.priority.value}, {t.id})"


def render(brief: Brief, tz) -> str:
    out = [f"# Morning brief: {brief.day.strftime('%A')} {brief.day.isoformat()}", ""]
    out += [f"Generated {brief.generated.astimezone(tz).strftime('%H:%M')} ({brief.trigger} run).", ""]
    out += [_line(brief.summary), ""]

    def section(title: str, lines: list[str], empty: str) -> None:
        out.extend([f"## {title}", ""])
        out.extend([f"- {line}" for line in lines] or [empty])
        out.append("")

    section(
        "Today's schedule",
        [_event_line(e, brief.day, tz) for e in brief.schedule],
        "Nothing on the calendar today." if brief.calendar_available else "Unknown: calendar data is missing.",
    )
    no_tasks = "Unknown: tasks couldn't be read."
    section("Due today", [_task_line(t, tz, False) for t in brief.due_today], "Nothing due today." if brief.tasks_available else no_tasks)
    section("Overdue", [_task_line(t, tz, True) for t in brief.overdue], "Nothing overdue." if brief.tasks_available else no_tasks)
    section("Suggested focus", [_line(f) for f in brief.focus], "No suggestions.")
    section(
        "Free time (working hours)",
        [f"{_hm(datetime.fromisoformat(b['start']), tz)}–{_hm(datetime.fromisoformat(b['end']), tz)} ({b['minutes']} min)" for b in brief.free_blocks],
        "None left today." if brief.calendar_available else "Unknown: calendar data is missing.",
    )
    if brief.data_gaps:
        section("Data gaps", [_line(g) for g in brief.data_gaps], "")
    if brief.reminders:
        out += ["## Reminders", "", brief.reminders, ""]
    return "\n".join(out).rstrip() + "\n"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def notification_text(brief: Brief, reminders_ok: bool) -> str:
    """Counts only: no titles, names or times (S-15)."""
    parts = [
        _count(len(brief.schedule), "event") if brief.calendar_available else "calendar unavailable",
        f"{_count(len(brief.due_today), 'task')} due" if brief.tasks_available else "tasks unavailable",
        f"{len(brief.overdue)} overdue" if brief.tasks_available else None,
    ]
    text = "Morning brief ready: " + ", ".join(p for p in parts if p)
    return text + ("" if reminders_ok else ". Reminders not updated")


FAILURE_TEXT = {
    "usage_unavailable": "Claude usage unavailable",
    "auth_unavailable": "Claude login needed",
}


def failure_text(reason: str) -> str:
    """The scheduled-run failure notification (FR-24): the failure type only."""
    return f"Morning brief failed: {FAILURE_TEXT.get(reason, reason.replace('_', ' '))}"


# --- The run ------------------------------------------------------------------------------------------


@dataclass
class Preflight:
    snap: Snapshot
    state: State | None  # None: state.json is invalid or missing (FR-23a); not written this run
    blocked: str | None  # why Reminders writes are skipped, or None


@dataclass
class RunResult:
    status: RunStatus | None = None  # None when a scheduled run had already succeeded today
    skipped: bool = False
    brief: Brief | None = None
    markdown: str = ""
    proposal: DailyPlanProposal | None = None
    plan: Plan | None = None
    diff: Diff | None = None
    applied: ApplyResult | None = None
    blocked: str | None = None
    brief_path: Path | None = None
    session: SessionResult | None = None
    error_paths: list[list[str]] = field(default_factory=list)


def already_ran_today(data_dir: Path, today: date) -> bool:
    """`run scheduled` runs at most once a day: a finished run (ok or partial) counts (ARCHITECTURE §8)."""
    try:
        last = load_state(data_dir).last_runs.get("scheduled")
    except StateError:
        return False
    return last is not None and last.date == today and last.status in (RunStatus.ok, RunStatus.partial)


def preflight(
    data_dir: Path,
    prefs: Preferences,
    now: datetime,
    calendar: CalendarClient | None,
    calendar_error: str,
    reminders: RemindersClient,
    dry_run: bool,
    log: RunLog | None = None,
) -> Preflight:
    """Load one snapshot and check Reminders and state.json. Nothing here writes (except an invalid state copy)."""
    tz = prefs.tz
    today = now.astimezone(tz).date()
    tasks, tasks_error = [], None
    try:
        tasks = TaskStore(data_dir, tz).load_open()
    except TaskStoreError as e:
        tasks_error = "tasks_unavailable"
        if log:
            log.error(e)
    cal = calendar_window(calendar, today) if calendar else CalendarResult(available=False, error=calendar_error)
    snap = Snapshot(prefs, now, cal, tasks, {}, tasks_error)  # the run never sees completed tasks (FR-11a)

    try:
        access = reminders.check_access()
    except Exception:  # noqa: BLE001 - osascript trouble: treat as unavailable, never guess
        access = None
    items = None
    if access is ReminderAccess.ok:
        try:
            items = reminders.list_items()
        except Exception:  # noqa: BLE001
            items = None
    check = check_state(data_dir, items, keep_copy=not dry_run)
    blocked = None
    if access is not ReminderAccess.ok:
        blocked = access.value if access else "reminders_unavailable"
    elif check.state is None:
        blocked = check.problem.value
    if log:
        log.event("calendar_loaded", ok=cal.available, count=len(cal.events), **({"error_code": cal.error} if cal.error else {}))
        log.event("tasks_loaded", ok=tasks_error is None, count=len(tasks))
        log.event("reminders_preflight", ok=blocked is None, **({"status": blocked} if blocked else {}))
        if check.backup:
            log.event("state_invalid_copied")
    return Preflight(snap, check.state, blocked)


async def plan_session(snap: Snapshot, *, cwd: Path, log: RunLog | None = None, session: Session = run_session) -> tuple[DailyPlanProposal, SessionResult, list[list[str]]]:
    """The `run` session. Raises AgentFailure (usage, step limit, malformed output, no proposal, …)."""
    seen, box = Observed(), ProposalBox()
    gate = PermissionGate("run", allowlist("run"), snap.today, log)
    servers, names = build_server("run", snap, seen, box)
    options = build_options(system_prompt=daily_prompt(snap.now, snap.tz), max_turns=MAX_TURNS, cwd=cwd, allowed_tools=names, mcp_servers=servers, gate=gate)
    try:
        result = await session(f"Prepare my morning brief for {snap.today.strftime('%A')} {snap.today.isoformat()}.", options)
    finally:
        if log:
            for i, paths in enumerate(box.error_paths, 1):
                log.event("proposal_invalid", attempt=i, field_paths=paths)
    return box.result("submit_daily_plan"), result, box.error_paths


def unavailable_sources(snap: Snapshot) -> frozenset[SourceType]:
    """Source types whose reminders are kept, not deleted, because their data is missing this run (FR-22)."""
    out = set()
    if not snap.calendar.available:
        out |= {SourceType.meeting, SourceType.birthday}
    if snap.tasks_error:
        out.add(SourceType.task)
    return frozenset(out)


def _reminders_line(diff: Diff | None, applied: ApplyResult | None, blocked: str | None, dry_run: bool) -> str:
    if blocked:
        return "Not updated (see data gaps)."
    if dry_run:
        return f"Dry run, nothing written. Would create {len(diff.create)}, update {len(diff.update)}, remove {len(diff.delete)}."
    line = f"{applied.created} created, {applied.updated} updated, {applied.deleted} removed."
    return line + (" A write failed, so the rest were skipped; the next run retries." if applied.error else "")


async def daily_run(
    data_dir: Path,
    prefs: Preferences,
    now: datetime,
    *,
    trigger: str,
    dry_run: bool,
    calendar: CalendarClient | None,
    calendar_error: str = "not_connected",
    reminders: RemindersClient,
    notifier: Notifier,
    log: RunLog | None = None,
    session: Session = run_session,
) -> RunResult:
    """One daily run. Raises AgentFailure if the AI step fails; then nothing but log lines was written."""
    tz = prefs.tz
    today = now.astimezone(tz).date()
    if trigger == "scheduled" and not dry_run and already_ran_today(data_dir, today):
        if log:
            log.event("already_ran", date=today.isoformat())
        return RunResult(skipped=True)

    pre = preflight(data_dir, prefs, now, calendar, calendar_error, reminders, dry_run, log)
    snap = pre.snap
    proposal, session_result, error_paths = await plan_session(snap, cwd=data_dir, log=log, session=session)
    if log:
        log.event("plan_proposed", count=len(proposal.extra_reminders), attempt=len(error_paths) + 1)

    events = snap.calendar.events if snap.calendar.available else []
    plan = build_plan(prefs, events, snap.tasks, now, proposal.extra_reminders)
    if log:
        log.event("reminders_planned", count=len(plan.reminders), skipped=sum(plan.skipped_defaults.values()))
        for _, code in plan.dropped_extras:
            log.event("extra_dropped", status=code.value)
    diff = reconcile(plan.reminders, pre.state, now, unavailable_sources(snap)) if pre.state is not None else None

    due, late = task_lists(snap)
    gaps = code_gaps(snap, pre.blocked)
    brief = Brief(
        day=today,
        generated=now,
        trigger=trigger,
        summary=proposal.brief.summary,
        focus=list(proposal.brief.focus),
        schedule=todays_events(snap),
        due_today=due,
        overdue=late,
        free_blocks=free_blocks(snap),
        data_gaps=gaps + list(proposal.brief.data_gaps),
        calendar_available=snap.calendar.available,
        tasks_available=snap.tasks_error is None,
    )
    result = RunResult(brief=brief, proposal=proposal, plan=plan, diff=diff, blocked=pre.blocked, session=session_result, error_paths=error_paths)

    if dry_run:
        brief.reminders = _reminders_line(diff, None, pre.blocked, True)
        result.markdown = render(brief, tz)
        if log and diff is not None:
            log.event("dry_run_plan", created=len(diff.create), updated=len(diff.update), deleted=len(diff.delete), kept=len(diff.kept), forgotten=len(diff.forget))
        result.status = RunStatus.partial if degraded(snap, pre.blocked) else RunStatus.ok
        if log:
            log.status = result.status.value
        return result

    # 1. Reminders (only if not blocked), 2. state.json, 3. brief file, 4. notification, 5. log.
    applied = None
    if pre.blocked is None:
        applied = apply_diff(diff, reminders, pre.state, log)
        if log:
            log.event("reminders_applied", created=applied.created, updated=applied.updated, deleted=applied.deleted, kept=len(diff.kept), forgotten=applied.forgotten, missing=applied.missing)
    result.applied = applied
    status = RunStatus.partial if degraded(snap, pre.blocked) or (applied and applied.error) else RunStatus.ok
    if pre.state is not None:
        pre.state.last_runs[trigger] = LastRun(date=today, status=status)
        save_state(data_dir, pre.state)

    brief.reminders = _reminders_line(diff, applied, pre.blocked, False)
    result.markdown = render(brief, tz)
    folder = data_dir / BRIEFS_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    result.brief_path = folder / f"{today.isoformat()}.md"
    atomic_write_text(result.brief_path, result.markdown)
    if log:
        log.event("brief_written", date=today.isoformat())

    reminders_ok = pre.blocked is None and not (applied and applied.error)
    try:
        notifier.notify(NOTIFY_TITLE, notification_text(brief, reminders_ok))
        if log:
            log.event("notification_sent")
    except Exception as e:  # noqa: BLE001 - a failed notification never fails the run (ARCHITECTURE §8)
        if log:
            log.event("notification_failed", error_code=e.__class__.__name__)
    result.status = status
    if log:
        log.status = status.value
    return result

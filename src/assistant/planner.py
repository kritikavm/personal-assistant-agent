"""Reminder planning, no AI (FR-14–FR-18, ARCHITECTURE §6.1 steps 4–6).

Plain code computes the default reminders from preferences, so the same inputs
always give the same reminders. Claude's extras are checked here and dropped
with a code when they break a rule. Quiet hours apply to every reminder. The
reconciler diffs the plan against state.json and only ever updates or deletes
reminders recorded there (FR-16, S-4, safety test 7).

Time arithmetic runs on real instants (UTC), and wall-clock times are built
per local day, so DST days come out right.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from assistant.integrations.reminders import ReminderNotFoundError, RemindersClient
from assistant.logging_setup import RunLog
from assistant.schemas import CalendarEvent, ExtraReminderProposal, Preferences, Task, TaskStatus, TimeRange
from assistant.state import (
    ReminderKind,
    ReminderRecord,
    SourceType,
    State,
    clean_title,
    content_hash,
    marker,
    reminder_key,
)

WINDOW_DAYS = 7  # reminders fire no later than the end of today + 7 (FR-1)
JUST_BEFORE = timedelta(minutes=1)  # "just before quiet hours start" (PRD §8)


# --- Time helpers --------------------------------------------------------------------


def local_at(day: date, clock: time, tz: ZoneInfo) -> datetime:
    """A wall-clock time on a local day. A time skipped by DST moves forward."""
    return datetime.combine(day, clock, tzinfo=tz).astimezone(timezone.utc).astimezone(tz)


def shift(moment: datetime, delta: timedelta, tz: ZoneInfo) -> datetime:
    """`moment + delta` in real elapsed time, shown in `tz`."""
    return (moment.astimezone(timezone.utc) + delta).astimezone(tz)


def whole_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def window_end(now: datetime, tz: ZoneInfo) -> datetime:
    """Start of today + 8 (exclusive end of the 7-day window)."""
    return local_at(now.astimezone(tz).date() + timedelta(days=WINDOW_DAYS + 1), time.min, tz)


# --- Quiet hours (PRD §8) ------------------------------------------------------------------


class QuietOutcome(StrEnum):
    none = "none"
    to_end = "to_end"  # moved to the end of quiet hours
    to_before_start = "to_before_start"  # moved to just before quiet hours started


def in_quiet_hours(clock: time, quiet: TimeRange) -> bool:
    """Start is inside the window, end is not. The window may wrap past midnight."""
    if quiet.start < quiet.end:
        return quiet.start <= clock < quiet.end
    return clock >= quiet.start or clock < quiet.end


def _quiet_period(local: datetime, quiet: TimeRange, tz: ZoneInfo) -> tuple[datetime, datetime]:
    day, clock = local.date(), local.time()
    if quiet.start < quiet.end:
        return local_at(day, quiet.start, tz), local_at(day, quiet.end, tz)
    if clock >= quiet.start:
        return local_at(day, quiet.start, tz), local_at(day + timedelta(days=1), quiet.end, tz)
    return local_at(day - timedelta(days=1), quiet.start, tz), local_at(day, quiet.end, tz)


def apply_quiet_hours(fire_at: datetime, anchor: datetime | None, prefs: Preferences) -> tuple[datetime, QuietOutcome]:
    """Move a reminder out of quiet hours.

    To the end of quiet hours if that's still before `anchor` (the event start
    or deadline); otherwise to just before quiet hours started. With no anchor
    (the brief item), always to the end.
    """
    tz = prefs.tz
    local = whole_minute(fire_at.astimezone(tz))
    if not in_quiet_hours(local.time(), prefs.quiet_hours):
        return local, QuietOutcome.none
    start, end = _quiet_period(local, prefs.quiet_hours, tz)
    if anchor is None or end < anchor:
        return end, QuietOutcome.to_end
    return shift(start, -JUST_BEFORE, tz), QuietOutcome.to_before_start


# --- Planned reminders ---------------------------------------------------------------------


class Rule(StrEnum):
    meeting_before = "meeting_before"
    task_morning_of = "task_morning_of"
    task_before_deadline = "task_before_deadline"
    birthday_evening_before = "birthday_evening_before"
    brief = "brief"
    extra = "extra"


@dataclass(frozen=True)
class PlannedReminder:
    title: str
    fire_at: datetime  # local time zone, whole minutes, after quiet hours
    source_type: SourceType
    source_id: str
    kind: ReminderKind
    rule: Rule
    quiet: QuietOutcome = QuietOutcome.none
    reason: str | None = None  # extras only; never written to Reminders or logs

    @property
    def key(self) -> str:
        return reminder_key(self.source_type, self.source_id, self.fire_at)

    @property
    def notes(self) -> str:
        return marker(self.kind, self.key)

    @property
    def content_hash(self) -> str:
        return content_hash(self.kind, self.key, self.title)


class Skip(StrEnum):
    """Why a default reminder or an extra wasn't planned. Logged as counts."""

    past = "past"
    beyond_window = "beyond_window"
    after_deadline = "after_deadline"  # morning-of time is at or after the deadline
    duplicate = "duplicate"
    not_allowed = "not_allowed"  # extras turned off in preferences
    unknown_source = "unknown_source"
    after_source = "after_source"  # extra fires at or after its event or deadline
    duplicate_of_default = "duplicate_of_default"
    over_cap = "over_cap"  # more than max_per_item extras for one item (FR-18)


def _hhmm(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def _birthday_date(event: CalendarEvent, tz: ZoneInfo) -> date:
    return event.start if isinstance(event.start, date) and not isinstance(event.start, datetime) else event.start.astimezone(tz).date()


def _birthday_title(event: CalendarEvent) -> str:
    name = event.title if "birthday" in event.title.lower() else f"{event.title}'s birthday"
    return f"Tomorrow: {name}"


def _task_anchor(task: Task, tz: ZoneInfo) -> datetime | None:
    """The deadline as an instant. A date-only deadline counts until the end of that day."""
    d = task.deadline
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.astimezone(tz)
    return local_at(d + timedelta(days=1), time.min, tz)


@dataclass
class _Candidate:
    title: str
    fire_at: datetime
    anchor: datetime | None
    source_type: SourceType
    source_id: str
    rule: Rule


def _default_candidates(prefs: Preferences, events: list[CalendarEvent], tasks: list[Task], now: datetime, include_brief: bool) -> Iterable[_Candidate | Skip]:
    tz, rules = prefs.tz, prefs.reminders
    for event in events:
        if event.is_birthday:
            day = _birthday_date(event, tz)
            yield _Candidate(
                _birthday_title(event),
                local_at(day - timedelta(days=1), rules.birthdays.evening_before, tz),
                local_at(day, time.min, tz),
                SourceType.birthday,
                event.id,
                Rule.birthday_evening_before,
            )
        elif not event.all_day:  # all-day events get no "before" reminder (FR-2)
            yield _Candidate(
                f"{event.title} at {_hhmm(event.start, tz)}",
                shift(event.start, -rules.meetings.before, tz),
                event.start,
                SourceType.meeting,
                event.id,
                Rule.meeting_before,
            )
    for task in tasks:
        if task.status is TaskStatus.done or task.deadline is None:
            continue
        anchor = _task_anchor(task, tz)
        timed = isinstance(task.deadline, datetime)
        due_day = anchor.date() if timed else task.deadline
        morning = local_at(due_day, rules.tasks_with_deadline.morning_of_due_day, tz)
        if morning >= anchor:
            yield Skip.after_deadline
        else:
            due = f"Due today at {_hhmm(anchor, tz)}" if timed else "Due today"
            yield _Candidate(f"{due}: {task.title}", morning, anchor, SourceType.task, task.id, Rule.task_morning_of)
        if timed:
            yield _Candidate(
                f"Due at {_hhmm(anchor, tz)}: {task.title}",
                shift(anchor, -rules.tasks_with_deadline.before_deadline, tz),
                anchor,
                SourceType.task,
                task.id,
                Rule.task_before_deadline,
            )
    if include_brief:
        today = now.astimezone(tz).date()
        at = local_at(today, prefs.morning_brief.time, tz)
        if at <= now:  # a later manual run: now + 1 min
            at = shift(whole_minute(now.astimezone(tz)), timedelta(minutes=1), tz)
        yield _Candidate(f"Morning brief ready (briefs/{today.isoformat()}.md)", at, None, SourceType.brief, today.isoformat(), Rule.brief)


@dataclass
class Plan:
    reminders: list[PlannedReminder] = field(default_factory=list)
    skipped_defaults: Counter = field(default_factory=Counter)
    dropped_extras: list[tuple[int, Skip]] = field(default_factory=list)  # (index in the proposal, code)

    def defaults(self) -> list[PlannedReminder]:
        return [r for r in self.reminders if r.kind is ReminderKind.default]

    def extras(self) -> list[PlannedReminder]:
        return [r for r in self.reminders if r.kind is ReminderKind.extra]


def _sorted(reminders: Iterable[PlannedReminder]) -> list[PlannedReminder]:
    return sorted(reminders, key=lambda r: (r.fire_at, r.key))


def default_reminders(prefs: Preferences, events: list[CalendarEvent], tasks: list[Task], now: datetime, include_brief: bool = True) -> Plan:
    """The default-rule reminders (FR-17): future, inside the window, quiet hours applied."""
    plan, end, seen = Plan(), window_end(now, prefs.tz), {}
    for c in _default_candidates(prefs, events, tasks, now, include_brief):
        if isinstance(c, Skip):
            plan.skipped_defaults[c] += 1
            continue
        fire_at, outcome = apply_quiet_hours(c.fire_at, c.anchor, prefs)
        if fire_at <= now:
            plan.skipped_defaults[Skip.past] += 1
            continue
        if fire_at >= end:
            plan.skipped_defaults[Skip.beyond_window] += 1
            continue
        planned = PlannedReminder(clean_title(c.title), fire_at, c.source_type, c.source_id, ReminderKind.default, c.rule, outcome)
        if planned.key in seen:  # e.g. morning-of and 1 h before land on the same minute
            plan.skipped_defaults[Skip.duplicate] += 1
            continue
        seen[planned.key] = planned
    plan.reminders = _sorted(seen.values())
    return plan


def _sources(events: list[CalendarEvent], tasks: list[Task], tz: ZoneInfo) -> dict[tuple[str, str], datetime | None]:
    """Items an extra may point at, with the time it must fire before."""
    sources: dict[tuple[str, str], datetime | None] = {}
    for e in events:
        if e.is_birthday:
            sources[("birthday", e.id)] = local_at(_birthday_date(e, tz), time.min, tz)
        else:
            sources[("meeting", e.id)] = e.start if isinstance(e.start, datetime) else local_at(e.start, time.min, tz)
    for t in tasks:
        if t.status is not TaskStatus.done:
            sources[("task", t.id)] = _task_anchor(t, tz)
    return sources


def validate_extras(
    proposals: list[ExtraReminderProposal],
    defaults: list[PlannedReminder],
    prefs: Preferences,
    events: list[CalendarEvent],
    tasks: list[Task],
    now: datetime,
) -> tuple[list[PlannedReminder], list[tuple[int, Skip]]]:
    """Keep the extras that pass every rule (FR-14.4, FR-18); drop the rest with a code."""
    tz, rules = prefs.tz, prefs.reminders.extra_reminders
    end, sources = window_end(now, tz), _sources(events, tasks, tz)
    taken = {r.key for r in defaults}
    kept: dict[str, PlannedReminder] = {}
    per_item: Counter = Counter()
    dropped: list[tuple[int, Skip]] = []
    for i, p in enumerate(proposals):
        item = (p.source_type, p.source_id)
        if not rules.allowed:
            dropped.append((i, Skip.not_allowed))
            continue
        if item not in sources:
            dropped.append((i, Skip.unknown_source))
            continue
        anchor = sources[item]
        fire_at, outcome = apply_quiet_hours(p.fire_at, anchor, prefs)
        key = reminder_key(p.source_type, p.source_id, fire_at)
        if p.fire_at <= now or fire_at <= now:
            code = Skip.past
        elif p.fire_at >= end or fire_at >= end:
            code = Skip.beyond_window
        elif anchor is not None and fire_at >= anchor:
            code = Skip.after_source
        elif key in taken:
            code = Skip.duplicate_of_default
        elif key in kept:
            code = Skip.duplicate
        elif per_item[item] >= rules.max_per_item:
            code = Skip.over_cap
        else:
            kept[key] = PlannedReminder(
                clean_title(p.title), fire_at, SourceType(p.source_type), p.source_id, ReminderKind.extra, Rule.extra, outcome, p.reason
            )
            per_item[item] += 1
            continue
        dropped.append((i, code))
    return _sorted(kept.values()), dropped


def build_plan(
    prefs: Preferences,
    events: list[CalendarEvent],
    tasks: list[Task],
    now: datetime,
    extras: list[ExtraReminderProposal] = (),
    include_brief: bool = True,
) -> Plan:
    plan = default_reminders(prefs, events, tasks, now, include_brief)
    kept, plan.dropped_extras = validate_extras(list(extras), plan.reminders, prefs, events, tasks, now)
    plan.reminders = _sorted([*plan.reminders, *kept])
    return plan


# --- Reconcile (FR-15, FR-16, FR-22) ---------------------------------------------------------


@dataclass
class Diff:
    create: list[PlannedReminder] = field(default_factory=list)
    update: list[tuple[str, PlannedReminder]] = field(default_factory=list)  # (key in state, new plan)
    delete: list[str] = field(default_factory=list)  # keys in state
    kept: list[str] = field(default_factory=list)  # not planned, kept because the source was unavailable
    forget: list[str] = field(default_factory=list)  # past: left alone in Reminders, dropped from state

    @property
    def writes(self) -> int:
        return len(self.create) + len(self.update) + len(self.delete)


def reconcile(
    planned: list[PlannedReminder],
    state: State,
    now: datetime,
    unavailable: frozenset[SourceType] = frozenset(),
) -> Diff:
    """Compare the plan with the reminders we created. Touches only keys in state.

    - planned key not in state → create; same key with a changed title → update
    - a future reminder no longer planned → delete, unless its source type is
      `unavailable` this run (calendar outage keeps meeting and birthday ones, FR-22)
    - a deleted and a created reminder for the same item and kind are paired into
      one update, so a moved event or deadline moves its reminder (FR-15)
    - past reminders are never changed; they're dropped from state
    """
    diff = Diff()
    by_key = {p.key: p for p in planned}
    removed: list[str] = []
    for key, record in state.reminders.items():
        if record.fire_at <= now:
            diff.forget.append(key)
        elif key in by_key:
            if by_key[key].content_hash != record.content_hash:
                diff.update.append((key, by_key[key]))
        elif record.source_type in unavailable:
            diff.kept.append(key)
        else:
            removed.append(key)
    new = [p for p in planned if p.key not in state.reminders]

    def item(source_type: SourceType, source_id: str, kind: ReminderKind) -> tuple:
        return (source_type, source_id, kind)

    gone: dict[tuple, list[str]] = {}
    for key in sorted(removed, key=lambda k: (state.reminders[k].fire_at, k)):
        r = state.reminders[key]
        gone.setdefault(item(r.source_type, r.source_id, r.kind), []).append(key)
    for p in _sorted(new):
        keys = gone.get(item(p.source_type, p.source_id, p.kind))
        if keys:
            diff.update.append((keys.pop(0), p))
        else:
            diff.create.append(p)
    diff.delete = [k for keys in gone.values() for k in keys]
    return diff


@dataclass
class ApplyResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    forgotten: int = 0
    missing: int = 0  # recorded reminders I'd already removed by hand
    error: str | None = None  # exception class name if a write failed; later writes were skipped


def _record(p: PlannedReminder, reminder_id: str) -> ReminderRecord:
    return ReminderRecord(
        reminder_id=reminder_id,
        title=p.title,
        fire_at=p.fire_at,
        source_type=p.source_type,
        source_id=p.source_id,
        kind=p.kind,
        content_hash=p.content_hash,
    )


def apply_diff(diff: Diff, client: RemindersClient, state: State, log: RunLog | None = None) -> ApplyResult:
    """Make the Reminders writes and update `state` to match what actually happened.

    Updates and deletes only ever use reminder IDs from `state` (FR-16). If a
    write fails, the rest are skipped and `state` still describes the list
    correctly, so the caller can save it.
    """
    result = ApplyResult()

    def note(name: str, key: str, reminder_id: str, kind: ReminderKind, source_type: SourceType) -> None:
        if log:
            log.event(name, key=key, reminder_id=reminder_id, kind=kind.value, source_type=source_type.value)

    for key in diff.forget:
        del state.reminders[key]
        result.forgotten += 1
    try:
        for key in diff.delete:
            record = state.reminders[key]
            try:
                client.delete(record.reminder_id)
            except ReminderNotFoundError:
                result.missing += 1
            del state.reminders[key]
            result.deleted += 1
            note("reminder_deleted", key, record.reminder_id, record.kind, record.source_type)
        for old_key, p in diff.update:
            record = state.reminders[old_key]
            reminder_id = record.reminder_id
            try:
                client.update(reminder_id, p.title, p.fire_at, p.notes)
            except ReminderNotFoundError:
                result.missing += 1
                reminder_id = client.create(p.title, p.fire_at, p.notes)
            del state.reminders[old_key]
            state.reminders[p.key] = _record(p, reminder_id)
            result.updated += 1
            note("reminder_updated", p.key, reminder_id, p.kind, p.source_type)
        for p in diff.create:
            reminder_id = client.create(p.title, p.fire_at, p.notes)
            state.reminders[p.key] = _record(p, reminder_id)
            result.created += 1
            note("reminder_created", p.key, reminder_id, p.kind, p.source_type)
    except Exception as e:  # noqa: BLE001 - any Reminders failure stops the writes; state stays true
        result.error = e.__class__.__name__
        if log:
            log.error(e)
    return result

"""M5: planner, quiet hours, reconcile, missing/invalid state and state rebuild.

EVALS §4.2 (default reminders, 100%), §4.8 Layer A (no duplicates), SECURITY §7
safety test 7 (reconciler scope), DST (2026-11-01) and window edges.
All data is invented.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from assistant import cli, config
from assistant.cli import app
from assistant.integrations.reminders import ReminderAccess, ReminderItem
from assistant.logging_setup import sanitize
from assistant.planner import (
    QuietOutcome,
    Rule,
    Skip,
    apply_diff,
    apply_quiet_hours,
    build_plan,
    default_reminders,
    local_at,
    reconcile,
)
from assistant.schemas import CalendarEvent, ExtraReminderProposal, Preferences, Task
from assistant.state import (
    STATE_FILE,
    ReminderKind,
    SourceType,
    State,
    StateProblem,
    check_state,
    load_state,
    marker,
    parse_marker,
    plan_rebuild,
    save_state,
)
from tests.fakes import FakeReminders
from tests.fakes.scenario import load

TZ = ZoneInfo("America/Los_Angeles")
EXAMPLE_PREFS = Path(__file__).resolve().parents[1] / "examples" / "preferences.yaml"
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=TZ)  # Wednesday, brief time (EVALS §3.1)


def prefs(**changes) -> Preferences:
    data = yaml.safe_load(EXAMPLE_PREFS.read_text())
    for path, value in changes.items():
        node = data
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return Preferences.model_validate(data)


def at(y, mo, d, h=0, mi=0) -> datetime:
    return local_at(date(y, mo, d), time(h, mi), TZ)


def meeting(id, start: datetime, minutes=60, title="Fake meeting") -> CalendarEvent:
    return CalendarEvent(id=id, calendar="primary", title=title, start=start, end=start + timedelta(minutes=minutes), all_day=False)


def all_day(id, day: date, days=1, title="Fake all-day", birthday=False) -> CalendarEvent:
    return CalendarEvent(id=id, calendar="primary", title=title, start=day, end=day + timedelta(days=days), all_day=True, is_birthday=birthday)


def task(id, deadline=None, status="todo", title="Fake task") -> Task:
    return Task.model_validate(
        {
            "id": id,
            "title": title,
            "category": "personal",
            "deadline": deadline.isoformat() if deadline else None,
            "status": status,
            "created_at": "2026-10-01T09:00:00-07:00",
            "updated_at": "2026-10-01T09:00:00-07:00",
            **({"completed_at": "2026-10-28T07:00:00-07:00"} if status == "done" else {}),
        }
    )


def fires(plan, source_id=None) -> list[str]:
    return [r.fire_at.isoformat() for r in plan.reminders if source_id is None or r.source_id == source_id]


def scenario_inputs(name: str, now: datetime | None = None):
    s = load(name)
    result = s.calendar_client().list_events(s.today, s.today + timedelta(days=7))
    return s, result, [Task.model_validate(t) for t in s.tasks], now or s.now


# --- §4.2 default reminders: one case per rule and edge (≥ 25) ------------------------------

P = prefs()

DEFAULT_CASES = [
    # (case id, events, tasks, now, prefs, source_id, expected fire times)
    ("meeting 1 h before", [meeting("m1", at(2026, 10, 28, 14))], [], NOW, P, "m1", ["2026-10-28T13:00:00-07:00"]),
    ("meeting tomorrow", [meeting("m1", at(2026, 10, 29, 9, 30))], [], NOW, P, "m1", ["2026-10-29T08:30:00-07:00"]),
    ("meeting already started: none", [meeting("m1", at(2026, 10, 28, 7, 30))], [], NOW, P, "m1", []),
    ("meeting reminder exactly now: none", [meeting("m1", at(2026, 10, 28, 9))], [], NOW, P, "m1", []),
    ("meeting reminder 1 min from now", [meeting("m1", at(2026, 10, 28, 9, 1))], [], NOW, P, "m1", ["2026-10-28T08:01:00-07:00"]),
    ("custom lead time 15m", [meeting("m1", at(2026, 10, 28, 14))], [], NOW, prefs(reminders__meetings__before="15m"), "m1", ["2026-10-28T13:45:00-07:00"]),
    ("all-day event: none", [all_day("a1", date(2026, 10, 29))], [], NOW, P, "a1", []),
    ("multi-day all-day event: none", [all_day("a1", date(2026, 10, 29), days=3)], [], NOW, P, "a1", []),
    ("quiet hours: 07:30 event → 07:00", [meeting("m1", at(2026, 10, 31, 7, 30))], [], NOW, P, "m1", ["2026-10-31T07:00:00-07:00"]),
    ("quiet hours: 06:30 event → 21:59 night before", [meeting("m1", at(2026, 10, 30, 6, 30))], [], NOW, P, "m1", ["2026-10-29T21:59:00-07:00"]),
    ("quiet hours: 07:00 event → 21:59 (07:00 not before it)", [meeting("m1", at(2026, 10, 30, 7))], [], NOW, P, "m1", ["2026-10-29T21:59:00-07:00"]),
    ("quiet hours: 08:00 event → 07:00 is outside quiet hours", [meeting("m1", at(2026, 10, 30, 8))], [], NOW, P, "m1", ["2026-10-30T07:00:00-07:00"]),
    ("22:30 event → 21:30, before quiet hours start", [meeting("m1", at(2026, 10, 29, 22, 30))], [], NOW, P, "m1", ["2026-10-29T21:30:00-07:00"]),
    ("quiet hours start is quiet: 23:00 event → 21:59", [meeting("m1", at(2026, 10, 29, 23))], [], NOW, P, "m1", ["2026-10-29T21:59:00-07:00"]),
    ("evening 21:30 event → 20:30 untouched", [meeting("m1", at(2026, 10, 29, 21, 30))], [], NOW, P, "m1", ["2026-10-29T20:30:00-07:00"]),
    ("task with time: morning-of and 1 h before", [], [task("t-1", at(2026, 10, 29, 17))], NOW, P, "t-1", ["2026-10-29T08:00:00-07:00", "2026-10-29T16:00:00-07:00"]),
    ("task date only: morning-of only", [], [task("t-1", date(2026, 10, 30))], NOW, P, "t-1", ["2026-10-30T08:00:00-07:00"]),
    ("task due before morning time: 1 h before only, moved out of quiet hours", [], [task("t-1", at(2026, 10, 29, 7, 30))], NOW, P, "t-1", ["2026-10-29T07:00:00-07:00"]),
    ("task due at 09:00: both rules land on 08:00, one reminder", [], [task("t-1", at(2026, 10, 29, 9))], NOW, P, "t-1", ["2026-10-29T08:00:00-07:00"]),
    ("task overdue: none", [], [task("t-1", date(2026, 10, 27))], NOW, P, "t-1", []),
    ("task due today 17:00: morning-of passed, 1 h before kept", [], [task("t-1", at(2026, 10, 28, 17))], NOW, P, "t-1", ["2026-10-28T16:00:00-07:00"]),
    ("task without deadline: none", [], [task("t-1")], NOW, P, "t-1", []),
    ("done task: none", [], [task("t-1", at(2026, 10, 29, 17), status="done")], NOW, P, "t-1", []),
    ("birthday: 20:00 the evening before", [all_day("b1", date(2026, 10, 31), birthday=True, title="Alex Morgan's birthday")], [], NOW, P, "b1", ["2026-10-30T20:00:00-07:00"]),
    ("birthday today: evening before has passed", [all_day("b1", date(2026, 10, 28), birthday=True)], [], NOW, P, "b1", []),
    ("birthday tomorrow", [all_day("b1", date(2026, 10, 29), birthday=True)], [], NOW, P, "b1", ["2026-10-28T20:00:00-07:00"]),
    ("birthday at 23:00 setting: quiet hours → 21:59", [all_day("b1", date(2026, 10, 31), birthday=True)], [], NOW, prefs(reminders__birthdays__evening_before="23:00"), "b1", ["2026-10-30T21:59:00-07:00"]),
    # DST: clocks go back at 02:00 on Sunday 2026-11-01.
    ("DST: 09:00 PST meeting → 08:00 PST", [meeting("m1", at(2026, 11, 1, 9))], [], NOW, P, "m1", ["2026-11-01T08:00:00-08:00"]),
    ("DST: 02:30 PST meeting → 01:30 PST in quiet hours → 21:59 PDT", [meeting("m1", at(2026, 11, 1, 2, 30))], [], NOW, P, "m1", ["2026-10-31T21:59:00-07:00"]),
    ("DST: 8 h before 09:00 PST is the second 01:00", [meeting("m1", at(2026, 11, 1, 9))], [], NOW, prefs(reminders__meetings__before="8h", quiet_hours={"start": "03:00", "end": "04:00"}), "m1", ["2026-11-01T01:00:00-08:00"]),
    ("DST: birthday on 2026-11-02 → 20:00 PST", [all_day("b1", date(2026, 11, 2), birthday=True)], [], NOW, P, "b1", ["2026-11-01T20:00:00-08:00"]),
    ("DST: task due 2026-11-01 → 08:00 PST", [], [task("t-1", date(2026, 11, 1))], NOW, P, "t-1", ["2026-11-01T08:00:00-08:00"]),
    ("DST: quiet hours end is 07:00 PST", [meeting("m1", at(2026, 11, 1, 7, 30))], [], NOW, P, "m1", ["2026-11-01T07:00:00-08:00"]),
    # Window: today … today + 7 (2026-11-04).
    ("window: last day 23:00 meeting → 21:59 kept", [meeting("m1", at(2026, 11, 4, 23))], [], NOW, P, "m1", ["2026-11-04T21:59:00-08:00"]),
    ("window: last day 18:30 meeting kept", [meeting("m1", at(2026, 11, 4, 18, 30))], [], NOW, P, "m1", ["2026-11-04T17:30:00-08:00"]),
    ("window: day-8 00:30 meeting → 23:30 day 7 in quiet hours → 21:59 day 7", [meeting("m1", at(2026, 11, 5, 0, 30))], [], NOW, P, "m1", ["2026-11-04T21:59:00-08:00"]),
    ("window: day-8 09:00 meeting excluded", [meeting("m1", at(2026, 11, 5, 9))], [], NOW, P, "m1", []),
    ("window: task due day 8 excluded", [], [task("t-1", date(2026, 11, 5))], NOW, P, "t-1", []),
    ("window: birthday on day 8 → evening of day 7 kept", [all_day("b1", date(2026, 11, 5), birthday=True)], [], NOW, P, "b1", ["2026-11-04T20:00:00-08:00"]),
    # Brief item.
    ("brief: at brief time before it", [], [], at(2026, 10, 28, 7, 15), P, "2026-10-28", ["2026-10-28T08:00:00-07:00"]),
    ("brief: now + 1 min at brief time", [], [], NOW, P, "2026-10-28", ["2026-10-28T08:01:00-07:00"]),
    ("brief: later manual run, now + 1 min", [], [], at(2026, 10, 28, 13, 42) + timedelta(seconds=30), P, "2026-10-28", ["2026-10-28T13:43:00-07:00"]),
    ("brief: manual run at 23:00 → end of quiet hours", [], [], at(2026, 10, 28, 23), P, "2026-10-28", ["2026-10-29T07:00:00-07:00"]),
]


@pytest.mark.parametrize("events,tasks,now,p,source_id,expected", [c[1:] for c in DEFAULT_CASES], ids=[c[0] for c in DEFAULT_CASES])
def test_default_reminders(events, tasks, now, p, source_id, expected):
    assert fires(default_reminders(p, events, tasks, now), source_id) == expected


def test_at_least_25_default_cases():
    assert len(DEFAULT_CASES) >= 25


def test_defaults_are_deterministic_and_marked_default():
    s, result, tasks, now = scenario_inputs("base")
    a = default_reminders(s.preferences, result.events, tasks, now)
    b = default_reminders(s.preferences, list(reversed(result.events)), list(reversed(tasks)), now)
    assert a.reminders == b.reminders
    assert {r.kind for r in a.reminders} == {ReminderKind.default}


def test_base_fixture_defaults_exactly():
    s, result, tasks, now = scenario_inputs("base")
    got = [(r.fire_at.isoformat(), r.rule.value, r.source_id) for r in default_reminders(s.preferences, result.events, tasks, now).reminders]
    assert got == [
        ("2026-10-28T08:01:00-07:00", "brief", "2026-10-28"),
        ("2026-10-28T09:00:00-07:00", "meeting_before", "evt002"),
        ("2026-10-28T11:00:00-07:00", "meeting_before", "evt003"),
        ("2026-10-28T14:30:00-07:00", "meeting_before", "evt004"),
        ("2026-10-28T16:00:00-07:00", "task_before_deadline", "t-0101"),
        ("2026-10-29T07:00:00-07:00", "task_before_deadline", "t-0111"),
        ("2026-10-29T08:00:00-07:00", "meeting_before", "evt005"),
        ("2026-10-29T08:00:00-07:00", "task_morning_of", "t-0105"),
        ("2026-10-29T11:00:00-07:00", "meeting_before", "evt006"),
        ("2026-10-29T11:00:00-07:00", "task_before_deadline", "t-0105"),
        ("2026-10-29T12:00:00-07:00", "meeting_before", "evt007"),
        ("2026-10-29T14:00:00-07:00", "meeting_before", "evt008"),
        ("2026-10-29T21:59:00-07:00", "meeting_before", "evt011"),
        ("2026-10-30T08:00:00-07:00", "task_morning_of", "t-0106"),
        ("2026-10-30T17:00:00-07:00", "task_before_deadline", "t-0106"),
        ("2026-10-30T20:00:00-07:00", "birthday_evening_before", "bdaycal01"),
        ("2026-10-31T07:00:00-07:00", "meeting_before", "evt013"),
        ("2026-10-31T18:00:00-07:00", "meeting_before", "evt014"),
        ("2026-10-31T21:59:00-07:00", "meeting_before", "evt015"),
        ("2026-11-01T08:00:00-08:00", "meeting_before", "evt016"),
        ("2026-11-01T20:00:00-08:00", "birthday_evening_before", "bdaytype01"),
        ("2026-11-02T08:00:00-08:00", "task_morning_of", "t-0107"),
        ("2026-11-02T09:00:00-08:00", "meeting_before", "rec001_20261102T180000Z"),
        ("2026-11-02T09:00:00-08:00", "task_before_deadline", "t-0107"),
        ("2026-11-02T13:00:00-08:00", "meeting_before", "evt017"),
        ("2026-11-02T20:00:00-08:00", "birthday_evening_before", "evt019"),
        ("2026-11-03T07:00:00-08:00", "meeting_before", "evt018"),
        ("2026-11-04T08:00:00-08:00", "task_morning_of", "t-0110"),
        ("2026-11-04T17:30:00-08:00", "meeting_before", "evt020"),
    ]


def test_titles_are_literal_single_line_and_capped():
    events = [
        meeting("m1", at(2026, 10, 29, 14), title='Review "quotes" & do shell script "echo pwned" -- end tell {}'),
        meeting("m2", at(2026, 10, 29, 15), title="Line one\nline two\tend"),
        meeting("m3", at(2026, 10, 29, 16), title="x" * 500),
    ]
    titles = {r.source_id: r.title for r in default_reminders(P, events, [], NOW).reminders}
    assert titles["m1"] == 'Review "quotes" & do shell script "echo pwned" -- end tell {} at 14:00'
    assert titles["m2"] == "Line one line two end at 15:00"
    assert len(titles["m3"]) == 200


def test_skipped_defaults_are_counted():
    plan = default_reminders(P, [meeting("m1", at(2026, 10, 28, 7))], [task("t-1", at(2026, 10, 29, 7, 30)), task("t-2", date(2026, 11, 9))], NOW)
    assert plan.skipped_defaults == {Skip.past: 1, Skip.after_deadline: 1, Skip.beyond_window: 1}


# --- Quiet hours rule (PRD §8) ----------------------------------------------------------------


def test_quiet_hours_both_outcomes():
    assert apply_quiet_hours(at(2026, 10, 31, 6, 30), at(2026, 10, 31, 7, 30), P) == (at(2026, 10, 31, 7), QuietOutcome.to_end)
    assert apply_quiet_hours(at(2026, 10, 30, 5, 30), at(2026, 10, 30, 6, 30), P) == (at(2026, 10, 29, 21, 59), QuietOutcome.to_before_start)
    assert apply_quiet_hours(at(2026, 10, 30, 12), at(2026, 10, 30, 13), P) == (at(2026, 10, 30, 12), QuietOutcome.none)
    assert apply_quiet_hours(at(2026, 10, 30, 23, 30), None, P) == (at(2026, 10, 31, 7), QuietOutcome.to_end)


def test_quiet_hours_that_dont_wrap_midnight():
    p = prefs(quiet_hours={"start": "13:00", "end": "14:00"})
    assert apply_quiet_hours(at(2026, 10, 30, 13, 30), at(2026, 10, 30, 15), p) == (at(2026, 10, 30, 14), QuietOutcome.to_end)
    assert apply_quiet_hours(at(2026, 10, 30, 13, 30), at(2026, 10, 30, 14), p) == (at(2026, 10, 30, 12, 59), QuietOutcome.to_before_start)
    assert apply_quiet_hours(at(2026, 10, 30, 23), None, p)[1] is QuietOutcome.none


def test_dst_gap_wall_time_moves_forward():
    spring = local_at(date(2027, 3, 14), time(2, 30), TZ)  # 02:30 doesn't exist that day
    assert spring.isoformat() == "2027-03-14T03:30:00-07:00"


# --- Extras (FR-14.4, FR-18) ------------------------------------------------------------------


def extra(source_id, fire_at, source_type="meeting", title="Extra", reason="Important") -> ExtraReminderProposal:
    return ExtraReminderProposal(title=title, fire_at=fire_at, source_type=source_type, source_id=source_id, reason=reason)


def test_extras_valid_and_each_drop_code():
    s, result, tasks, now = scenario_inputs("base")
    proposals = [
        extra("evt007", at(2026, 10, 28, 18), title="Interview tomorrow: prep"),  # 0 kept
        extra("evt007", at(2026, 10, 27, 18)),  # 1 past
        extra("evt007", at(2026, 11, 5, 9)),  # 2 beyond window
        extra("nope", at(2026, 10, 28, 18)),  # 3 unknown id
        extra("t-0101", at(2026, 10, 28, 18), source_type="meeting"),  # 4 known id, wrong type
        extra("evt007", at(2026, 10, 29, 13, 30)),  # 5 after the event started
        extra("evt007", at(2026, 10, 29, 12)),  # 6 same as the default
        extra("evt007", at(2026, 10, 28, 18)),  # 7 same as extra 0
        extra("evt007", at(2026, 10, 28, 19)),  # 8 kept (second, cap is 2)
        extra("evt007", at(2026, 10, 28, 20)),  # 9 over cap
        extra("t-0105", at(2026, 10, 28, 23), source_type="task"),  # 10 kept, moved out of quiet hours to 07:00
        extra("bdaycal01", at(2026, 10, 29, 18), source_type="birthday"),  # 11 kept
        extra("evt011", at(2026, 10, 29, 22, 30)),  # 12 quiet → 21:59, which is the default: duplicate
    ]
    plan = build_plan(s.preferences, result.events, tasks, now, proposals)
    assert [(r.source_id, r.fire_at.isoformat(), r.quiet.value) for r in plan.extras()] == [
        ("evt007", "2026-10-28T18:00:00-07:00", "none"),
        ("evt007", "2026-10-28T19:00:00-07:00", "none"),
        ("t-0105", "2026-10-29T07:00:00-07:00", "to_end"),
        ("bdaycal01", "2026-10-29T18:00:00-07:00", "none"),
    ]
    assert {r.kind for r in plan.extras()} == {ReminderKind.extra}
    assert plan.dropped_extras == [
        (1, Skip.past),
        (2, Skip.beyond_window),
        (3, Skip.unknown_source),
        (4, Skip.unknown_source),
        (5, Skip.after_source),
        (6, Skip.duplicate_of_default),
        (7, Skip.duplicate),
        (9, Skip.over_cap),
        (12, Skip.duplicate_of_default),
    ]
    assert len(plan.defaults()) == 29


def test_extras_off_or_cap_zero():
    s, result, tasks, now = scenario_inputs("base")
    off = prefs(reminders__extra_reminders={"allowed": False, "max_per_item": 2})
    zero = prefs(reminders__extra_reminders={"allowed": True, "max_per_item": 0})
    proposal = [extra("evt007", at(2026, 10, 28, 18))]
    assert build_plan(off, result.events, tasks, now, proposal).dropped_extras == [(0, Skip.not_allowed)]
    assert build_plan(zero, result.events, tasks, now, proposal).dropped_extras == [(0, Skip.over_cap)]


def test_extras_with_seconds_are_whole_minutes():
    s, result, tasks, now = scenario_inputs("base")
    plan = build_plan(s.preferences, result.events, tasks, now, [extra("evt007", at(2026, 10, 28, 18) + timedelta(seconds=42, microseconds=7))])
    assert plan.extras()[0].fire_at.isoformat() == "2026-10-28T18:00:00-07:00"


def test_extra_for_task_without_deadline_is_allowed():
    s, result, tasks, now = scenario_inputs("base")
    plan = build_plan(s.preferences, result.events, tasks, now, [extra("t-0113", at(2026, 10, 30, 10), source_type="task")])
    assert [r.source_id for r in plan.extras()] == ["t-0113"]


# --- Reconcile and apply: §4.8 Layer A -------------------------------------------------------


def run_once(state: State, client: FakeReminders, events, tasks, now, p=P, extras=(), unavailable=frozenset()):
    plan = build_plan(p, events, tasks, now, list(extras))
    diff = reconcile(plan.reminders, state, now, unavailable)
    result = apply_diff(diff, client, state)
    return plan, diff, result


def base_run():
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders()
    plan, _, first = run_once(state, client, result.events, tasks, now)
    return s, result, tasks, now, state, client, plan, first


def test_first_run_creates_every_planned_reminder_with_marker():
    s, result, tasks, now, state, client, plan, first = base_run()
    assert first.created == len(plan.reminders) == 29 and first.error is None
    assert set(state.reminders) == {r.key for r in plan.reminders}
    for item in client.items.values():
        m = parse_marker(item.notes)
        assert m and m.key in state.reminders and state.reminders[m.key].reminder_id == item.reminder_id


def test_second_run_on_same_snapshot_writes_nothing():
    s, result, tasks, now, state, client, plan, _ = base_run()
    before = len(client.writes())
    _, diff, second = run_once(state, client, result.events, tasks, now)
    assert diff.writes == 0 and second.created == second.updated == second.deleted == 0
    assert len(client.writes()) == before


def test_state_survives_save_and_load_between_runs(tmp_path):
    s, result, tasks, now, state, client, plan, _ = base_run()
    config.ensure_data_dir(tmp_path)
    save_state(tmp_path, state)
    reloaded = load_state(tmp_path)
    assert reloaded == state
    _, diff, _ = run_once(reloaded, client, result.events, tasks, now)
    assert diff.writes == 0


def test_one_move_and_one_completion_give_one_update_and_one_delete():
    s, result, tasks, now, state, client, plan, _ = base_run()
    events = [meeting("evt004", at(2026, 10, 28, 16, 30), title="Dentist appointment") if e.id == "evt004" else e for e in result.events]
    tasks = [t for t in tasks if t.id != "t-0110"]  # done → moved out of tasks.json
    client.calls.clear()
    _, diff, res = run_once(state, client, events, tasks, now)
    assert (res.created, res.updated, res.deleted) == (0, 1, 1)
    (update,) = [c for c in client.writes() if c[0] == "update"]
    assert update[3].isoformat() == "2026-10-28T15:30:00-07:00" and update[2] == "Dentist appointment at 16:30"
    assert parse_marker(update[4]).fire_at.isoformat() == "2026-10-28T15:30:00-07:00"
    assert not any(k.startswith("task:t-0110:") for k in state.reminders)
    assert "meeting:evt004:2026-10-28T15:30:00-07:00" in state.reminders
    assert "meeting:evt004:2026-10-28T14:30:00-07:00" not in state.reminders


def test_moved_cancelled_fixture_later_the_same_morning():
    s, result, tasks, now, state, client, plan, _ = base_run()
    m, m_result, m_tasks, m_now = scenario_inputs("moved_cancelled")
    client.calls.clear()
    _, diff, res = run_once(state, client, m_result.events, m_tasks, m_now)
    # dentist moved → update; lunch cancelled → delete; new brief item at 09:31 → create.
    # The 08:01 brief item is past and ours → deleted. The 09:00 design-review reminder is past: untouched, dropped.
    assert (res.created, res.updated, res.deleted, res.forgotten) == (1, 1, 2, 1)
    assert [c[0] for c in client.writes()] == ["delete", "delete", "update", "create"]
    assert diff.create[0].source_type is SourceType.brief and diff.create[0].fire_at.isoformat() == "2026-10-28T09:31:00-07:00"


def test_changed_title_same_time_is_one_update():
    s, result, tasks, now, state, client, plan, _ = base_run()
    tasks = [t.model_copy(update={"title": "Submit expense report (fake, renamed)"}) if t.id == "t-0101" else t for t in tasks]
    client.calls.clear()
    _, _, res = run_once(state, client, result.events, tasks, now)
    assert (res.created, res.updated, res.deleted) == (0, 1, 0)


def test_different_extras_second_time_are_replaced_not_stacked():
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders()
    run_once(state, client, result.events, tasks, now, extras=[extra("evt007", at(2026, 10, 28, 18))])
    client.calls.clear()
    _, _, res = run_once(state, client, result.events, tasks, now, extras=[extra("evt007", at(2026, 10, 28, 19), title="Different wording")])
    assert (res.created, res.updated, res.deleted) == (0, 1, 0)
    extras = [r for r in state.reminders.values() if r.kind is ReminderKind.extra]
    assert len(extras) == 1 and extras[0].fire_at.isoformat() == "2026-10-28T19:00:00-07:00"
    _, _, res = run_once(state, client, result.events, tasks, now)  # no extras this time
    assert (res.created, res.updated, res.deleted) == (0, 0, 1)
    assert len(client.items) == 29


def test_calendar_outage_keeps_meeting_and_birthday_reminders():
    s, result, tasks, now, state, client, plan, _ = base_run()
    kept_before = {k for k, r in state.reminders.items() if r.source_type in (SourceType.meeting, SourceType.birthday)}
    client.calls.clear()
    _, diff, res = run_once(state, client, [], tasks, now, unavailable=frozenset({SourceType.meeting, SourceType.birthday}))
    assert res.deleted == 0 and res.updated == 0 and set(diff.kept) == kept_before
    assert kept_before <= set(state.reminders)


def test_calendar_back_after_outage_writes_nothing():
    s, result, tasks, now, state, client, plan, _ = base_run()
    run_once(state, client, [], tasks, now, unavailable=frozenset({SourceType.meeting, SourceType.birthday}))
    client.calls.clear()
    _, diff, _ = run_once(state, client, result.events, tasks, now)
    assert diff.writes == 0


def test_without_unavailable_flag_missing_events_are_deleted():
    """Guard for the test above: the outage flag is what keeps them."""
    s, result, tasks, now, state, client, plan, _ = base_run()
    _, _, res = run_once(state, client, [], tasks, now)
    assert res.deleted == sum(r.source_type in (SourceType.meeting, SourceType.birthday) for r in plan.reminders)


def test_past_reminders_are_never_changed():
    s, result, tasks, now, state, client, plan, _ = base_run()
    later = at(2026, 10, 28, 12)  # 09:00 and 11:00 reminders have fired
    client.calls.clear()
    events = [e for e in result.events if e.id != "evt002"]  # its reminder fired already
    brief_id = next(r.reminder_id for r in state.reminders.values() if r.source_type is SourceType.brief)
    _, diff, res = run_once(state, client, events, tasks, later)
    assert res.updated == 0
    # the only write to a past reminder is deleting our own past brief item
    assert [c for c in client.writes() if c[0] in ("update", "delete")] == [("delete", brief_id)]
    assert res.deleted == 1 and res.forgotten == 2  # 09:00 and 11:00 meetings: left in Reminders
    assert all(r.fire_at > later for r in state.reminders.values())


def test_reminder_removed_by_hand_is_recreated_on_update_and_ignored_on_delete():
    s, result, tasks, now, state, client, plan, _ = base_run()
    dentist = state.reminders["meeting:evt004:2026-10-28T14:30:00-07:00"].reminder_id
    lunch = state.reminders["meeting:evt003:2026-10-28T11:00:00-07:00"].reminder_id
    del client.items[dentist], client.items[lunch]
    events = [meeting("evt004", at(2026, 10, 28, 16, 30), title="Dentist appointment") if e.id == "evt004" else e for e in result.events if e.id != "evt003"]
    _, _, res = run_once(state, client, events, tasks, now)
    assert res.error is None and res.missing == 2 and (res.created, res.updated, res.deleted) == (0, 1, 1)
    assert state.reminders["meeting:evt004:2026-10-28T15:30:00-07:00"].reminder_id in client.items


def test_failed_write_stops_and_state_matches_the_list():
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders()
    real_create, calls = client.create, []

    def flaky(title, fire_at, notes):
        if len(calls) == 3:
            raise OSError("simulated osascript failure")
        calls.append(1)
        return real_create(title, fire_at, notes)

    client.create = flaky
    _, _, res = run_once(state, client, result.events, tasks, now)
    assert res.error == "OSError" and res.created == 3
    assert {r.reminder_id for r in state.reminders.values()} == set(client.items)
    client.create = real_create
    _, _, res = run_once(state, client, result.events, tasks, now)  # next run finishes the job
    assert res.created == 26 and len(client.items) == 29


def test_blocked_list_writes_nothing_and_state_is_unchanged():
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders(access=ReminderAccess.account_not_allowed)
    _, _, res = run_once(state, client, result.events, tasks, now)
    assert res.error == "RemindersBlockedError" and res.created == 0
    assert state.reminders == {} and client.writes() == []


def test_apply_logs_ids_and_keys_only(tmp_path):
    from assistant.logging_setup import RunLog

    s, result, tasks, now = scenario_inputs("base")
    config.ensure_data_dir(tmp_path)
    state, client = State(), FakeReminders()
    plan = build_plan(s.preferences, result.events, tasks, now)
    with RunLog(tmp_path, "run", TZ) as log:
        apply_diff(reconcile(plan.reminders, state, now), client, state, log)
    lines = [json.loads(line) for f in (tmp_path / "logs").iterdir() for line in f.read_text().splitlines()]
    created = [line for line in lines if line["event"] == "reminder_created"]
    assert len(created) == 29 and all("redacted" not in line for line in created)
    text = "\n".join(f.read_text() for f in (tmp_path / "logs").iterdir())
    assert "Dentist" not in text and "pwned" not in text and "Morning brief" not in text


def test_planned_keys_and_ids_match_the_log_formats():
    s, result, tasks, now, state, client, plan, _ = base_run()
    for key, record in state.reminders.items():
        fields = {"key": key, "reminder_id": record.reminder_id, "source_id": record.source_id}
        assert sanitize(fields) == fields


# --- Safety test 7: reconciler scope (SECURITY §7) --------------------------------------------


def test_reconciler_never_touches_reminders_outside_state():
    s, result, tasks, now, state, client, plan, _ = base_run()
    mine = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000A", title="Call mum (made by hand, fake)", notes="")
    stray_key = "meeting:evt002:2026-10-28T09:30:00-07:00"
    stray = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000B", title="Marked but not in state", notes=marker("default", stray_key))
    client.items[mine.reminder_id] = mine
    client.items[stray.reminder_id] = stray
    # Everything changes: no events, no tasks, a different time of day.
    for events, tasks_now, when in [([], [], now), (result.events, [], at(2026, 10, 29, 12)), ([], tasks, at(2026, 11, 3, 9))]:
        ours = {r.reminder_id for r in state.reminders.values()}
        client.calls.clear()
        run_once(state, client, events, tasks_now, when)
        touched = {c[1] for c in client.writes() if c[0] in ("update", "delete")}
        assert touched <= ours
        assert mine.reminder_id not in touched and stray.reminder_id not in touched
    assert client.items[mine.reminder_id] == mine and client.items[stray.reminder_id] == stray


def test_reconcile_only_names_keys_in_state():
    s, result, tasks, now, state, client, plan, _ = base_run()
    diff = reconcile([], state, at(2026, 10, 29, 12))
    assert set(diff.delete) | {k for k, _ in diff.update} | set(diff.forget) <= set(state.reminders)


# --- Missing or invalid state.json (FR-23a) -----------------------------------------------------


def test_missing_state_with_empty_list_starts_fresh(tmp_path):
    config.ensure_data_dir(tmp_path)
    hand_made = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000A", title="By hand", notes="no marker")
    check = check_state(tmp_path, [hand_made])
    assert check.problem is None and check.state == State()


def test_missing_state_with_marked_items_blocks_writes(tmp_path):
    s, result, tasks, now, state, client, plan, _ = base_run()
    config.ensure_data_dir(tmp_path)
    check = check_state(tmp_path, client.list_items())
    assert check.state is None and check.problem is StateProblem.missing
    assert not (tmp_path / STATE_FILE).exists()


def test_missing_state_with_unreadable_list_blocks_writes(tmp_path):
    config.ensure_data_dir(tmp_path)
    check = check_state(tmp_path, None)
    assert check.state is None and check.problem is StateProblem.missing_unverified


def test_invalid_state_is_copied_once_and_blocks_writes(tmp_path):
    config.ensure_data_dir(tmp_path)
    (tmp_path / STATE_FILE).write_text('{"schema_version": 1, "reminders": {"x": 1}}')
    first = check_state(tmp_path, [])
    second = check_state(tmp_path, [])
    assert first.state is None and first.problem is StateProblem.invalid and first.details
    assert first.backup and first.backup.read_text() == (tmp_path / STATE_FILE).read_text()
    assert oct(first.backup.stat().st_mode & 0o777) == "0o600"
    assert second.backup is None and len(list((tmp_path / "backups").glob("state-invalid-*.json"))) == 1
    (tmp_path / STATE_FILE).write_text("not json")
    assert check_state(tmp_path, []).backup is not None  # a different bad file gets its own copy
    assert (tmp_path / STATE_FILE).read_text() == "not json"  # never repaired


def test_valid_state_loads(tmp_path):
    s, result, tasks, now, state, client, plan, _ = base_run()
    config.ensure_data_dir(tmp_path)
    save_state(tmp_path, state)
    assert check_state(tmp_path, client.list_items()).state == state


# --- Marker and state rebuild (ARCHITECTURE §6.5) -------------------------------------------------


def test_marker_round_trip_for_every_planned_reminder():
    s, result, tasks, now, state, client, plan, _ = base_run()
    for r in plan.reminders:
        m = parse_marker(r.notes)
        assert (m.kind, m.source_type, m.source_id, m.fire_at, m.key) == (r.kind, r.source_type, r.source_id, r.fire_at, r.key)


@pytest.mark.parametrize(
    "notes",
    [
        "",
        "no marker here",
        "[pa:meeting:evt001:2026-10-28T09:00:00-07:00]",  # old format, no kind
        "[pa:bogus:meeting:evt001:2026-10-28T09:00:00-07:00]",
        "[pa:default:party:evt001:2026-10-28T09:00:00-07:00]",
        "[pa:default:meeting:evt001:2026-10-28T09:00-07:00]",  # not the canonical time format
        "[pa:default:meeting:evt001:2026-13-28T09:00:00-07:00]",
        "[pa:default:meeting:evt001:2026-10-28T09:00:00-07:00][pa:extra:task:t-1:2026-10-28T09:00:00-07:00]",
        "[pa:default:meeting::2026-10-28T09:00:00-07:00]",
        "[pa:default:meeting:evt001:2026-10-28T16:00:00-00:00]",  # reads back as +00:00: not canonical
    ],
)
def test_malformed_markers_are_ignored(notes):
    assert parse_marker(notes) is None


def test_rebuild_restores_state_and_next_run_makes_no_duplicates():
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders()
    run_once(state, client, result.events, tasks, now, extras=[extra("evt007", at(2026, 10, 28, 18))])
    hand_made = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000A", title="By hand", notes="")
    client.items[hand_made.reminder_id] = hand_made
    rebuilt = plan_rebuild(client.list_items())
    assert rebuilt.state.reminders == state.reminders  # same keys, IDs, kinds, titles and hashes
    assert rebuilt.state.last_runs == {} and rebuilt.unmarked == 1 and rebuilt.duplicates == 0
    assert rebuilt.counts == {"meeting": 17, "task": 9, "birthday": 3, "brief": 1}
    client.calls.clear()
    _, diff, _ = run_once(rebuilt.state, client, result.events, tasks, now, extras=[extra("evt007", at(2026, 10, 28, 18))])
    assert diff.writes == 0


def test_rebuild_keeps_one_of_two_items_with_the_same_marker():
    note = marker("default", "task:t-1:2026-10-29T08:00:00-07:00")
    a = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000A", title="A", notes=note)
    b = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000B", title="B", notes=note)
    rebuilt = plan_rebuild([b, a])
    assert [r.reminder_id for r in rebuilt.state.reminders.values()] == [a.reminder_id] and rebuilt.duplicates == 1


def test_rebuild_cleans_odd_titles():
    note = marker("default", "task:t-1:2026-10-29T08:00:00-07:00")
    item = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-00000000000A", title="  ", notes=note)
    assert plan_rebuild([item]).state.reminders["task:t-1:2026-10-29T08:00:00-07:00"].title == "(no title)"


# --- CLI: assistant state rebuild ------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config.ensure_data_dir(data_dir)
    (data_dir / "preferences.yaml").write_text(EXAMPLE_PREFS.read_text())
    monkeypatch.setattr(config, "default_data_dir", lambda: data_dir)
    s, result, tasks, now = scenario_inputs("base")
    state, client = State(), FakeReminders()
    run_once(state, client, result.events, tasks, now)
    monkeypatch.setattr(cli, "reminders_client", lambda: client)
    return data_dir, state, client


def rebuild(input: str):
    return CliRunner().invoke(app, ["state", "rebuild"], input=input)


def test_cli_rebuild_declined_writes_nothing(cli_env):
    data_dir, state, client = cli_env
    out = rebuild("n\n")
    assert out.exit_code == 0 and "Not rebuilt." in out.output
    assert "meeting  16" in out.output and "task     9" in out.output
    assert not (data_dir / STATE_FILE).exists()
    assert len(client.writes()) == 29  # only the setup run's creates; rebuild never writes to Reminders


def test_cli_rebuild_default_answer_is_no(cli_env):
    data_dir, _, _ = cli_env
    assert "Not rebuilt." in rebuild("\n").output
    assert not (data_dir / STATE_FILE).exists()


def test_cli_rebuild_confirmed_backs_up_and_writes(cli_env):
    data_dir, state, client = cli_env
    (data_dir / STATE_FILE).write_text("not json")
    out = rebuild("y\n")
    assert out.exit_code == 0, out.output
    assert "Rebuilt state.json with 29 reminder(s)" in out.output
    assert load_state(data_dir).reminders == state.reminders and load_state(data_dir).last_runs == {}
    (saved,) = (data_dir / "backups").glob("state-*.json")
    assert saved.read_text() == "not json"
    assert oct((data_dir / STATE_FILE).stat().st_mode & 0o777) == "0o600"
    log = "\n".join(f.read_text() for f in (data_dir / "logs").iterdir())
    assert '"event": "state_rebuilt"' in log and '"count": 29' in log


def test_cli_rebuild_blocked_list(cli_env, monkeypatch):
    data_dir, _, client = cli_env
    client.access = ReminderAccess.list_missing
    out = rebuild("y\n")
    assert out.exit_code == 1 and "list_missing" in out.output
    assert not (data_dir / STATE_FILE).exists()


def test_cli_rebuild_waits_for_no_other_run(cli_env):
    from assistant.guard import run_lock

    data_dir, _, _ = cli_env
    with run_lock(data_dir):
        out = rebuild("y\n")
    assert out.exit_code == 1 and "another run is in progress" in out.output
    assert not (data_dir / STATE_FILE).exists()


def test_cli_rebuild_when_osascript_fails_changes_nothing(cli_env, monkeypatch):
    import subprocess

    from assistant.integrations.reminders import AppleRemindersClient

    def failing(args):
        return subprocess.CompletedProcess(args, 1, "", "execution error: something odd (-2700)")

    monkeypatch.setattr(cli, "reminders_client", lambda: AppleRemindersClient(failing))
    out = rebuild("y\n")
    assert out.exit_code == 1 and "couldn't read" in out.output and "Nothing was changed" in out.output


# --- Past brief items are cleaned up (only ours, only tracked) ---------------------------------------------


def test_past_brief_item_is_deleted_but_nothing_else_is_touched():
    from assistant.integrations.reminders import ReminderItem

    s, result, tasks, now, state, client, plan, _ = base_run()
    mine = ReminderItem(reminder_id="x-apple-reminder://00000000-0000-4000-8000-000000000001", title="Morning brief ready (by hand)", notes="")
    marked_untracked = ReminderItem(
        reminder_id="x-apple-reminder://00000000-0000-4000-8000-000000000002",
        title="Morning brief ready",
        notes="[pa:default:brief:2026-10-27:2026-10-27T08:01:00-07:00]",
    )
    client.items[mine.reminder_id] = mine
    client.items[marked_untracked.reminder_id] = marked_untracked
    brief_key, brief = next((k, r) for k, r in state.reminders.items() if r.source_type is SourceType.brief)
    past_meetings = {r.reminder_id for r in state.reminders.values() if r.source_type is not SourceType.brief and r.fire_at <= at(2026, 10, 29, 9)}
    client.calls.clear()
    tomorrow = at(2026, 10, 29, 9)
    _, diff, res = run_once(state, client, result.events, tasks, tomorrow)
    assert brief_key in diff.delete and brief_key not in diff.forget
    assert ("delete", brief.reminder_id) in client.writes() and brief.reminder_id not in client.items
    assert brief_key not in state.reminders
    # a hand-made item and a marked item not in state.json are never touched, nor are past meeting reminders
    assert mine.reminder_id in client.items and marked_untracked.reminder_id in client.items
    deleted = {c[1] for c in client.writes() if c[0] == "delete"}
    assert not deleted & past_meetings and past_meetings <= set(client.items)


def test_past_brief_item_already_gone_is_dropped_quietly():
    s, result, tasks, now, state, client, plan, _ = base_run()
    brief_key, brief = next((k, r) for k, r in state.reminders.items() if r.source_type is SourceType.brief)
    del client.items[brief.reminder_id]  # I deleted or the list lost it
    _, _, res = run_once(state, client, result.events, tasks, at(2026, 10, 29, 9))
    assert res.error is None and res.missing >= 1 and brief_key not in state.reminders


def test_future_brief_item_is_not_deleted_early():
    s, result, tasks, now, state, client, plan, _ = base_run()
    brief = next(r for r in state.reminders.values() if r.source_type is SourceType.brief)
    client.calls.clear()
    run_once(state, client, result.events, tasks, brief.fire_at - timedelta(minutes=1))
    assert ("delete", brief.reminder_id) not in client.writes()

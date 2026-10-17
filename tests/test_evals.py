"""M9: the eval runner's scoring, stop rules and the capture case file (no AI)."""

import asyncio
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.agent.runner import AgentFailure, FailureKind
from assistant.flows.capture import CaptureOutcome
from assistant.schemas import TaskProposal
from evals import run_evals as R
from tests.fakes.scenario import load

TZ = ZoneInfo("America/Los_Angeles")


def case(**over):
    base = {"id": 1, "input": "x", "now": "2026-10-28T08:00:00-07:00", "expected": {"category": "shopping", "deadline": ["2026-10-29"], "priority": "medium"}, "relative_date": True, "clarify_ok": False}
    return base | over


def proposal(**over):
    return TaskProposal.model_validate({"title": "Buy milk", "category": "shopping", "deadline": "2026-10-29", "assumptions": ["tomorrow = 2026-10-29"]} | over)


# --- Scoring ------------------------------------------------------------------------------------------


def test_all_correct():
    checks = R.score_capture(case(), proposal(), None, TZ)
    assert checks == {"category": True, "deadline": True, "priority": True, "assumptions": True} and R.passed(checks)


def test_date_only_expectation_accepts_a_time_on_that_day():
    assert R.score_capture(case(), proposal(deadline="2026-10-29T18:00"), None, TZ)["deadline"]


def test_time_expectation_needs_the_exact_instant():
    c = case(expected={"category": "shopping", "deadline": ["2026-11-01T07:00"], "priority": "medium"})
    assert R.score_capture(c, proposal(deadline="2026-11-01T07:00", assumptions=["2026-11-01"]), None, TZ)["deadline"]
    assert not R.score_capture(c, proposal(deadline="2026-11-01", assumptions=["2026-11-01"]), None, TZ)["deadline"]
    assert not R.score_capture(c, proposal(deadline="2026-11-01T07:00-07:00", assumptions=["2026-11-01"]), None, TZ)["deadline"]  # PDT is an hour off


def test_invented_deadline_fails():
    c = case(expected={"category": "shopping", "deadline": [None], "priority": "low"}, relative_date=False)
    assert not R.score_capture(c, proposal(priority="low"), None, TZ)["deadline"]
    assert R.score_capture(c, proposal(priority="low", deadline=None), None, TZ)["deadline"]


def test_clarifying_question_counts_only_when_allowed():
    asked = proposal(deadline=None, clarifying_question="When?")
    assert R.score_capture(case(clarify_ok=True), asked, "When?", TZ)["deadline"]
    assert not R.score_capture(case(clarify_ok=False), asked, "When?", TZ)["deadline"]


def test_assumptions_must_state_the_resolved_date():
    checks = R.score_capture(case(), proposal(assumptions=["tomorrow"]), None, TZ)
    assert checks["assumptions"] is False and not R.passed(checks)
    assert R.score_capture(case(relative_date=False), proposal(assumptions=[]), None, TZ)["assumptions"] is None


def test_wrong_category_or_priority():
    checks = R.score_capture(case(), proposal(category="errands", priority="high"), None, TZ)
    assert checks["category"] is False and checks["priority"] is False


# --- Running: stop rules and summary ------------------------------------------------------------------


def fake_capture(script):
    calls = iter(script)

    async def _capture(text, snap, **kw):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return CaptureOutcome(item)

    return _capture


def test_usage_failure_stops_and_marks_the_rest_not_run(monkeypatch):
    _, cases = R.load_cases("capture")
    monkeypatch.setattr(R, "capture", fake_capture([proposal(), AgentFailure(FailureKind.usage_unavailable, "x")]))
    results = asyncio.run(R.run_capture(cases[:5], "base"))
    assert [r["status"] for r in results] == ["run", "not_run", "not_run", "not_run", "not_run"]
    summary = R.summarize(results)
    assert summary["not_run"] == 4 and summary["category"]["met"] is False  # never "met" with cases not run


def test_other_failures_count_as_failed_cases(monkeypatch):
    _, cases = R.load_cases("capture")
    monkeypatch.setattr(R, "capture", fake_capture([AgentFailure(FailureKind.malformed_output, "x"), proposal()]))
    results = asyncio.run(R.run_capture(cases[:2], "base"))
    assert [r["status"] for r in results] == ["failed", "run"] and results[0]["passed"] is False
    assert R.summarize(results)["category"]["of"] == 2


def test_dry_run_uses_no_ai(monkeypatch, capsys):
    monkeypatch.setattr(R, "capture", lambda *a, **k: pytest.fail("no session in a dry run"))
    assert R.main(["--set", "capture", "--dry-run"]) == 0
    assert "35 case(s) → 35 Claude session(s)" in capsys.readouterr().out


def test_runner_refuses_with_billing_variable(monkeypatch):
    from assistant.guard import BillingCredentialError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(BillingCredentialError):
        R.main(["--set", "capture", "--case", "1"])


# --- The case file (EVALS §4.1) -----------------------------------------------------------------------


def all_cases():
    return R.load_cases("capture")[1]


def test_at_least_30_cases_and_5_per_category():
    cases = all_cases()
    prefs = load("base").preferences
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    for cat in prefs.categories:
        assert sum(c["expected"]["category"] == cat for c in cases) >= 5, cat


def test_expectations_are_well_formed():
    prefs = load("base").preferences
    for c in all_cases():
        exp = c["expected"]
        assert exp["category"] in prefs.categories and exp["priority"] in ("high", "medium", "low")
        assert datetime.fromisoformat(c["now"]).tzinfo is not None
        for d in exp["deadline"]:
            assert d is None or (datetime.fromisoformat(d) if "T" in d else date.fromisoformat(d))


def _weekday(now: date, name: str, which: str) -> date:
    """FR-8: 'this X' = X in the current week, 'next X' = X in the following week (weeks start Monday)."""
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    monday = now - timedelta(days=now.weekday())
    return monday + timedelta(days=days.index(name) + (7 if which == "next" else 0))


@pytest.mark.parametrize("which", ["this", "next"])
def test_this_and_next_weekday_expectations_follow_fr8(which):
    checked = 0
    for c in all_cases():
        words = c["input"].lower().replace(",", "").split()
        for i, w in enumerate(words[:-1]):
            if w == which and words[i + 1] in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
                now = datetime.fromisoformat(c["now"]).date()
                assert _weekday(now, words[i + 1], which).isoformat() in c["expected"]["deadline"], c["id"]
                checked += 1
    assert checked >= 2


def test_fr8_examples_present():
    by_input = {c["input"]: c for c in all_cases()}
    assert by_input["send the quarterly report to my manager by next Friday"]["expected"]["deadline"] == ["2026-11-06"]
    assert by_input["get a birthday present for Sam by this Friday"]["expected"]["deadline"] == ["2026-10-30"]


def test_cases_are_fake_and_clean():
    text = json.dumps(all_cases())
    assert "@" not in text  # no emails


# --- M10: daily-run sets (extras, brief, repeat) ------------------------------------------------------------

import functools  # noqa: E402

from assistant.flows import daily_run as D  # noqa: E402
from assistant.integrations.reminders import ReminderItem  # noqa: E402
from assistant.state import marker  # noqa: E402
from tests.fakes.session import Claude, recording_server  # noqa: E402, F401 - autouse fixture

EXTRA = {"title": "Tomorrow 13:00: interview", "fire_at": "2026-10-28T18:00", "source_type": "meeting", "source_id": "evt007", "reason": "Interview tomorrow"}
NO_EXTRAS = {"brief": {"summary": "Meetings from 09:00 to 16:15, expense report due 17:00."}, "extra_reminders": []}
WITH_EXTRA = {**NO_EXTRAS, "extra_reminders": [EXTRA]}


def scripted(monkeypatch, *plans):
    """Each session in the run gets the next plan."""
    queue = list(plans)

    async def session(prompt, options):
        return await Claude(queue.pop(0))(prompt, options)

    monkeypatch.setattr(R, "daily_run", functools.partial(D.daily_run, session=session))


def run_set(name, cases):
    return asyncio.run(R.run_daily_set(name, cases))


@pytest.mark.parametrize("name,sessions", [("extras", 8), ("brief", 6), ("repeat", 2)])
def test_daily_sets_session_counts(name, sessions, capsys, monkeypatch):
    monkeypatch.setattr(R, "daily_run", lambda *a, **k: pytest.fail("no session in a dry run"))
    assert R.main(["--set", name, "--dry-run"]) == 0
    assert f"→ {sessions} Claude session(s)" in capsys.readouterr().out


def test_brief_case_lists_match_code(tmp_path):
    """EVALS §4.4 structure is Layer A now: the reviewed lists in the case file are what code computes."""
    _, cases = R.load_cases("brief")
    assert [c["kind"] for c in cases] == ["normal day", "busy day", "empty day", "overdue tasks", "calendar outage", "birthday"]
    for c in cases:
        world = R.DailyWorld(c, tmp_path)
        snap = D.preflight(world.data, world.sc.preferences, world.sc.now, world.sc.calendar_client(), "", world.reminders, True).snap
        due, late = D.task_lists(snap)
        got = {"schedule": [e.id for e in D.todays_events(snap)], "due_today": [t.id for t in due], "overdue": [t.id for t in late]}
        assert got == c["expected"], c["id"]


def test_extras_case_ids_exist_in_their_fixtures():
    _, cases = R.load_cases("extras")
    assert len(cases) == 8
    for c in cases:
        sc = R.scenario(c)
        real = {e["id"] for events in sc.calendars.values() for e in events} | {t["id"] for t in sc.tasks}
        assert set(c["should"]) | set(c["must_not"]) <= real, c["id"]
        assert not set(c["should"]) & set(c["must_not"])


def test_extras_scoring(monkeypatch):
    _, cases = R.load_cases("extras")
    case1 = cases[0]  # should: evt007
    scripted(monkeypatch, WITH_EXTRA, NO_EXTRAS, {**NO_EXTRAS, "extra_reminders": [{**EXTRA, "source_id": "evt009", "source_type": "meeting"}]})
    ok, missing, wrong = run_set("extras", [case1, case1 | {"id": 2}, case1 | {"id": 3}])
    assert ok["passed"] and ok["checks"]["should"] and ok["proposed"][0]["dropped"] is None
    assert not missing["passed"] and missing["checks"]["should"] is False
    assert not wrong["passed"] and wrong["checks"]["must_not"] is False


def test_an_extra_the_planner_drops_fails_the_case(monkeypatch):
    _, cases = R.load_cases("extras")
    late = {**EXTRA, "fire_at": "2026-10-29T14:00"}  # after the interview starts
    scripted(monkeypatch, {**WITH_EXTRA, "extra_reminders": [EXTRA, late]})
    (r,) = run_set("extras", [cases[0]])
    assert r["checks"]["valid"] is False and r["proposed"][1]["dropped"] == "after_source" and not r["passed"]


def test_no_extras_cases_fail_on_any_proposal(monkeypatch):
    _, cases = R.load_cases("extras")
    extras_off = next(c for c in cases if c["fixture"] == "extras_off")
    scripted(monkeypatch, WITH_EXTRA, NO_EXTRAS)
    proposed, clean = run_set("extras", [extras_off, extras_off | {"id": 99}])
    assert proposed["checks"]["none_proposed"] is False and proposed["proposed"][0]["dropped"] == "not_allowed"
    assert clean["passed"]


def test_mentioned_times():
    assert R.mentioned_times("Sync at 09:00, dentist 3:30pm, call at 7 am and 12:15") == {"09:00", "15:30", "07:00", "12:15"}
    assert R.mentioned_times("3 tasks, 2 overdue, version 1.5") == set()


def test_brief_prechecks_flag_unknown_times_and_ids(monkeypatch):
    _, cases = R.load_cases("brief")
    invented = {"brief": {"summary": "Standup at 11:45, then t-9999.", "focus": ["Call the bank at 4pm"]}}
    scripted(monkeypatch, NO_EXTRAS, invented)
    good, bad = run_set("brief", [cases[0], cases[0] | {"id": 2}])
    assert good["passed"] and good["checks"]["lists"] and good["checklist"] == {"grounded": None, "no_invented": None, "data_gaps": None, "useful_focus": None}
    assert not bad["passed"] and bad["checks"]["unknown_times"] == ["11:45", "16:00"] and bad["checks"]["unknown_ids"] == ["t-9999"]


def test_outage_brief_states_the_gap(monkeypatch):
    _, cases = R.load_cases("brief")
    outage = next(c for c in cases if c["kind"] == "calendar outage")
    scripted(monkeypatch, {"brief": {"summary": "Calendar data is missing today; two tasks are due."}})
    (r,) = run_set("brief", [outage])
    assert r["checks"]["gap_stated"] is True and r["data_gaps"][0].startswith("Calendar data is missing")


def test_repeat_two_runs_no_duplicates_even_when_extras_change(monkeypatch):
    _, cases = R.load_cases("repeat")
    moved = {**WITH_EXTRA, "extra_reminders": [{**EXTRA, "fire_at": "2026-10-28T19:00"}]}
    scripted(monkeypatch, WITH_EXTRA, moved)
    (r,) = run_set("repeat", cases)
    assert r["passed"] and r["found"]["duplicates"] == 0 and r["second_run"] == {"create": 0, "update": 1, "delete": 0}


def test_repeat_check_counts_duplicates(tmp_path):
    _, cases = R.load_cases("repeat")
    world = R.DailyWorld(cases[0], tmp_path)
    note = marker("default", "meeting:evt001:2026-10-28T08:00:00-07:00")
    for n in (1, 2):
        item = ReminderItem(reminder_id=f"x-apple-reminder://AAAAAAAA-0000-0000-0000-00000000000{n}", title="Sync", notes=note)
        world.reminders.items[item.reminder_id] = item
    found = R.repeat_check(world)
    assert found["duplicates"] == 1 and found["matches_state"] is False


def test_daily_set_usage_failure_stops_the_rest(monkeypatch):
    _, cases = R.load_cases("brief")

    async def session(prompt, options):
        raise AgentFailure(FailureKind.usage_unavailable, "rate_limit_rejected:five_hour")

    monkeypatch.setattr(R, "daily_run", functools.partial(D.daily_run, session=session))
    results = run_set("brief", cases[:3])
    assert [r["status"] for r in results] == ["not_run"] * 3
    assert R.summarize_daily("brief", results)["met"] is None


def test_extras_target_needs_every_case_run(monkeypatch):
    results = [{"status": "run", "passed": True, "proposed": []}] * 7 + [{"status": "not_run"}]
    assert R.summarize_daily("extras", results)["met"] is False
    full = [{"status": "run", "passed": i != 0, "proposed": []} for i in range(8)]
    assert R.summarize_daily("extras", full)["met"] is True

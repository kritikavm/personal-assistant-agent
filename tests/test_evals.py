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

"""Layer B eval runner (EVALS §6). Developer script, not an `assistant` command.

    uv run python evals/run_evals.py --set capture            # whole set (capture, extras, brief, repeat, qa)
    uv run python evals/run_evals.py --set capture --case 12  # one case
    uv run python evals/run_evals.py --set capture --dry-run  # list cases and session count, no AI

Runs on the Claude subscription with the same billing check as the product,
fake fixture data only, and writes evals/results/YYYY-MM-DD-HHMM-<set>.json.
If usage runs out, the remaining cases are marked "not_run", never passed.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.agent import runner  # noqa: E402
from assistant.agent.prompts import ASK, CAPTURE, DAILY  # noqa: E402
from assistant.agent.runner import AgentFailure, FailureKind  # noqa: E402
from assistant.agent.tools import (  # noqa: E402
    ANSWER_SCHEMA,
    DAILY_PLAN_SCHEMA,
    TASK_CHANGE_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    UNAVAILABLE,
    MIN_TITLE_MATCH,
    Observed,
    Snapshot,
    _mentions,
    calendar_window,
    free_time,
)
from assistant.flows.ask import AskOutcome, ask  # noqa: E402
from assistant.flows.capture import capture  # noqa: E402
from assistant.flows.daily_run import RunResult, daily_run  # noqa: E402
from assistant.state import load_state, parse_marker  # noqa: E402
from assistant.guard import check_billing  # noqa: E402
from assistant.schemas import Task, TaskProposal  # noqa: E402
from tests.fakes.notify import FakeNotifier  # noqa: E402
from tests.fakes.reminders import FakeReminders  # noqa: E402
from tests.fakes.scenario import Scenario, load  # noqa: E402
from claude_agent_sdk.types import CanUseToolShadowedWarning  # noqa: E402

# Expected: allowlisted tools skip can_use_tool; the PreToolUse hook checks every call (ARCHITECTURE §7).
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

CASES = ROOT / "evals" / "cases"
RESULTS = ROOT / "evals" / "results"
SETS = ("capture", "extras", "brief", "repeat", "qa")  # failure, injection arrive with M12
STOP_KINDS = {FailureKind.usage_unavailable, FailureKind.auth_unavailable, FailureKind.api_key_auth, FailureKind.cli_missing}
TARGETS = {"category": 0.90, "deadline": 0.90, "priority": 0.80}


# --- Scoring (plain code, EVALS §5.1) -------------------------------------------------------------


def _deadline_matches(got: date | datetime | None, expected: str | None, tz) -> bool:
    if expected is None:
        return got is None
    if got is None:
        return False
    if "T" in expected:  # a time was given: compare the instant
        want = datetime.fromisoformat(expected)
        want = want if want.tzinfo else want.replace(tzinfo=tz)
        return isinstance(got, datetime) and got == want
    got_day = got.astimezone(tz).date() if isinstance(got, datetime) else got
    return got_day == date.fromisoformat(expected)


def score_capture(case: dict[str, Any], proposal: TaskProposal, question: str | None, tz) -> dict[str, bool | None]:
    """Exact checks for one case (EVALS §4.1). `proposal` is what would be saved (deadline dropped if a question went unanswered)."""
    exp = case["expected"]
    got = proposal.deadline_value(tz)
    asked_ok = bool(question) and case.get("clarify_ok", False)
    deadline_ok = asked_ok or any(_deadline_matches(got, e, tz) for e in exp["deadline"])
    assumptions_ok: bool | None = None
    if case.get("relative_date") and got is not None:
        day = (got.astimezone(tz).date() if isinstance(got, datetime) else got).isoformat()
        assumptions_ok = any(day in a for a in proposal.assumptions)
    checks = {
        "category": proposal.category == exp["category"],
        "deadline": deadline_ok,
        "priority": proposal.priority.value == exp["priority"],
        "assumptions": assumptions_ok,
    }
    return checks


def passed(checks: dict[str, bool | None]) -> bool:
    return all(v is not False for v in checks.values())


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    run = [r for r in results if r["status"] == "run"]
    summary: dict[str, Any] = {"cases": len(results), "run": len(run), "not_run": sum(r["status"] == "not_run" for r in results)}
    summary["failed_sessions"] = sum(r["status"] == "failed" for r in results)
    scored = [r for r in results if r["status"] in ("run", "failed")]
    for field in ("category", "deadline", "priority"):
        ok = sum(bool(r.get("checks", {}).get(field)) for r in scored)
        summary[field] = {"correct": ok, "of": len(scored), "score": round(ok / len(scored), 3) if scored else None, "target": TARGETS[field]}
        summary[field]["met"] = bool(scored) and summary["not_run"] == 0 and ok / len(scored) >= TARGETS[field]
    rel = [r for r in run if r["checks"].get("assumptions") is not None]
    summary["assumptions"] = {"stated": sum(r["checks"]["assumptions"] for r in rel), "of": len(rel)}
    summary["passed"] = sum(r.get("passed", False) for r in results)
    return summary


# --- Running ---------------------------------------------------------------------------------------


def prompt_version(set_name: str = "capture") -> str:
    """Changes whenever the set's prompt or proposal schema changes (EVALS §7.1 column)."""
    if set_name == "capture":
        text = CAPTURE + json.dumps(TASK_PROPOSAL_SCHEMA, sort_keys=True)
    elif set_name == "qa":
        text = ASK + json.dumps(ANSWER_SCHEMA, sort_keys=True) + json.dumps(TASK_CHANGE_SCHEMA, sort_keys=True)
    else:
        text = DAILY + json.dumps(DAILY_PLAN_SCHEMA, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def code_version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "src"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return rev + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_cases(name: str, case_id: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads((CASES / name / "cases.json").read_text())
    cases = data["cases"]
    if case_id is not None:
        cases = [c for c in cases if c["id"] == case_id]
        if not cases:
            raise SystemExit(f"no case {case_id} in set {name}")
    return data, cases


def capture_snapshot(fixture: str, now: datetime) -> Snapshot:
    s = load(fixture)
    return Snapshot(s.preferences, now, UNAVAILABLE, [Task.model_validate(t) for t in s.tasks], {})


async def run_capture(cases: list[dict[str, Any]], fixture: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stop: str | None = None
    with tempfile.TemporaryDirectory(prefix="pa-eval-") as cwd:
        for case in cases:
            base = {"id": case["id"], "input": case["input"], "now": case["now"], "expected": case["expected"]}
            if stop:
                results.append({**base, "status": "not_run", "reason": stop})
                continue
            snap = capture_snapshot(fixture, datetime.fromisoformat(case["now"]))
            try:
                outcome = await capture(case["input"], snap, cwd=Path(cwd), answer=None)
            except AgentFailure as e:
                print(f"  case {case['id']:>2}: FAILED ({e.kind.value})")
                if e.kind in STOP_KINDS:
                    stop = e.kind.value
                    results.append({**base, "status": "not_run", "reason": stop})
                else:
                    results.append({**base, "status": "failed", "failure": e.kind.value, "checks": {}, "passed": False})
                continue
            p = outcome.proposal
            checks = score_capture(case, p, outcome.question, snap.tz)
            ok = passed(checks)
            results.append(
                {
                    **base,
                    "status": "run",
                    "proposal": json.loads(p.model_dump_json(exclude_none=True)),
                    "question": outcome.question,
                    "retries": len(outcome.error_paths),
                    "turns": sum(t.num_turns for t in outcome.turns),
                    "duration_ms": sum(t.duration_ms for t in outcome.turns),
                    "checks": checks,
                    "passed": ok,
                }
            )
            marks = " ".join(f"{k}={'ok' if v else ('-' if v is None else 'FAIL')}" for k, v in checks.items())
            print(f"  case {case['id']:>2}: {'pass' if ok else 'FAIL'}  {marks}")
    return results


# --- Daily-run sets: extras, brief, repeat (EVALS §4.3, §4.4, §4.8) ------------------------------------------


def scenario(case: dict[str, Any]) -> Scenario:
    s = load(case["fixture"])
    if case.get("now"):
        s.now = datetime.fromisoformat(case["now"])
    return s


class DailyWorld:
    """One case's scratch data folder, fixture calendar and fake Reminders list. Never touches data/."""

    def __init__(self, case: dict[str, Any], folder: Path):
        self.sc = scenario(case)
        self.data = self.sc.write_data_dir(folder / f"case-{case['id']}")
        self.reminders = FakeReminders()
        self.notifier = FakeNotifier()

    async def run(self) -> RunResult:
        return await daily_run(
            self.data,
            self.sc.preferences,
            self.sc.now,
            trigger="manual",
            dry_run=False,
            calendar=self.sc.calendar_client(),
            reminders=self.reminders,
            notifier=self.notifier,
        )


def score_extras(case: dict[str, Any], result: RunResult) -> dict[str, bool | None]:
    """EVALS §4.3: every proposed extra passes the planner, should items get one, must-not items don't."""
    kept = {r.source_id for r in result.plan.extras()}
    return {
        "valid": not result.plan.dropped_extras,
        "should": all(item in kept for item in case.get("should", [])),
        "must_not": not kept & set(case.get("must_not", [])),
        "none_proposed": not result.proposal.extra_reminders if case.get("no_extras") else None,
    }


def extras_record(result: RunResult) -> dict[str, Any]:
    dropped = dict(result.plan.dropped_extras)
    return {
        "proposed": [
            {**json.loads(x.model_dump_json()), "dropped": dropped[i].value if i in dropped else None}
            for i, x in enumerate(result.proposal.extra_reminders)
        ],
    }


_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.IGNORECASE)
_ID = re.compile(r"\b(?:t-\d{4}|evt\d{3}|bday\w+|rec\d{3}\w*|rtn\d{3})\b")


def mentioned_times(text: str) -> set[str]:
    """Clock times in free text, as HH:MM (24-hour)."""
    out = set()
    for m in _CLOCK.finditer(text):
        if m.group(3):
            hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
            hour += 12 if m.group(3).lower().startswith("p") else 0
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
        out.add(f"{hour:02d}:{minute:02d}")
    return out


def known_times(world: DailyWorld, result: RunResult) -> set[str]:
    """Every clock time the fixture or code gave Claude: events, deadlines, free blocks, preferences."""
    tz = world.sc.preferences.tz
    times = set()
    window = world.sc.calendar_client().list_events(world.sc.today, date.fromordinal(world.sc.today.toordinal() + 7))
    for e in window.events:
        for t in (e.start, e.end):
            if isinstance(t, datetime):
                times.add(t.astimezone(tz).strftime("%H:%M"))
    for t in world.sc.tasks:
        d = t.get("deadline")
        if d and "T" in d:
            times.add(datetime.fromisoformat(d).astimezone(tz).strftime("%H:%M"))
    for b in result.brief.free_blocks:
        times |= {datetime.fromisoformat(b[k]).astimezone(tz).strftime("%H:%M") for k in ("start", "end")}
    prefs = world.sc.preferences
    for clock in (prefs.morning_brief.time, prefs.working_hours.start, prefs.working_hours.end, prefs.quiet_hours.start, prefs.quiet_hours.end):
        times.add(clock.strftime("%H:%M"))
    times.add(world.sc.now.astimezone(tz).strftime("%H:%M"))
    return times


def score_brief(case: dict[str, Any], world: DailyWorld, result: RunResult) -> dict[str, Any]:
    """EVALS §4.4: the lists must match the case (code fills them); text gets code pre-checks, then my marks."""
    brief = result.brief
    lists = {
        "schedule": [e.id for e in brief.schedule],
        "due_today": [t.id for t in brief.due_today],
        "overdue": [t.id for t in brief.overdue],
    }
    text = " ".join([brief.summary, *brief.focus])
    real_ids = {e["id"] for events in world.sc.calendars.values() for e in events} | {t["id"] for t in world.sc.tasks}
    unknown_times = sorted(mentioned_times(text) - known_times(world, result))
    unknown_ids = sorted(set(_ID.findall(text)) - real_ids)
    return {
        "lists": lists == case["expected"],
        "times_known": not unknown_times,
        "ids_known": not unknown_ids,
        "gap_stated": bool(brief.data_gaps) if not brief.calendar_available or not brief.tasks_available else None,
        "unknown_times": unknown_times,
        "unknown_ids": unknown_ids,
    }


def repeat_check(world: DailyWorld) -> dict[str, Any]:
    """EVALS §4.8: no duplicate reminders after the runs; the list matches state.json."""
    items = list(world.reminders.items.values())
    markers = [parse_marker(i.notes) for i in items]
    keys = [m.key for m in markers if m]
    per_item: dict[tuple, int] = {}
    for m in markers:
        if m and m.kind.value == "extra":
            per_item[(m.source_type, m.source_id)] = per_item.get((m.source_type, m.source_id), 0) + 1
    state = load_state(world.data)
    cap = world.sc.preferences.reminders.extra_reminders.max_per_item
    return {
        "duplicates": len(keys) - len(set(keys)),
        "unmarked": sum(m is None for m in markers),
        "over_cap": sum(n > cap for n in per_item.values()),
        "matches_state": set(keys) == set(state.reminders) and {i.reminder_id for i in items} == {r.reminder_id for r in state.reminders.values()},
    }


async def run_daily_set(set_name: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stop: str | None = None
    with tempfile.TemporaryDirectory(prefix="pa-eval-") as folder:
        for case in cases:
            base = {"id": case["id"], "fixture": case["fixture"], "now": case.get("now"), "kind": case.get("kind"), "note": case.get("note")}
            base = {k: v for k, v in base.items() if v is not None}
            if stop:
                results.append({**base, "status": "not_run", "reason": stop})
                continue
            world = DailyWorld(case, Path(folder))
            runs: list[RunResult] = []
            try:
                for _ in range(case.get("runs", 1)):
                    runs.append(await world.run())
            except AgentFailure as e:
                print(f"  case {case['id']:>2}: FAILED ({e.kind.value})")
                if e.kind in STOP_KINDS:
                    stop = e.kind.value
                    results.append({**base, "status": "not_run", "reason": stop, "runs_done": len(runs)})
                else:
                    results.append({**base, "status": "failed", "failure": e.kind.value, "checks": {}, "passed": False})
                continue
            last = runs[-1]
            record: dict[str, Any] = {
                **base,
                "status": "run",
                "retries": sum(len(r.error_paths) for r in runs),
                "turns": sum(r.session.num_turns for r in runs),
                "duration_ms": sum(r.session.duration_ms for r in runs),
            }
            if set_name == "extras":
                checks = score_extras(case, last)
                record |= {"should": case.get("should", []), "must_not": case.get("must_not", []), **extras_record(last)}
            elif set_name == "brief":
                checks = score_brief(case, world, last)
                record |= {
                    "summary": last.brief.summary,
                    "focus": last.brief.focus,
                    "data_gaps": last.brief.data_gaps,
                    "markdown": last.markdown,
                    "checklist": {"grounded": None, "no_invented": None, "data_gaps": None, "useful_focus": None},  # my marks (§5.2)
                }
            else:
                found = repeat_check(world)
                second = runs[-1].diff
                checks = {"no_duplicates": found["duplicates"] == 0, "within_cap": found["over_cap"] == 0, "matches_state": found["matches_state"]}
                record |= {"found": found, "second_run": {"create": len(second.create), "update": len(second.update), "delete": len(second.delete)}}
            ok = all(v is not False for k, v in checks.items() if isinstance(v, bool) or v is None)
            record |= {"checks": checks, "passed": ok}
            results.append(record)
            marks = " ".join(f"{k}={'ok' if v else ('-' if v is None else 'FAIL')}" for k, v in checks.items() if isinstance(v, bool) or v is None)
            print(f"  case {case['id']:>2}: {'pass' if ok else 'FAIL'}  {marks}")
            if set_name == "brief":
                print("    summary: " + last.brief.summary)
                for line in last.brief.focus:
                    print("    focus:   " + line)
    return results


def summarize_daily(set_name: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    run = [r for r in results if r["status"] == "run"]
    summary: dict[str, Any] = {
        "cases": len(results),
        "run": len(run),
        "not_run": sum(r["status"] == "not_run" for r in results),
        "failed_sessions": sum(r["status"] == "failed" for r in results),
        "passed": sum(r.get("passed", False) for r in results),
    }
    complete = summary["not_run"] == 0
    if set_name == "extras":
        summary["target"] = "≥ 7 of 8 correct; 0 invalid extras reach the plan"
        summary["met"] = complete and summary["passed"] >= 7 and len(results) == 8
        summary["dropped_extras"] = sum(1 for r in run for x in r["proposed"] if x["dropped"])
    elif set_name == "brief":
        summary["target"] = "0 invented items in 6/6; full checklist in ≥ 5/6 (my marks)"
        summary["code_prechecks_passed"] = summary["passed"]
        summary["met"] = None  # decided after my checklist marks
    else:
        summary["target"] = "0 duplicates"
        summary["met"] = complete and summary["passed"] == len(results)
    return summary


# --- Q&A set (EVALS §4.5) ---------------------------------------------------------------------------------


def qa_snapshot(case: dict[str, Any]) -> tuple[Scenario, Snapshot]:
    """The case's fixture as one `ask` snapshot: calendar window, open and completed tasks."""
    sc = scenario(case)
    completed = {d: [Task.model_validate(t) for t in ts] for d, ts in sc.completed.items()}
    snap = Snapshot(sc.preferences, sc.now, calendar_window(sc.calendar_client(), sc.today), [Task.model_validate(t) for t in sc.tasks], completed)
    return sc, snap


def qa_known_times(snap: Snapshot) -> set[str]:
    """Every clock time the tools could have shown: events, deadlines, free blocks on each day, preferences, now."""
    tz, times = snap.tz, set()
    hm = lambda moment: moment.astimezone(tz).strftime("%H:%M")  # noqa: E731
    if snap.calendar.available:
        for e in snap.calendar.events:
            times |= {hm(t) for t in (e.start, e.end) if isinstance(t, datetime)}
        for i in range(8):
            day = date.fromordinal(snap.today.toordinal() + i)
            for b in free_time(snap, Observed(), day.isoformat())["free"]:
                times |= {hm(datetime.fromisoformat(b[k])) for k in ("start", "end")}
    for t in snap.tasks + [t for ts in snap.completed.values() for t in ts]:
        if isinstance(t.deadline, datetime):
            times.add(hm(t.deadline))
    p = snap.prefs
    times |= {c.strftime("%H:%M") for c in (p.morning_brief.time, p.working_hours.start, p.working_hours.end, p.quiet_hours.start, p.quiet_hours.end)}
    times.add(hm(snap.now))
    return times


_VERDICTS = (("yes", re.compile(r"^yes\b", re.I)), ("no", re.compile(r"^no\b", re.I)), ("unknown", re.compile(r"^i (?:can't|cannot|can’t) tell\b", re.I)))
_CALENDAR_GAP = re.compile(r"calendar\b[^.]*\b(?:unavailable|missing|not available|isn't connected|not connected|couldn't|can't|cannot|unknown)|"
                           r"\b(?:unavailable|missing|can't see|cannot see|no access to)\b[^.]*\bcalendar", re.I)


_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_OVERDUE = re.compile(r"\boverdue\b", re.I)
_NOT_OVERDUE = re.compile(r"(?:\bnot\b|n't\b|\bno longer\b)\W+(?:\w+\W+){0,2}overdue\b", re.I)


def _claims_overdue(text: str) -> bool:
    return bool(_OVERDUE.search(text)) and not _NOT_OVERDUE.search(text)


def called_overdue(answer: str, snap: Snapshot) -> set[str]:
    """IDs of tasks the answer calls overdue: named in a sentence or list item that says "overdue" (not "not yet
    overdue"), or in a list under a heading that does ("Overdue:"). A pre-check; my marks decide."""
    tasks = snap.tasks + [t for ts in snap.completed.values() for t in ts]
    named = lambda text: {t.id for t in tasks if _mentions(text, t.id) or (len(t.title) >= MIN_TITLE_MATCH and _mentions(text, t.title))}  # noqa: E731
    out: set[str] = set()
    heading = False
    for line in answer.splitlines():
        if not line.strip():
            continue
        if _BULLET.match(line):
            if heading or _claims_overdue(line):
                out |= named(line)
            continue
        heading = line.rstrip().endswith(":") and _claims_overdue(line)
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if _claims_overdue(sentence):
                out |= named(sentence)
    return out


def verdict(answer: str) -> str | None:
    text = answer.strip()
    return next((name for name, pattern in _VERDICTS if pattern.match(text)), None)


def score_qa(case: dict[str, Any], snap: Snapshot, outcome: AskOutcome) -> dict[str, Any]:
    """EVALS §4.5 exact checks, plus code pre-checks on the text (times, calendar gap). My checklist marks decide the text."""
    exp = case["expected"]
    events = {s.id for s in outcome.sources if s.type == "event"}
    tasks = {s.id for s in outcome.sources if s.type == "task"}
    checks: dict[str, Any] = {}
    if case["type"] == "day":
        checks["events_exact"] = events == set(exp["events"])
    elif case["type"] == "focus":
        checks["must_cite"] = set(exp["must_cite"]) <= tasks
    elif case["type"] == "overdue":
        want = set(exp["overdue"])
        checks["overdue_cited"] = want - set(exp.get("may_omit", [])) <= tasks
        wrong = sorted(called_overdue(outcome.answer.answer, snap) - want)
        checks["none_wrongly_overdue"] = not wrong
    else:
        checks["verdict"] = verdict(outcome.answer.answer) == exp["verdict"]
    expected_change = exp.get("change")
    got = [{"action": c.action, "task_id": c.task_id} for c in outcome.changes]
    checks["changes"] = got == ([expected_change] if expected_change else [])
    unknown_times = sorted(mentioned_times(outcome.answer.answer) - qa_known_times(snap))
    checks["times_known"] = not unknown_times
    # Only questions that depend on the calendar must say it's missing (an overdue list doesn't).
    needs_calendar = case.get("calendar_missing") and case["type"] in ("day", "focus", "fits")
    checks["calendar_gap_said"] = bool(_CALENDAR_GAP.search(outcome.answer.answer)) if needs_calendar else None
    checks["unknown_times"] = unknown_times
    if case["type"] == "overdue":
        checks["wrongly_overdue"] = wrong
    return checks


async def run_qa(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stop: str | None = None
    with tempfile.TemporaryDirectory(prefix="pa-eval-") as cwd:
        for case in cases:
            base = {"id": case["id"], "type": case["type"], "fixture": case["fixture"], "now": case["now"], "question": case["question"], "expected": case["expected"]}
            if stop:
                results.append({**base, "status": "not_run", "reason": stop})
                continue
            _, snap = qa_snapshot(case)
            try:
                outcome = await ask(case["question"], snap, cwd=Path(cwd))
            except AgentFailure as e:
                print(f"  case {case['id']:>2}: FAILED ({e.kind.value})")
                if e.kind in STOP_KINDS:
                    stop = e.kind.value
                    results.append({**base, "status": "not_run", "reason": stop})
                else:
                    results.append({**base, "status": "failed", "failure": e.kind.value, "checks": {}, "passed": False})
                continue
            checks = score_qa(case, snap, outcome)
            ok = all(v is not False for k, v in checks.items() if isinstance(v, bool) or v is None)
            results.append(
                {
                    **base,
                    "status": "run",
                    "answer": outcome.answer.answer,
                    "sources": [{"type": s.type, "id": s.id} for s in outcome.sources],
                    "data_gaps": outcome.data_gaps,
                    "changes": [{"action": c.action, "task_id": c.task_id, "fields": {k: str(v) for k, v in c.fields.items()}, "reason": c.reason} for c in outcome.changes],
                    "retries": len(outcome.error_paths),
                    "change_retries": len(outcome.change_error_paths),
                    "turns": outcome.session.num_turns,
                    "duration_ms": outcome.session.duration_ms,
                    "checks": checks,
                    "passed": ok,
                    "checklist": {"grounded": None, "cites_sources": None, "no_invented": None, "data_gaps": None, "answers_question": None},  # my marks (§5.2)
                }
            )
            marks = " ".join(f"{k}={'ok' if v else ('-' if v is None else 'FAIL')}" for k, v in checks.items() if isinstance(v, bool) or v is None)
            print(f"  case {case['id']:>2}: {'pass' if ok else 'FAIL'}  {marks}")
            print("    answer: " + " ".join(outcome.answer.answer.split()))
    return results


def summarize_qa(results: list[dict[str, Any]]) -> dict[str, Any]:
    run = [r for r in results if r["status"] == "run"]
    summary = {
        "cases": len(results),
        "run": len(run),
        "not_run": sum(r["status"] == "not_run" for r in results),
        "failed_sessions": sum(r["status"] == "failed" for r in results),
        "passed": sum(r.get("passed", False) for r in results),
        "target": "12/12 correct with citations (exact checks, then my checklist marks)",
    }
    summary["exact_checks_passed"] = summary["passed"]
    summary["met"] = None  # decided after my checklist marks
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--case", type=int)
    parser.add_argument("--dry-run", action="store_true", help="list the cases and how many sessions they'd use; no AI")
    args = parser.parse_args(argv)

    data, cases = load_cases(args.set, args.case)
    if args.dry_run:
        sessions = 0
        for c in cases:
            label = c.get("input") or c.get("question") or c.get("kind") or c.get("note") or c["fixture"]
            print(f"  {c['id']:>2}  {(c.get('now') or load(c['fixture']).now.isoformat())[:16]}  {c.get('fixture', data.get('fixture'))}: {label}")
            sessions += c.get("runs", 1)
        print(f"{len(cases)} case(s) → {sessions} Claude session(s) on the subscription (model {runner.MODEL}).")
        return 0

    check_billing(os.environ)
    login = runner.check_login()
    if not login.ok:
        raise SystemExit(f"Claude: {login.summary}")

    started = datetime.now().astimezone()
    print(f"Running {args.set}: {len(cases)} case(s), model {runner.MODEL}, prompt {prompt_version(args.set)}")
    if args.set == "capture":
        results = asyncio.run(run_capture(cases, data["fixture"]))
        summary = summarize(results)
    elif args.set == "qa":
        results = asyncio.run(run_qa(cases))
        summary = summarize_qa(results)
    else:
        results = asyncio.run(run_daily_set(args.set, cases))
        summary = summarize_daily(args.set, results)
    out = {
        "set": args.set,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": runner.MODEL,
        "prompt_version": prompt_version(args.set),
        "code_version": code_version(),
        "fixture": data.get("fixture", "per case"),
        "summary": summary,
        "cases": results,
    }
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{started:%Y-%m-%d-%H%M}-{args.set}{'-case' + str(args.case) if args.case else ''}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Saved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

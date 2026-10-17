"""Layer B eval runner (EVALS §6). Developer script, not an `assistant` command.

    uv run python evals/run_evals.py --set capture            # whole set
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
from assistant.agent.prompts import CAPTURE  # noqa: E402
from assistant.agent.runner import AgentFailure, FailureKind  # noqa: E402
from assistant.agent.tools import TASK_PROPOSAL_SCHEMA, UNAVAILABLE, Snapshot  # noqa: E402
from assistant.flows.capture import capture  # noqa: E402
from assistant.guard import check_billing  # noqa: E402
from assistant.schemas import Task, TaskProposal  # noqa: E402
from tests.fakes.scenario import load  # noqa: E402
from claude_agent_sdk.types import CanUseToolShadowedWarning  # noqa: E402

# Expected: allowlisted tools skip can_use_tool; the PreToolUse hook checks every call (ARCHITECTURE §7).
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

CASES = ROOT / "evals" / "cases"
RESULTS = ROOT / "evals" / "results"
SETS = ("capture",)  # extras, brief, qa, failure, injection, repeat arrive with M10–M12
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


def prompt_version() -> str:
    """Changes whenever the capture prompt or the submit_task schema changes (EVALS §7.1 column)."""
    text = CAPTURE + json.dumps(TASK_PROPOSAL_SCHEMA, sort_keys=True)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--case", type=int)
    parser.add_argument("--dry-run", action="store_true", help="list the cases and how many sessions they'd use; no AI")
    args = parser.parse_args(argv)

    data, cases = load_cases(args.set, args.case)
    if args.dry_run:
        for c in cases:
            print(f"  {c['id']:>2}  {c['now'][:16]}  {c['input']}")
        print(f"{len(cases)} case(s) → {len(cases)} Claude session(s) on the subscription (model {runner.MODEL}).")
        return 0

    check_billing(os.environ)
    login = runner.check_login()
    if not login.ok:
        raise SystemExit(f"Claude: {login.summary}")

    started = datetime.now().astimezone()
    print(f"Running {args.set}: {len(cases)} case(s), model {runner.MODEL}, prompt {prompt_version()}")
    results = asyncio.run(run_capture(cases, data["fixture"]))
    summary = summarize(results)
    out = {
        "set": args.set,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": runner.MODEL,
        "prompt_version": prompt_version(),
        "code_version": code_version(),
        "fixture": data["fixture"],
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

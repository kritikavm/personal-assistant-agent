"""M9 Layer A: task capture with scripted fake sessions (EVALS §4.6 malformed output, step limit)."""

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from typer.testing import CliRunner

from assistant import cli, config
from assistant.agent import tools as T
from assistant.agent.runner import AgentFailure, FailureKind, SessionResult
from assistant.agent.tools import UNAVAILABLE, Observed, ProposalBox, Snapshot
from assistant.flows import capture as C
from assistant.schemas import Task, TaskProposal
from assistant.tasks_store import TaskStore

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=TZ)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

GOOD = {"title": "Buy milk", "category": "shopping", "deadline": "2026-10-29", "assumptions": ["tomorrow = 2026-10-29"]}


@pytest.fixture(autouse=True)
def recording_server(monkeypatch):
    """Replace the in-process MCP server with one the fake session can call directly."""
    monkeypatch.setattr(T, "create_sdk_mcp_server", lambda name, tools: {"type": "sdk", "name": name, "tools": {t.name: t for t in tools}})


class FakeConversation:
    """Plays Claude: each send() runs the next scripted turn of submit_task calls.

    Like the SDK, arguments that break the tool's JSON schema are rejected before the handler runs.
    A turn can also be an exception to raise (e.g. the step limit).
    """

    script: list = []
    sent: list[str] = []

    def __init__(self, options):
        self.tools = options.mcp_servers["pa"]["tools"]
        self.gate_hook = options.hooks["PreToolUse"][0].hooks[0]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send(self, prompt):
        FakeConversation.sent.append(prompt)
        turn = FakeConversation.script.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for args in turn:
            decision = await self.gate_hook({"tool_name": "mcp__pa__submit_task", "tool_input": args}, "id", {})
            assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
            tool = self.tools["submit_task"]
            try:
                jsonschema.validate(args, tool.input_schema)
            except jsonschema.ValidationError:
                continue
            await tool.handler(args)
        return SessionResult(text="ok", init={}, num_turns=1)


def snap(tasks=None) -> Snapshot:
    from assistant.schemas import Preferences
    import yaml

    prefs = Preferences.model_validate(yaml.safe_load((EXAMPLES / "preferences.yaml").read_text()))
    return Snapshot(prefs, NOW, UNAVAILABLE, tasks or [], {})


def run(script, answer=None, log=None):
    FakeConversation.script = list(script)
    FakeConversation.sent = []
    return asyncio.run(C.capture("buy milk tomorrow", snap(), cwd=Path("/tmp"), answer=answer, log=log, conversation=FakeConversation))


# --- submit_task validation -----------------------------------------------------------------------


def test_valid_proposal_is_accepted():
    out = run([[GOOD]])
    assert out.proposal.title == "Buy milk" and out.proposal.deadline == "2026-10-29" and out.error_paths == []
    assert FakeConversation.sent == ["Add this task: buy milk tomorrow"]


def test_priority_defaults_to_preferences_default():
    assert run([[GOOD]]).proposal.priority.value == "medium"


@pytest.mark.parametrize(
    "bad,paths",
    [
        ({**GOOD, "category": "groceries"}, ["category"]),
        ({**GOOD, "priority": "urgent"}, ["priority"]),
        ({**GOOD, "deadline": "tomorrow"}, ["deadline"]),
        ({**GOOD, "deadline": "2026-02-30"}, ["deadline"]),
        ({k: v for k, v in GOOD.items() if k != "title"}, ["title"]),
        ({**GOOD, "estimated_minutes": 0}, ["estimated_minutes"]),
        ({**GOOD, "colour": "red"}, ["colour"]),
        ({**GOOD, "assumptions": [""]}, ["assumptions[0]"]),
    ],
)
def test_invalid_then_fixed_is_one_retry(bad, paths):
    out = run([[bad, GOOD]])
    assert out.proposal.title == "Buy milk" and out.error_paths == [paths]


def test_two_invalid_attempts_fail_cleanly():
    with pytest.raises(AgentFailure) as e:
        run([[{**GOOD, "category": "nope"}, {**GOOD, "category": "still nope"}, GOOD]])
    assert e.value.kind is FailureKind.malformed_output


def test_third_attempt_is_refused_even_if_valid():
    box = ProposalBox()
    s = snap()
    assert T.submit_task(s, box, {**GOOD, "category": "x"})["accepted"] is False
    assert T.submit_task(s, box, {**GOOD, "category": "y"})["accepted"] is False
    assert T.submit_task(s, box, GOOD) == {"accepted": False, "errors": ["no more attempts; stop here"]}


def test_errors_back_to_claude_name_fields_not_values():
    out = T.submit_task(snap(), ProposalBox(), {**GOOD, "category": "SECRET-ish", "priority": "CANARY-cap1"})
    assert out["accepted"] is False and "priority" in " ".join(out["errors"])
    assert "CANARY-cap1" not in json.dumps(out)


def test_schema_rejected_arguments_never_reach_the_handler_but_session_must_still_submit():
    with pytest.raises(AgentFailure) as e:
        run([[{**GOOD, "estimated_minutes": "thirty"}]])  # SDK-level type error only
    assert e.value.kind is FailureKind.no_proposal


def test_no_submission_fails_cleanly():
    with pytest.raises(AgentFailure) as e:
        run([[]])
    assert e.value.kind is FailureKind.no_proposal


def test_step_limit_fails_cleanly():
    with pytest.raises(AgentFailure) as e:
        run([AgentFailure(FailureKind.step_limit, "error_max_turns")])
    assert e.value.kind is FailureKind.step_limit


def test_second_submission_after_acceptance_is_refused():
    box = ProposalBox()
    assert T.submit_task(snap(), box, GOOD) == {"accepted": True}
    assert T.submit_task(snap(), box, {**GOOD, "title": "Something else"})["accepted"] is False
    assert box.accepted.title == "Buy milk"


def test_deadline_with_time_and_offset():
    p = TaskProposal.model_validate({**GOOD, "deadline": "2026-10-29T17:00"})
    assert p.deadline_value(TZ) == datetime(2026, 10, 29, 17, 0, tzinfo=TZ)
    p = TaskProposal.model_validate({**GOOD, "deadline": "2026-10-29T17:00-04:00"})
    assert p.deadline_value(TZ).utcoffset().total_seconds() == -4 * 3600


# --- Clarifying question (FR-9) ---------------------------------------------------------------------


ASKS = {"title": "Renew passport", "category": "personal", "assumptions": ["no deadline given"], "clarifying_question": "When do you need it by?", "deadline": "2026-11-30"}


def test_answered_question_gets_a_second_turn():
    answers = []
    second = {k: v for k, v in ASKS.items() if k != "clarifying_question"} | {"deadline": "2026-11-20"}
    out = run([[ASKS], [second]], answer=lambda q: answers.append(q) or "by Nov 20")
    assert answers == ["When do you need it by?"] and out.answered and out.proposal.deadline == "2026-11-20"
    assert FakeConversation.sent[1] == "My answer to your question: by Nov 20"


def test_unanswered_question_saves_without_deadline():
    out = run([[ASKS]], answer=lambda q: "   ")
    assert out.deadline_dropped and out.proposal.deadline is None and len(FakeConversation.sent) == 1


def test_no_answer_function_means_no_deadline():
    out = run([[ASKS]])
    assert out.question == "When do you need it by?" and out.deadline_dropped and out.proposal.deadline is None


def test_follow_up_gets_a_fresh_retry():
    out = run([[ASKS], [{**GOOD, "category": "bad"}, GOOD]], answer=lambda q: "tomorrow")
    assert out.proposal.title == "Buy milk" and out.error_paths == [["category"]]


def test_logs_have_field_paths_only(tmp_path):
    from assistant.logging_setup import RunLog

    config.ensure_data_dir(tmp_path)
    with RunLog(tmp_path, "add", TZ) as log:
        with pytest.raises(AgentFailure):
            run([[{**GOOD, "category": "CANARY-cap2"}, {**GOOD, "title": "", "category": "CANARY-cap2"}]], log=log)
    text = "\n".join(f.read_text() for f in (tmp_path / "logs").iterdir())
    lines = [json.loads(line) for line in text.splitlines()]
    invalid = [line for line in lines if line["event"] == "proposal_invalid"]
    assert [line["field_paths"] for line in invalid] == [["category"], ["title"]]  # the category check runs only once the rest is valid and [line["attempt"] for line in invalid] == [1, 2]
    assert "CANARY-cap2" not in text and "Buy milk" not in text


# --- Task store add ------------------------------------------------------------------------------------


def test_store_add_sets_code_fields(tmp_path):
    config.ensure_data_dir(tmp_path)
    store = TaskStore(tmp_path, TZ, now=lambda: NOW)
    task = store.add(TaskProposal.model_validate(GOOD).task_fields(TZ))
    assert task.id == "t-0001" and task.status.value == "todo" and task.created_at == NOW and task.deadline == date(2026, 10, 29)
    assert store.add(TaskProposal.model_validate(GOOD).task_fields(TZ)).id == "t-0002"
    assert [t.id for t in store.load_open()] == ["t-0001", "t-0002"]


def test_next_id_counts_completed_tasks(tmp_path):
    config.ensure_data_dir(tmp_path)
    store = TaskStore(tmp_path, TZ, now=lambda: NOW)
    store.add(TaskProposal.model_validate(GOOD).task_fields(TZ))
    store.complete("t-0001")
    assert store.add(TaskProposal.model_validate(GOOD).task_fields(TZ)).id == "t-0002"


# --- CLI: assistant add ---------------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    (d / config.PREFERENCES_FILE).write_text((EXAMPLES / "preferences.yaml").read_text())
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    monkeypatch.setattr(C, "Conversation", FakeConversation)
    return d


def add(args, script, input=None):
    FakeConversation.script = list(script)
    FakeConversation.sent = []
    real = C.capture

    async def fake_capture(text, snap, **kw):
        return await real(text, snap, **{**kw, "conversation": FakeConversation})

    import assistant.cli as cli_module

    original = cli_module.capture
    cli_module.capture = fake_capture
    try:
        return CliRunner().invoke(cli.app, ["add", *args], input=input)
    finally:
        cli_module.capture = original


def saved(d) -> list[Task]:
    return TaskStore(d, TZ).load_open()


def test_cli_add_shows_and_asks_default_yes(data_dir):
    out = add(["buy milk tomorrow"], [[GOOD]], input="\n")
    assert out.exit_code == 0, out.output
    assert "Task:      Buy milk" in out.output and "Category:  shopping" in out.output and "Priority:  medium" in out.output
    assert "Deadline:  2026-10-29 (Thursday)" in out.output and "  - tomorrow = 2026-10-29" in out.output
    assert "Save? [Y/n]" in out.output and "Saved as t-0001." in out.output
    assert [t.title for t in saved(data_dir)] == ["Buy milk"]


def test_cli_add_declined_writes_nothing(data_dir):
    out = add(["buy milk tomorrow"], [[GOOD]], input="n\n")
    assert out.exit_code == 0 and "Not saved." in out.output and saved(data_dir) == []


def test_cli_add_yes_still_prints_then_saves(data_dir):
    out = add(["buy milk tomorrow", "--yes"], [[GOOD]])
    assert out.exit_code == 0 and "Assumptions:" in out.output and "Save?" not in out.output and len(saved(data_dir)) == 1


def test_cli_add_question_answered(data_dir):
    second = {k: v for k, v in ASKS.items() if k != "clarifying_question"} | {"deadline": "2026-11-20"}
    out = add(["renew passport"], [[ASKS], [second]], input="by Nov 20\n\n")
    assert out.exit_code == 0, out.output
    assert "Question:  When do you need it by?" in out.output and saved(data_dir)[0].deadline == date(2026, 11, 20)


def test_cli_add_question_skipped_saves_without_deadline(data_dir):
    out = add(["renew passport"], [[ASKS]], input="\n\n")
    assert "saved without a deadline" in out.output and saved(data_dir)[0].deadline is None


def test_cli_add_yes_with_question_never_asks(data_dir):
    out = add(["renew passport", "--yes"], [[ASKS]])
    assert "Question:" not in out.output and "saved without a deadline" in out.output and saved(data_dir)[0].deadline is None


def test_cli_add_failure_saves_nothing(data_dir):
    out = add(["buy milk"], [[{**GOOD, "category": "x"}, {**GOOD, "category": "y"}]])
    assert out.exit_code == 1 and "malformed_output" in out.output and "Nothing was saved" in out.output and saved(data_dir) == []
    log = "\n".join(f.read_text() for f in (data_dir / "logs").iterdir())
    assert '"status": "malformed_output"' in log


def test_cli_add_refuses_with_billing_variable(data_dir):
    out = CliRunner().invoke(cli.app, ["add", "buy milk"], env={"ANTHROPIC_API_KEY": "x"})
    assert out.exit_code == 1 and "ANTHROPIC_API_KEY" in out.output and saved(data_dir) == []


def test_cli_add_refuses_on_corrupt_tasks_file(data_dir):
    (data_dir / "tasks.json").write_text("not json")
    out = add(["buy milk"], [[GOOD]])
    assert out.exit_code == 1 and "refusing to write" in out.output


def test_cli_add_doesnt_hold_the_lock_while_asking(data_dir):
    """The lock is only taken for the write, like `tasks delete` (a waiting prompt never blocks a run)."""
    from assistant.guard import run_lock

    seen = {}

    def answer_while_checking(q):
        try:
            with run_lock(data_dir):
                seen["free"] = True
        except Exception:
            seen["free"] = False
        return ""

    FakeConversation.script = [[ASKS]]
    asyncio.run(C.capture("x", snap(), cwd=data_dir, answer=answer_while_checking, conversation=FakeConversation))
    assert seen == {"free": True}


def test_cli_add_log_has_ids_not_text(data_dir):
    add(["buy milk tomorrow", "--yes"], [[GOOD]])
    log = "\n".join(f.read_text() for f in (data_dir / "logs").iterdir())
    assert '"task_id": "t-0001"' in log and "Buy milk" not in log and "milk" not in log

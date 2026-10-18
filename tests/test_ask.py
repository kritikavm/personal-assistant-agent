"""M11 Layer A: Q&A with a scripted Claude (FR-19–FR-21, EVALS §4.5 structure, safety test 4 for `ask`)."""

import asyncio
import json
from datetime import date, datetime

import pytest
from typer.testing import CliRunner

from assistant import cli, config
from assistant.agent import tools as T
from assistant.agent.runner import AgentFailure, FailureKind
from assistant.flows import ask as A
from assistant.logging_setup import RunLog
from assistant.schemas import Task, TaskUpdate
from assistant.tasks_store import TaskStore
from tests.fakes.scenario import load
from tests.fakes.session import Asker, recording_server  # noqa: F401 - autouse fixture

TODAY = "2026-10-28"
READS = [
    ("get_tasks", {}),
    ("get_calendar_events", {"start_date": TODAY, "end_date": "2026-11-04"}),
    ("find_free_time", {"date": TODAY}),
]
OVERDUE = {
    "answer": "Two tasks are overdue: Repot the monstera (due 2026-10-21) and Renew library books (due 2026-10-27).",
    "sources": [{"type": "task", "id": "t-0104", "title": "Repot the monstera"}, {"type": "task", "id": "t-0103", "title": "Renew library books"}],
}


def snapshot(name="base", now=None, calendar=True, tasks_error=None) -> T.Snapshot:
    sc = load(name)
    if now:
        sc.now = now
    cal = T.calendar_window(sc.calendar_client(), sc.today) if calendar else T.UNAVAILABLE
    completed = {d: [Task.model_validate(t) for t in ts] for d, ts in sc.completed.items()}
    tasks = [] if tasks_error else [Task.model_validate(t) for t in sc.tasks]
    return T.Snapshot(sc.preferences, sc.now, cal, tasks, completed, tasks_error)


def run(claude, snap=None, log=None, tmp_path=None):
    return asyncio.run(A.ask("What is overdue?", snap or snapshot(), cwd=tmp_path or config.default_data_dir(), log=log, session=claude))


def answers(claude) -> list[dict]:
    return [r for r in claude.replies if "accepted" in r]


# --- submit_answer: citations (FR-20) ---------------------------------------------------------------


def test_cited_answer_is_accepted_and_sources_come_from_the_data(tmp_path):
    claude = Asker(*READS, ("submit_answer", {**OVERDUE, "sources": [{"type": "task", "id": "t-0104", "title": "Wrong title"}, OVERDUE["sources"][1]]}))
    out = run(claude, tmp_path=tmp_path)
    assert answers(claude)[-1] == {"accepted": True}
    assert [(s.type, s.id, s.item.title) for s in out.sources] == [("task", "t-0104", "Repot the monstera"), ("task", "t-0103", "Renew library books")]
    assert out.changes == [] and out.data_gaps == []


def test_unknown_id_is_rejected_then_fixed(tmp_path):
    bad = {**OVERDUE, "sources": [*OVERDUE["sources"], {"type": "task", "id": "t-9999"}]}
    claude = Asker(*READS, ("submit_answer", bad), ("submit_answer", OVERDUE))
    out = run(claude, tmp_path=tmp_path)
    first = answers(claude)[0]
    assert first["accepted"] is False and first["errors"] == ["sources[2].id: not an ID of that type returned by a tool this session"]
    assert out.error_paths == [["sources[2].id"]]


def test_id_of_the_wrong_type_is_rejected(tmp_path):
    bad = {"answer": "You have the team sync.", "sources": [{"type": "task", "id": "evt001"}]}
    claude = Asker(*READS, ("submit_answer", bad))
    with pytest.raises(AgentFailure):
        run(claude, tmp_path=tmp_path)
    assert answers(claude)[0]["errors"][0].startswith("sources[0].id")


def test_id_not_returned_this_session_is_rejected_even_if_real(tmp_path):
    """t-0103 exists, but no tool returned it in this session."""
    claude = Asker(("submit_answer", OVERDUE))
    with pytest.raises(AgentFailure) as e:
        run(claude, tmp_path=tmp_path)
    assert e.value.kind is FailureKind.malformed_output


@pytest.mark.parametrize(
    "answer",
    [
        "Repot the monstera and Renew library books are overdue; also Submit expense report is due at 17:00.",
        "Overdue: t-0104, t-0103 and t-0101.",
        "Repot the monstera, Renew library books, then SUBMIT EXPENSE REPORT.",
    ],
)
def test_answer_naming_an_uncited_item_is_rejected(tmp_path, answer):
    claude = Asker(*READS, ("submit_answer", {**OVERDUE, "answer": answer}))
    with pytest.raises(AgentFailure):
        run(claude, tmp_path=tmp_path)
    assert answers(claude)[0]["errors"] == ["sources: the answer mentions task t-0101 but doesn't cite it"]


def test_uncited_event_title_is_rejected(tmp_path):
    claude = Asker(*READS, ("submit_answer", {"answer": "First the Weekly team sync at 09:00.", "sources": []}))
    with pytest.raises(AgentFailure):
        run(claude, tmp_path=tmp_path)
    assert "event evt001" in answers(claude)[0]["errors"][0]


def test_mentions_are_whole_words():
    assert T._mentions("after gym class, rest", "Gym class")
    assert not T._mentions("t-0101 is due", "t-010")
    assert not T._mentions("photosynthesis", "synth")


def test_short_titles_dont_count_as_mentions(tmp_path):
    """A title under 4 characters would match ordinary words; it's left to the checklist."""
    snap = snapshot()
    snap.tasks.append(snap.tasks[0].model_copy(update={"id": "t-0500", "title": "Gym"}))
    seen = T.Observed(task_ids={"t-0500"})
    assert T.uncited(snap, seen, "Go to the gym.", set()) == []


def test_answer_with_no_sources_is_fine_when_it_names_nothing(tmp_path):
    claude = Asker(*READS, ("submit_answer", {"answer": "Nothing is overdue.", "sources": []}))
    assert run(claude, snapshot("empty_day"), tmp_path=tmp_path).sources == []


def test_no_answer_fails_cleanly(tmp_path):
    with pytest.raises(AgentFailure) as e:
        run(Asker(*READS), tmp_path=tmp_path)
    assert e.value.kind is FailureKind.no_proposal


def test_two_invalid_answers_fail_cleanly(tmp_path):
    bad = {"answer": "", "sources": []}
    claude = Asker(*READS, ("submit_answer", bad), ("submit_answer", bad), ("submit_answer", OVERDUE))
    with pytest.raises(AgentFailure) as e:
        run(claude, tmp_path=tmp_path)
    assert e.value.kind is FailureKind.malformed_output
    assert answers(claude)[-1]["errors"] == ["no more attempts; stop here"]


def test_completed_tasks_can_be_cited_when_read(tmp_path):
    snap = snapshot()
    done_id = next(iter(snap.completed.values()))[0].id
    claude = Asker(("get_tasks", {"include_completed": True}), ("submit_answer", {"answer": "Done recently.", "sources": [{"type": "task", "id": done_id}]}))
    out = run(claude, snap, tmp_path=tmp_path)
    assert out.sources[0].id == done_id


# --- Data gaps (FR-20, FR-22) -------------------------------------------------------------------------


def test_calendar_unavailable_gap_is_added_by_code(tmp_path):
    claude = Asker(*READS, ("submit_answer", {"answer": "The calendar is unavailable, so I can't tell.", "sources": [], "data_gaps": ["x"]}))
    out = run(claude, snapshot(calendar=False), tmp_path=tmp_path)
    assert out.data_gaps[0].startswith("Calendar data is missing: Google Calendar isn't connected yet")
    assert out.data_gaps[1:] == ["x"]


def test_calendar_outage_free_time_is_unknown_and_no_event_can_be_cited(tmp_path):
    claude = Asker(*READS, ("submit_answer", {"answer": "Can't tell.", "sources": [{"type": "event", "id": "evt001"}]}))
    with pytest.raises(AgentFailure):
        run(claude, snapshot("calendar_outage"), tmp_path=tmp_path)
    free = next(r for r in claude.replies if "free" in r)
    assert free["free"] == [] and free["note"] == "calendar unavailable: free time unknown"


def test_unreadable_tasks_gap(tmp_path):
    claude = Asker(("get_tasks", {}), ("submit_answer", {"answer": "I can't read your tasks.", "sources": []}))
    out = run(claude, snapshot(tasks_error="tasks_unavailable"), tmp_path=tmp_path)
    assert any(g.startswith("Tasks couldn't be read") for g in out.data_gaps)


def test_skipped_calendar_gap():
    snap = snapshot()
    snap.calendar = snap.calendar.model_copy(update={"skipped_calendars": ["missing@example.com"]})
    assert A.ask_gaps(snap) == ["Calendar(s) not found and skipped: missing@example.com."]


# --- propose_task_change (FR-21) ------------------------------------------------------------------------


def queue_with(*proposals, snap=None, seen_tasks=True):
    snap = snap or snapshot()
    seen = T.Observed()
    if seen_tasks:
        T.tasks(snap, seen)
    queue = T.ChangeQueue()
    outs = [T.propose_task_change(snap, seen, queue, p) for p in proposals]
    return queue, outs


def test_complete_is_queued_not_written(tmp_path):
    queue, outs = queue_with({"action": "complete", "task_id": "t-0103", "reason": "User returned the books."})
    assert outs == [{"queued": True}]
    assert [(c.action, c.task_id, c.fields) for c in queue.changes] == [("complete", "t-0103", {})]


def test_update_converts_fields_for_the_store():
    queue, outs = queue_with(
        {"action": "update", "task_id": "t-0101", "changes": {"priority": "low", "deadline": "2026-10-30T09:00", "estimated_minutes": None}, "reason": "asked"}
    )
    assert outs == [{"queued": True}]
    fields = queue.changes[0].fields
    assert fields["priority"] == "low" and fields["estimated_minutes"] is None
    assert fields["deadline"] == datetime.fromisoformat("2026-10-30T09:00:00-07:00")


def test_create_uses_capture_rules():
    queue, outs = queue_with({"action": "create", "changes": {"title": "Buy stamps", "category": "errands", "deadline": "2026-10-29"}, "reason": "asked"})
    assert outs == [{"queued": True}]
    f = queue.changes[0].fields
    assert f["title"] == "Buy stamps" and f["priority"] == "medium" and f["deadline"] == date(2026, 10, 29)


@pytest.mark.parametrize(
    "proposal, error",
    [
        ({"action": "complete", "task_id": "t-9999", "reason": "r"}, "task_id: not an open task returned by get_tasks this session"),
        ({"action": "complete", "reason": "r"}, "task_id: required for complete"),
        ({"action": "complete", "task_id": "t-0103", "changes": {"title": "x"}, "reason": "r"}, "changes: leave it out for complete"),
        ({"action": "create", "task_id": "t-0103", "changes": {"title": "x", "category": "errands"}, "reason": "r"}, "task_id: leave it out for create"),
        ({"action": "create", "changes": {"title": "x", "category": "nope"}, "reason": "r"}, "changes.category: must be one of"),
        ({"action": "create", "changes": {"title": "x"}, "reason": "r"}, "changes.category: Field required"),
        ({"action": "create", "changes": {"title": "x", "category": "errands", "deadline": "2026-02-30"}, "reason": "r"}, "changes.deadline: not a real date"),
        ({"action": "update", "task_id": "t-0101", "changes": {}, "reason": "r"}, "changes: nothing to change"),
        ({"action": "update", "task_id": "t-0101", "changes": {"title": None}, "reason": "r"}, "changes: title can't be cleared"),
        ({"action": "update", "task_id": "t-0101", "changes": {"status": "done"}, "reason": "r"}, "changes.status"),
        ({"action": "update", "task_id": "t-0101", "changes": {"id": "t-1"}, "reason": "r"}, "changes.id: Extra inputs are not permitted"),
        ({"action": "update", "task_id": "t-0101", "changes": {"category": "nope"}, "reason": "r"}, "changes.category: must be one of"),
        ({"action": "archive", "task_id": "t-0101", "reason": "r"}, "action"),
        ({"action": "complete", "task_id": "t-0103"}, "reason: Field required"),
    ],
)
def test_invalid_changes_are_rejected(proposal, error):
    queue, outs = queue_with(proposal)
    assert outs[0]["queued"] is False and outs[0]["errors"][0].startswith(error), outs
    assert queue.changes == []


def test_completed_task_cant_be_changed():
    snap = snapshot()
    done_id = next(iter(snap.completed.values()))[0].id
    seen = T.Observed()
    T.tasks(snap, seen, include_completed=True)
    out = T.propose_task_change(snap, seen, T.ChangeQueue(), {"action": "delete", "task_id": done_id, "reason": "r"})
    assert out["queued"] is False


def test_task_not_shown_this_session_cant_be_changed():
    _, outs = queue_with({"action": "delete", "task_id": "t-0103", "reason": "r"}, seen_tasks=False)
    assert outs[0]["queued"] is False


def test_one_retry_in_all_for_changes():
    bad = {"action": "complete", "reason": "r"}
    good = {"action": "complete", "task_id": "t-0103", "reason": "r"}
    queue, outs = queue_with(bad, bad, good)
    assert outs[2] == {"queued": False, "errors": ["no more attempts; tell the user the change couldn't be prepared"]}
    assert queue.changes == []


def test_at_most_five_changes_and_one_per_task():
    ids = ["t-0101", "t-0102", "t-0103", "t-0104", "t-0105", "t-0106"]
    queue, outs = queue_with(*({"action": "complete", "task_id": i, "reason": "r"} for i in ids))
    assert len(queue.changes) == 5 and outs[5]["errors"] == ["at most 5 changes per question"]
    _, outs = queue_with({"action": "complete", "task_id": "t-0101", "reason": "r"}, {"action": "delete", "task_id": "t-0101", "reason": "r"})
    assert outs[1]["errors"] == ["task_id: a change for this task is already queued"]


def test_invalid_change_doesnt_fail_the_answer(tmp_path):
    claude = Asker(*READS, ("propose_task_change", {"action": "complete", "task_id": "t-9999", "reason": "r"}), ("submit_answer", OVERDUE))
    out = run(claude, tmp_path=tmp_path)
    assert out.changes == [] and out.change_error_paths == [["task_id"]]


def test_task_update_store_changes():
    u = TaskUpdate.model_validate({"description": "", "deadline": "2026-11-01", "priority": "high"})
    assert u.store_changes(load("base").preferences.tz) == {"deadline": date(2026, 11, 1), "description": None, "priority": "high"}


# --- Session set-up (safety test 4) -----------------------------------------------------------------------


def test_ask_session_has_only_its_tools_and_step_limit(tmp_path):
    claude = Asker(*READS, ("submit_answer", OVERDUE))
    run(claude, tmp_path=tmp_path)
    names = set(claude.options.mcp_servers["pa"]["tools"])
    assert names == {"get_preferences", "get_tasks", "get_calendar_events", "find_free_time", "submit_answer", "propose_task_change"}
    assert claude.options.max_turns == A.MAX_TURNS == 10
    assert "Task changes: only when the user's own question explicitly asks" in claude.options.system_prompt
    assert claude.prompt == "What is overdue?"


def test_calendar_range_is_clamped_in_ask(tmp_path):
    claude = Asker(("get_calendar_events", {"start_date": "2026-10-01", "end_date": "2026-12-31"}), ("submit_answer", {"answer": "x", "sources": []}))
    run(claude, tmp_path=tmp_path)
    assert claude.replies[0]["range"] == {"start_date": TODAY, "end_date": "2026-11-04"}


def test_log_has_paths_and_ids_only(tmp_path):
    snap = snapshot()
    claude = Asker(*READS, ("propose_task_change", {"action": "complete", "reason": "Renew library books"}), ("submit_answer", {**OVERDUE, "sources": []}), ("submit_answer", OVERDUE))
    with RunLog(tmp_path, "ask", snap.tz) as log:
        run(claude, snap, log=log, tmp_path=tmp_path)
    text = "\n".join(p.read_text() for p in (tmp_path / "logs").iterdir())
    assert '"field_paths": ["sources", "sources"]' in text and '"field_paths": ["task_id"]' in text
    assert "library" not in text.lower() and "monstera" not in text.lower()


# --- CLI ---------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_data(tmp_path, monkeypatch):
    sc = load("base")
    data = sc.write_data_dir(tmp_path / "data")
    monkeypatch.setattr(config, "default_data_dir", lambda: data)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return sc.now.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    return data


def cli_ask(monkeypatch, claude, input=None, question="What is overdue?"):
    real = A.ask

    async def fake(q, snap, **kw):
        return await real(q, snap, **{**kw, "session": claude})

    monkeypatch.setattr(cli, "ask", fake)
    return CliRunner().invoke(cli.app, ["ask", question], input=input)


COMPLETE = ("propose_task_change", {"action": "complete", "task_id": "t-0103", "reason": "User returned the books."})


def open_ids(data) -> list[str]:
    return [t.id for t in TaskStore(data, load("base").preferences.tz).load_open()]


def test_cli_prints_answer_sources_and_calendar_gap(cli_data, monkeypatch):
    out = cli_ask(monkeypatch, Asker(*READS, ("submit_answer", OVERDUE)))
    assert out.exit_code == 0, out.output
    assert out.output.startswith(OVERDUE["answer"])
    assert "Sources:\n  task   t-0104  Repot the monstera (medium, due 2026-10-21)" in out.output
    assert "Data gaps:\n  - Calendar data is missing: Google Calendar isn't connected yet" in out.output


def test_cli_change_needs_y_default_is_no(cli_data, monkeypatch):
    before = (cli_data / "tasks.json").read_bytes()
    out = cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)), input="\n")
    assert "Proposed: Mark done: t-0103 Renew library books" in out.output and "Apply? [y/N]" in out.output
    assert "Not applied." in out.output
    assert (cli_data / "tasks.json").read_bytes() == before


def test_cli_change_n_writes_nothing(cli_data, monkeypatch):
    before = {p.name: p.read_bytes() for p in cli_data.glob("*.json")}
    cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)), input="n\n")
    assert {p.name: p.read_bytes() for p in cli_data.glob("*.json")} == before
    assert not (cli_data / "backups").exists() or not any((cli_data / "backups").iterdir())


def test_cli_change_y_applies_through_the_store(cli_data, monkeypatch):
    out = cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)), input="y\n")
    assert out.exit_code == 0, out.output
    assert "Applied: t-0103" in out.output and "t-0103" not in open_ids(cli_data)
    completed = json.loads((cli_data / "completed_tasks.json").read_text())
    assert any(t["id"] == "t-0103" for ts in completed.values() for t in ts)


def test_cli_each_change_is_asked_separately(cli_data, monkeypatch):
    update = ("propose_task_change", {"action": "update", "task_id": "t-0101", "changes": {"priority": "low"}, "reason": "asked"})
    out = cli_ask(monkeypatch, Asker(*READS, COMPLETE, update, ("submit_answer", OVERDUE)), input="n\ny\n")
    assert "Change t-0101 Submit expense report: priority high → low" in out.output
    assert "t-0103" in open_ids(cli_data)
    task = next(t for t in TaskStore(cli_data, load("base").preferences.tz).load_open() if t.id == "t-0101")
    assert task.priority.value == "low"


def test_cli_create_change(cli_data, monkeypatch):
    create = ("propose_task_change", {"action": "create", "changes": {"title": "Buy stamps", "category": "errands"}, "reason": "asked"})
    out = cli_ask(monkeypatch, Asker(*READS, create, ("submit_answer", OVERDUE)), input="y\n")
    assert "Proposed: Add task: Buy stamps (category errands, priority medium)" in out.output
    assert "t-0114" in open_ids(cli_data)


def test_cli_failure_writes_nothing(cli_data, monkeypatch):
    before = {p.name: p.read_bytes() for p in cli_data.glob("*.json")}
    out = cli_ask(monkeypatch, Asker(fail=AgentFailure(FailureKind.step_limit, "x")))
    assert out.exit_code == 1 and "couldn't answer: step_limit. Nothing was changed." in out.output
    assert {p.name: p.read_bytes() for p in cli_data.glob("*.json")} == before


def test_cli_refuses_with_billing_variable(cli_data, monkeypatch):
    out = CliRunner().invoke(cli.app, ["ask", "x"], env={"ANTHROPIC_API_KEY": "x"})
    assert out.exit_code == 1 and "ANTHROPIC_API_KEY" in out.output


def test_cli_unreadable_tasks_still_answers(cli_data, monkeypatch):
    (cli_data / "tasks.json").write_text("{not json")
    out = cli_ask(monkeypatch, Asker(("get_tasks", {}), ("submit_answer", {"answer": "I can't read your tasks.", "sources": []})))
    assert out.exit_code == 0 and "Tasks couldn't be read" in out.output


def test_cli_doesnt_hold_the_lock_while_asking(cli_data, monkeypatch):
    from assistant.guard import run_lock

    held = {}

    def confirm(*a, **kw):
        with run_lock(cli_data):  # would raise if `ask` held it
            held["free"] = True
        return False

    monkeypatch.setattr(cli.typer, "confirm", confirm)
    cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)))
    assert held == {"free": True}


def test_cli_log_has_no_question_or_answer_text(cli_data, monkeypatch):
    cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)), input="y\n", question="Is the CANARY-Q task overdue?")
    log = "\n".join(p.read_text() for p in (cli_data / "logs").iterdir())
    assert '"event": "change_applied"' in log and '"task_id": "t-0103"' in log
    assert "CANARY-Q" not in log and "library" not in log.lower() and "overdue" not in log.lower()


def test_cli_change_is_not_applied_while_another_run_holds_the_lock(cli_data, monkeypatch):
    from assistant.guard import run_lock

    with run_lock(cli_data):
        out = cli_ask(monkeypatch, Asker(*READS, COMPLETE, ("submit_answer", OVERDUE)), input="y\n")
    assert "Not applied: " in out.output and "t-0103" in open_ids(cli_data)

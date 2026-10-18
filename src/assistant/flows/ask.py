"""Q&A: one question → a cited answer, plus any task changes I asked for (FR-19–FR-21, ARCHITECTURE §6.3).

Claude only reads and proposes. Code checks every cited ID, adds the data gaps
it knows about, and shows the sources from the data itself. Task changes are
queued here and applied by the CLI one at a time, each after an explicit `y`.
The eval runner calls `ask` the same way and never applies anything.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from assistant.agent.permissions import PermissionGate
from assistant.agent.prompts import ask_prompt
from assistant.agent.runner import SessionResult, build_options, run_session
from assistant.agent.tools import ChangeQueue, Observed, ProposalBox, QueuedChange, Snapshot, allowlist, build_server
from assistant.flows.daily_run import CALENDAR_GAPS
from assistant.logging_setup import RunLog
from assistant.schemas import AnswerProposal, CalendarEvent, Task
from assistant.tasks_store import TaskStore

MAX_TURNS = 10  # step limit for `ask` (ARCHITECTURE key decisions)

Session = Callable[[str, ClaudeAgentOptions], Awaitable[SessionResult]]

def ask_gaps(snap: Snapshot) -> list[str]:
    """Gaps code always reports (FR-20, FR-22), whatever Claude wrote."""
    gaps = []
    cal = snap.calendar
    if not cal.available:
        why = CALENDAR_GAPS.get(cal.error or "", "The calendar couldn't be reached or its login expired (run `assistant auth google` if this continues).")
        gaps.append(f"Calendar data is missing: {why} Events, birthdays and free time aren't included.")
    elif cal.skipped_calendars:
        gaps.append(f"Calendar(s) not found and skipped: {', '.join(cal.skipped_calendars)}.")
    if snap.tasks_error:
        gaps.append("Tasks couldn't be read (tasks.json is invalid; see backups/), so tasks aren't included.")
    return gaps


@dataclass
class Source:
    """A cited item, as the data has it (Claude's copy of the title isn't shown)."""

    type: str
    id: str
    item: CalendarEvent | Task


def resolve_sources(snap: Snapshot, answer: AnswerProposal) -> list[Source]:
    events = {e.id: e for e in snap.calendar.events} if snap.calendar.available else {}
    tasks = {t.id: t for t in snap.tasks} | {t.id: t for group in snap.completed.values() for t in group}
    out, done = [], set()
    for s in answer.sources:
        item = (events if s.type == "event" else tasks).get(s.id)
        if item is not None and (s.type, s.id) not in done:  # submit_answer already refused unknown IDs
            out.append(Source(s.type, s.id, item))
            done.add((s.type, s.id))
    return out


@dataclass
class AskOutcome:
    answer: AnswerProposal
    sources: list[Source]
    data_gaps: list[str]  # code's gaps first, then Claude's
    changes: list[QueuedChange]
    session: SessionResult
    error_paths: list[list[str]] = field(default_factory=list)  # invalid answers, field paths only
    change_error_paths: list[list[str]] = field(default_factory=list)  # invalid change proposals


async def ask(question: str, snap: Snapshot, *, cwd: Path, log: RunLog | None = None, session: Session = run_session) -> AskOutcome:
    """Run the `ask` session. Raises AgentFailure (usage, step limit, malformed output, no answer, …)."""
    seen, box, queue = Observed(), ProposalBox(), ChangeQueue()
    gate = PermissionGate("ask", allowlist("ask"), snap.today, log)
    servers, names = build_server("ask", snap, seen, box, queue)
    options = build_options(system_prompt=ask_prompt(snap.now, snap.tz), max_turns=MAX_TURNS, cwd=cwd, allowed_tools=names, mcp_servers=servers, gate=gate)
    try:
        result = await session(question, options)
    finally:
        if log:
            for i, paths in enumerate(box.error_paths, 1):
                log.event("proposal_invalid", attempt=i, field_paths=paths)
            for i, paths in enumerate(queue.error_paths, 1):
                log.event("change_invalid", attempt=i, field_paths=paths)
    answer = box.result("submit_answer")
    return AskOutcome(
        answer=answer,
        sources=resolve_sources(snap, answer),
        data_gaps=ask_gaps(snap) + list(answer.data_gaps),
        changes=list(queue.changes),
        session=result,
        error_paths=box.error_paths,
        change_error_paths=queue.error_paths,
    )


def apply_change(store: TaskStore, change: QueuedChange, categories: list[str]) -> Task:
    """Write one confirmed change through the task store (validation, backup, atomic write). Raises TaskStoreError."""
    if change.action == "create":
        return store.add(change.fields)
    if change.action == "update":
        return store.edit(change.task_id, change.fields, categories=categories)
    if change.action == "complete":
        return store.complete(change.task_id)
    return store.delete(change.task_id)


def when_text(value: datetime | object, tz) -> str:
    if isinstance(value, datetime):
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)

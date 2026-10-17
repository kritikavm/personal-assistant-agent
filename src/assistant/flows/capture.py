"""Task capture: plain language → one validated task proposal (FR-7–FR-10, ARCHITECTURE §6.2).

Claude only proposes. Nothing here writes: the CLI shows the proposal, asks,
and saves through the task store. The eval runner calls `capture` the same way
and reads the proposal without saving.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from assistant.agent.permissions import PermissionGate
from assistant.agent.prompts import capture_prompt
from assistant.agent.runner import Conversation, SessionResult, build_options
from assistant.agent.tools import Observed, ProposalBox, Snapshot, allowlist, build_server
from assistant.logging_setup import RunLog
from assistant.schemas import TaskProposal

MAX_TURNS = 6  # step limit for `add` (ARCHITECTURE key decisions)


@dataclass
class CaptureOutcome:
    proposal: TaskProposal
    question: str | None = None  # clarifying question Claude asked, if any
    answered: bool = False  # the user answered it and Claude submitted again
    deadline_dropped: bool = False  # question left unanswered: saved without a deadline (FR-9)
    turns: list[SessionResult] = field(default_factory=list)
    error_paths: list[list[str]] = field(default_factory=list)  # invalid attempts, field paths only


def _accepted(box: ProposalBox) -> TaskProposal:
    return box.result("submit_task")


def _log_attempts(log: RunLog | None, box: ProposalBox, start: int) -> None:
    if log:
        for i, paths in enumerate(box.error_paths[start:], start + 1):
            log.event("proposal_invalid", attempt=i, field_paths=paths)


async def capture(
    text: str,
    snap: Snapshot,
    *,
    cwd: Path,
    answer: Callable[[str], str] | None = None,
    log: RunLog | None = None,
    conversation: Callable[..., Conversation] = Conversation,
) -> CaptureOutcome:
    """Run the `add` session. `answer` asks the user a clarifying question (None: don't ask).

    Raises AgentFailure (usage, step limit, malformed output, no proposal, …).
    """
    seen, box = Observed(), ProposalBox()
    gate = PermissionGate("add", allowlist("add"), snap.today, log)
    servers, names = build_server("add", snap, seen, box)
    options = build_options(
        system_prompt=capture_prompt(snap.now, snap.tz),
        max_turns=MAX_TURNS,
        cwd=cwd,
        allowed_tools=names,
        mcp_servers=servers,
        gate=gate,
    )
    async with conversation(options) as conv:
        try:
            turns = [await conv.send(f"Add this task: {text}")]
        finally:
            _log_attempts(log, box, 0)
        outcome = CaptureOutcome(_accepted(box), turns=turns)
        question = outcome.proposal.clarifying_question
        if question:
            outcome.question = question
            reply = answer(question).strip() if answer else ""
            if reply:
                done = len(box.error_paths)
                box.new_turn()
                try:
                    outcome.turns.append(await conv.send(f"My answer to your question: {reply}"))
                finally:
                    _log_attempts(log, box, done)
                outcome.proposal = _accepted(box)
                outcome.answered = True
            else:
                outcome.proposal = outcome.proposal.model_copy(update={"deadline": None})
                outcome.deadline_dropped = True
    outcome.error_paths = box.error_paths
    return outcome

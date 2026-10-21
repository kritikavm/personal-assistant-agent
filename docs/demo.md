# Demo: a permission-constrained agent in 2 minutes

A short talk-through of this project as an agentic AI case study. Everything shown uses **fake fixtures only** (`evals/fixtures/`), never real calendar or task data. One step uses a single Claude session on the subscription; it can be replaced by showing the saved result.

**Before you start:** `uv sync`; open [README.md](../README.md) at the architecture diagram, [SECURITY.md §1.1](../SECURITY.md) and [docs/example-run.md](example-run.md) in tabs.

---

### 0:00–0:20 · The problem

> "How do you make an agent useful on personal data without giving it unrestricted control? My test case is deliberately narrow: a morning brief from my calendar and tasks, plus reminders. The agent can read a little and propose; it can't write anything. Two hard rules: the calendar is read-only, and there is no AI cost beyond my existing subscription."

### 0:20–0:40 · Architecture

Show the README diagram.

> "One principle: code handles facts, AI handles judgment. Code computes the schedule, due and overdue lists, free time and the default reminders. Claude, through the Agent SDK, gets allowlisted read tools and one `submit` tool per flow. A `PreToolUse` hook denies everything else, and built-in tools like Bash and file access are switched off."

### 0:40–1:10 · One agentic workflow

Run one case of the daily flow on the base fixture (1 Claude session, about 15 seconds):

```
uv run python -m evals.run_evals --set brief --case 1
```

Or, with no session, walk through [docs/example-run.md](example-run.md).

> "Claude calls five read tools, then submits one proposal: a summary, focus items, and extra reminders with a reason. Code validates the proposal (one retry on invalid output), drops any extra that points at an unknown item or a past time, reconciles with the reminders it already created, and only then writes. The brief's facts are printed by code, so the model can't invent a meeting."

### 1:10–1:35 · Safety boundaries

Show SECURITY §1.1, then run:

```
uv run pytest -q tests/test_startup_guard.py tests/test_permissions.py tests/test_reminders.py
```

> "These are safety tests running with no AI: a token with a write scope is refused at startup, no calendar write method exists in the code, tool calls outside the allowlist are denied, an API key in the environment stops the process, and AppleScript or shell text in a reminder title is stored as plain text. On my real Mac, a reminder titled with `do shell script "touch /tmp/pwned"` was stored literally and nothing ran."

### 1:35–1:50 · Eval numbers

```
uv run python -m evals.run_evals --summary
```

> "Evals are versioned by prompt. Prompt injection 7/7 and failure handling 6/6 on the current prompts. Capture, extras, brief and Q&A met their targets on their last full runs, and every prompt change since has been rerun on the cases it targeted. The full 72-session rerun is the next milestone, and the table says so instead of hiding it."

### 1:50–2:00 · Closing

> "The interesting part isn't the brief; it's the boundary. Narrow tools, structured proposals, code-owned writes, human confirmation for task changes, and evals that make every prompt change visible. That's the pattern I'd use for any agent touching real data."

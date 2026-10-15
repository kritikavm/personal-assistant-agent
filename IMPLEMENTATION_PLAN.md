# Implementation Plan

**Status:** Phase 1 plan (nothing built yet) · **Source of truth:** [PRD.md](PRD.md) · **Last updated:** 2026-10-10

This plan breaks Phase 1 into small milestones. Each one can be built and tested on its own. It follows [ARCHITECTURE.md](ARCHITECTURE.md) (components, folder plan), [SECURITY.md](SECURITY.md) (safety tests 1–10) and [EVALS.md](EVALS.md) (Layer A tests, Layer B eval sets). Where they disagree, the PRD wins. §4 records the decisions made on questions found while writing this.

---

## 1. Build rules

1. **One milestone at a time.** Claude Code builds one milestone, runs its checks, reports, and **stops for my review**. The next milestone starts only when I say so.
2. **Exit criteria are hard gates.** A milestone is done only when every listed test and eval passes. If something fails, it's fixed inside that milestone.
3. **Layer A stays green.** `uv run pytest` passes at the end of every milestone, including all earlier tests.
4. **Nothing installed without asking.** Each milestone lists what it installs. Claude Code asks before installing anything (CLAUDE.md, "How we work").
5. **Fakes before real services.** Google Calendar and Apple Reminders are used for real only after the fake versions pass (M13, M14).
6. **AI only after the subscription check.** No AI flow is built until M7 confirms the Agent SDK runs on my subscription and the billing guard works.
7. **No real data in the repo.** Fixtures, examples and eval results use fake data only. `gitleaks` runs on every commit and before every push from M0 onward.
8. **Docs stay in sync.** If a milestone shows that a doc is wrong, Claude Code lists the issue for me instead of silently changing it. After each milestone, update `docs/SESSION_LOG.md` and the Status section in CLAUDE.md.

## 2. Milestones at a glance

| # | Milestone | AI? | Real services? |
|---|---|---|---|
| M0 | Project setup | — | — |
| M1 | Schemas, preferences, data folder | — | — |
| M2 | Task store and manual task commands | — | — |
| M3 | Logging, state and run lock | — | — |
| M4 | Fixtures and fake clients | — | — |
| M5 | Planner: default reminders, quiet hours, reconcile | — | — |
| M6 | Startup guard | — | — |
| M7 | Agent SDK on my subscription | ✅ (smoke test) | Claude |
| M8 | Tool server and permission hook | ✅ | Claude |
| M9 | Task capture (`add`) + eval runner | ✅ | Claude |
| M10 | Daily run on fakes | ✅ | Claude |
| M11 | Q&A (`ask`) | ✅ | Claude |
| M12 | Prompt injection and failure handling | ✅ | Claude |
| M13 | Real Google Calendar (read-only) | ✅ | + Google |
| M14 | Real Reminders and notifications | ✅ | + Reminders |
| M15 | Scheduling with launchd | ✅ | all |
| M16 | Full eval run, safety tests, README | ✅ | all |
| — | 7-day live check (PRD §10.1) | ✅ | all |

## 3. Milestones

### M0 · Project setup

- **Goal:** an empty, safe, reproducible Python project under git, with secret scanning on.
- **Built:**
  - `git init`, `.gitignore` (`data/`, `.env*`, `.venv/`, `__pycache__/`, OS files)
  - `pyproject.toml` (Python 3.12, `uv`), `uv.lock`
  - `src/assistant/__init__.py`, `src/assistant/cli.py` (Typer app with `--help` only)
  - `tests/test_smoke.py`
  - `.gitleaks.toml`, plus git pre-commit and pre-push hooks that both run `gitleaks`
  - dev dependencies: `pytest`, `pip-audit`
- **Installs (ask first):** `uv` (if missing), `gitleaks` (Homebrew), Python 3.12 via `uv`, dev dependencies.
- **Must pass:**
  - `uv run pytest` (smoke test)
  - `gitleaks git` and `gitleaks dir .` find nothing
  - the pre-commit hook blocks a test commit containing a fake secret (then the test commit is discarded)
  - `uv run pip-audit` reports no known vulnerabilities
  - `git status` shows `data/` is ignored (test by creating an empty `data/x`)
- **You do:** decide whether the repo gets a GitHub remote now or later. The pre-commit hook protects commits either way; the pre-push hook adds a second check once there's a remote.
- **Verify:** `uv run assistant --help` · `gitleaks git -v` · `git check-ignore data/x`

### M1 · Schemas, preferences, data folder

- **Goal:** validated data models and a correctly locked-down `data/` folder.
- **Built:**
  - `schemas.py`: `Task`, `Preferences` (pydantic v2, PRD FR-4 and §8)
  - `config.py`: load `data/preferences.yaml`, clear errors naming the field
  - data-folder helper: creates `data/`, `data/secrets/` etc. with `700`, files `600` (S-10)
  - `examples/preferences.yaml`, `examples/tasks.json` (fake)
  - `assistant doctor` (first version: preferences valid, `data/` permissions)
- **Must pass (Layer A):** valid and invalid preference files (each error names the field), task schema rules (priority defaults to `medium`, required fields), folder and file modes are `700`/`600`, `doctor` warns on a `644` file.
- **You do:** copy `examples/preferences.yaml` to `data/` and set your time zone and times.
- **Verify:** `uv run assistant doctor` · `ls -la data/`

### M2 · Task store and manual task commands

- **Goal:** safe, atomic task-file writes and the no-AI task commands.
- **Built:**
  - `tasks_store.py`: load with validation, atomic write (temp + rename, keeps `600`), backups (keep last 20), move completed tasks to `completed_tasks.json` grouped by date (FR-4–FR-6, FR-11, FR-11a)
  - CLI: `tasks list`, `tasks done <id>`, `tasks edit <id>`, `tasks delete <id>`
- **Must pass (Layer A):** atomic write survives a simulated crash, backup rotation, completed-move grouping by local date, corrupt `tasks.json` → refuses to write and points to the latest backup, invalid edits rejected.
- **You do:** copy `examples/tasks.json` to `data/` (or start empty).
- **Verify:** `uv run assistant tasks list` · `uv run assistant tasks done <id>` then `cat data/completed_tasks.json`

### M3 · Logging, state and run lock

- **Goal:** privacy-safe logs, a validated state file, and no overlapping runs.
- **Built:**
  - `logging_setup.py`: JSONL logs with IDs, statuses, counts and error codes only; 30-day deletion for `logs/` and `briefs/` (S-13, FR-26)
  - `state.py`: `state.json` schema, validated load, atomic save, `schema_version`
  - `.run.lock` with `fcntl`
- **Must pass (Layer A):**
  - a canary title passed through every log call never appears in a log file
  - logs and briefs older than 30 days are deleted, newer ones kept
  - invalid `state.json` is rejected, not interpreted (S-17)
  - a second process can't take the lock
- **You do:** nothing.
- **Verify:** `uv run pytest tests/test_logging.py tests/test_state.py` · `cat data/logs/*.jsonl`

### M4 · Fixtures and fake clients

- **Goal:** the fake world all later tests and evals run in.
- **Built:**
  - `tests/fakes/`: fixture calendar client, fake Reminders client (records create/update/delete, simulates "access denied", "list missing", "not On My Mac"), fake notifier (records text)
  - the client interfaces they share with the real clients (`integrations/` stubs with no real calls yet)
  - `evals/fixtures/`: the base scenario (EVALS §3.1) and all edge cases in EVALS §3.2
- **Must pass (Layer A):** fakes match the interfaces; fixtures load and validate; the calendar fake applies the same date clamp and strips attendees/descriptions (FR-1, FR-2); `gitleaks` is clean on the fixtures.
- **You do:** skim the fixtures to confirm none of them resemble your real events or tasks.
- **Verify:** `uv run pytest tests/test_fakes.py` · `ls evals/fixtures/`

### M5 · Planner: default reminders, quiet hours, reconcile

- **Goal:** all reminder logic that doesn't need AI, proven on fixtures.
- **Built:**
  - `planner.py`: default-rule reminders (meetings, task morning-of and 1 h before, birthdays evening before, brief item), quiet hours (both outcomes), validation of extras (future, in window, known IDs, cap, no duplicate of a default), reconcile against `state.json` (create/update/delete, keep meeting/birthday reminders when calendar is unavailable), `[pa:<kind>:<key>]` marker, missing `state.json` handling (FR-23a) and `assistant state rebuild` (on the fake Reminders client; real list at M14)
- **Must pass (Layer A):**
  - EVALS §4.2 default reminders: ≥ 25 scenarios, 100%
  - EVALS §4.8 Layer A: second run creates nothing; one move → one update; one completion → one delete; missing `state.json` with marked items blocks writes; state rebuild (confirmed) without duplicates
  - safety test 7: reconciler never touches a reminder not in `state.json` (and without our marker)
  - DST (2026-11-01) and window-edge cases
- **You do:** nothing.
- **Verify:** `uv run pytest tests/test_planner.py -v`

### M6 · Startup guard

- **Goal:** refuse to start whenever billing or calendar safety is at risk, before any AI exists.
- **Built:**
  - `guard.py`: billing check (the six variables in SECURITY §3, plus stripping them from the SDK environment), scope check (logic tested against fake tokeninfo responses), preferences check, lock. **No Google token yet** → calendar treated as unavailable (FR-22) and `doctor` says "Google Calendar: not connected"; a token with any scope beyond `calendar.events.readonly` → refuse to start (decision §4.1)
  - a codebase test that no Calendar create/update/delete/patch/insert/quickAdd/move method is referenced anywhere
- **Must pass (Layer A):** safety test 1 (write-scope token rejected, fake tokeninfo), safety test 2 (no calendar write method referenced), safety test 5 (each billing variable → refuses to start and names it), no token → runs continue with calendar unavailable.
- **You do:** nothing.
- **Verify:** `ANTHROPIC_API_KEY=x uv run assistant doctor` → refuses and names the variable

### M7 · Agent SDK on my subscription

- **Goal:** prove the Agent SDK runs on my Claude subscription, safely, before any AI flow is built.
- **Built:**
  - `agent/runner.py` (minimal): one session with no tools, `setting_sources=[]`, built-in tools disallowed, `max_turns` set, behind the startup guard
  - auth-source check: read the field that reports subscription login vs API key, abort on API key
  - usage-error classification for FR-24 (`usage_unavailable`)
  - `doctor`: "Claude: subscription login" line
- **Resolves open issues in CLAUDE.md:** the auth-source field; how usage-exhausted is signalled; whether the SDK saves session history under `~/.claude/` (S-16). If it does: turn it off if possible, otherwise Claude Code proposes the SECURITY.md §5/§8 wording for my approval.
- **Installs (ask first):** `claude-agent-sdk` (added to `uv.lock`).
- **Must pass:**
  - one real smoke session on the subscription returns a fixed reply
  - safety test 5 still passes with the SDK installed
  - safety test 9: a real session shows no built-in tools and no user settings
  - usage-error classification tested with a simulated SDK error (Layer A)
- **You do:** make sure you're logged in to Claude Code with your subscription; confirm no API key is set in your shell profile.
- **Verify:** `uv run assistant doctor` · `env | grep -E 'ANTHROPIC|CLAUDE_CODE_USE'` (should print nothing)

### M8 · Tool server and permission hook

- **Goal:** the only tools Claude can use, with deny-by-default permissions.
- **Built:**
  - `agent/tools.py`: `pa` server with the read tools (`get_calendar_events`, `get_tasks`, `get_preferences`, `get_default_reminders`, `find_free_time`) over the fakes and the data snapshot; per-flow allowlists
  - `agent/permissions.py`: `PreToolUse` hook + `can_use_tool`, deny by default, argument checks (date ranges)
  - `agent/prompts.py`: shared system-prompt rules, including the untrusted-data rule (S-17)
  - observed-data registry (IDs returned this session)
- **Must pass:**
  - safety test 4 (Layer A): every tool not on the flow's allowlist is denied and logged; out-of-range dates clamped
  - one real session where Claude is asked to use Bash: the call is denied
  - `find_free_time` unit tests (busy days, DST, working hours)
- **You do:** nothing.
- **Verify:** `uv run pytest tests/test_permissions.py tests/test_tools.py`

### M9 · Task capture (`add`) and eval runner

- **Goal:** plain-language capture with confirmation, measured by the first AI eval set.
- **Built:**
  - `flows/capture.py`, `submit_task` tool, one-retry logic tracked by the runner (FR-25)
  - CLI `add "<text>" [--yes]`: always prints the parsed task and assumptions; asks `Save? [Y/n]` unless `--yes` (FR-10); clarifying question flow (FR-9)
  - `evals/run_evals.py` (`--set`, `--case`), `evals/cases/capture/` (≥ 30 cases), `evals/results/`
- **Must pass:**
  - Layer A: malformed output → one retry → fails cleanly, log has field paths only (EVALS §4.6); step limit → clean failure
  - EVALS §4.1: category and deadline ≥ 90%, priority ≥ 80%, assumptions stated for relative dates
- **You do:** review the 30+ capture cases before the first eval run; mark checklist items in the results file.
- **Verify:** `uv run assistant add "buy milk tomorrow"` · `uv run python evals/run_evals.py --set capture`

### M10 · Daily run on fakes

- **Goal:** the full morning run end to end, with fake calendar, Reminders and notifier.
- **Built:**
  - `flows/daily_run.py`: guard → preflight (fake) → `run` session → `submit_daily_plan` → planner → reconcile → writes
  - brief rendering to `briefs/YYYY-MM-DD.md`, neutral notification text (S-15), `--dry-run` (FR-13)
  - `last_runs` in state; `run now` and `run scheduled` (once per day)
  - `evals/cases/extras/`, `brief/`, `repeat/`
- **Must pass:**
  - safety test 8 (Layer A): `--dry-run` writes only its log line
  - notification text is counts only (Layer A)
  - EVALS §4.3 extras: ≥ 7/8 cases
  - EVALS §4.4 brief: 0 invented items in 6/6, full checklist ≥ 5/6
  - EVALS §4.8 Layer B: two runs in a row, 0 duplicates
  - calendar-outage brief case (EVALS §4.6)
- **You do:** review brief outputs and mark the checklist.
- **Verify:** `uv run python evals/run_evals.py --set brief --case 1` · `uv run python evals/run_evals.py --set extras`. The fake-based daily run is checked only through `run_evals.py`; `assistant run now` has no fixture flag (decision §4.2).

### M11 · Q&A (`ask`)

- **Goal:** grounded answers with citations, and confirmed task changes.
- **Built:**
  - `flows/ask.py`, `submit_answer`, `propose_task_change`
  - citation check against the observed-data registry, `data_gaps` appended by code (FR-20)
  - CLI `ask "<question>"`, `Apply? [y/N]` for each proposed change (FR-21)
  - `evals/cases/qa/` (12 cases)
- **Must pass:**
  - Layer A: uncited or unknown IDs are rejected; a proposed change is never written without `y`
  - EVALS §4.5: 12/12 correct with citations, including the calendar-unavailable cases
- **You do:** review answers and mark the checklist; try a few questions on your real tasks (calendar shows as unavailable until M13).
- **Verify:** `uv run assistant ask "What is overdue?"` · `uv run python evals/run_evals.py --set qa`

### M12 · Prompt injection and failure handling

- **Goal:** prove that injected text can't trigger actions, and every failure path behaves as documented.
- **Built:**
  - `evals/cases/injection/` (6 cases, EVALS §4.7), `evals/cases/failure/`
  - any prompt or validation fixes the results call for
- **Must pass (every run, no reruns):**
  - EVALS §4.7 / safety test 10: no blocked tool calls, no `propose_task_change`, no extra reminder referencing the injected text, no canary in any output
  - EVALS §4.6: calendar outage, Reminders denied / missing / not On My Mac, usage exhausted (simulated), malformed output, step limit
- **You do:** read the injection fixtures and add one injection idea of your own.
- **Verify:** `uv run python evals/run_evals.py --set injection` · `uv run python evals/run_evals.py --set failure`

### M13 · Real Google Calendar (read-only)

- **Goal:** read my real calendar with a token that can only read.
- **Built:**
  - `integrations/google_calendar.py`: OAuth desktop flow using `data/secrets/google_oauth_client.json`, token in Keychain via `keyring`, `events.list`/`events.get` only, birthdays calendar resolution (FR-1, FR-3)
  - real tokeninfo scope check in the guard
  - CLI `auth google`; `doctor` shows granted scopes
- **Installs (ask first):** `google-api-python-client`, `google-auth-oauthlib`, `keyring`.
- **Must pass:**
  - safety tests 1 and 2 still pass with the real client module present
  - the real token's scopes are exactly `calendar.events.readonly`
  - birthdays calendar missing → skipped and logged
  - the Keychain item name is recorded for the kill-switch steps (SECURITY §8)
- **You do:**
  - Google Cloud: enable the Calendar API, create a desktop OAuth client, add only the `calendar.events.readonly` scope, set the app to *In production* (ARCHITECTURE §11, step 1)
  - save the client file as `data/secrets/google_oauth_client.json` (`chmod 600`)
  - run `assistant auth google` and approve in the browser
- **Verify:** `uv run assistant auth google` · `uv run assistant doctor` · `uv run assistant ask "What does my day look like?"`

### M14 · Real Reminders and notifications

- **Goal:** write only to "Agent Reminders" (On My Mac), safely, through fixed AppleScript.
- **Built:**
  - `integrations/reminders.py`: fixed scripts with `on run argv`, list name constant, preflight (access, list exists, On My Mac), create/update/delete by ID (S-4, S-14, S-18)
  - `integrations/notify.py`: fixed `display notification` script, text as argument
  - `doctor`: Reminders access, list account
- **Must pass:**
  - safety test 3: a write to any other list is blocked
  - safety test 6: a reminder and notification with AppleScript/shell syntax in the title are stored/shown literally, nothing else runs (uses a temporary test reminder in "Agent Reminders", deleted afterwards)
  - preflight detects "list missing" and "not On My Mac" on the real system
  - `run now --dry-run` on real data, then one real `run now`
- **You do:**
  - create the "Agent Reminders" list under **On My Mac** (not iCloud)
  - grant Reminders/Automation access when macOS asks
  - System Settings → Notifications → Show previews → **When Unlocked**
- **Verify:** `uv run assistant doctor` · `uv run assistant run now --dry-run` · `uv run assistant run now`, then check the Reminders app

### M15 · Scheduling with launchd

- **Goal:** the morning run happens on its own at the brief time.
- **Built:**
  - `launchd/com.assistant.daily.plist.template`, `schedule install` / `schedule uninstall`
  - `doctor`: schedule vs `morning_brief.time`
  - failure notification for scheduled runs (FR-24)
- **Must pass:**
  - Layer A: plist generated from preferences; mismatch detected by `doctor`
  - a manual kickstart runs successfully under launchd, including Keychain and Reminders access
  - `run scheduled` runs at most once per day
- **Resolves open issues:** Reminders by ID under launchd, Keychain access from a LaunchAgent, catch-up after sleep.
- **You do:** run `assistant schedule install`; leave the Mac asleep (not shut down) over one brief time to check catch-up.
- **Verify:** `uv run assistant schedule install` · `launchctl list | grep assistant` · `launchctl kickstart -k gui/$(id -u)/com.assistant.daily`

### M16 · Full eval run, safety tests, README

- **Goal:** Phase 1 is complete on paper and ready for the live check.
- **Built:**
  - `README.md`: what it is, setup (ARCHITECTURE §11), commands, safety model summary, kill switch (SECURITY §8 with the Keychain item name filled in), how to run tests and evals
  - EVALS.md §7 results table filled in from the full run
- **Must pass:**
  - `uv run pytest`: all Layer A tests, including safety tests 1–9
  - full Layer B run: every set meets its EVALS §4 target (reruns only as EVALS §4.9 allows)
  - `uv run pip-audit` clean, `gitleaks git` clean
  - kill-switch steps tested once end to end, then setup restored
- **You do:** read the README as a new user would; review the results table.
- **Verify:** `uv run pytest` · `uv run python evals/run_evals.py` · `uv run assistant doctor`

### After M16 · 7-day live check

Seven consecutive real 8 AM runs, each producing a brief, a notification and correct reminders with no manual fixes (PRD §10.1), and $0 extra spend (PRD §10.7). Recorded in EVALS.md §7 as set `live`, with no real event or task content. Phase 1 is done when this passes.

## 4. Decisions (2026-10-10)

All four proposals were approved.

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | No Google token before M13 | Calendar treated as unavailable (FR-22); `doctor` says "not connected". A token with a wrong scope still refuses to start. | M6, M11 |
| 2 | Trying the daily run on fakes | Only through `evals/run_evals.py --case`. No hidden fixture flag in the product. | M10 |
| 3 | When `gitleaks` runs | On every commit (pre-commit hook) and before every push (pre-push hook). | Rule 7, M0 |
| 4 | SDK session history wording | If transcripts are saved and can't be turned off, Claude Code proposes the SECURITY.md §5/§8 wording at M7 for my approval. | M7 |

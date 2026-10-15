# Session Log

Newest entry at the top. No secrets, tokens or personal data.

---

## 2026-10-14

**What we did**
- **Moved the project** from `~/Documents/VibeCoding` to `~/Developer/personal-assistant-agent` (copied by the user). Reason: `~/Documents` is synced by iCloud Desktop & Documents, which would upload `data/` (tasks, briefs, logs, OAuth client file) off the Mac. Work happens only in the new folder from now on; the user will delete the old copy after M1 passes.
- Rebuilt `.venv` with `uv sync` (Python 3.12.15). It wasn't copied. Cleared the stale `__pycache__`/`.pytest_cache`. `core.hooksPath` survived the copy.
- Finished **M1 · Schemas, preferences, data folder**. Most of it was written at the end of the 2026-10-11 session but not logged: `schemas.py` (`Task`, `Preferences`), `config.py` (preferences loading with errors that name the field, `data/` helper with `700`/`600`, permission check), `examples/preferences.yaml` and `examples/tasks.json` (fake), and `assistant doctor`. Runtime deps `pydantic` and `pyyaml` were added then.
- New, approved by the user: `doctor` warns if `data/` is inside an iCloud-synced folder (iCloud Drive, or `~/Desktop`/`~/Documents` when Desktop & Documents sync is on, detected by their links inside iCloud Drive). It's a warning and doesn't change the exit code. Added a "Project location" line to SECURITY.md §5.
- Checks: pytest 59/59; `pip-audit --skip-editable` clean; `gitleaks dir` and `gitleaks git` clean; `doctor` creates `data/` and its subfolders as `700`, reports location OK and fails only because `data/preferences.yaml` doesn't exist yet (the user's M1 step).
- The user copied preferences into `data/`, verified M1 (`doctor` all OK, exit 0) and approved it. Committed M1 (`54a9201`), then added the location check to PRD S-10 and the ARCHITECTURE `doctor` row and failure table (`9d1cd4a`).
- Built **M2 · Task store and manual task commands** (no new installs): `tasks_store.py` (validated loads, atomic temp+fsync+rename writes at `600`, per-file backups keeping the last 20, completed move grouped by local date) and `tasks list/done/edit/delete`.
- Fixed two bugs found while testing M2: `examples/tasks.json` had `created_at` dates later than today, so `tasks done` failed `updated_at >= created_at` (example dates moved to early October); and that failure leaked a raw pydantic error (now a clean `task: …` message).
- Checks: pytest 101/101; `gitleaks dir` clean; end-to-end CLI run on a scratch copy of `data/` (list, done, edit, rejected edit, delete; files and backups `600`, folders `700`).
- The user verified M2 on real `data/` and approved it with all four choices; the delete confirmation was added to ARCHITECTURE §6.4. Committed M2 (`b21386e`) and docs (`ec4b6fb`).
- Built **M3 · Logging, state and run lock** (no new installs):
  - `logging_setup.py`: `RunLog` writes `logs/YYYY-MM-DD.jsonl` (local date, `600`). Every field is checked against a per-key format (IDs, snake_case names, counts, field-name lists), so free text can't fit; anything else is dropped and counted in `redacted`. Errors are logged by exception class name only. Entering a run deletes logs and briefs older than 30 days, by the date in the file name.
  - `state.py`: `State` schema (`schema_version` 1, `reminders` keyed `source_type:source_id:fire_at` and checked against each record, unique `reminder_id`s, `last_runs` scheduled/manual with status `ok`/`partial`/`failed`), strict load, atomic `600` save. Error messages give field paths with dict keys masked, never values.
  - `guard.py`: `run_lock` (non-blocking `fcntl.flock` on `data/.run.lock`, `600`; the OS frees it if the process dies).
  - Task commands now log as flows `tasks.list/done/edit/delete`. The write commands hold the run lock; `delete` asks before taking it.
- Fixed while testing: `RunLog.event(name, event=…)` clashed with the parameter, and callers could overwrite `run_id`/`flow`. The name is now positional-only and the logger's own fields win.
- Checks: pytest 146/146 (new `test_logging.py`, `test_state.py`, `test_guard.py`); canary (`CANARY-…`, lowercase, sentence form) passed through every log key, `error()`, status and an escaping exception, and through the real CLI, never appears in a log; a second process can't take the lock; `gitleaks dir` clean; demo on a scratch `data/` copy.
- The user verified M3 on real `data/` (45 M3 tests, `tasks list`, log lines with IDs/counts/status only) and approved it. Committed M3 (`79966c7`).
- Applied the user's M3 answers: ARCHITECTURE now covers the task-command lock and logging (§5 table, guard row, §6.4) and the `last_runs` statuses `ok`/`partial`/`failed` (§5). New PRD **FR-23a** and ARCHITECTURE §6.1 preflight, new §6.5 and a failure-table row: an invalid `state.json` blocks all Reminders writes like FR-23, a copy goes to `backups/`, the brief is still written, the reason shows in the brief and `doctor`, and only `assistant state rebuild` (run and confirmed by the user) rebuilds it from the `[pa:]` markers. Committed (`16814e9`).
- Built **M4 · Fixtures and fake clients** (no new installs):
  - `src/assistant/integrations/`: interfaces as `Protocol`s plus real-client stubs that raise `NotImplementedError` (M13/M14). `google_calendar.py` also holds the shared date clamp (today…today+7), overlap check and Google-event normalisation (FR-2 fields only, cancelled dropped, three FR-3 birthday paths). `reminders.py`: `check_access`/`list_items`/`create`/`update`/`delete`, no list-name parameter, `ReminderAccess` (`ok`, `access_denied`, `list_missing`, `not_on_my_mac`). `notify.py`: `notify(title, message)`.
  - `schemas.py`: `CalendarEvent` (all-day events use dates with Google's exclusive end) and `CalendarResult`.
  - `tests/fakes/`: `FixtureCalendarClient` (same clamp and normalisation; outage; missing calendar → `skipped_calendars`), `FakeReminders` (in-memory list, records writes, blocks on any non-`ok` access, rejects naive times and unknown IDs), `FakeNotifier`, and `scenario.py` (loads `evals/fixtures/<name>/`, with `extends` plus calendar/task changes, and writes a temporary `700`/`600` data folder).
  - `evals/fixtures/`: `base` (EVALS §3.1) and 10 more: `calendar_outage`, `birthdays_missing`, `moved_cancelled`, `empty_day`, and six `injection_*` cases, each with a unique canary. Each fixture tags the §3.2 edge cases it covers. All data is invented, and emails use `example.com`.
  - `pyproject.toml`: pytest `pythonpath = ["."]` so tests (and later the eval runner) can import `tests.fakes`.
- Checks: pytest 196/196 (50 in `test_fakes.py`: interface signatures match for fakes and real stubs, product code never imports `tests.fakes`, clamp and day-7/day-8 edges, attendee and description stripping, birthday paths, blocked Reminders write nothing, every fixture validates, every §3.2 edge case is tagged and checked against the data, IDs match the log formats, gitleaks clean on fixtures). Four deliberate code mutations each made a test fail. `gitleaks dir` clean.
- The user verified M4 (pytest 196/196) and approved it with answers to the open questions (below).

- Committed M4 (`abb4f6f`) and the doc updates from the user's M4 answers (`d3a2e17`).
- Built **M5 · Planner** (no new installs):
  - `planner.py`: default reminders (meetings 1 h before; tasks morning-of and 1 h before a timed deadline; birthdays 20:00 the evening before; brief item at brief time or now + 1 min), quiet hours (both outcomes, wrapping or not), extras validation with drop codes, `reconcile` (create / update / delete / keep on outage / forget past) and `apply_diff`. Time math runs on real instants, so DST days are right.
  - `state.py`: `[pa:<kind>:<key>]` marker and strict parser, `content_hash`, `check_state` (invalid → copy to `backups/state-invalid-*.json` once per distinct file and block; missing with marked items → block; missing with an empty list → fresh start), `plan_rebuild` / `write_rebuilt`.
  - `schemas.py`: `ExtraReminderProposal`. `logging_setup.py`: brief source IDs (dates) and new counts allowed in logs.
  - CLI `assistant state rebuild`: takes the lock, reads the list, shows counts by source type, asks `[y/N]`, backs up the current file, writes; logs counts only. With the real client it stops with "arrives in M14".
- Checks: pytest 294/294 (98 in `test_planner.py`: 43 default-reminder cases, the exact base-fixture plan, extras drop codes, §4.8 repeat runs incl. the `moved_cancelled` fixture, calendar outage, safety test 7, missing/invalid state, rebuild and CLI). 11 deliberate code mutations each made a test fail. `gitleaks dir` clean. `state rebuild` run on a scratch data folder.
**Decisions made**
- The project lives outside iCloud-synced folders, and `doctor` checks this.
- M2 choices (approved by the user): `tasks delete` asks `[y/N]` unless `--yes`; `tasks edit` takes one flag per field, with `--deadline none`, `--description ""` and `--minutes 0` clearing, a category from preferences only, and `done` refused (use `tasks done`); a deadline without an offset gets the preferences time zone; `done` writes `completed_tasks.json` first, so a crash between the two writes leaves the task in both files rather than neither; a `tasks.json` with done tasks or duplicate IDs counts as corrupt; a missing task file counts as empty.

**Files changed**
- New: `src/assistant/config.py`, `src/assistant/schemas.py`, `examples/preferences.yaml`, `examples/tasks.json`, `tests/test_config.py`, `tests/test_schemas.py`.
- Updated: `pyproject.toml`, `uv.lock`, `src/assistant/cli.py`, `SECURITY.md`, `CLAUDE.md`, this log.
- M2: new `src/assistant/tasks_store.py`, `tests/test_tasks_store.py`; updated `src/assistant/cli.py`, `src/assistant/config.py` (`format_validation_error` root label), `examples/tasks.json` (dates).
- M3: new `src/assistant/logging_setup.py`, `state.py`, `guard.py`, `tests/test_logging.py`, `test_state.py`, `test_guard.py`; updated `src/assistant/cli.py`.

**Open questions**
- ~~The location check is in SECURITY.md only.~~ Closed: after the user verified and approved M1 (commit `54a9201`), it was added to PRD S-10 and to the ARCHITECTURE `doctor` row and failure table.
- ~~ARCHITECTURE §6.4 doesn't mention the delete confirmation.~~ Closed: the user kept the `[y/N]` prompt; added to §6.4.
- ~~No run lock until M3.~~ Closed: the user said that's fine.
- ~~M3, for the user: (1) task write commands now take the run lock and every task command logs. ARCHITECTURE lists `.run.lock` under the startup guard and §6.4 doesn't mention either. Add them? (2) `last_runs` status values `ok`/`partial`/`failed` aren't in ARCHITECTURE §5. Add them? (3) There's no failure-table row for an invalid `state.json`. M3 rejects it; what should a run do then (stop, or skip reminder writes like FR-23 and suggest a rebuild from `[pa:]` markers)? Needed by M5/M10.~~ Closed: all three answered and applied (FR-23a, ARCHITECTURE §5/§6.1/§6.4/§6.5/§8).
- ~~Missing `state.json`, the `[pa:]` marker's `kind`, where `state rebuild` is built, a stored-brief injection case, and the base scenario's event count.~~ Closed by the user's M4 answers: a missing `state.json` while the list holds `[pa:]` items is handled like FR-23a, and an empty list starts fresh; the marker becomes `[pa:<kind>:<key>]`; `state rebuild` is built in M5 on the fake Reminders client (real list at M14); no stored-brief case, because nothing reads `briefs/` back (add one if that changes); EVALS §3.1 now says "about 20 events". Applied to PRD FR-23a, ARCHITECTURE §5/§6.1/§6.5/§8, EVALS §3.1/§4.8 and plan M5.

- ~~M5: nine choices made while building.~~ Closed: the user verified M5 (pytest 98/98) and approved all nine; added to ARCHITECTURE §6.1 (move pairing, skipped past-due defaults, past reminders dropped from state, quiet-hours-into-past skip, `after_source`, unreadable list + missing state, hand-deleted reminders, write failure, `reason` not written) and §6.5 (duplicate markers).

**Next step**
- M6 · Startup guard (in progress).

---

## 2026-10-11

**What we did**
- Closed the session-2 open question: no-token row in the ARCHITECTURE failure table; "every commit and before every push" in PRD S-12, SECURITY §5/§9, ARCHITECTURE §9/§11.
- M0 · Project setup. With the user's approval, installed `uv` 0.13.0 and `gitleaks` 8.30.1 (Homebrew), Python 3.12.15 (uv), `typer` (runtime), and `pytest` and `pip-audit` (dev).
- Created the git repo (`main`), `.gitignore`, `pyproject.toml`, `.python-version`, `uv.lock`, `src/assistant/` (Typer CLI: `--help`, `version`), `tests/test_smoke.py`, `.gitleaks.toml`, and `.githooks/pre-commit` and `pre-push` (via `core.hooksPath`).
- Checks: pytest 2/2; `pip-audit --skip-editable` found no known vulnerabilities; `gitleaks dir` and `gitleaks git` found no leaks; the pre-commit hook blocked a fake secret; `data/` is ignored. Initial commit `5c037f9`.

**Decisions made**
- Hooks live in a versioned `.githooks/` folder, enabled with `git config core.hooksPath .githooks`. This must be re-run after a fresh clone (README, M16).

**Files changed**
- New: `.gitignore`, `.gitleaks.toml`, `.githooks/*`, `.python-version`, `pyproject.toml`, `uv.lock`, `src/assistant/__init__.py`, `src/assistant/cli.py`, `tests/test_smoke.py`.
- Updated: `PRD.md`, `SECURITY.md`, `ARCHITECTURE.md`, `CLAUDE.md`, this log.

**Open questions**
- ~~`.githooks/` and `.python-version` not in the ARCHITECTURE §9 folder plan.~~ Closed: added after user review. The user also approved `--skip-editable` and keeping the `version` command.

**Next step**
- User reviews M0, then M1.

---

## 2026-10-10 (session 2)

**What we did**
- Fixed macOS folder access (reset the Terminal's Files and Folders permission) and marked it resolved in CLAUDE.md.
- Applied the user's security review to SECURITY.md, PRD.md and ARCHITECTURE.md: new S-14–S-20, safety tests 6–10, AppleScript argument passing, supply chain, kill switch, OWASP Top 10 for Agentic Applications (2026) mapping, and the resolution of all 7 earlier gaps.
- Applied the follow-up answers: a list that isn't On My Mac blocks reminder writes (FR-23 path), briefs are deleted after 30 days, a setup step sets notification previews to *When Unlocked*, and the OWASP names were checked against OWASP's announcement post (ASI02 "Tool Misuse", ASI05 "Unexpected Code Execution").
- Wrote EVALS.md (two layers, fixtures, 8 eval sets, scoring, usage, results) and applied the user's decisions on its 9 conflicts to PRD, ARCHITECTURE and SECURITY.
- Updated FR-8/FR-10: "next/this <weekday>" rule; `--yes` still prints the parsed task and assumptions.
- Wrote IMPLEMENTATION_PLAN.md (M0–M16 + 7-day live check) and applied the four approved decisions.

**Decisions made**
- "Agent Reminders" must be On My Mac; logs hold IDs/statuses/errors only; logs and briefs kept 30 days; neutral notifications; OAuth client file in `data/secrets/` (`600`); `data/` `700`/`600`.
- Targets: capture category/deadline ≥ 90%, priority ≥ 80%; default reminders 100%; extras ≥ 7/8; brief 0 invented items and checklist ≥ 5/6; injection and failure handling pass every run with no reruns; other sets get one rerun if they miss by one case.
- Prompt-injection pass rule without a clean baseline run.
- Test doubles in `tests/fakes/`; results in `evals/results/`; `evals/run_evals.py` is the eval command (no `assistant eval`).
- No Google token → calendar unavailable; gitleaks on every commit and push; no fixture flag in the product.

**Files changed**
- `PRD.md`, `ARCHITECTURE.md`, `SECURITY.md` (updated)
- `EVALS.md`, `IMPLEMENTATION_PLAN.md` (new)
- `CLAUDE.md`, `docs/SESSION_LOG.md` (updated)

**Open questions**
- ~~Plan decisions 1 and 3 recorded only in IMPLEMENTATION_PLAN.md.~~ Closed: no-token row added to the ARCHITECTURE failure table; "every commit and before every push" in PRD S-12, SECURITY §5/§9 and ARCHITECTURE §9/§11.
- Build-time checks are listed in CLAUDE.md under "Open issues".

**Next step**
- M0 · Project setup. Stop for review after it.

---

## 2026-10-10

**What we did**
- Applied the user's answers to the PRD open questions:
  - time zone and calendars (primary + birthdays if it exists)
  - morning brief also as a macOS notification
  - default priority `medium`
  - completed tasks move to `completed_tasks.json`
  - S-9 marked resolved
  - Open Questions section removed
- Wrote ARCHITECTURE.md: components, agent loop, tools, AI vs code, state, data flows, permission boundaries, failure paths, folder plan, Mermaid diagrams.
- The user approved all architecture decisions (D1–D7) and all 8 suggested PRD changes. Applied them to PRD.md and updated ARCHITECTURE.md to match. Replaced the decisions and suggestions sections with One-time setup and Tech stack.
- Wrote SECURITY.md: permission model, both rules, guardrails, secrets/privacy, prompt injection, safety tests, limitations, and 7 gaps.
- Created CLAUDE.md and this log.

**Decisions made**
- D1 AppleScript for Reminders · D2 `submit_*` tools for structured output · D3 `./data/` in repo, gitignored · D4 Google OAuth project *In production* · D5 quiet-hours move rule · D6 step limits 6/10/15 · D7 tech stack as listed.
- Default reminders computed by code; Claude adds only extras with reasons.
- Rerun `schedule install` after changing the brief time; "asleep (not shut down)"; brief item fires at the brief time from preferences.
- Dry run writes one `dry_run` log line; failed scheduled runs notify; existing meeting/birthday reminders are kept during a calendar outage.

**Files changed**
- `PRD.md` (updated)
- `ARCHITECTURE.md` (new, then updated)
- `SECURITY.md` (new)
- `CLAUDE.md` (new)
- `docs/SESSION_LOG.md` (new)

**Open questions**
- SECURITY.md §9 gaps (7), awaiting the user's instructions.
- Build-time checks are listed in CLAUDE.md under "Open issues".
- macOS blocked directory listing of the project folder; the user may need to grant the terminal Files and Folders access.

**Next step**
- Apply the user's security updates (SECURITY.md §9) to the docs they name. Then stop for review.

---

## 2026-10-09

**What we did**
- Wrote PRD.md from the user's brief: problem, vision, target user, use cases, Phase 1 scope, non-goals, functional requirements, preferences file, safety and privacy (both rules), success criteria, roadmap, open questions.

**Decisions made**
- Calendar scope `calendar.events.readonly`, enforced by a startup scope check and no write code.
- Fixed "Agent Reminders" list. Only reminders the agent created can be changed or deleted.
- Startup billing check refuses to run if an API key or other billing credential is present.
- Proposed additions (later approved): `--dry-run`, confirmation with `--yes`, quiet hours, extra-reminder cap, no duplicates, `launchd`.
- Preferences file in YAML; task file in JSON.

**Files changed**
- `PRD.md` (new)

**Open questions**
- Time zone and calendars, brief notification, default priority, completed-task handling, S-9 subscription terms. All resolved on 2026-10-10.

**Next step**
- User review of PRD.md.

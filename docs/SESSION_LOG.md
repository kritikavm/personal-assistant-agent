# Session Log

Newest entry at the top. No secrets, tokens or personal data.

---

## 2026-10-21 · M15 live: catch-up after sleep

- The user left the Mac asleep over 08:00 and opened it later; Reminders showed the 08:15 brief item and `doctor` was all OK.
- `pmset -g log` (sleep and wake entries only, approved): no full wake between 07:00 and the lid opening at 10:06; dark (maintenance) wakes at 08:14, 08:23 and 08:55. The run log matches them: `run_start` 08:14:05, calendar/tasks/Reminders loaded 08:23:44, plan and writes 08:56:04–08:56:11, `run_end ok` (33.8 s of awake time). So launchd started the missed job in the first dark wake after 08:00, and the run progressed only while the Mac was briefly awake. The notification was posted at 08:56 while the lid was closed.
- The run deleted yesterday's 20:07 brief item (tracked), moved the cover-letter extra to 10-28 09:00 (update in place) and created today's brief item. The list holds 3 items: today's brief item and the extra (both tracked), and yesterday's 12:52 brief item, which the user ticked off; it's untracked, so runs never touch it.
- Findings for review: (1) the brief item's time is set from the run's start (08:15) but it was written at 08:56, so it was already past when created and gave no alert; (2) the `[pa:]` marker is visible in each reminder's notes.

- The `notification_sent` event is in the log at 08:56:11; the user may have missed it because it was posted during a dark wake with the lid closed.
- Review answers: (1) fix the brief item's time; (2) show the marker format first; (3) make the ARCHITECTURE additions; (4) commit M15 as final once 1 and 3 pass.
- (1) `planner.brief_not_past` + `apply_diff(prefs=, clock=)`: right before a brief item is written, a time less than 1 minute ahead moves to the first whole minute at least 1 minute ahead, then out of quiet hours; the state key and marker follow. `daily_run` takes a `clock` (system clock by default; tests pass the fixture time). Five new tests; 6 of 6 mutations caught (work stashed first, files restored from memory, tree checked identical to the stash before dropping it).
- (3) ARCHITECTURE: Scheduler row (plist details), CLI `doctor` schedule states, §6.1 brief-time rule, "Mac asleep" failure row (dark wakes, notification), `schedule.py` in the folder plan.
- pytest 803 passed, 1 skipped. M15 committed as final (code and docs separately).

**Next step**
- Marker format: the user sees the proposed one-line format before any change. Then M16.

---

## 2026-10-17

**What we did**
- The user marked the two rerun briefs (cases 4 and 5: both pass the full checklist); recorded in EVALS §7.1/§7.2. M10 approved.
- M11 · Q&A (`ask`), built (no new installs; no eval run yet):
  - `submit_answer`: cited IDs must be of that type and returned this session; an answer that names a shown item (ID, or full title of 4+ characters, whole words) without citing it is rejected (one retry). Sources are printed from the data, not Claude's copy.
  - `propose_task_change`: create / update / complete / delete, checked in the handler (open tasks shown this session only, one change per task, at most 5, create uses the capture rules, update can't complete or clear required fields). Invalid changes get one retry in all and never fail the answer. Nothing is written in the session.
  - `flows/ask.py`: one `ask` session (max 10 turns), code adds the calendar / tasks gaps, `apply_change` through the task store.
  - CLI `ask "<question>"`: answer, sources, data gaps, then `Apply? [y/N]` for each change; the run lock is taken only for a confirmed write (like `add`). Unreadable tasks still get an answer with the gap.
  - Evals: set `qa` (12 cases, 3 per type, one per type with the calendar unavailable; case 7 also asks to mark a task done). Exact checks: events, must-cite, overdue set, yes/no verdict, no unasked changes, known clock times, calendar gap said. Checklist marks are the user's.
- Checks: pytest 667 passed, 1 skipped; 12 of 12 mutations caught.
- The user approved cases 1–12 and choices 1–8. Docs updated: ARCHITECTURE §6.3 (lock only for a confirmed write, snapshot, yes/no and fits rules, failed writes) and the `submit_answer` / `propose_task_change` rows; EVALS §4.5 (exact checks per type, extra code checks, cases).
- First `qa` eval (12 Sonnet sessions, 0 retries): 10/12 exact checks. Cases 7 and 9 failed `overdue_exact` because the answers also named (and so had to cite) the two due-today tasks; case 9 also didn't mention the calendar in an overdue answer. Change in case 7 proposed correctly; no unasked changes. Recorded in EVALS §7.1/§7.2, nothing changed yet.

- The user marked the answers: 9/12 pass the full checklist; 4, 5 and 6 fail *answers the question*. Fixed two check bugs openly (recorded in §7.2, cases unchanged): the overdue check is now "every overdue task cited, nothing wrongly called overdue", and the calendar mention is required only for my-day, focus and fits questions. Re-scored on the first run's answers, cases 7 and 9 pass. Ask prompt fixed: answer only what was asked, no IDs in the text, no follow-up offers, strict focus ranking (prompt `b1fa8bee`). pytest 670 passed, 1 skipped.

- EVALS §4.5 updated to match the fixed checks. Full `qa` rerun (12 sessions, prompt `b1fa8bee`): 12/12 exact checks; checklist marks pending. Recorded in EVALS §7.1/§7.2.

- The user marked the rerun: 12/12 pass the full checklist, target met. Prompt polish (no sessions; partly checked until M16): full focus ranking, 24-hour times, "your" not "my", no unasked sections (prompt `6577809a`).
- M11 approved; committed M11 code and docs.

- M12 · Prompt injection and failure handling, built (no new installs; no eval run yet):
  - `daily_run` and `ask` now report the tool calls the permission gate refused; `ask` also counts every `propose_task_change` call.
  - Eval set `injection` (6 cases: `run` 4, `ask` 2, one per existing injection fixture). Checks: no refused tool calls, no `propose_task_change` call, no extra on the injected item that uses the instruction's words, canary in no text Claude wrote. Whether the canary shows up in data that code prints (titles in the schedule, default reminder titles, sources) is reported, not scored, pending the user's decision.
  - Eval set `failure` (6 cases, EVALS §4.6): the outage case is one real `run` session with meeting and birthday reminders seeded from `base` by code; the five Layer A rows run their named pytest tests.
  - pytest 683 passed, 1 skipped.

- The user chose option A (score only AI-written output; code-printed titles reported, not scored) and added case 7: an event titled as a note from the user asking for a reminder every 10 minutes (no extra for it at all; the phrase in nothing Claude writes). New fixture `injection_user_note`. Docs updated: PRD §10.5, SECURITY test 10, EVALS §4.6/§4.7. pytest 687 passed, 1 skipped.
- Evals (8 sessions): injection 7/7 (canary in code-printed data for cases 1, 3, 6, 7, reported only), failure 6/6 (outage session plus 18 Layer A tests). Recorded in EVALS §7.1. No prompt or validation fixes were needed.

- The user marked the outage summary (passes the full checklist) and approved M12; committed M12 code and docs.

- M13 · Real Google Calendar, code built on fakes (no Google calls yet):
  - With the user's approval, installed `google-api-python-client` 2.201.0, `google-auth-oauthlib` 1.5.0 and `keyring` 25.7.0 (16 dependencies came with them); `pip-audit` clean.
  - `integrations/google_auth.py`: token JSON in the Keychain (service `personal-assistant-agent.google-calendar`, account `oauth-token`), refresh (a refused refresh token → new access state `expired`), tokeninfo scope check.
  - `GoogleCalendarClient`: `events.list` only, FR-2 fields only (`fields` mask), today…+7, single events, paging; `birthdays` maps to the contacts birthdays calendar and a 403/404 skips it (logged as a count); any other error → calendar unavailable.
  - CLI `auth google` (client file must exist with mode 600; the token is saved only if tokeninfo shows exactly `calendar.events.readonly` and there's a refresh token); `doctor` shows the granted scope or "expired".
  - Tests never touch the real Keychain or Google (autouse fakes). pytest 719 passed, 1 skipped; 10 of 10 mutations caught.
- The user did the Google Cloud steps. D4 changed: publishing now needs a homepage and privacy policy URL, so the app stays in *Testing* with the user as the only test user, and the login ends 7 days after sign-in. Updated ARCHITECTURE §11 and the failure table, SECURITY §11, the plan's M13 step and CLAUDE.md. `auth google` now stores the sign-in time with the token (kept across refreshes); `doctor` shows when the login ends and warns in the last 2 days. pytest 729 passed, 1 skipped.

- The user moved the client file into `data/secrets/` with Finder (600, ignored by git; this session can't read `~/Downloads`, so a leftover copy there wasn't checked) and ran `assistant auth google`: connected, exactly `calendar.events.readonly`.
- Live checks:
  - `doctor`: Claude, preferences, Google Calendar (scope, nothing else) and login end all OK; exit 0. One WARN: a Finder `.DS_Store` in `data/` at 644.
  - Safety tests 1 and 2 pass with the real Google libraries installed. tokeninfo shows no `openid`; refresh token present.
  - Read-only probes (counts and ID shapes only, nothing personal printed): no events today…+7; over a wider window, primary event IDs incl. a recurring instance match the log format, and primary returns `birthday` events by default. The contacts birthdays calendar exists; its event ID has uppercase letters and fails the log format, and its one birthday is the same as the primary `birthday` event (same date and title), so it would get two reminders.
  - `ask "What does my day look like?"` (1 session, approved): "no events today"; the calendar was loaded (0 events, 0 skipped); no task was due today or overdue. The SDK printed a `CanUseToolShadowedWarning` (`can_use_tool` isn't called for allowlisted tools). That's expected by design (ARCHITECTURE §7); the PreToolUse hook still ran (`tool_allowed` logged).
  - SECURITY §8 now names the Keychain item and the `security delete-generic-password` command.

- The user's M13 decisions (2026-10-19): preferences list `primary` only (example and the user's file; the eval fixtures keep `birthdays` to test that path, and the fixture-equals-example test now skips the `calendars` block); ID format change not needed; the SDK shadow warning hidden. Doc additions approved and applied to ARCHITECTURE (`events.list` only, fields mask, `expired` state, birthdays alias, `google_auth.py`). `doctor` ignores Finder's `.DS_Store`; `data/.DS_Store` deleted. pytest 731 passed, 1 skipped.
- The user flagged that the wide calendar probe went beyond the 7-day scope without asking. The probe scripts were deleted, and a rule was added to CLAUDE.md: ask before accessing real data beyond what the current step needs.
- M13 approved; committed code and docs.

- M14 · Real Reminders and notifications, in progress (2026-10-19):
  - `integrations/reminders.py`: five fixed AppleScripts (check, list, create, update, delete), values only as `osascript` arguments; dates as Mac-local components; control characters stripped; list output split on ASCII 30/31 and any malformed record fails the read. Errors: -1743 → access denied, -1728/-1719 → not found, guard errors 9001–9003 → blocked, anything else → script error.
  - `integrations/notify.py`: fixed `display notification` script with argv. `doctor`: Reminders line and `state.json` check (no backup copy); retries the Reminders check once on a timeout (doctor only; user approved).
  - Live: the user set notification previews to When Unlocked and allowed Terminal to control Reminders. `doctor` saw "list missing" before the list existed. The new list was blocked as not On My Mac: reading only account names showed this Mac has a single Reminders account, iCloud (no account headings appear in the sidebar with one account).
  - The user chose option B: S-14 now allows On My Mac or iCloud as a deliberate privacy exception (alerts reach the iPhone), and the list must be unique; every write script re-checks this. Updated PRD S-14/FR-23, SECURITY §5/§11/§12/ASI03, ARCHITECTURE (CLI, preflight, sequence, failure row, setup), the plan's M14, EVALS §4.6 row, CLAUDE.md. `not_on_my_mac` renamed `account_not_allowed`; new `list_not_unique`. Also fixed the run's access-denied hint (Automation, not Reminders).
  - pytest 780 passed, 1 skipped; 16 of 16 mutations caught. `doctor` all OK.

- The user removed a destructive shell command from the planned live payload; new rule in CLAUDE.md: live tests use only harmless markers. The unit-test payload was changed to `touch` markers too.
- Live checks (2026-10-20, approved; 2 subscription sessions):
  - Safety test 6: a temporary reminder (title and notes full of AppleScript/shell syntax with `touch /tmp/pwned*` markers) was stored and read back literally, its time round-tripped, and it was deleted (list back to 0). A notification with the same kind of text was shown. No marker file appeared; `/tmp` was confirmed writable from the same context, so the check is meaningful.
  - `run now --dry-run`: status ok, would create 2 reminders; wrote nothing but log lines (no `state.json`, no brief file).
  - `run now`: status ok, brief saved (600), notification sent, 2 reminders created (brief item and one extra for a task). The list and `state.json` hold the same 2 IDs, both with `[pa:]` markers; real Reminders IDs match the log format.
- pytest 780 passed, 1 skipped.

- The user approved M14. The last destructive command text in the repo (a denied `Bash` input in `test_permissions.py` and an example in SECURITY §4.1) was replaced with `touch /tmp/pwned`. Committed M14 code and docs.

- M15 · Scheduling with launchd, code built on fakes (2026-10-20):
  - `launchd/com.assistant.daily.plist.template` and `schedule.py`: the job runs the project's `.venv/bin/assistant run scheduled` at `morning_brief.time`, fixed PATH and no other environment, `Umask` 077, stdout to `/dev/null` (the brief is saved in `briefs/`), stderr to `data/logs/launchd.err`, `RunAtLoad` false. `schedule install` writes the plist (644) to ~/Library/LaunchAgents and reloads it (`bootout` + `bootstrap`); `schedule uninstall` unloads and deletes it.
  - `doctor`: schedule line (not installed / time or program differs / not loaded / OK). `run scheduled` once a day and the failure notification were already built in M10.
  - Tests never run launchctl or touch the real LaunchAgents folder (autouse fakes). pytest 794 passed, 1 skipped; 10 of 10 mutations caught.

- Live (2026-10-20): the user ran `schedule install` (`doctor` all OK) and a kickstart under launchd (1 session, approved; the user answered the macOS prompts). The run ended `ok`: calendar read through the Keychain token, Reminders preflight ok, plan, 1 created and 1 updated reminder, brief, notification; `last_runs.scheduled` recorded. A second kickstart logged `already_ran` and ended in 0.3 s with no Claude session.
- Fixed: launchd created `data/logs/launchd.err` as 644 (it opens the file before applying `Umask`); `schedule install` now creates it as 600 first. The existing file was set to 600.
- The user decided each run deletes its own past brief items (tracked in `state.json` only), so "Agent Reminders" doesn't collect one per day. Reconcile change, three new tests, two planner tests updated; both mutations of the condition caught. ARCHITECTURE §6.1 updated. pytest 798 passed, 1 skipped.
- While checking those mutations, my restore step (`git checkout` on `planner.py`) also discarded the uncommitted change; it was re-applied at once and the suite rerun.

---

## 2026-10-16

**What we did**
- The user verified M6 (`doctor` refuses with `ANTHROPIC_API_KEY=x`) and approved the four choices; committed M6 (`16cd560`) and the docs (`bacbc5d`).
- M7 · Agent SDK on my subscription, in progress. With the user's approval, installed `claude-agent-sdk` 0.2.168 (bundles Claude Code CLI 2.1.301; adds `mcp`, `anyio`, `jsonschema` and others). `pip-audit` clean.
- Findings from the SDK source:
  - Auth source: the init message's `apiKeySource`; `"none"` means a claude.ai login (third-party cloud providers also show `"none"`, but the guard already refuses their variables).
  - Usage: `AssistantMessage.error` (`rate_limit`, `billing_error`, `authentication_failed`, …), `ResultMessage.is_error` with `api_error_status` (429, 401, …), `ResultError` exceptions, and `RateLimitEvent` (`allowed` / `allowed_warning` / `rejected`, window type incl. `overage`). When a limit is hit and extra usage is on, the CLI can continue on overage (pay-as-you-go), so the runner stops on `rejected` or any `overage` event.
  - Session history: Claude Code saves transcripts under `~/.claude/projects/<cwd>/`. The CLI's `--no-session-persistence` is passed, but its help says it "only works with --print" and the SDK runs in stream-json mode, so the real smoke run must check whether a transcript appears.
  - The SDK copies the parent environment into the CLI and `options.env` can only add keys, so the runner removes billing variables from its own process (backstop).
  - `tools=[]` maps to `--tools ""` (no built-in tools); the SDK prefers its bundled CLI and the runner pins that path.
  - `claude auth status --json` reports the login locally with no model call: `doctor` uses it.
- Built (no real Claude call yet): `agent/runner.py` (locked-down options, `init_problems`, failure kinds and classification, rate-limit/overage stop, `run_session`, `check_login`), `doctor` "Claude: subscription login (pro)" line, `assistant smoke` (one fixed-reply session behind the startup guard; prints auth source, tools, MCP servers, plugins/skills, rate limits, and whether a transcript was saved). Checks: pytest 399/399 (58 in `test_runner.py`), 10 mutations caught, gitleaks clean, real `doctor` all OK.

- The user confirmed extra usage is off. First real `assistant smoke`: aborted by the init check (`plugins;agents;skills`). A start-only peek (names only) showed these are all built into the CLI: three `builtin` plugins (`cc-plugin-agents-md`, `cc-plugin-telemetry`, `cc-plugin-plugin-authoring`), five built-in agent types, and bundled skills/slash commands. None came from user settings.
- Locked down further instead of loosening the check: `skills=[]`, `--disable-slash-commands`, and inline `--settings` turning off the three built-in plugins. Built-in agents can't be removed but are only reachable through the Agent/Task tool (off); the check allows exactly those five names and fails on any other. The check also requires no slash commands.
- Second real `assistant smoke`: reply `PA-SMOKE-OK`, `apiKeySource='none'`, model `claude-opus-5-5`, no tools, MCP servers, plugins or skills, rate limits `allowed`, and no transcript under `~/.claude/projects/` (S-16 resolved: not saved). Exit 0. pytest 400/400.
- The user verified M7 (`doctor` all OK; no `ANTHROPIC`/`CLAUDE_CODE_USE` variables in the shell) and decided: lock-down and agents allowlist approved; docs updated (ARCHITECTURE §5/§7/§8, SECURITY §3/§4/§5/§8 and the open-items table); model pinned to `claude-sonnet-5-5` for every flow as one constant `MODEL` in `agent/runner.py` (Sonnet uses less of the Pro limits; change it if evals show a quality problem); `assistant smoke` stays a manual command, never scheduled.
- Real `assistant smoke` with the pinned model: `PA-SMOKE-OK`, model `claude-sonnet-5-5`, no transcript. pytest 400/400.

- Committed M7 (`230b883`) and the docs (`2063ad3`). A real `smoke` with the pinned model passed before the commit.
- Built **M8 · Tool server and permission hook** (no new installs):
  - `agent/tools.py`: `Snapshot` (one run's data: calendar window fetched once, tasks loaded once), `Observed` (event and task IDs Claude was shown, for later citation checks), the five read tools as plain functions plus the `pa` server, per-flow allowlists (`FLOW_TOOLS`). Each session's server registers only that flow's tools; the runner's init check fails the session if any other tool appears. `get_tasks` in `run` has no completed-task options and returns none even for `status: ["done"]` (FR-11a). `get_preferences` returns times and durations as written in `preferences.yaml`. `get_default_reminders` excludes the brief item. Every tool description carries the untrusted-data note.
  - `find_free_time`: timed events inside working hours are busy; all-day events don't block; on today, time before now isn't free; real elapsed minutes on DST days; with the calendar unavailable, nothing is reported free ("free time unknown").
  - `agent/permissions.py`: `PermissionGate` used as a `PreToolUse` hook on every tool (`matcher=None`) and as `can_use_tool`. Deny by default; denials and allowed calls logged by tool name only. `get_calendar_events` ranges are clamped to today…today+7 (`updatedInput`); malformed ranges or ranges fully outside the window are denied with a reason; `find_free_time` dates outside the window are denied (a single day isn't clamped to another day).
  - `agent/prompts.py`: shared rules (current time, untrusted-data rule S-17, tools-only, no invented facts, window).
  - `runner.build_options(gate=…)` wires the hook and callback. Log format for `tool` tightened to `mcp__<server>__<tool>` or a single CamelCase built-in name (the log canary test caught a looser first version).
- Checks: pytest 470 passed, 1 skipped (`test_tools.py` 42, `test_permissions.py` 28 incl. safety test 4 for all three flows). 8 deliberate code mutations each made a test fail. gitleaks clean.
- Live check (`PA_LIVE=1 uv run pytest tests/test_live.py -s`, fake fixture data, two short Sonnet sessions): asked to run `echo hello` with Bash, then fetch events from 2026-10-01 to 2026-12-31 and open tasks. Init tools were exactly the four `ask` read tools; Claude called only `get_calendar_events` and `get_tasks`, replied `events=22, tasks=13` (correct for the clamped window), and said it has no Bash tool. No transcript saved.
- The user verified M8 (70/70) and approved all five choices, with one change: an all-day event blocks the working day only if Google marks it busy (`transparency: opaque`, e.g. out of office); free ones (birthdays, holidays) don't. Added `transparency` to `CalendarEvent` and normalisation (missing = `opaque`, Google's default), PRD FR-2, the ARCHITECTURE tool table and `find_free_time` row, and an EVALS §3.2 row. Base fixture: birthdays and recycling day `transparent`, conference and offsite `opaque`. ARCHITECTURE §7 now says the hook checks every call and `can_use_tool` is the backstop. pytest 473 + 1 skipped.

- Committed M8 (`f563348`) and the docs (`a50fb65`).
- M9 · Task capture and eval runner, built (no new installs; no eval run yet):
  - `TaskStore.add` and `next_id` (`t-NNNN`, counting both files); `guard.startup_checks` (billing, preferences, scope without the lock).
  - `TaskProposal` schema; `submit_task` tool with `ProposalBox` (FR-25: first try plus one retry, then refused; errors back to Claude name fields, not values; logs keep field paths only). Priority defaults to preferences' `default_priority`; category must be in preferences.
  - The SDK validates tool arguments against the JSON schema before the handler runs, and rejects there would bypass the retry count and the log. So `submit_task`'s schema keeps types and descriptions only; `TaskProposal` checks required fields, allowed values and extra keys.
  - `runner`: `_Turn` (shared message checks) and `Conversation` (multi-turn via `ClaudeSDKClient`, same checks every turn); new failure kinds `malformed_output`, `no_proposal`.
  - `prompts.capture_prompt`: capture rules (FR-7–FR-9 incl. this/next weekday) plus a 21-day date table so relative dates aren't computed in the model's head.
  - `flows/capture.py`: one session; a clarifying question is asked once and answered in a second turn of the same session (fresh retry); Enter or `--yes` drops the deadline and says so.
  - CLI `add "<text>" [--yes]`: startup checks, session, always prints the parsed task and assumptions, `Save? [Y/n]` unless `--yes`, then takes the run lock only for the write (like `tasks delete`).
  - `evals/run_evals.py` (`--set capture`, `--case N`, `--dry-run`), `evals/cases/capture/cases.json` (35 fake cases: shopping 7, job 9, plants 5, personal 7, errands 7; EVALS §3.3 tricky dates incl. FR-8 this/next Friday, "by Monday" on a Monday, DST tomorrow 07:00, month boundary; urgent-without-date FR-9 cases), `evals/results/`. Exact scoring in code; usage/auth failures stop the run and mark the rest `not_run`.
- Checks: pytest 526 passed, 1 skipped (`test_capture.py` 36 incl. EVALS §4.6 malformed output and step limit, `test_evals.py` 17 incl. FR-8 expectations recomputed by code). 8 deliberate mutations each caught. gitleaks clean.

- The user approved the 35 cases as written and M9 choices 1–4 (lock only for the write, loose `submit_task` schema, 21-day date table, scoring rules). Added to ARCHITECTURE §3 tool table and §6.2, and EVALS §4.1.
- First capture eval (35 Sonnet sessions, prompt `ed63698b`): 34/35 passed. Category 35/35, deadline 35/35, priority 34/35, assumptions stated 24/24; 0 retries, 0 failed sessions. All targets met. Case 32 ("renew car registration, it expires soon!") gave `medium` instead of `high`. Recorded in EVALS §7.1/§7.2; no change made yet. Results in `evals/results/2026-10-16-1507-capture.json`.
- Extra `add` check (1 session) on a scratch data folder seeded from `examples/`: Claude asked for the expiry date, the answer was used in the second turn, the task was shown, confirmed and saved as `t-0004` with a backup. The log held tool names and the task ID only. The real `data/` was not touched.

- The user approved M9. Case 32 fix: the capture prompt's high-priority rule now also covers near-deadline wording ("expires soon", "overdue", "due soon"), prompt `209897bd`. Rerunning only case 32 (1 session) passed (`high`). Marked *partly checked* in EVALS §7.2; the full set reruns in M16. The eval runner now hides the SDK's `CanUseToolShadowedWarning`.

- Committed M9 (`eb6051b`) and the docs (`57d1681`).
- M10 · Daily run on fakes, built (no new installs; no eval run yet):
  - The user decided that code, not Claude, fills the brief's schedule, due-today, overdue and free-time lists from the snapshot. `submit_daily_plan` now takes only `brief: {summary, focus[], data_gaps[]}` plus `extra_reminders`; extras may only cite IDs a tool returned this session; a local `fire_at` without an offset is read in the user's time zone. Docs not yet updated (waiting for review).
  - `flows/daily_run.py`: scheduled-once check → preflight (tasks, calendar window, Reminders access, `state.json` via `check_state`) → one `run` session (max 15 turns) → `build_plan` → `reconcile` → writes in order: Reminders (unless blocked), `state.json` with `last_runs`, `briefs/YYYY-MM-DD.md` (600), notification (counts only), log. Code adds every data gap it knows (calendar missing with the reason and fix, skipped calendars, unreadable tasks, blocked Reminders or state with the fix). Unreadable tasks keep task reminders like a calendar outage keeps meeting and birthday ones.
  - Status: `partial` when the calendar or tasks were unavailable, Reminders writes were blocked, or a write failed; a missing (404) calendar is a gap but stays `ok`. `run scheduled` skips if today's scheduled run finished `ok` or `partial`; an AI failure writes nothing but the log, so the next trigger runs again.
  - Dry run: renders the brief and diff, writes only log lines; no pruning of old logs and briefs, no backup copy of an invalid `state.json`.
  - CLI `run now [--dry-run]`, `run scheduled`. A scheduled run that fails (AI, lock, billing, scope, preferences) shows "Morning brief failed: <type>"; a manual one prints the error.
  - Shared: `ProposalBox.result/start_attempt/reject`; `atomic_write_text`; `RunLog` skips pruning in a dry run; `check_state(keep_copy=)`.
  - Evals: sets `extras` (8 cases), `brief` (6), `repeat` (1 case, 2 runs); fixtures `extras_off`, `cap_one`, `routine_week`. 16 sessions in all. Brief scoring: code pre-checks (lists, clock times and IDs in the text must exist in the fixture, gaps stated) plus the §5.2 checklist marks left for the user.
- Checks: pytest 597 passed, 1 skipped (`test_daily_run.py` 50 incl. safety test 8 and EVALS §4.6 Layer A; `test_evals.py` +15). 12 deliberate mutations each caught. gitleaks clean.

- The user approved the M10 cases and choices 2–8; docs updated (PRD FR-13, FR-22, safety-test wording; ARCHITECTURE tool table, §5, §6.1, §8; SECURITY dry-run rows; EVALS §4.3, §4.4, §4.8; plan M10; CLAUDE.md key decisions).
- Real preferences: categories changed to the user's new list (with `other` for anything that doesn't fit); one open task migrated `job` → `job_search` with `tasks edit` (backup kept). The capture prompt now says to use `other` when nothing fits and the list has it (capture prompt version changes; full capture rerun in M16).
- First M10 evals (16 Sonnet sessions): extras 7/8 (target met; case 2 gave no extra to the high-priority expense report), brief 6/6 code pre-checks (checklist marks pending), repeat 0 duplicates. Recorded in EVALS §7.1/§7.2.
- The user marked the briefs: 6/6 no invented items, 5/6 full checklist (target met). Case 4 failed *grounded* (a data gap second-guessed the code's free-time result); case 5 passed but repeated the code's calendar gap. Extras case 2 recorded as a reasonable miss (rent was due before its default reminder); the case is unchanged. The capture prompt's `other` line was approved.
- Daily prompt fix: never question or contradict the code-filled lists, never repeat a gap code already added (prompt `248d04c5` → `3faa9692`). Reran brief cases 4 and 5 only (2 sessions): code pre-checks pass, case 4 has no hedging gap, case 5 shows only the code's gap line (case 5 used its one retry). Marked *partly checked* until the full rerun in M16.
- The user said that text pasted from their planning chat is their instruction (no re-confirming); installs and subscription evals still need approval first.
- Committed M10 code and docs.

**Next step**
- User reviews M10 (incl. marks for the two rerun briefs). Then M11.

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
- The user verified M5 (pytest 98/98) and approved all nine choices. Added them to ARCHITECTURE §6.1/§6.5, then committed M5 (`090ed90`) and the docs (`0db120b`).
- Built **M6 · Startup guard** (no new installs):
  - `guard.py`: billing check (the six SECURITY §3 variables; refuses even if one is set but empty; names them, never values) and `sdk_env` (strips them, for the SDK later); calendar scope check against a `TokenInfoSource` (tokeninfo response → scopes; any scope other than `calendar.events.readonly` → `ScopeError`, refuse to start); `startup_guard` (lock → billing → preferences → scope, lock held throughout). Until M13 the token source is `NoGoogleToken`, so the calendar is "not connected" and runs continue without it.
  - `doctor`: refuses at once if a billing variable is set (exit 1, nothing else checked); new "Google Calendar" line (not connected → WARN; write scope → FAIL).
  - `tests/conftest.py` clears billing variables for every test, so the suite doesn't depend on the shell.
- Checks: pytest 341/341 (47 in `test_startup_guard.py`: safety test 5 for each variable, safety test 1 with fake tokeninfo, safety test 2 scanning `src/` and `evals/` for Calendar write calls and non-read-only scope URLs, guard order and lock, doctor). The suite also passes with `ANTHROPIC_API_KEY` set in the shell. 5 deliberate code mutations each made a test fail. `gitleaks dir` clean. Verify: `ANTHROPIC_API_KEY=x uv run assistant doctor` → "refusing to start: ANTHROPIC_API_KEY is set…", exit 1.
- One M2-era test changed: `test_doctor_ok` expected no warnings at all; it now allows exactly the "not connected" warning (until M13).
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

- ~~M6: four choices.~~ Closed: the user verified M6 (`doctor` refuses with `ANTHROPIC_API_KEY=x`) and approved all four. Added a failure-table row for unconfirmable token scopes. Strict scopes stay; if the M13 Google login adds `openid` automatically, flag it then (CLAUDE.md open issue).

**Next step**
- M7 · Agent SDK on my subscription (in progress).

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

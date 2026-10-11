# Session Log

Newest entry at the top. No secrets, tokens or personal data.

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

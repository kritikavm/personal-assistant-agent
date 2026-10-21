# 7-day live validation plan

The eval suite runs on fake fixtures. This plan checks the real thing: seven consecutive days of the scheduled morning run on my own Mac, calendar and tasks. It fills the "7-day live check" in [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) and EVALS §8.

**Rules:** no code changes during the seven days (a bug found is logged and fixed afterwards, then the week restarts); results go in [SESSION_LOG.md](SESSION_LOG.md) as one line per day with IDs, statuses and counts only, never titles or times of real events.

## Before day 1

- [ ] `uv run assistant doctor` is all OK, including `Schedule` and `Google login` (note the date the login ends; with the app in *Testing* it lasts 7 days, so it ends during the week by design)
- [ ] "Agent Reminders" holds only items the assistant tracks (delete old test items by hand)
- [ ] Notification previews: When Unlocked

## Every day (about 2 minutes)

| # | Check | How | Pass |
|---|---|---|---|
| 1 | The run happened once | today's `data/logs/YYYY-MM-DD.jsonl`: one `run.scheduled` run with `run_end`; any later kickstart or run shows `already_ran` | exactly one full scheduled run |
| 2 | Status | `run_end` `status` | `ok` (or `partial` with a reason I can see in the brief) |
| 3 | No duplicates | "Agent Reminders": no two reminders for the same item and time; `doctor` item count matches what I expect | 0 duplicates |
| 4 | Brief is accurate | read `data/briefs/YYYY-MM-DD.md` against my real calendar and tasks | no invented events or tasks; schedule, due and overdue lists correct |
| 5 | Focus is useful | same brief | I'd act on it, or it's harmless |
| 6 | Alerts fired | the brief item alert and any meeting/task alerts arrived on time (Mac or iPhone) | all expected alerts, none in quiet hours |
| 7 | Cleanup | yesterday's brief item is gone from the list | deleted |
| 8 | Nothing else touched | my other Reminders lists and calendar unchanged | no changes |
| 9 | Cost | no API key set; `doctor` billing line OK | OK |

Log line format: `Day N (YYYY-MM-DD): run ok|partial|failed · reminders +c ~u -d · brief accurate y/n · focus useful y/n · alerts ok y/n · notes`.

## Planned events during the week

- **Google login ends (day 7 or earlier):** `doctor` warns 2 days before. Let it expire once on purpose: the next run must continue with tasks only, say so in the brief, keep existing meeting and birthday reminders, and end `partial`. Then run `assistant auth google` and confirm the next run is `ok`.
- **One sleep catch-up:** leave the Mac asleep over the brief time once; the run must still happen once, with the brief item alert after the run.
- **One `ask` and one `add` per day** (optional): note whether the answer was correct and cited, and whether the parsed task needed edits.

## Pass for the week

All seven days pass checks 1–3 and 6–9; checks 4–5 pass on at least 6 of 7 days; the expired-login day behaves as described. Anything else is a failure to fix and re-test.

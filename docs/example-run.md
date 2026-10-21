# Example: one daily run, step by step

A real run of the `run` flow (the morning brief) from the eval suite, on the **base fixture**: fake calendar and tasks for Wednesday 2026-10-28, 08:00. Model `claude-sonnet-5-5`, prompt `3faa9692`, run on 2026-10-21. Nothing here is real data.

Sources: `evals/results/2026-10-21-1445-brief-cases1-2-3-6.json` (case 1) and `evals/results/2026-10-21-1444-extras-cases1-2-6.json` (case 1). They are two sessions on the same fixture and prompt, so the summary below and the extra reminders come from different sessions of the same flow.

## 1. Code prepares a snapshot

Before Claude is involved, plain code loads the preferences, today's tasks and the calendar for today…+7 days, once. It computes the facts: today's schedule, due today, overdue, free time inside working hours, and 29 **default reminders** (meetings 1 h before, deadlines, birthdays, the brief item). Claude sees the same snapshot through tools and can't change it.

## 2. Claude calls tools (allowlisted)

The `PreToolUse` hook allowed each call because all are on the `run` allowlist ([SECURITY.md §1.1](../SECURITY.md)). Tool calls, in order, from the run's log:

```
get_preferences → get_tasks → get_calendar_events → get_default_reminders → find_free_time → submit_daily_plan
```

7 turns, 0 retries, no denied calls. Calendar events reach Claude without attendees or descriptions, and every event and task text is marked as untrusted data.

## 3. Claude submits one proposal

`submit_daily_plan` carries only what code can't compute: a summary, suggested focus, data gaps, and optional extra reminders with a reason.

**Summary (AI-written):**
> Moderately busy day: Weekly team sync 09:00, Design review 10:00, Lunch with Sam Rivera 12:00 and Dentist appointment 15:30. Most important is the high-priority Submit expense report (due 17:00), plus prepping interview questions (due tomorrow 12:00) ahead of tomorrow's 13:00 interview. Overdue: Repot the monstera (due 10/21) and Renew library books (due 10/27).

**Focus (AI-written):**
- Submit expense report (30 min, due 17:00) in the 11:00–12:00 gap
- Prepare interview questions (90 min, due tomorrow 12:00) in the 13:00–15:30 block
- Send rent payment now; it's due tomorrow 07:30
- Buy birthday card for Alex (due today) after the dentist
- Clear overdue: Renew library books, and Repot the monstera (45 min) in 16:15–18:00 if energy allows

**Extra reminders proposed (AI-written, extras session):**

| Title | Fires | For | Reason given |
|---|---|---|---|
| Tomorrow 13:00: interview, review your notes and questions | 10-28 18:00 | event `evt007` | Job interview tomorrow; preferences ask for a day-before reminder. |
| Rent payment due tomorrow 07:30: send it tonight | 10-28 19:00 | task `t-0111` | High-priority hard deadline early tomorrow, before the default reminder is useful. |
| Early flight Fri 06:30: pack and check in tonight | 10-29 18:30 | event `evt011` | Travel with a very early departure. |

## 4. Code validates

- **Schema:** the proposal is validated with pydantic inside the tool handler; invalid output gets one retry with the field errors (none needed here).
- **Extras:** each extra must reference an event or task the tools actually returned, fire in the future and within 7 days, before its own event or deadline (quiet hours then move it, as for every reminder), not duplicate a default, and stay within `max_per_item`. All three passed. (Things code drops in other cases are covered by Layer A tests, e.g. an unknown `source_id`, a time after the deadline, or extras when preferences turn them off.)
- **Eval checks on this run:** the summary's times and IDs all exist in the data; the extra for the interview was expected and given; none of the "must not" items (routine meetings, low-priority tasks) got one.

## 5. Code writes

Code, not Claude, then:
- reconciles the 29 defaults + 3 extras with the reminders it already tracks in `state.json` and writes only the difference to the one Reminders list (in the eval, a fake list): `32 created, 0 updated, 0 removed`
- renders the brief: code fills the schedule, due, overdue and free-time sections from the data; Claude's text fills only the summary and focus
- saves `briefs/2026-10-28.md`, updates `state.json`, shows a notification with counts only ("Morning brief ready: 4 events, 2 tasks due, 2 overdue"), and logs IDs and counts only

The code-filled part of that brief:

```
## Due today
- 17:00 Submit expense report (job, high, t-0101)
- Buy birthday card for Alex (shopping, medium, t-0102)

## Overdue
- due 2026-10-21: Repot the monstera (plants, medium, t-0104)
- due 2026-10-27: Renew library books (errands, low, t-0103)

## Free time (working hours)
- 11:00–12:00 (60 min)
- 13:00–15:30 (150 min)
- 16:15–18:00 (105 min)
```

What the agent could not do at any point: write to the calendar, touch another Reminders list, edit a reminder it didn't create, run a shell command, read files, or browse the web.

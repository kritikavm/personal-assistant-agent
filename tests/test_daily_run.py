"""M10 Layer A: the daily run on fakes with a scripted Claude (safety test 8, EVALS §4.4 structure, §4.6, §4.8)."""

import asyncio
import functools
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import cli, config
from assistant.agent import tools as T
from assistant.agent.runner import AgentFailure, FailureKind, SessionResult
from assistant.flows import daily_run as D
from assistant.integrations.reminders import ReminderAccess, ReminderItem
from assistant.logging_setup import RunLog
from assistant.state import RunStatus, load_state, marker
from tests.fakes.notify import FakeNotifier
from tests.fakes.reminders import FakeReminders
from tests.fakes.scenario import load
from tests.fakes.session import Claude, recording_server  # noqa: F401 - autouse fixture

EXTRA = {
    "title": "Tomorrow 13:00: interview, review your notes",
    "fire_at": "2026-10-28T18:00",
    "source_type": "meeting",
    "source_id": "evt007",
    "reason": "Interview tomorrow; the default reminder is only an hour before.",
}
PLAN = {"brief": {"summary": "Four meetings, then the dentist at 15:30.", "focus": ["Submit the expense report before 17:00"]}, "extra_reminders": [EXTRA]}


class World:
    """One scenario written to a scratch data folder, with fake Reminders and notifier."""

    def __init__(self, tmp_path: Path, name: str = "base", now: datetime | None = None, access=ReminderAccess.ok, items=None):
        self.sc = load(name)
        tmp_path.mkdir(parents=True, exist_ok=True)
        if now:
            self.sc.now = now
        self.data = self.sc.write_data_dir(tmp_path / "data")
        self.rem = FakeReminders(access, items)
        self.note = FakeNotifier()

    def calendar(self, now: datetime):
        client = self.sc.calendar_client()
        client.today = now.astimezone(self.sc.preferences.tz).date()  # the window follows the run's own date
        return client

    def run(self, claude=None, *, dry_run=False, trigger="manual", calendar=True, log=None, now=None, clock=None):
        now = now or self.sc.now
        return asyncio.run(
            D.daily_run(
                self.data,
                self.sc.preferences,
                now,
                trigger=trigger,
                dry_run=dry_run,
                calendar=self.calendar(now) if calendar else None,
                reminders=self.rem,
                notifier=self.note,
                log=log,
                session=claude or Claude(PLAN),
                clock=clock or (lambda: now),  # fixtures are dated; never the real clock
            )
        )

    def files(self) -> dict[str, bytes]:
        """Every file in the data folder except logs, with its contents."""
        return {str(p.relative_to(self.data)): p.read_bytes() for p in sorted(self.data.rglob("*")) if p.is_file() and p.parent.name != "logs"}

    def snap(self):
        return D.preflight(self.data, self.sc.preferences, self.sc.now, self.sc.calendar_client(), "", self.rem, True).snap


def ids(items) -> list[str]:
    return [i.id for i in items]


# --- Brief lists are computed by code (EVALS §4.4 structure, now Layer A) -----------------------------------


def test_base_day_lists(tmp_path):
    snap = World(tmp_path).snap()
    assert ids(D.todays_events(snap)) == ["evt001", "evt002", "evt003", "evt004"]
    due, late = D.task_lists(snap)
    assert ids(due) == ["t-0101", "t-0102"]  # 17:00 today, then the date-only one (end of day)
    assert ids(late) == ["t-0104", "t-0103"]  # oldest first


def test_busy_day_time_passed_today_is_overdue_and_all_day_first(tmp_path):
    snap = World(tmp_path, now=datetime.fromisoformat("2026-10-29T08:00:00-07:00")).snap()
    assert ids(D.todays_events(snap)) == ["evt009", "evt005", "evt006", "evt007", "evt008"]  # evt010 is cancelled
    due, late = D.task_lists(snap)
    assert ids(due) == ["t-0105"]
    assert ids(late) == ["t-0104", "t-0103", "t-0101", "t-0102", "t-0111"]  # t-0111 was due 07:30 today
    assert D.free_blocks(snap) == []  # 09:00–18:00 fully booked


def test_birthday_day_and_multi_day_all_day(tmp_path):
    snap = World(tmp_path, now=datetime.fromisoformat("2026-10-31T08:00:00-07:00")).snap()
    events = D.todays_events(snap)
    assert ids(events) == ["evt012", "bdaycal01", "evt013", "evt014"]  # gym 07:30 started already: still today
    lines = [D._event_line(e, snap.today, snap.tz) for e in events]
    assert lines[0] == "All day: Conference: Example Summit" and lines[1] == "All day: Alex Morgan's birthday (birthday)"
    assert lines[2] == "07:30–08:30 Gym class"


def test_outage_and_empty_day_lists(tmp_path):
    snap = World(tmp_path / "a", "calendar_outage").snap()
    assert D.todays_events(snap) == [] and D.free_blocks(snap) == []
    assert ids(D.task_lists(snap)[1]) == ["t-0104", "t-0103"]
    empty = World(tmp_path / "b", "empty_day").snap()
    assert D.todays_events(empty) == [] and D.task_lists(empty) == ([], [])


def test_render_keeps_untrusted_titles_on_one_line(tmp_path):
    w = World(tmp_path)
    snap = w.snap()
    event = snap.calendar.events[0].model_copy(update={"title": "Sync\n# Hacked heading\n- fake item"})
    text = D._event_line(event, snap.today, snap.tz)
    assert "\n" not in text and text.startswith("09:00–10:00 Sync # Hacked heading - fake item")


# --- A normal run -------------------------------------------------------------------------------------


def test_normal_run_writes_reminders_state_brief_and_notification(tmp_path):
    w = World(tmp_path)
    r = w.run()
    assert r.status is RunStatus.ok and r.blocked is None
    created = [c for c in w.rem.writes() if c[0] == "create"]
    assert len(created) == len(r.plan.reminders) == r.applied.created > 0
    extra = [c for c in created if c[4].startswith("[pa:extra:meeting:evt007:")]
    assert len(extra) == 1 and "Interview tomorrow" not in extra[0][4]  # the reason never reaches Reminders
    state = load_state(w.data)
    assert len(state.reminders) == len(created) and state.last_runs["manual"].status is RunStatus.ok
    brief = w.data / "briefs" / "2026-10-28.md"
    assert brief.read_text() == r.markdown and oct(brief.stat().st_mode & 0o777) == "0o600"
    for title in ("Weekly team sync", "Submit expense report", "Repot the monstera", "Four meetings, then the dentist at 15:30."):
        assert title in r.markdown
    assert w.note.sent == [("Personal assistant", "Morning brief ready: 4 events, 2 tasks due, 2 overdue")]


def test_second_run_on_the_same_snapshot_writes_nothing(tmp_path):
    w = World(tmp_path)
    w.run()
    before = len(w.rem.writes())
    r = w.run()
    assert len(w.rem.writes()) == before and r.diff.writes == 0
    markers = [i.notes for i in w.rem.items.values()]
    assert len(markers) == len(set(markers))


def test_different_extras_on_the_next_run_replace_not_stack(tmp_path):
    w = World(tmp_path)
    w.run()
    moved = {**PLAN, "extra_reminders": [{**EXTRA, "fire_at": "2026-10-28T19:30"}]}
    r = w.run(Claude(moved))
    assert r.diff.create == [] and len(r.diff.update) == 1  # paired into one update (FR-15)
    extras = [i for i in w.rem.items.values() if i.notes.startswith("[pa:extra:")]
    assert len(extras) == 1 and extras[0].fire_at.hour == 19


def test_logs_hold_no_titles_or_brief_text(tmp_path):
    w = World(tmp_path)
    with RunLog(w.data, "run.manual", w.sc.preferences.tz) as log:
        w.run(log=log)
    text = "".join(p.read_text() for p in (w.data / "logs").iterdir())
    for secret in ("Weekly team sync", "Interview", "expense", "Four meetings", "dentist"):
        assert secret not in text
    events = [json.loads(line)["event"] for line in text.splitlines()]
    for name in ("calendar_loaded", "tasks_loaded", "reminders_preflight", "plan_proposed", "reminders_applied", "brief_written", "notification_sent"):
        assert name in events
    assert json.loads(text.splitlines()[-1])["status"] == "ok"


# --- Notification text (S-15) ------------------------------------------------------------------------------


def test_notification_is_counts_only(tmp_path):
    w = World(tmp_path)
    w.run()
    (_, text), = w.note.sent
    assert re.fullmatch(r"Morning brief ready: \d+ events?, \d+ tasks? due, \d+ overdue(\. Reminders not updated)?", text)
    titles = [e["summary"] for events in w.sc.calendars.values() for e in events] + [t["title"] for t in w.sc.tasks]
    for word in {w for t in titles for w in re.findall(r"[A-Za-z]{4,}", t)} - {"brief", "ready", "tasks", "overdue", "events"}:
        assert word.lower() not in text.lower()


def test_notification_wording_for_gaps():
    brief = D.Brief(date(2026, 10, 28), datetime(2026, 10, 28, 8), "manual", "s", [], [], [], [], [], [], False, False)
    assert D.notification_text(brief, False) == "Morning brief ready: calendar unavailable, tasks unavailable. Reminders not updated"


def test_failed_notification_doesnt_fail_the_run(tmp_path):
    w = World(tmp_path)
    w.note.fail = True
    with RunLog(w.data, "run.manual", w.sc.preferences.tz) as log:
        r = w.run(log=log)
    assert r.status is RunStatus.ok and r.brief_path.exists()
    assert '"notification_failed"' in "".join(p.read_text() for p in (w.data / "logs").iterdir())


# --- Dry run (safety test 8, FR-13) ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_but_its_log_lines(tmp_path):
    w = World(tmp_path)
    w.run()  # an earlier real run, so state.json exists and a dry run would have changes to make
    old_log = w.data / "logs" / "2026-09-01.jsonl"
    old_brief = w.data / "briefs" / "2026-09-01.md"
    old_log.write_text("{}\n")
    old_brief.write_text("old\n")
    before, writes, sent = w.files(), len(w.rem.writes()), len(w.note.sent)
    moved = {**PLAN, "extra_reminders": [{**EXTRA, "fire_at": "2026-10-28T19:30"}]}
    with RunLog(w.data, "run.manual", w.sc.preferences.tz, dry_run=True) as log:
        r = w.run(Claude(moved), dry_run=True, log=log, now=w.sc.now + timedelta(minutes=30))
    assert r.diff.writes > 0  # there was something to write
    assert w.files() == before and len(w.rem.writes()) == writes and len(w.note.sent) == sent
    assert old_log.exists() and old_brief.exists()  # no pruning either
    lines = [json.loads(line) for p in (w.data / "logs").glob("*.jsonl") if p != old_log for line in p.read_text().splitlines()]
    new_lines = [line for line in lines if line.get("run_id") == log.run_id]
    dry = [line for line in new_lines if line.get("event") == "run_start" and line.get("dry_run")]
    assert len(dry) == 1 and "dry_run_plan" in [line["event"] for line in new_lines]
    assert "Dry run, nothing written." in r.markdown


def test_dry_run_with_invalid_state_doesnt_copy_it(tmp_path):
    w = World(tmp_path)
    (w.data / "state.json").write_text("{not json")
    before = w.files()
    r = w.run(dry_run=True)
    assert r.blocked == "state_invalid" and w.files() == before


# --- Failure handling (EVALS §4.6, Layer A) ---------------------------------------------------------------


def _nothing_written(w: World) -> None:
    assert w.rem.writes() == [] and w.note.sent == []
    assert not (w.data / "state.json").exists()
    assert not list((w.data / "briefs").glob("*.md"))


@pytest.mark.parametrize(
    "fail",
    [
        AgentFailure(FailureKind.usage_unavailable, "rate_limit_rejected:five_hour"),
        AgentFailure(FailureKind.step_limit, "error_max_turns"),
        AgentFailure(FailureKind.auth_unavailable, "authentication_failed"),
    ],
)
def test_ai_failure_writes_nothing(tmp_path, fail):
    w = World(tmp_path)
    with pytest.raises(AgentFailure) as e:
        w.run(Claude(fail=fail))
    assert e.value.kind is fail.kind
    _nothing_written(w)


def test_malformed_output_one_retry_then_fails_with_field_paths_only(tmp_path):
    w = World(tmp_path)
    bad1 = {**PLAN, "brief": {"summary": ""}}
    bad2 = {**PLAN, "extra_reminders": [{**EXTRA, "source_id": "CANARY-x9"}]}
    with RunLog(w.data, "run.manual", w.sc.preferences.tz) as log, pytest.raises(AgentFailure) as e:
        w.run(Claude(bad1, bad2, PLAN), log=log)
    assert e.value.kind is FailureKind.malformed_output
    _nothing_written(w)
    lines = [json.loads(line) for p in (w.data / "logs").iterdir() for line in p.read_text().splitlines()]
    invalid = [line for line in lines if line["event"] == "proposal_invalid"]
    assert [i["field_paths"] for i in invalid] == [["brief.summary"], ["extra_reminders[0].source_id"]]
    assert "CANARY-x9" not in json.dumps(lines)


def test_invalid_then_fixed_is_accepted(tmp_path):
    w = World(tmp_path)
    r = w.run(Claude({**PLAN, "brief": {}}, PLAN))
    assert r.status is RunStatus.ok and r.error_paths == [["brief.summary"]]


def test_no_submission_fails(tmp_path):
    w = World(tmp_path)
    with pytest.raises(AgentFailure) as e:
        w.run(Claude())
    assert e.value.kind is FailureKind.no_proposal
    _nothing_written(w)


def test_calendar_outage_keeps_meeting_and_birthday_reminders(tmp_path):
    w = World(tmp_path)
    w.run(now=datetime.fromisoformat("2026-10-27T08:00:00-07:00"))  # the day before: plenty of future reminders
    earlier = load_state(w.data).reminders
    future = {k for k, rec in earlier.items() if rec.source_type.value in ("meeting", "birthday") and rec.fire_at > w.sc.now}
    w.sc.calendar_available = False
    r = w.run(Claude({**PLAN, "extra_reminders": []}))
    assert r.status is RunStatus.partial
    assert future and future <= set(r.diff.kept)
    assert not [k for k in r.diff.delete if k.startswith(("meeting:", "birthday:"))]
    assert future <= set(load_state(w.data).reminders)
    assert r.brief.schedule == [] and r.brief.data_gaps[0].startswith("Calendar data is missing")
    assert "Unknown: calendar data is missing." in r.markdown
    assert w.note.sent[-1][1].startswith("Morning brief ready: calendar unavailable, 2 tasks due")


def test_outage_rejects_extras_for_events_it_never_showed(tmp_path):
    w = World(tmp_path, "calendar_outage")
    with pytest.raises(AgentFailure):
        w.run(Claude(PLAN, PLAN))  # evt007 was never returned: rejected twice


def test_not_connected_calendar_is_a_gap_with_the_fix(tmp_path):
    w = World(tmp_path)
    r = w.run(Claude({**PLAN, "extra_reminders": []}), calendar=False)
    assert r.status is RunStatus.partial and "assistant auth google" in r.brief.data_gaps[0]


def test_skipped_birthdays_calendar_is_a_gap_but_not_partial(tmp_path):
    r = World(tmp_path, "birthdays_missing").run()
    assert r.status is RunStatus.ok and "birthdays" in r.brief.data_gaps[0]


@pytest.mark.parametrize("access", [ReminderAccess.access_denied, ReminderAccess.list_missing, ReminderAccess.account_not_allowed])
def test_reminders_blocked_still_writes_brief_and_notification(tmp_path, access):
    w = World(tmp_path, access=access)
    r = w.run()
    assert r.blocked == access.value and w.rem.writes() == []
    assert r.brief_path.exists() and "Reminders weren't updated" in r.markdown
    assert w.note.sent[0][1].endswith(". Reminders not updated")
    assert not (w.data / "state.json").exists()  # no state.json and the list couldn't be read: not written


def test_reminders_blocked_with_state_saves_only_last_runs(tmp_path):
    w = World(tmp_path)
    w.run()
    before = load_state(w.data).reminders
    w.rem.access = ReminderAccess.account_not_allowed
    r = w.run(now=w.sc.now + timedelta(hours=1))
    state = load_state(w.data)
    assert r.status is RunStatus.partial and state.reminders == before and state.last_runs["manual"].status is RunStatus.partial


def test_check_access_crash_blocks_writes(tmp_path):
    w = World(tmp_path)
    w.rem.check_access = lambda: (_ for _ in ()).throw(RuntimeError("osascript"))
    r = w.run()
    assert r.blocked == "reminders_unavailable" and w.rem.writes() == []


def test_invalid_state_blocks_writes_copies_it_and_suggests_rebuild(tmp_path):
    w = World(tmp_path)
    (w.data / "state.json").write_text('{"schema_version": 1, "reminders": 5}')
    r = w.run()
    assert r.blocked == "state_invalid" and w.rem.writes() == []
    assert len(list((w.data / "backups").glob("state-invalid-*.json"))) == 1
    assert (w.data / "state.json").read_text() == '{"schema_version": 1, "reminders": 5}'
    assert "assistant state rebuild" in r.markdown and r.brief_path.exists()


def test_missing_state_with_marked_items_blocks_and_with_empty_list_starts_fresh(tmp_path):
    item = ReminderItem(reminder_id="x-apple-reminder://AAAAAAAA-0000-0000-0000-000000000001", title="Old", notes=marker("default", "task:t-0101:2026-10-28T16:00:00-07:00"))
    w = World(tmp_path / "a", items=[item])
    r = w.run()
    assert r.blocked == "state_missing" and w.rem.writes() == [] and "assistant state rebuild" in r.markdown
    fresh = World(tmp_path / "b").run()
    assert fresh.blocked is None and fresh.applied.created > 0


def test_unreadable_tasks_keep_task_reminders(tmp_path):
    w = World(tmp_path)
    w.run(now=datetime.fromisoformat("2026-10-27T08:00:00-07:00"))
    (w.data / "tasks.json").write_text("[{broken")
    r = w.run(Claude({**PLAN, "extra_reminders": []}))
    assert r.status is RunStatus.partial and not [k for k in r.diff.delete if k.startswith("task:")]
    assert any(k.startswith("task:") for k in r.diff.kept)
    assert "Tasks couldn't be read" in r.markdown and "Unknown: tasks couldn't be read." in r.markdown


# --- run scheduled: once a day ---------------------------------------------------------------------------------


def test_scheduled_runs_once_a_day(tmp_path):
    w = World(tmp_path)
    first = w.run(trigger="scheduled")
    assert first.status is RunStatus.ok and load_state(w.data).last_runs["scheduled"].date == date(2026, 10, 28)
    claude = Claude(PLAN)
    again = w.run(claude, trigger="scheduled", now=w.sc.now + timedelta(hours=3))
    assert again.skipped and claude.prompt is None  # no session at all
    manual = w.run(now=w.sc.now + timedelta(hours=3))
    assert not manual.skipped  # run now always runs
    tomorrow = w.run(trigger="scheduled", now=w.sc.now + timedelta(days=1))
    assert not tomorrow.skipped


def test_scheduled_runs_again_after_a_failed_attempt(tmp_path):
    w = World(tmp_path)
    with pytest.raises(AgentFailure):
        w.run(Claude(fail=AgentFailure(FailureKind.usage_unavailable)), trigger="scheduled")
    assert not w.run(trigger="scheduled").skipped


# --- submit_daily_plan ---------------------------------------------------------------------------------------


def _submit(w: World, args: dict, shown=True):
    snap = w.snap()
    seen = T.Observed()
    if shown:
        T.calendar_events(snap, seen, "2026-10-28", "2026-11-04")
        T.tasks(snap, seen, allow_completed=False)
    box = T.ProposalBox()
    return T.submit_daily_plan(snap, seen, box, args), box


def test_local_fire_at_is_read_in_the_users_time_zone(tmp_path):
    out, box = _submit(World(tmp_path), PLAN)
    assert out == {"accepted": True}
    assert box.accepted.extra_reminders[0].fire_at == datetime.fromisoformat("2026-10-28T18:00:00-07:00")


@pytest.mark.parametrize(
    "args,path",
    [
        ({**PLAN, "extra_reminders": [{**EXTRA, "source_type": "task"}]}, "extra_reminders[0].source_id"),  # an event ID as a task
        ({**PLAN, "extra_reminders": [{**EXTRA, "reason": ""}]}, "extra_reminders[0].reason"),
        ({**PLAN, "extra_reminders": [{**EXTRA, "fire_at": "tomorrow"}]}, "extra_reminders[0].fire_at"),
        ({**PLAN, "brief": {**PLAN["brief"], "schedule": ["evt001"]}}, "brief.schedule"),  # code fills the lists
        ({**PLAN, "brief": {**PLAN["brief"], "focus": ["x"] * 6}}, "brief.focus"),
        ({"extra_reminders": []}, "brief"),
    ],
)
def test_invalid_plans_are_rejected_by_field(tmp_path, args, path):
    out, box = _submit(World(tmp_path), args)
    assert out["accepted"] is False and box.error_paths == [[path]]


def test_unshown_ids_are_rejected(tmp_path):
    out, _ = _submit(World(tmp_path), PLAN, shown=False)
    assert out["accepted"] is False and "extra_reminders[0].source_id" in out["errors"][0]


def test_completed_tasks_are_never_offered_to_the_run(tmp_path):
    w = World(tmp_path)
    claude = Claude(PLAN)
    w.run(claude)
    tasks_reply = next(r for r in claude.replies if "tasks" in r)
    assert all(t["status"] != "done" for t in tasks_reply["tasks"])
    assert "include_completed" not in claude.options.mcp_servers["pa"]["tools"]["get_tasks"].input_schema["properties"]


# --- CLI ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(config, "default_data_dir", lambda: w.data)
    monkeypatch.setattr(cli, "reminders_client", lambda: w.rem)
    monkeypatch.setattr(cli, "notifier", lambda: w.note)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return w.sc.now.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    return w


def _use(monkeypatch, claude):
    monkeypatch.setattr(cli, "daily_run", functools.partial(D.daily_run, session=claude))


def test_cli_run_now_prints_the_brief(cli_world, monkeypatch):
    _use(monkeypatch, Claude({**PLAN, "extra_reminders": []}))
    out = CliRunner().invoke(cli.app, ["run", "now"])
    assert out.exit_code == 0, out.output
    assert "# Morning brief: Wednesday 2026-10-28" in out.output and "Saved briefs/2026-10-28.md." in out.output
    assert "Google Calendar isn't connected yet" in out.output  # no token before M13


def test_cli_dry_run_shows_the_changes_and_writes_nothing(cli_world, monkeypatch):
    _use(monkeypatch, Claude({**PLAN, "extra_reminders": []}))
    before = {k: v for k, v in cli_world.files().items() if k != ".run.lock"}
    out = CliRunner().invoke(cli.app, ["run", "now", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "Reminder changes (dry run, nothing written):" in out.output and "+ 2026-10-28 08:01  Morning brief ready" in out.output
    assert {k: v for k, v in cli_world.files().items() if k != ".run.lock"} == before
    assert cli_world.rem.writes() == [] and cli_world.note.sent == []


def test_cli_scheduled_failure_notifies_with_the_type_only(cli_world, monkeypatch):
    _use(monkeypatch, Claude(fail=AgentFailure(FailureKind.usage_unavailable, "overage")))
    out = CliRunner().invoke(cli.app, ["run", "scheduled"])
    assert out.exit_code == 1 and "usage_unavailable" in out.output
    assert cli_world.note.sent == [("Personal assistant", "Morning brief failed: Claude usage unavailable")]
    assert not (cli_world.data / "state.json").exists() and not list((cli_world.data / "briefs").glob("*.md"))
    (log_file,) = (cli_world.data / "logs").glob("*.jsonl")  # named by the real clock
    last = json.loads(log_file.read_text().splitlines()[-1])
    assert last["event"] == "run_end" and last["status"] == "failed"


def test_cli_manual_failure_doesnt_notify(cli_world, monkeypatch):
    _use(monkeypatch, Claude(fail=AgentFailure(FailureKind.step_limit)))
    out = CliRunner().invoke(cli.app, ["run", "now"])
    assert out.exit_code == 1 and cli_world.note.sent == []


def test_cli_scheduled_lock_held_notifies(cli_world, monkeypatch):
    _use(monkeypatch, Claude(PLAN))
    from assistant.guard import run_lock

    with run_lock(cli_world.data):
        out = CliRunner().invoke(cli.app, ["run", "scheduled"])
    assert out.exit_code == 1
    assert cli_world.note.sent == [("Personal assistant", "Morning brief failed: another run in progress")]


def test_cli_scheduled_skips_the_second_time(cli_world, monkeypatch):
    _use(monkeypatch, Claude({**PLAN, "extra_reminders": []}))
    assert CliRunner().invoke(cli.app, ["run", "scheduled"]).exit_code == 0
    out = CliRunner().invoke(cli.app, ["run", "scheduled"])
    assert out.exit_code == 0 and "already finished" in out.output


def test_brief_files_are_600_in_a_700_folder(cli_world, monkeypatch):
    _use(monkeypatch, Claude({**PLAN, "extra_reminders": []}))
    CliRunner().invoke(cli.app, ["run", "now"])
    folder = cli_world.data / "briefs"
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in folder.iterdir())
    assert config.permission_issues(cli_world.data) == []
    assert os.listdir(folder) == ["2026-10-28.md"]


# --- The brief item is never written in the past (M15) ------------------------------------------------------


def test_slow_run_moves_the_brief_item_after_the_write(tmp_path):
    # The run starts at 08:00 but its writes happen at 08:42 (e.g. a scheduled run spread over dark wakes).
    w = World(tmp_path)
    written_at = w.sc.now + timedelta(minutes=42)
    w.run(clock=lambda: written_at)
    (brief,) = [c for c in w.rem.writes() if c[0] == "create" and "Morning brief ready" in c[2]]
    assert brief[3] >= written_at + timedelta(minutes=1)
    assert brief[3] == written_at + timedelta(minutes=1) and brief[3].second == 0  # 08:42:00 → 08:43
    state = load_state(w.data)
    (key,) = [k for k, r in state.reminders.items() if r.source_type.value == "brief"]
    assert key.endswith(brief[3].isoformat()) and brief[4].endswith(f"{key}]")  # state key and marker match the new time


def test_on_time_run_keeps_the_brief_time(tmp_path):
    w = World(tmp_path)
    w.run()
    (brief,) = [c for c in w.rem.writes() if c[0] == "create" and "Morning brief ready" in c[2]]
    assert brief[3] >= w.sc.now + timedelta(minutes=1)
    assert brief[3] - w.sc.now <= timedelta(minutes=2)

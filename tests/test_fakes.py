import inspect
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from assistant.integrations.google_calendar import CalendarClient, GoogleCalendarClient, clamp_range
from assistant.integrations.notify import MacNotifier, NotificationError, Notifier
from assistant.integrations.reminders import (
    AppleRemindersClient,
    ReminderAccess,
    ReminderItem,
    ReminderNotFoundError,
    RemindersBlockedError,
    RemindersClient,
)
from assistant.logging_setup import sanitize
from assistant.state import load_state
from assistant.tasks_store import TaskStore
from tests.fakes import FakeNotifier, FakeReminders, FixtureCalendarClient
from tests.fakes import scenario as scenarios

ROOT = Path(__file__).resolve().parents[1]
BASE = scenarios.load("base")
TZ = BASE.preferences.tz
TODAY = BASE.today  # Wednesday 2026-10-28
DAY7, DAY8 = TODAY + timedelta(days=7), TODAY + timedelta(days=8)


def at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=TZ)


def window(s=BASE, start=None, end=None):
    return s.calendar_client().list_events(start or s.today, end or s.today + timedelta(days=7))


def by_id(result) -> dict:
    return {e.id: e for e in result.events}


# --- Fakes match the interfaces ----------------------------------------------------------


PAIRS = [
    (CalendarClient, [FixtureCalendarClient, GoogleCalendarClient]),
    (RemindersClient, [FakeReminders, AppleRemindersClient]),
    (Notifier, [FakeNotifier, MacNotifier]),
]


def protocol_methods(protocol) -> list[str]:
    return [n for n, v in vars(protocol).items() if callable(v) and not n.startswith("_")]


@pytest.mark.parametrize(("protocol", "classes"), PAIRS, ids=lambda x: getattr(x, "__name__", ""))
def test_fakes_and_real_clients_share_the_interface(protocol, classes):
    methods = protocol_methods(protocol)
    assert methods
    for cls in classes:
        for name in methods:
            expected = inspect.signature(getattr(protocol, name))
            actual = inspect.signature(getattr(cls, name))
            assert actual == expected, f"{cls.__name__}.{name}{actual} != {protocol.__name__}.{name}{expected}"


def test_instances_satisfy_protocols():
    assert isinstance(BASE.calendar_client(), CalendarClient)
    assert isinstance(GoogleCalendarClient(["primary"], TZ, TODAY), CalendarClient)
    assert isinstance(FakeReminders(), RemindersClient) and isinstance(AppleRemindersClient(), RemindersClient)
    assert isinstance(FakeNotifier(), Notifier) and isinstance(MacNotifier(), Notifier)


def test_real_clients_make_no_calls_yet():
    with pytest.raises(NotImplementedError, match="M13"):
        GoogleCalendarClient(["primary"], TZ, TODAY).list_events(TODAY, TODAY)
    for call in (lambda c: c.check_access(), lambda c: c.list_items(), lambda c: c.delete("x")):
        with pytest.raises(NotImplementedError, match="M14"):
            call(AppleRemindersClient())
    with pytest.raises(NotImplementedError, match="M14"):
        MacNotifier().notify("t", "m")


def test_product_code_never_imports_fakes():
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert "tests.fakes" not in text and "from tests" not in text, path


def test_reminder_functions_take_no_list_name():
    for name in protocol_methods(RemindersClient):
        params = inspect.signature(getattr(RemindersClient, name)).parameters
        assert not any("list" in p for p in params), name  # the list is fixed (S-4)


# --- Calendar fake: clamp, stripping, birthdays (FR-1–FR-3) -------------------------------------


def test_window_includes_day_7_and_excludes_day_8_and_the_past():
    ids = by_id(window())
    assert "evt020" in ids  # day 7 (2026-11-04)
    assert "evt021" not in ids and "evt022" not in ids  # day 8, all-day and timed
    assert "evt023" not in ids  # yesterday


def test_requests_are_clamped_to_the_window():
    client = BASE.calendar_client()
    wide = client.list_events(TODAY - timedelta(days=30), TODAY + timedelta(days=60))
    assert by_id(wide).keys() == by_id(window()).keys()
    assert client.requests[-1] == (TODAY - timedelta(days=30), TODAY + timedelta(days=60))  # raw request kept for the permission hook
    assert client.list_events(DAY8, DAY8 + timedelta(days=3)).events == []  # wholly outside → nothing
    assert client.list_events(TODAY - timedelta(days=5), TODAY - timedelta(days=1)).events == []
    assert clamp_range(TODAY + timedelta(days=3), TODAY + timedelta(days=1), TODAY) is None


def test_single_day_query_and_multi_day_all_day_overlap():
    sat = date(2026, 10, 31)
    ids = by_id(window(start=sat, end=sat))
    assert "evt012" in ids  # conference 10-30 → 11-01 (exclusive end) covers Saturday
    assert {"evt013", "evt014", "bdaycal01"} <= ids.keys()
    assert "evt016" not in ids  # Sunday
    assert "evt012" not in by_id(window(start=date(2026, 11, 1), end=date(2026, 11, 1)))  # end date is exclusive


def test_attendees_and_descriptions_are_stripped():
    raw_evt001 = next(e for e in BASE.calendars["primary"] if e["id"] == "evt001")
    assert raw_evt001["attendees"] and raw_evt001["description"]  # the fixture does contain them
    result = window()
    for event in result.events:
        assert set(event.model_dump()) == {"id", "calendar", "title", "start", "end", "all_day", "location", "is_birthday", "transparency"}
    dumped = result.model_dump_json()
    for secret in ("example.com/evt001", "dana.kim@example.com", "sam.rivera@example.com", "Dial-in", "insurance card"):
        assert secret not in dumped
    assert by_id(result)["evt001"].location == "Room Fern (fake)"


def test_cancelled_events_are_dropped():
    assert "evt010" not in by_id(window(start=date(2026, 10, 29), end=date(2026, 10, 29)))


def test_birthdays_detected_by_all_three_paths():
    events = by_id(window())
    assert events["bdaycal01"].is_birthday and events["bdaycal01"].calendar == "birthdays"  # birthdays calendar
    assert events["bdaytype01"].is_birthday and "birthday" not in events["bdaytype01"].title.lower()  # event type
    assert events["evt019"].is_birthday and events["evt019"].all_day  # all-day with "birthday" in title
    assert [e.id for e in events.values() if e.is_birthday] == ["bdaycal01", "bdaytype01", "evt019"]


def test_events_sorted_and_all_day_typed():
    events = window().events
    keys = [(e.start if isinstance(e.start, datetime) else datetime.combine(e.start, datetime.min.time(), TZ)) for e in events]
    assert keys == sorted(keys)
    for e in events:
        assert e.all_day == (not isinstance(e.start, datetime))


def test_outage_and_missing_birthdays_calendar():
    outage = window(scenarios.load("calendar_outage"))
    assert outage.available is False and outage.events == [] and outage.error
    missing = window(scenarios.load("birthdays_missing"))
    assert missing.available and missing.skipped_calendars == ["birthdays"]
    assert "bdaycal01" not in by_id(missing) and "bdaytype01" in by_id(missing)  # primary still read


# --- Reminders fake (FR-23, S-4, S-14) ------------------------------------------------------------


def mine(**kw) -> ReminderItem:
    return ReminderItem(reminder_id="x-apple-reminder://00000000-0000-0000-0000-000000000001", title="Mine, made by hand", **kw)


def test_reminders_fake_records_writes():
    r = FakeReminders(items=[mine()])
    rid = r.create("Team sync in 1 hour", at("2026-10-28", "08:00"), "[pa:meeting:evt001:2026-10-28T08:00:00-07:00]")
    r.update(rid, "Team sync in 1 hour (moved)", at("2026-10-28", "09:00"), "[pa:x]")
    other = r.create("Another", at("2026-10-29", "08:00"), "")
    r.delete(other)
    assert [c[0] for c in r.writes()] == ["create", "update", "create", "delete"]
    assert r.items[rid].title.endswith("(moved)") and other not in r.items
    assert r.items[mine().reminder_id] == mine()  # untouched
    assert sanitize({"reminder_id": rid}) == {"reminder_id": rid}  # ID format matches the log allowlist


@pytest.mark.parametrize("access", [ReminderAccess.access_denied, ReminderAccess.list_missing, ReminderAccess.not_on_my_mac])
def test_reminders_fake_blocked_writes_nothing(access):
    r = FakeReminders(access=access, items=[mine()])
    assert r.check_access() is access
    for call in (
        lambda: r.list_items(),
        lambda: r.create("x", at("2026-10-28", "08:00"), ""),
        lambda: r.update(mine().reminder_id, "x", at("2026-10-28", "08:00"), ""),
        lambda: r.delete(mine().reminder_id),
    ):
        with pytest.raises(RemindersBlockedError) as e:
            call()
        assert e.value.reason is access
    assert r.writes() == [] and r.items == {mine().reminder_id: mine()}


def test_reminders_fake_rejects_unknown_ids_and_naive_times():
    r = FakeReminders()
    with pytest.raises(ReminderNotFoundError):
        r.delete("x-apple-reminder://00000000-0000-0000-0000-00000000dead")
    with pytest.raises(ReminderNotFoundError):
        r.update("nope", "x", at("2026-10-28", "08:00"), "")
    with pytest.raises(ValueError, match="timezone-aware"):
        r.create("x", datetime(2026, 10, 28, 8), "")
    assert r.writes() == []


def test_notifier_fake():
    n = FakeNotifier()
    n.notify("Morning brief ready", "3 meetings, 2 tasks due")
    assert n.sent == [("Morning brief ready", "3 meetings, 2 tasks due")]
    with pytest.raises(NotificationError):
        FakeNotifier(fail=True).notify("x", "y")


# --- Fixtures load and validate ---------------------------------------------------------------


@pytest.mark.parametrize("name", scenarios.names())
def test_fixture_loads_and_validates(name, tmp_path):
    s = scenarios.load(name)
    assert s.now.tzinfo is not None and s.now.utcoffset() == s.now.astimezone(s.preferences.tz).utcoffset()
    data_dir = s.write_data_dir(tmp_path / "data")
    store = TaskStore(data_dir, s.preferences.tz)
    open_tasks, completed = store.load_open(), store.load_completed()
    assert all(t.created_at <= t.updated_at <= s.now for t in open_tasks)
    assert all(t.completed_at <= s.now for ts in completed.values() for t in ts)
    load_state(data_dir)  # validates if present
    window(s)  # every event normalises
    for raw in (e for events in s.calendars.values() for e in events):
        assert sanitize({"event_id": raw["id"]}) == {"event_id": raw["id"]}, raw["id"]
    for t in open_tasks + [t for ts in completed.values() for t in ts]:
        assert sanitize({"task_id": t.id}) == {"task_id": t.id}, t.id


def test_base_preferences_are_the_example():
    assert (ROOT / "evals/fixtures/base/preferences.yaml").read_text() == (ROOT / "examples/preferences.yaml").read_text()


def test_base_scenario_shape():
    assert BASE.now == at("2026-10-28", "08:00") and BASE.now.strftime("%A") == "Wednesday"
    assert 15 <= len(window().events) <= 25
    assert {t["category"] for t in BASE.tasks} == set(BASE.preferences.categories)
    assert {t["priority"] for t in BASE.tasks} == {"high", "medium", "low"}
    assert 10 <= len(BASE.tasks) <= 15 and sum(len(v) for v in BASE.completed.values()) >= 3


def test_write_data_dir_modes(tmp_path):
    path = BASE.write_data_dir(tmp_path / "data")
    assert oct(path.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in path.iterdir() if p.is_file())


# --- Every EVALS §3.2 edge case is present ----------------------------------------------------------


def _deadline(t) -> str:
    return t.get("deadline", "")


def check_all_day(s):
    events = by_id(window(s))
    assert events["evt009"].all_day and (events["evt009"].end - events["evt009"].start).days == 1
    assert events["evt012"].all_day and (events["evt012"].end - events["evt012"].start).days == 2


def check_birthdays_three_paths(s):
    test_birthdays_detected_by_all_three_paths()


def check_overdue(s):
    deadlines = {_deadline(t) for t in s.tasks}
    assert str(s.today - timedelta(days=1)) in deadlines and str(s.today - timedelta(days=7)) in deadlines


def check_due_today_time_and_date(s):
    today = str(s.today)
    assert any(_deadline(t) == today for t in s.tasks)
    assert any(_deadline(t).startswith(today + "T") for t in s.tasks)


def check_quiet_hours(s):
    # A task deadline at 07:30: the 1-hour-before reminder (06:30) falls in quiet hours.
    assert any(_deadline(t).endswith("T07:30:00-07:00") for t in s.tasks)


def check_quiet_hours_0730_0630(s):
    starts = {e.start.strftime("%H:%M") for e in window(s).events if not e.all_day}
    assert {"07:30", "06:30"} <= starts


def check_dst(s):
    sunday = [e for e in window(s, date(2026, 11, 1), date(2026, 11, 1)).events if not e.all_day]
    assert {e.start.utcoffset() for e in sunday} == {timedelta(hours=-8)}
    assert any(e.start.hour < 3 for e in sunday)  # near the repeated 01:00–02:00 hour
    saturday = [e for e in window(s, date(2026, 10, 31), date(2026, 10, 31)).events if not e.all_day]
    assert {e.start.utcoffset() for e in saturday} == {timedelta(hours=-7)}


def check_back_to_back_full_day(s):
    thu = sorted((e for e in window(s, date(2026, 10, 29), date(2026, 10, 29)).events if not e.all_day), key=lambda e: e.start)
    assert all(a.end == b.start for a, b in zip(thu, thu[1:]))
    assert thu[0].start.strftime("%H:%M") == "09:00" and thu[-1].end.strftime("%H:%M") == "18:00"
    today = sorted((e for e in window(s, s.today, s.today).events if not e.all_day), key=lambda e: e.start)
    assert today[0].end == today[1].start  # back-to-back pair


def check_calendar_outage(s):
    assert window(s).available is False


def check_birthdays_missing(s):
    assert window(s).skipped_calendars == ["birthdays"]


def check_moved_cancelled(s):
    before, after = by_id(window(BASE)), by_id(window(s))
    assert "evt003" in before and "evt003" not in after  # cancelled
    assert before["evt004"].start != after["evt004"].start  # moved
    assert "t-0102" not in {t["id"] for t in s.tasks}  # completed


def check_empty_day(s):
    assert window(s, s.today, s.today).events == []
    assert not any(_deadline(t)[:10] <= str(s.today) for t in s.tasks if _deadline(t))


def check_window_edges(s):
    test_window_includes_day_7_and_excludes_day_8_and_the_past()


def check_injection(s):
    canary = scenarios.json.loads((scenarios.FIXTURES / s.name / "scenario.json").read_text())["canary"]
    blob = scenarios.json.dumps([s.calendars, s.tasks, s.completed, s.state])
    assert canary in blob


def check_applescript_title(s):
    assert any('"' in e.title and "do shell script" in e.title for e in window(s).events)


def check_extras_off(s):
    assert s.preferences.reminders.extra_reminders.allowed is False


def check_extras_cap(s):
    assert s.preferences.reminders.extra_reminders.max_per_item == 1
    interviews = [e for e in window(s).events if "interview" in e.title.lower()]
    assert len(interviews) == 2  # two items that each deserve an extra, so the cap matters


def check_routine(s):
    assert window(s).events and not any("interview" in e.title.lower() or e.all_day for e in window(s).events)
    assert {t["priority"] for t in s.tasks} <= {"low", "medium"}


EDGE_CASES = {
    "all_day": check_all_day,
    "birthdays_three_paths": check_birthdays_three_paths,
    "overdue": check_overdue,
    "due_today_time_and_date": check_due_today_time_and_date,
    "quiet_hours": check_quiet_hours,
    "quiet_hours_0730_0630": check_quiet_hours_0730_0630,
    "dst": check_dst,
    "back_to_back_full_day": check_back_to_back_full_day,
    "calendar_outage": check_calendar_outage,
    "birthdays_missing": check_birthdays_missing,
    "moved_cancelled": check_moved_cancelled,
    "empty_day": check_empty_day,
    "window_edges": check_window_edges,
    "injection": check_injection,
    "applescript_title": check_applescript_title,
    "extras_off": check_extras_off,
    "extras_cap": check_extras_cap,
    "routine": check_routine,
}


def test_every_edge_case_is_tagged_by_some_fixture():
    tagged = {tag for name in scenarios.names() for tag in scenarios.load(name).tags}
    assert tagged == set(EDGE_CASES)


@pytest.mark.parametrize("name", scenarios.names())
def test_fixture_tags_are_true(name):
    s = scenarios.load(name)
    for tag in s.tags:
        EDGE_CASES[tag](s)


def test_injection_fixtures_cover_every_place_with_unique_canaries():
    injected = [scenarios.load(n) for n in scenarios.names() if "injection" in scenarios.load(n).tags]
    canaries = [scenarios.json.loads((scenarios.FIXTURES / s.name / "scenario.json").read_text())["canary"] for s in injected]
    assert len(injected) == 7 and len(set(canaries)) == 7
    assert {s.name.removeprefix("injection_") for s in injected} == {
        "event_title", "event_location", "task_title", "task_description", "completed_task", "stored_state", "user_note",
    }  # fmt: skip


def test_fixture_files_parse():
    for path in (ROOT / "evals/fixtures").rglob("*"):
        if path.suffix == ".json":
            scenarios.json.loads(path.read_text())
        elif path.suffix == ".yaml":
            yaml.safe_load(path.read_text())


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_clean_on_fixtures():
    result = subprocess.run(
        ["gitleaks", "dir", str(ROOT / "evals/fixtures"), "--no-banner", "--config", str(ROOT / ".gitleaks.toml"), "--exit-code", "1"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr

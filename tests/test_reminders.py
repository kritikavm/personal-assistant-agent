"""M14 Layer A: the real Reminders client and notifier, with osascript replaced (S-4, S-14, S-18, FR-23)."""

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from assistant import cli, config
from assistant.integrations import notify, reminders
from assistant.integrations.notify import MacNotifier, NotificationError
from assistant.integrations.reminders import (
    AGENT_LIST,
    ALLOWED_ACCOUNTS,
    AppleRemindersClient,
    ReminderAccess,
    ReminderNotFoundError,
    RemindersBlockedError,
    RemindersScriptError,
    clean,
    parse_items,
)
from tests.fakes import FakeReminders

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "check": reminders.CHECK_SCRIPT,
    "list": reminders.LIST_SCRIPT,
    "create": reminders.CREATE_SCRIPT,
    "update": reminders.UPDATE_SCRIPT,
    "delete": reminders.DELETE_SCRIPT,
}
RID = "x-apple-reminder://0F3C2B9A-1111-4222-8333-444455556666"
EVIL = 'Lunch" & (do shell script "touch /tmp/pwned") & " $(touch /tmp/pwned2) `touch /tmp/pwned3`; tell application "Finder" to activate'


class Recorder:
    """Stands in for osascript: records each argument list, answers from a queue."""

    def __init__(self, *answers):
        self.calls: list[list[str]] = []
        self.answers = list(answers) or [(0, "ok", "")]

    def __call__(self, args):
        self.calls.append(list(args))
        code, out, err = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return subprocess.CompletedProcess(args, code, out + "\n" if out else "", err)

    def script(self, i=-1):
        return self.calls[i][2]

    def values(self, i=-1):
        return self.calls[i][3:]


def record(rid, title, notes, completed, date):
    return "\x1f".join([rid, title, notes, completed, date])


def when(h=9, m=30):
    return datetime(2026, 10, 28, h, m, tzinfo=ZoneInfo("America/Los_Angeles"))


# --- Fixed scripts (S-18) ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_is_fixed_and_reads_argv(name):
    script = SCRIPTS[name]
    assert "on run argv" in script
    assert "do shell script" not in script and "run script" not in script


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_reaches_only_our_list_through_the_guard(name):
    # Safety test 3 (code side): one fixed name, allowed accounts only, and a unique match, checked in the
    # script itself on every call; reminders are only ever addressed inside `theList`.
    script = SCRIPTS[name]
    assert f'every list whose name is "{AGENT_LIST}"' in script and script.count("whose name is") == 1
    assert 'if (count of found) > 1 then error "list_not_unique" number 9002' in script
    assert '{"On My Mac", "iCloud"} does not contain (name of (container of theList))' in script
    assert "agentList()" in script.split("on run argv", 1)[1]
    for line in script.splitlines():
        if re.search(r"\breminders? of\b|\breminder of\b", line):
            assert re.search(r"\breminders? of theList\b", line), line


def test_allowed_accounts_are_exactly_on_my_mac_and_icloud():
    assert ALLOWED_ACCOUNTS == ("On My Mac", "iCloud")


def test_no_other_list_name_appears_in_product_code():
    for path in (ROOT / "src").rglob("*.py"):
        names = set(re.findall(r'\blist "([^"]+)"', path.read_text()))
        assert names <= {AGENT_LIST, "{AGENT_LIST}"}, (path, names)  # the literal or the constant in an f-string


def test_osascript_gets_an_argument_list_never_a_shell():
    rec = Recorder((0, RID, ""))
    AppleRemindersClient(rec).create("Standup", when(), "[pa:default:meeting:e1:2026-10-28T09:30:00-07:00]")
    args = rec.calls[0]
    assert args[:3] == ["/usr/bin/osascript", "-e", reminders.CREATE_SCRIPT]
    src = (ROOT / "src/assistant/integrations/reminders.py").read_text() + (ROOT / "src/assistant/integrations/notify.py").read_text()
    assert "shell=True" not in src and "os.system" not in src


def test_malicious_title_is_only_an_argument():
    # Safety test 6 (code side): the script text never changes; the title is passed as one argv item, verbatim.
    rec = Recorder((0, RID, ""))
    client = AppleRemindersClient(rec)
    client.create(EVIL, when(), EVIL)
    client.update(RID, EVIL, when(), EVIL)
    assert rec.script(0) == reminders.CREATE_SCRIPT and rec.values(0)[:2] == [EVIL, EVIL]
    assert rec.script(1) == reminders.UPDATE_SCRIPT and rec.values(1)[:3] == [RID, EVIL, EVIL]
    assert EVIL not in rec.script(0) and EVIL not in rec.script(1)


def test_notification_text_is_only_an_argument():
    rec = Recorder((0, "", ""))
    MacNotifier(rec).notify(EVIL, EVIL + " message")
    assert rec.calls[0] == ["/usr/bin/osascript", "-e", notify.NOTIFY_SCRIPT, EVIL, EVIL + " message"]
    assert "on run argv" in notify.NOTIFY_SCRIPT and "do shell script" not in notify.NOTIFY_SCRIPT


# --- Values ------------------------------------------------------------------------------------------------


@pytest.fixture
def mac_in_tokyo(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_dates_go_as_mac_local_components(mac_in_tokyo):
    # 09:30 in Los Angeles is 01:30 the next day on a Mac set to Tokyo time: AppleScript dates are Mac-local.
    rec = Recorder((0, RID, ""))
    AppleRemindersClient(rec).create("t", when(9, 30), "n")
    assert rec.values()[2:] == ["2026", "10", "29", "1", "30", "0"]


def test_read_back_dates_are_mac_local(mac_in_tokyo):
    (item,) = parse_items(record(RID, "t", "n", "false", "2026-10-29-1-30-0"))
    assert item.fire_at == when(9, 30)


def test_naive_time_is_refused_before_osascript():
    rec = Recorder()
    with pytest.raises(ValueError):
        AppleRemindersClient(rec).create("t", datetime(2026, 10, 28, 9), "n")
    assert rec.calls == []


def test_control_characters_are_removed_but_newlines_kept():
    assert clean("a\x1fb\x1ec\x00d\te\nf\x7f") == "abcd\te\nf"
    rec = Recorder((0, RID, ""))
    AppleRemindersClient(rec).create("Call\x1e Mum", when(), "line1\nline2\x1f")
    assert rec.values()[:2] == ["Call Mum", "line1\nline2"]


def test_create_returns_the_new_id():
    assert AppleRemindersClient(Recorder((0, RID, ""))).create("t", when(), "n") == RID


# --- Reading the list --------------------------------------------------------------------------------------


def test_parse_items():
    out = "\x1e".join(
        [
            record(RID, "Standup", "[pa:x]\nmore", "false", "2026-10-28-9-30-0"),
            record(RID.replace("0F", "AA"), "Mine, no date", "", "true", ""),
        ]
    )
    a, b = parse_items(out)
    assert (a.reminder_id, a.title, a.notes, a.completed) == (RID, "Standup", "[pa:x]\nmore", False)
    assert a.fire_at == datetime(2026, 10, 28, 9, 30).astimezone()
    assert b.completed and b.fire_at is None and b.notes == ""
    assert parse_items("") == []


@pytest.mark.parametrize("bad", ["only\x1ftwo", record(RID, "t", "n", "false", "2026-13-40-9-0-0"), record(RID, "t", "n", "false", "soon")])
def test_malformed_list_output_fails_the_whole_read(bad):
    with pytest.raises(RemindersScriptError):
        parse_items(bad)


def test_list_items_checks_access_first():
    rec = Recorder((0, "list_missing", ""))
    with pytest.raises(RemindersBlockedError) as e:
        AppleRemindersClient(rec).list_items()
    assert e.value.reason is ReminderAccess.list_missing and len(rec.calls) == 1


def test_list_items_after_ok():
    rec = Recorder((0, "ok", ""), (0, record(RID, "t", "n", "false", ""), ""))
    items = AppleRemindersClient(rec).list_items()
    assert [i.reminder_id for i in items] == [RID] and rec.script(1) == reminders.LIST_SCRIPT


# --- Errors ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["ok", "list_missing", "account_not_allowed", "list_not_unique"])
def test_check_access_reports_the_script_answer(answer):
    assert AppleRemindersClient(Recorder((0, answer, ""))).check_access() is ReminderAccess(answer)


DENIED = (1, "", "execution error: Not authorized to send Apple events to Reminders. (-1743)")


def test_missing_permission_is_access_denied():
    client = AppleRemindersClient(Recorder(DENIED))
    assert client.check_access() is ReminderAccess.access_denied
    with pytest.raises(RemindersBlockedError) as e:
        client.create("t", when(), "n")
    assert e.value.reason is ReminderAccess.access_denied


@pytest.mark.parametrize("err", ["execution error: Can't get reminder 1 whose id = \"x\". Invalid index. (-1719)", "execution error: Can't get list. (-1728)"])
def test_unknown_id_is_not_found(err):
    client = AppleRemindersClient(Recorder((1, "", err)))
    with pytest.raises(ReminderNotFoundError):
        client.update(RID, "t", when(), "n")
    with pytest.raises(ReminderNotFoundError):
        client.delete(RID)


@pytest.mark.parametrize(
    "code, reason",
    [("9001", ReminderAccess.list_missing), ("9002", ReminderAccess.list_not_unique), ("9003", ReminderAccess.account_not_allowed)],
)
def test_guard_errors_during_a_write_block_it(code, reason):
    client = AppleRemindersClient(Recorder((1, "", f"execution error: {reason.value} ({code})")))
    for call in (lambda: client.create("t", when(), "n"), lambda: client.update(RID, "t", when(), "n"), lambda: client.delete(RID)):
        with pytest.raises(RemindersBlockedError) as e:
            call()
        assert e.value.reason is reason


def test_anything_else_is_a_script_error():
    with pytest.raises(RemindersScriptError):
        AppleRemindersClient(Recorder((1, "", "execution error: odd (-2700)"))).check_access()


def test_unknown_check_answer_is_refused():
    with pytest.raises(ValueError):
        AppleRemindersClient(Recorder((0, "maybe", ""))).check_access()


def test_notifier_failures_become_notification_error():
    with pytest.raises(NotificationError):
        MacNotifier(Recorder((1, "", "boom"))).notify("t", "m")

    def timeout(args):
        raise subprocess.TimeoutExpired(args, 20)

    with pytest.raises(NotificationError):
        MacNotifier(timeout).notify("t", "m")


def test_default_runners_are_blocked_in_tests():
    with pytest.raises(AssertionError, match="osascript"):
        AppleRemindersClient().check_access()
    with pytest.raises(AssertionError, match="osascript"):
        MacNotifier().notify("t", "m")


# --- doctor --------------------------------------------------------------------------------------------------


@pytest.fixture
def doctor_dir(tmp_path, monkeypatch):
    import shutil

    d = tmp_path / "data"
    config.ensure_data_dir(d)
    shutil.copy(ROOT / "examples" / "preferences.yaml", d / "preferences.yaml")
    (d / "preferences.yaml").chmod(0o600)
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    return d


def doctor_with(monkeypatch, client):
    monkeypatch.setattr(cli, "reminders_client", lambda: client)
    return CliRunner().invoke(cli.app, ["doctor"])


@pytest.mark.parametrize(
    "access, text",
    [
        (ReminderAccess.access_denied, "Privacy & Security → Automation"),
        (ReminderAccess.list_missing, 'no list named "Agent Reminders"'),
        (ReminderAccess.account_not_allowed, "account other than iCloud or On My Mac"),
        (ReminderAccess.list_not_unique, "more than one list"),
    ],
)
def test_doctor_reminders_blocked(doctor_dir, monkeypatch, access, text):
    out = doctor_with(monkeypatch, FakeReminders(access))
    assert "WARN  Reminders: writes are blocked: " in out.output and text in out.output


def test_doctor_reminders_ok_and_state_missing_with_marked_items(doctor_dir, monkeypatch):
    from assistant.integrations.reminders import ReminderItem

    item = ReminderItem(reminder_id=RID, title="t", notes="[pa:default:meeting:e1:2026-10-28T09:30:00-07:00]")
    out = doctor_with(monkeypatch, FakeReminders(items=[item]))
    assert 'OK    Reminders: "Agent Reminders" found in an allowed account (iCloud or On My Mac; S-14) (1 item(s))' in out.output
    assert "WARN  state.json: state_missing" in out.output and "assistant state rebuild" in out.output
    assert not (doctor_dir / "backups").exists() or not list((doctor_dir / "backups").iterdir())


def test_doctor_invalid_state_is_reported_but_not_copied(doctor_dir, monkeypatch):
    (doctor_dir / "state.json").write_text("{not json")
    (doctor_dir / "state.json").chmod(0o600)
    out = doctor_with(monkeypatch, FakeReminders())
    assert "WARN  state.json: state_invalid" in out.output
    backups = doctor_dir / "backups"
    assert not backups.exists() or not list(backups.iterdir())


def test_doctor_osascript_trouble_is_a_warning(doctor_dir, monkeypatch):
    out = doctor_with(monkeypatch, AppleRemindersClient(Recorder((1, "", "odd (-2700)"))))
    assert "WARN  Reminders: couldn't check (RemindersScriptError)" in out.output and out.exit_code == 0


class Timeouts:
    """check_access times out `n` times, then answers."""

    def __init__(self, n, then=ReminderAccess.list_missing):
        self.n, self.then, self.calls = n, then, 0

    def check_access(self):
        self.calls += 1
        if self.calls <= self.n:
            raise subprocess.TimeoutExpired(["osascript"], 60)
        return self.then


def test_doctor_retries_once_after_a_timeout(doctor_dir, monkeypatch):
    client = Timeouts(1)
    out = doctor_with(monkeypatch, client)
    assert client.calls == 2 and "Trying once more" in out.output
    assert 'WARN  Reminders: writes are blocked: no list named "Agent Reminders"' in out.output


def test_doctor_gives_up_after_the_second_timeout(doctor_dir, monkeypatch):
    client = Timeouts(5)
    out = doctor_with(monkeypatch, client)
    assert client.calls == 2 and "WARN  Reminders: couldn't check (TimeoutExpired)" in out.output and out.exit_code == 0


def test_runs_do_not_retry_a_timeout():
    import inspect

    from assistant.flows import daily_run

    assert "_retry_timeout_once" not in inspect.getsource(daily_run)

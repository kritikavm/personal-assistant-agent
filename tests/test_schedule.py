"""M15 Layer A: the launchd plist, schedule install/uninstall and doctor's schedule line (FR-12), no launchctl."""

import plistlib
import subprocess
from datetime import time

import pytest
from typer.testing import CliRunner

from assistant import cli, config, schedule
from tests.fakes import FakeReminders


class Launchctl:
    """Records launchctl calls; `print` answers `loaded`."""

    def __init__(self, bootstrap_code=0, loaded=True):
        self.calls, self.bootstrap_code, self.is_loaded = [], bootstrap_code, loaded

    def __call__(self, args):
        self.calls.append(list(args))
        code = {"bootstrap": self.bootstrap_code, "print": 0 if self.is_loaded else 113, "bootout": 0}[args[0]]
        return subprocess.CompletedProcess(args, code, "", "Bootstrap failed: 5: Input/output error" if code else "")


def test_rendered_plist(tmp_path):
    plist = plistlib.loads(schedule.render(time(7, 5), tmp_path / "data"))
    assert plist["Label"] == "com.assistant.daily"
    assert plist["ProgramArguments"] == [str(schedule.PROGRAM), "run", "scheduled"]
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    assert plist["WorkingDirectory"] == str(schedule.REPO)
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}  # nothing else, no billing vars (S-7)
    assert plist["Umask"] == 0o077 and plist["RunAtLoad"] is False
    assert plist["StandardOutPath"] == "/dev/null"  # the brief isn't copied anywhere else
    assert plist["StandardErrorPath"] == str(tmp_path / "data" / "logs" / "launchd.err")


def test_paths_are_xml_escaped(tmp_path):
    plist = plistlib.loads(schedule.render(time(8, 0), tmp_path / "a&b<c>"))
    assert plist["StandardErrorPath"].startswith(str(tmp_path / "a&b<c>"))


def test_install_writes_644_and_reloads(tmp_path):
    lc = Launchctl()
    path = schedule.install(time(8, 0), tmp_path / "data", home=tmp_path, launchctl=lc)
    assert path == tmp_path / "Library/LaunchAgents/com.assistant.daily.plist"
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert [c[0] for c in lc.calls] == ["bootout", "bootstrap"] and lc.calls[1][2] == str(path)
    assert schedule.installed(tmp_path) == schedule.Installed(8, 0, str(schedule.PROGRAM))


def test_install_creates_the_stderr_file_as_600(tmp_path):
    data = tmp_path / "data"
    schedule.install(time(8, 0), data, home=tmp_path, launchctl=Launchctl())
    err = data / "logs" / "launchd.err"
    assert err.exists() and oct(err.stat().st_mode & 0o777) == "0o600"
    err.chmod(0o644)  # e.g. created by launchd before this fix
    schedule.install(time(8, 0), data, home=tmp_path, launchctl=Launchctl())
    assert oct(err.stat().st_mode & 0o777) == "0o600"


def test_failed_bootstrap_is_an_error(tmp_path):
    with pytest.raises(schedule.ScheduleError, match="bootstrap failed"):
        schedule.install(time(8, 0), tmp_path / "data", home=tmp_path, launchctl=Launchctl(bootstrap_code=5))


def test_uninstall(tmp_path):
    lc = Launchctl()
    assert schedule.uninstall(home=tmp_path, launchctl=lc) is False
    schedule.install(time(8, 0), tmp_path / "data", home=tmp_path, launchctl=lc)
    assert schedule.uninstall(home=tmp_path, launchctl=lc) is True
    assert not schedule.plist_path(tmp_path).exists() and lc.calls[-1][0] == "bootout"


def test_mismatch():
    ok = schedule.Installed(8, 0, str(schedule.PROGRAM))
    assert schedule.mismatch(ok, time(8, 0)) == []
    assert "runs at 08:00 but morning_brief.time is 07:30" in schedule.mismatch(ok, time(7, 30))[0]
    moved = schedule.Installed(8, 0, "/old/place/.venv/bin/assistant")
    assert "not this project's" in schedule.mismatch(moved, time(8, 0))[0]


# --- CLI and doctor ------------------------------------------------------------------------------------------


@pytest.fixture
def data(tmp_path, monkeypatch):
    import shutil

    d = tmp_path / "data"
    config.ensure_data_dir(d)
    shutil.copy(schedule.REPO / "examples" / "preferences.yaml", d / "preferences.yaml")
    (d / "preferences.yaml").chmod(0o600)
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    monkeypatch.setattr(cli, "reminders_client", FakeReminders)
    return d


def test_cli_install_and_uninstall(data, monkeypatch, no_launchd):
    lc = Launchctl()
    monkeypatch.setattr(schedule, "run_launchctl", lc)
    out = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert out.exit_code == 0, out.output
    assert "every day at 08:00" in out.output and schedule.plist_path(no_launchd).exists()
    assert '"event": "schedule_installed"' in "".join(p.read_text() for p in (data / "logs").glob("*.jsonl"))
    out = CliRunner().invoke(cli.app, ["schedule", "uninstall"])
    assert out.exit_code == 0 and "Schedule removed" in out.output and not schedule.plist_path(no_launchd).exists()


def test_cli_install_failure(data, monkeypatch):
    monkeypatch.setattr(schedule, "run_launchctl", Launchctl(bootstrap_code=5))
    out = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert out.exit_code == 1 and "bootstrap failed" in out.output


def doctor_line(monkeypatch, lc=None):
    if lc:
        monkeypatch.setattr(schedule, "run_launchctl", lc)
    out = CliRunner().invoke(cli.app, ["doctor"])
    return [line for line in out.output.splitlines() if "Schedule:" in line]


def test_doctor_not_installed(data, monkeypatch):
    assert doctor_line(monkeypatch) == ["WARN  Schedule: not installed, so the morning brief won't run on its own; run `assistant schedule install`"]


def test_doctor_ok(data, monkeypatch, no_launchd):
    schedule.install(time(8, 0), data, home=no_launchd, launchctl=Launchctl())
    assert doctor_line(monkeypatch, Launchctl()) == ["OK    Schedule: launchd runs `assistant run scheduled` daily at 08:00"]


def test_doctor_time_changed(data, monkeypatch, no_launchd):
    schedule.install(time(7, 0), data, home=no_launchd, launchctl=Launchctl())
    (line,) = doctor_line(monkeypatch, Launchctl())
    assert line.startswith("WARN  Schedule: it runs at 07:00 but morning_brief.time is 08:00") and "schedule install" in line


def test_doctor_not_loaded(data, monkeypatch, no_launchd):
    schedule.install(time(8, 0), data, home=no_launchd, launchctl=Launchctl())
    assert doctor_line(monkeypatch, Launchctl(loaded=False)) == ["WARN  Schedule: installed but not loaded in launchd; run `assistant schedule install`"]


def test_doctor_unreadable_plist(data, monkeypatch, no_launchd):
    path = schedule.plist_path(no_launchd)
    path.parent.mkdir(parents=True)
    path.write_text("not a plist")
    (line,) = doctor_line(monkeypatch)
    assert line.startswith("WARN  Schedule: the installed plist can't be read")


def test_template_has_no_secrets_or_personal_values():
    text = schedule.TEMPLATE.read_text()
    assert "${" in text and "/Users/" not in text
    assert "ANTHROPIC" not in text and "API_KEY" not in text and "TOKEN" not in text.upper()

import os
import shutil
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from assistant import config
from assistant.cli import app

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_prefs(path: Path, data: dict | str) -> Path:
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def example() -> dict:
    return yaml.safe_load((EXAMPLES / "preferences.yaml").read_text())


# --- Preferences errors name the field ---------------------------------------------


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda d: d.update(timezone="Mars/Olympus"), "timezone"),
        (lambda d: d["morning_brief"].update(time="8am"), "morning_brief.time"),
        (lambda d: d["morning_brief"].update(time="25:00"), "morning_brief.time"),
        (lambda d: d["working_hours"].update(start="19:00"), "working_hours"),
        (lambda d: d["reminders"]["meetings"].update(before="soon"), "reminders.meetings.before"),
        (lambda d: d["reminders"]["meetings"].update(before="0m"), "reminders.meetings.before"),
        (lambda d: d["reminders"]["meetings"].update(before="8d"), "reminders.meetings.before"),
        (lambda d: d["reminders"]["extra_reminders"].update(max_per_item=50), "reminders.extra_reminders.max_per_item"),
        (lambda d: d.update(default_priority="urgent"), "default_priority"),
        (lambda d: d.update(categories=[]), "categories"),
        (lambda d: d.update(categories=["job", "Job"]), "categories"),
        (lambda d: d["quiet_hours"].update(end="22:00"), "quiet_hours"),
        (lambda d: d.pop("timezone"), "timezone"),
        (lambda d: d.update(surprise=True), "surprise"),
    ],
)
def test_invalid_preferences_name_the_field(tmp_path, change, field):
    data = example()
    change(data)
    with pytest.raises(config.PreferencesError) as e:
        config.load_preferences(write_prefs(tmp_path / "preferences.yaml", data))
    assert any(p.startswith(f"{field}:") for p in e.value.problems), e.value.problems


def test_error_shows_the_bad_value(tmp_path):
    data = example()
    data["timezone"] = "Mars/Olympus"
    with pytest.raises(config.PreferencesError) as e:
        config.load_preferences(write_prefs(tmp_path / "p.yaml", data))
    assert "Mars/Olympus" in e.value.problems[0]


def test_unquoted_time_gets_a_hint(tmp_path):
    text = (EXAMPLES / "preferences.yaml").read_text().replace('evening_before: "20:00"', "evening_before: 20:00")
    with pytest.raises(config.PreferencesError) as e:
        config.load_preferences(write_prefs(tmp_path / "p.yaml", text))
    assert any(p.startswith("reminders.birthdays.evening_before:") and "quotes" in p for p in e.value.problems)


def test_missing_file(tmp_path):
    with pytest.raises(config.PreferencesError, match="not found"):
        config.load_preferences(tmp_path / "preferences.yaml")


@pytest.mark.parametrize("text", ["timezone: [unclosed", "- just\n- a list\n"])
def test_not_yaml_mapping(tmp_path, text):
    with pytest.raises(config.PreferencesError):
        config.load_preferences(write_prefs(tmp_path / "p.yaml", text))


# --- Data folder and permissions (S-10) ----------------------------------------------


def test_ensure_data_dir_creates_700(tmp_path):
    data_dir = tmp_path / "data"
    created = config.ensure_data_dir(data_dir)
    assert data_dir in created
    for path in (data_dir, *(data_dir / name for name in config.SUBDIRS)):
        assert path.is_dir() and mode(path) == 0o700
    assert config.permission_issues(data_dir) == []


def test_ensure_data_dir_leaves_existing_folders_alone(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)
    config.ensure_data_dir(data_dir)
    assert mode(data_dir) == 0o755  # reported, not silently changed


def test_permission_issues_flags_loose_file_and_folder(tmp_path):
    data_dir = tmp_path / "data"
    config.ensure_data_dir(data_dir)
    loose_file = data_dir / "tasks.json"
    loose_file.write_text("[]")
    os.chmod(loose_file, 0o644)
    os.chmod(data_dir / "logs", 0o755)
    ok_file = data_dir / "state.json"
    ok_file.write_text("{}")
    os.chmod(ok_file, 0o600)

    found = {i.path: (i.mode, i.expected) for i in config.permission_issues(data_dir)}
    assert found == {data_dir / "logs": (0o755, 0o700), loose_file: (0o644, 0o600)}


# --- iCloud-synced location -----------------------------------------------------------


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Developer", "Library/Mobile Documents/com~apple~CloudDocs"):
        (home / name).mkdir(parents=True)
    return home


def sync_desktop_and_documents(home: Path) -> None:
    for name in ("Desktop", "Documents"):
        (home / "Library/Mobile Documents/com~apple~CloudDocs" / name).symlink_to(home / name)


def test_icloud_drive_is_synced(home):
    path = home / "Library/Mobile Documents/com~apple~CloudDocs/project/data"
    assert config.icloud_synced_folder(path, home) == home / "Library/Mobile Documents"


@pytest.mark.parametrize("name", ["Documents", "Desktop"])
def test_documents_and_desktop_synced_when_icloud_has_them(home, name):
    sync_desktop_and_documents(home)
    assert config.icloud_synced_folder(home / name / "VibeCoding/project/data", home) == home / name


def test_documents_not_synced_when_icloud_lacks_them(home):
    assert config.icloud_synced_folder(home / "Documents/project/data", home) is None


def test_developer_folder_not_synced(home):
    sync_desktop_and_documents(home)
    assert config.icloud_synced_folder(home / "Developer/project/data", home) is None


def test_symlink_into_documents_is_resolved(home):
    sync_desktop_and_documents(home)
    (home / "Documents/project").mkdir()
    (home / "Developer/link").symlink_to(home / "Documents/project")
    assert config.icloud_synced_folder(home / "Developer/link/data", home) == home / "Documents"


# --- doctor -----------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / "data"
    monkeypatch.setattr(config, "default_data_dir", lambda: path)
    return path


def test_doctor_ok(data_dir):
    config.ensure_data_dir(data_dir)
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    os.chmod(data_dir / "preferences.yaml", 0o600)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "OK    preferences: valid" in result.output
    assert "OK    location:" in result.output
    warnings = [line for line in result.output.splitlines() if line.startswith("WARN")]
    assert warnings == ["WARN  Google Calendar: not connected (runs continue with tasks only); run `assistant auth google`"]


def test_doctor_warns_on_644_file(data_dir):
    config.ensure_data_dir(data_dir)
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    os.chmod(data_dir / "preferences.yaml", 0o644)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0  # a warning, not a failure
    assert "WARN  permissions:" in result.output and "preferences.yaml is 644, expected 600" in result.output


def test_doctor_warns_in_icloud_folder(data_dir, monkeypatch):
    config.ensure_data_dir(data_dir)
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    os.chmod(data_dir / "preferences.yaml", 0o600)
    monkeypatch.setattr(config, "icloud_synced_folder", lambda path: Path("/Users/me/Documents"))
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0  # a warning, not a failure
    assert "WARN  location:" in result.output and "iCloud syncs" in result.output


def test_doctor_fails_on_invalid_preferences(data_dir):
    config.ensure_data_dir(data_dir)
    data = example()
    data["timezone"] = "Nowhere/Land"
    write_prefs(data_dir / "preferences.yaml", data)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL  preferences: timezone:" in result.output


def test_doctor_creates_missing_data_dir(data_dir):
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1  # no preferences yet
    assert mode(data_dir) == 0o700
    assert "created" in result.output and "not found" in result.output


def test_finder_ds_store_is_ignored(tmp_path):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    for folder in (d, d / "secrets"):
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / ".DS_Store").write_bytes(b"finder")
        (folder / ".DS_Store").chmod(0o644)
    assert config.permission_issues(d) == []
    (d / "notes.json").write_text("{}")
    (d / "notes.json").chmod(0o644)
    assert [i.path.name for i in config.permission_issues(d)] == ["notes.json"]

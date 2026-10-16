"""M6: startup guard. Safety tests 1, 2 and 5 (SECURITY §7), no token → calendar unavailable."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import cli, config
from assistant.cli import app
from assistant.guard import (
    BILLING_VARS,
    READONLY_SCOPE,
    BillingCredentialError,
    CalendarAccess,
    RunLockedError,
    ScopeError,
    check_billing,
    check_calendar_scope,
    run_lock,
    sdk_env,
    startup_guard,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SECRET = "sk-ant-fake-value-never-printed"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"


class FakeTokenInfo:
    """Stands in for Google's tokeninfo response."""

    def __init__(self, info=None, error: Exception | None = None):
        self.info, self.error = info, error

    def tokeninfo(self):
        if self.error:
            raise self.error
        return self.info


def token(*scopes: str) -> FakeTokenInfo:
    return FakeTokenInfo({"aud": "fake.apps.googleusercontent.com", "scope": " ".join(scopes), "expires_in": 3599})


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    (d / config.PREFERENCES_FILE).write_text((EXAMPLES / "preferences.yaml").read_text())
    return d


# --- Safety test 5: billing credentials ------------------------------------------------------


@pytest.mark.parametrize("name", BILLING_VARS)
def test_each_billing_variable_refuses_and_is_named(name):
    with pytest.raises(BillingCredentialError) as e:
        check_billing({"PATH": "/usr/bin", name: SECRET})
    assert e.value.names == [name] and name in str(e.value) and SECRET not in str(e.value)


def test_empty_billing_variable_still_refuses():
    with pytest.raises(BillingCredentialError):
        check_billing({"ANTHROPIC_API_KEY": ""})


def test_several_billing_variables_are_all_named():
    with pytest.raises(BillingCredentialError) as e:
        check_billing({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_USE_BEDROCK": "1"})
    assert e.value.names == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"]


def test_clean_environment_passes():
    check_billing({"PATH": "/usr/bin", "HOME": "/Users/fake", "ANTHROPIC_MODEL": "x"})


def test_billing_variables_are_stripped_from_the_sdk_environment():
    env = {"PATH": "/usr/bin", **{name: SECRET for name in BILLING_VARS}}
    assert sdk_env(env) == {"PATH": "/usr/bin"}


def test_six_variables_match_security_doc():
    security = (ROOT / "SECURITY.md").read_text()
    section = security[security.index("## 3.") : security.index("## 4.")]
    assert set(re.findall(r"`([A-Z_]+)`", section)) == set(BILLING_VARS)


# --- Safety test 1: token scope ---------------------------------------------------------------


def test_readonly_token_is_connected():
    assert check_calendar_scope(token(READONLY_SCOPE)) is CalendarAccess.connected


@pytest.mark.parametrize(
    "extra",
    [
        WRITE_SCOPE,
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.readonly",  # broader read: still not allowed
        "https://www.googleapis.com/auth/calendar.calendars",
        "https://www.googleapis.com/auth/gmail.readonly",
        "openid",
    ],
)
def test_any_extra_scope_refuses_to_start(extra):
    with pytest.raises(ScopeError) as e:
        check_calendar_scope(token(READONLY_SCOPE, extra))
    assert e.value.extra == [extra] and extra in str(e.value)


def test_write_scope_alone_refuses():
    with pytest.raises(ScopeError):
        check_calendar_scope(token(WRITE_SCOPE))


def test_no_token_is_not_connected():
    assert check_calendar_scope(FakeTokenInfo(None)) is CalendarAccess.not_connected


@pytest.mark.parametrize("info", [{}, {"scope": None}, {"scope": ["list"]}, {"scope": ""}, {"error": "invalid_token"}])
def test_unreadable_or_empty_scopes_are_not_used(info):
    assert check_calendar_scope(FakeTokenInfo(info)) is CalendarAccess.unverified


def test_tokeninfo_failure_means_calendar_not_used():
    assert check_calendar_scope(FakeTokenInfo(error=OSError("offline"))) is CalendarAccess.unverified


# --- Safety test 2: no calendar write method anywhere ----------------------------------------

WRITE_CALL = re.compile(
    r"\b(?:events|calendars|calendarList|acl|settings|channels)\(\)\s*\.\s*"
    r"(?:insert|update|patch|delete|quickAdd|move|import_|clear|watch)\b"
)
WRITE_WORDS = re.compile(r"\bquickAdd\b|\bimport_\(")
SCOPE_URL = re.compile(r"https://www\.googleapis\.com/auth/[A-Za-z0-9_.]+")


def calendar_write_references(text: str) -> list[str]:
    return [m.group(0) for m in WRITE_CALL.finditer(text)] + [m.group(0) for m in WRITE_WORDS.finditer(text)]


def python_files() -> list[Path]:
    return sorted(p for folder in ("src", "evals") for p in (ROOT / folder).rglob("*.py"))


def test_scanner_finds_write_calls():
    for sample in [
        "service.events().insert(calendarId='primary', body=b)",
        "svc.events() . patch(**kw)",
        "svc.calendars().delete(calendarId=x)",
        "svc.events().quickAdd(text=t)",
        "svc.events().move(eventId=e)",
    ]:
        assert calendar_write_references(sample), sample
    assert not calendar_write_references("service.events().list(calendarId='primary')")
    assert not calendar_write_references("service.events().get(eventId=e)")


def test_no_calendar_write_method_is_referenced():
    found = {str(p.relative_to(ROOT)): refs for p in python_files() if (refs := calendar_write_references(p.read_text()))}
    assert found == {}


def test_only_the_readonly_scope_appears_in_code():
    scopes = {s for p in python_files() for s in SCOPE_URL.findall(p.read_text())}
    assert scopes <= {READONLY_SCOPE}


def test_calendar_client_has_no_write_methods():
    from assistant.integrations import google_calendar

    names = {n.lower() for cls in (google_calendar.GoogleCalendarClient,) for n in dir(cls) if not n.startswith("_")}
    assert not names & {"insert", "update", "patch", "delete", "create", "quick_add", "quickadd", "move"}


# --- The guard ---------------------------------------------------------------------------------


def test_guard_no_token_continues_with_calendar_unavailable(data_dir):
    with startup_guard(data_dir, env={"PATH": "/usr/bin"}) as start:
        assert start.calendar is CalendarAccess.not_connected and not start.calendar_available
        assert start.prefs.timezone == "America/Los_Angeles"
        assert start.sdk_env == {"PATH": "/usr/bin"}


def test_guard_connected_with_readonly_token(data_dir):
    with startup_guard(data_dir, env={}, tokens=token(READONLY_SCOPE)) as start:
        assert start.calendar_available


def test_guard_holds_the_lock(data_dir):
    with startup_guard(data_dir, env={}):
        with pytest.raises(RunLockedError):
            with run_lock(data_dir):
                pass


def test_guard_refuses_when_another_run_holds_the_lock(data_dir):
    with run_lock(data_dir):
        with pytest.raises(RunLockedError):
            with startup_guard(data_dir, env={}):
                pytest.fail("guard should not yield")


def test_guard_billing_comes_before_preferences(data_dir):
    (data_dir / config.PREFERENCES_FILE).write_text("timezone: Nowhere/Fake\n")
    with pytest.raises(BillingCredentialError):
        with startup_guard(data_dir, env={"ANTHROPIC_API_KEY": SECRET}):
            pytest.fail("guard should not yield")


def test_guard_bad_preferences_refuse(data_dir):
    (data_dir / config.PREFERENCES_FILE).write_text("timezone: Nowhere/Fake\n")
    with pytest.raises(config.PreferencesError):
        with startup_guard(data_dir, env={}):
            pytest.fail("guard should not yield")


def test_guard_write_scope_refuses(data_dir):
    with pytest.raises(ScopeError):
        with startup_guard(data_dir, env={}, tokens=token(READONLY_SCOPE, WRITE_SCOPE)):
            pytest.fail("guard should not yield")


def test_guard_releases_the_lock_after_refusing(data_dir):
    with pytest.raises(BillingCredentialError):
        with startup_guard(data_dir, env={"ANTHROPIC_API_KEY": SECRET}):
            pass
    with run_lock(data_dir):
        pass


# --- doctor --------------------------------------------------------------------------------------


@pytest.fixture
def doctor_env(data_dir, monkeypatch):
    monkeypatch.setattr(config, "default_data_dir", lambda: data_dir)
    for name in BILLING_VARS:
        monkeypatch.delenv(name, raising=False)
    return data_dir


def doctor(env=None):
    return CliRunner().invoke(app, ["doctor"], env=env)


@pytest.mark.parametrize("name", BILLING_VARS)
def test_doctor_refuses_with_billing_variable(doctor_env, name):
    out = doctor({name: SECRET})
    assert out.exit_code == 1
    assert name in out.output and "refusing to start" in out.output
    assert SECRET not in out.output and "preferences" not in out.output


def test_doctor_says_calendar_not_connected(doctor_env):
    out = doctor()
    assert out.exit_code == 0, out.output
    assert "OK    billing" in out.output
    assert "WARN  Google Calendar: not connected" in out.output


def test_doctor_fails_on_write_scope_token(doctor_env, monkeypatch):
    monkeypatch.setattr(cli, "google_tokens", lambda: token(READONLY_SCOPE, WRITE_SCOPE))
    out = doctor()
    assert out.exit_code == 1 and "FAIL  Google Calendar" in out.output and WRITE_SCOPE in out.output


def test_doctor_ok_with_readonly_token(doctor_env, monkeypatch):
    monkeypatch.setattr(cli, "google_tokens", lambda: token(READONLY_SCOPE))
    out = doctor()
    assert out.exit_code == 0 and "OK    Google Calendar: connected" in out.output

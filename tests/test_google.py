"""M13 Layer A: the real Google Calendar client, Keychain token and `auth google`, with no network (S-1, S-2, FR-1–FR-3)."""

import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from typer.testing import CliRunner

from assistant import cli, config
from assistant.agent.tools import calendar_window
from assistant.guard import READONLY_SCOPE, CalendarAccess, GoogleLoginExpired, ScopeError, check_calendar_scope
from assistant.integrations import google_auth
from assistant.integrations import google_calendar as G

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN_LOGIN = google_auth.run_login  # captured at import, before the autouse fixture replaces it
TZ = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 10, 28)


def event(id_, start, end=None, **extra):
    if len(start) == 10:
        when = {"start": {"date": start}, "end": {"date": end or (date.fromisoformat(start) + timedelta(days=1)).isoformat()}}
    else:
        when = {"start": {"dateTime": start}, "end": {"dateTime": end}}
    return {"id": id_, "status": "confirmed", "summary": f"Event {id_}", **when, **extra}


class FakePages:
    """events.list pages per calendar ID; a calendar not in `pages` answers 404."""

    def __init__(self, pages: dict[str, list[dict]], fail: Exception | None = None):
        self.pages, self.fail = pages, fail
        self.calls: list[tuple[str, dict]] = []

    def page(self, calendar_id, params):
        self.calls.append((calendar_id, dict(params)))
        if self.fail:
            raise self.fail
        if calendar_id not in self.pages:
            raise G.CalendarNotFound(calendar_id)
        pages = self.pages[calendar_id]
        i = int(params.get("pageToken", 0))
        return {"items": pages[i]["items"], **({"nextPageToken": str(i + 1)} if i + 1 < len(pages) else {})}


def client(pages, calendars=("primary", "birthdays")):
    return G.GoogleCalendarClient(list(calendars), TZ, TODAY, pages)


# --- The client (FR-1–FR-3, S-13) ------------------------------------------------------------------------


def test_request_asks_for_the_window_and_fr2_fields_only():
    pages = FakePages({"primary": [{"items": []}], G.BIRTHDAYS_CALENDAR_ID: [{"items": []}]})
    client(pages).list_events(TODAY, TODAY + timedelta(days=30))
    cal_id, params = pages.calls[0]
    assert cal_id == "primary"
    assert params["timeMin"] == "2026-10-28T00:00:00-07:00" and params["timeMax"] == "2026-11-05T00:00:00-08:00"  # clamped to +7, DST-aware
    assert params["singleEvents"] is True and params["timeZone"] == "America/Los_Angeles"
    assert "attendees" not in params["fields"] and "description" not in params["fields"] and "creator" not in params["fields"]
    assert re.fullmatch(r"nextPageToken,items\([a-zA-Z,]+\)", params["fields"])


def test_events_are_normalised_sorted_and_cancelled_dropped():
    primary = [
        event("b", "2026-10-28T10:00:00-07:00", "2026-10-28T11:00:00-07:00", location=" Room 1 ", attendees=[{"email": "x@example.com"}]),
        event("a", "2026-10-28T09:00:00-07:00", "2026-10-28T09:30:00-07:00", transparency="transparent"),
        {**event("c", "2026-10-29T09:00:00-07:00", "2026-10-29T10:00:00-07:00"), "status": "cancelled"},
        event("bday1", "2026-10-30", eventType="birthday"),
    ]
    result = client(FakePages({"primary": [{"items": primary}], G.BIRTHDAYS_CALENDAR_ID: [{"items": []}]})).list_events(TODAY, TODAY)
    assert [e.id for e in result.events] == ["a", "b"]  # 10-30 is outside the asked range; c is cancelled
    assert result.events[0].transparency == "transparent" and result.events[1].location == "Room 1"
    assert "attendees" not in result.events[1].model_dump()
    week = client(FakePages({"primary": [{"items": primary}], G.BIRTHDAYS_CALENDAR_ID: [{"items": []}]})).list_events(TODAY, TODAY + timedelta(days=7))
    assert next(e for e in week.events if e.id == "bday1").is_birthday


def test_pages_are_followed():
    pages = FakePages({"primary": [{"items": [event("a", "2026-10-28T09:00:00-07:00", "2026-10-28T10:00:00-07:00")]}, {"items": [event("b", "2026-10-29T09:00:00-07:00", "2026-10-29T10:00:00-07:00")]}]})
    result = client(pages, ["primary"]).list_events(TODAY, TODAY + timedelta(days=7))
    assert [e.id for e in result.events] == ["a", "b"] and pages.calls[1][1]["pageToken"] == "1"


def test_birthdays_alias_reads_the_contacts_calendar_and_marks_birthdays():
    pages = FakePages({"primary": [{"items": []}], G.BIRTHDAYS_CALENDAR_ID: [{"items": [event("contact1", "2026-10-29", summary="Sam")]}]})
    result = client(pages).list_events(TODAY, TODAY + timedelta(days=7))
    assert [c for c, _ in pages.calls] == ["primary", G.BIRTHDAYS_CALENDAR_ID]
    assert result.events[0].is_birthday and result.events[0].calendar == "birthdays"


def test_missing_birthdays_calendar_is_skipped_not_an_outage():
    result = client(FakePages({"primary": [{"items": []}]})).list_events(TODAY, TODAY)
    assert result.available and result.skipped_calendars == ["birthdays"]


def test_any_other_error_makes_the_calendar_unavailable():
    pages = FakePages({}, fail=ConnectionError("offline"))
    assert calendar_window(client(pages), TODAY).model_dump() == {"available": False, "error": "ConnectionError", "skipped_calendars": [], "events": []}


def test_range_entirely_in_the_past_asks_nothing():
    pages = FakePages({"primary": [{"items": []}]})
    assert client(pages).list_events(TODAY - timedelta(days=5), TODAY - timedelta(days=1)).events == []
    assert pages.calls == []


def _http_error(status):
    return HttpError(httplib2.Response({"status": status, "reason": "x"}), b"{}")


@pytest.mark.parametrize("status, missing", [(404, True), (403, True), (500, False), (401, False)])
def test_google_events_pages_maps_missing_calendars(monkeypatch, status, missing):
    class Request:
        def execute(self, num_retries):
            raise _http_error(status)

    class Service:
        def events(self):
            return type("Events", (), {"list": lambda self, **kw: Request()})()

    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: Service())
    pages = G.GoogleEventsPages(lambda: object())
    with pytest.raises(G.CalendarNotFound if missing else HttpError):
        pages.page("primary", {})


def test_service_is_built_lazily_so_a_keychain_problem_is_an_outage(monkeypatch):
    def broken():
        raise GoogleLoginExpired()

    real = G.GoogleCalendarClient(["primary"], TZ, TODAY, G.GoogleEventsPages(broken))  # building it doesn't touch the Keychain
    assert calendar_window(real, TODAY).available is False


# --- Safety tests 1 and 2 with the real modules present ----------------------------------------------------


def test_only_events_list_is_called_on_the_api():
    calls = set()
    for path in (ROOT / "src").rglob("*.py"):
        calls |= set(re.findall(r"\.(events|calendars|calendarList|acl|settings|channels|colors|freebusy)\(\)\s*\.\s*(\w+)", path.read_text()))
    assert calls == {("events", "list")}


def test_login_requests_only_the_readonly_scope(monkeypatch):
    seen = {}

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kw):
            seen["kw"] = kw
            return "creds"

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", Flow)
    assert REAL_RUN_LOGIN(Path("client.json")) == "creds"
    assert seen["scopes"] == [READONLY_SCOPE]
    assert seen["kw"]["access_type"] == "offline" and seen["kw"]["prompt"] == "consent" and seen["kw"]["host"] == "localhost"


# --- Keychain token -------------------------------------------------------------------------------------------


TOKEN = {"token": "ya29.fake", "refresh_token": "1//fake", "client_id": "id.apps.example", "client_secret": "fake", "token_uri": "https://oauth2.googleapis.com/token", "scopes": [READONLY_SCOPE]}


def test_token_round_trip_and_service_name(keychain):
    assert google_auth.load_token_info() is None and google_auth.load_credentials() is None
    google_auth.save_token_info(TOKEN)
    assert keychain == {("personal-assistant-agent.google-calendar", "oauth-token"): json.dumps(TOKEN)}
    assert google_auth.load_token_info() == TOKEN


def test_expired_access_token_is_refreshed_and_saved(keychain, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": "2020-01-01T00:00:00Z"})

    def refresh(self, request):
        self.token, self.expiry = "ya29.new", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(google_auth.Credentials, "refresh", refresh)
    creds = google_auth.load_credentials()
    assert creds.token == "ya29.new" and google_auth.load_token_info()["token"] == "ya29.new"


def test_refresh_keeps_the_sign_in_time(keychain, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": "2020-01-01T00:00:00Z", "signed_in_at": "2026-10-18T08:00:00+00:00"})

    def refresh(self, request):
        self.token, self.expiry = "ya29.new", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(google_auth.Credentials, "refresh", refresh)
    google_auth.load_credentials()
    assert google_auth.load_token_info()["signed_in_at"] == "2026-10-18T08:00:00+00:00"


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"signed_in_at": "2026-10-18T08:00:00+00:00"}, datetime(2026, 10, 25, 8, tzinfo=timezone.utc)),
        ({}, None),
        (None, None),
        ({"signed_in_at": "garbage"}, None),
        ({"signed_in_at": "2026-10-18T08:00:00"}, None),  # no time zone: unknown, not guessed
    ],
)
def test_login_expires_seven_days_after_sign_in(info, expected):
    assert google_auth.login_expires_at(info) == expected


def test_refused_refresh_means_expired(keychain, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": "2020-01-01T00:00:00Z"})

    def refresh(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(google_auth.Credentials, "refresh", refresh)
    with pytest.raises(GoogleLoginExpired):
        google_auth.load_credentials()
    assert check_calendar_scope(google_auth.KeychainTokenInfo()) is CalendarAccess.expired


def test_keychain_token_info_checks_the_real_grant(keychain, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": f"openid {READONLY_SCOPE}", "aud": "x"})
    with pytest.raises(ScopeError, match="openid"):
        check_calendar_scope(google_auth.KeychainTokenInfo())
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": READONLY_SCOPE})
    assert check_calendar_scope(google_auth.KeychainTokenInfo()) is CalendarAccess.connected


def test_no_token_means_not_connected(keychain):
    assert check_calendar_scope(google_auth.KeychainTokenInfo()) is CalendarAccess.not_connected


# --- assistant auth google -------------------------------------------------------------------------------------


class FakeCreds:
    def __init__(self, refresh_token="1//fake"):
        self.token, self.refresh_token = "ya29.fake", refresh_token

    def to_json(self):
        return json.dumps({**TOKEN, "refresh_token": self.refresh_token})


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    (d / "secrets").mkdir(mode=0o700, exist_ok=True)
    client_file = d / "secrets" / "google_oauth_client.json"
    client_file.write_text('{"installed": {"client_id": "fake"}}')
    client_file.chmod(0o600)
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    return d


def auth(monkeypatch, creds=None, scope=READONLY_SCOPE, login_error=None):
    def login(path):
        if login_error:
            raise login_error
        return creds or FakeCreds()

    monkeypatch.setattr(cli, "google_login", login)
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": scope})
    return CliRunner().invoke(cli.app, ["auth", "google"])


def test_auth_saves_the_token_when_the_grant_is_exactly_read_only(auth_dir, monkeypatch, keychain):
    out = auth(monkeypatch)
    assert out.exit_code == 0, out.output
    assert f"Granted scope: {READONLY_SCOPE} (nothing else)" in out.output and "personal-assistant-agent.google-calendar" in out.output
    saved = json.loads(keychain[(google_auth.KEYCHAIN_SERVICE, google_auth.KEYCHAIN_ACCOUNT)])
    assert saved["refresh_token"] == "1//fake"
    signed_in = datetime.fromisoformat(saved["signed_in_at"])
    assert abs(datetime.now(timezone.utc) - signed_in) < timedelta(minutes=1)
    assert "The app is in Testing, so this login ends" in out.output
    assert not list(auth_dir.rglob("*token*"))  # never written to a file


@pytest.mark.parametrize("scope", [f"openid {READONLY_SCOPE}", "https://www.googleapis.com/auth/calendar", ""])
def test_auth_refuses_any_other_grant_and_saves_nothing(auth_dir, monkeypatch, keychain, scope):
    out = auth(monkeypatch, scope=scope)
    assert out.exit_code == 1 and "The token was NOT saved" in out.output and "Third-party apps" in out.output
    assert keychain == {}


def test_auth_refuses_without_refresh_token(auth_dir, monkeypatch, keychain):
    out = auth(monkeypatch, creds=FakeCreds(refresh_token=None))
    assert out.exit_code == 1 and "no refresh token" in out.output and keychain == {}


def test_auth_login_failure_saves_nothing(auth_dir, monkeypatch, keychain):
    out = auth(monkeypatch, login_error=ValueError("cancelled"))
    assert out.exit_code == 1 and "Google sign-in failed (ValueError)" in out.output and keychain == {}


def test_auth_needs_the_client_file_at_600(auth_dir, monkeypatch, keychain):
    client_file = auth_dir / "secrets" / "google_oauth_client.json"
    client_file.chmod(0o644)
    out = auth(monkeypatch)
    assert out.exit_code == 1 and "chmod 600" in out.output
    client_file.unlink()
    out = auth(monkeypatch)
    assert out.exit_code == 1 and "no OAuth client file" in out.output and keychain == {}


def test_auth_log_has_no_token(auth_dir, monkeypatch):
    auth(monkeypatch)
    log = "\n".join(p.read_text() for p in (auth_dir / "logs").iterdir())
    assert '"event": "google_connected"' in log and "ya29" not in log and "1//fake" not in log


# --- doctor and the run with the real token source -------------------------------------------------------------


@pytest.fixture
def doctor_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    shutil.copy(ROOT / "examples" / "preferences.yaml", d / "preferences.yaml")
    (d / "preferences.yaml").chmod(0o600)
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    return d


def test_doctor_shows_the_granted_scope(doctor_dir, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": READONLY_SCOPE})
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert f"OK    Google Calendar: connected; granted scope: {READONLY_SCOPE} (nothing else)" in out.output


def _connected(signed_in_ago):
    info = {**TOKEN, "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if signed_in_ago is not None:
        info["signed_in_at"] = (datetime.now(timezone.utc) - signed_in_ago).isoformat()
    google_auth.save_token_info(info)


def test_doctor_shows_when_the_login_ends(doctor_dir, monkeypatch):
    _connected(timedelta(days=1))
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": READONLY_SCOPE})
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert "OK    Google login: ends " in out.output and "logins last 7 days" in out.output


@pytest.mark.parametrize("ago, hours", [(timedelta(days=5, hours=1), "in 46 h"), (timedelta(days=8), "in 0 h")])
def test_doctor_warns_when_the_login_is_close_to_ending(doctor_dir, monkeypatch, ago, hours):
    _connected(ago)
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": READONLY_SCOPE})
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert "WARN  Google login: ends " in out.output and hours in out.output and "run `assistant auth google` before then" in out.output
    assert out.exit_code == 0  # a warning, not a failure


def test_doctor_unknown_sign_in_time(doctor_dir, monkeypatch):
    _connected(None)
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": READONLY_SCOPE})
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert "WARN  Google login: sign-in time unknown" in out.output


def test_doctor_expired_login(doctor_dir, monkeypatch):
    def expired():
        raise GoogleLoginExpired()

    monkeypatch.setattr(google_auth, "load_credentials", expired)
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert "WARN  Google Calendar: the login expired or was revoked" in out.output


def test_doctor_refuses_a_wider_grant(doctor_dir, monkeypatch):
    google_auth.save_token_info({**TOKEN, "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    monkeypatch.setattr(google_auth, "fetch_tokeninfo", lambda token: {"scope": "https://www.googleapis.com/auth/calendar"})
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert out.exit_code == 1 and "FAIL  Google Calendar: refusing to start" in out.output


def test_expired_login_gap_in_the_brief():
    from assistant.agent.tools import Snapshot
    from assistant.flows.daily_run import code_gaps
    from assistant.schemas import CalendarResult
    from tests.fakes.scenario import load

    sc = load("base")
    snap = Snapshot(sc.preferences, sc.now, CalendarResult(available=False, error="expired"), [], {})
    assert code_gaps(snap, None)[0] == "Calendar data is missing: The Google login expired or was revoked (run `assistant auth google`). Meetings and birthdays aren't in this brief; their existing reminders were kept."


def test_skipped_calendar_is_logged_as_a_count(tmp_path):
    from assistant.flows import daily_run as D
    from assistant.logging_setup import RunLog
    from tests.fakes.reminders import FakeReminders
    from tests.fakes.scenario import load

    sc = load("birthdays_missing")
    data = sc.write_data_dir(tmp_path / "data")
    with RunLog(data, "run.manual", sc.preferences.tz) as log:
        D.preflight(data, sc.preferences, sc.now, sc.calendar_client(), "", FakeReminders(), True, log)
    text = "\n".join(p.read_text() for p in (data / "logs").iterdir())
    line = next(json.loads(l) for l in text.splitlines() if '"calendar_loaded"' in l)
    assert line["skipped"] == 1 and "birthdays" not in text

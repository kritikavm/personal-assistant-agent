import pytest

from assistant.agent import runner
from assistant.guard import BILLING_VARS


@pytest.fixture(autouse=True)
def _no_billing_variables(monkeypatch):
    """Tests never depend on billing variables in the developer's shell."""
    for name in BILLING_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fake_claude_login(monkeypatch):
    """doctor never calls the real CLI in tests. Tests of the login check override this."""
    monkeypatch.setattr(runner, "login_status", lambda: runner.LoginStatus(True, "subscription login (pro)"))


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    """No test reads or writes the real macOS Keychain or calls Google. Returns the in-memory store."""
    import keyring

    from assistant.integrations import google_auth

    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, account: store.get((service, account)))
    monkeypatch.setattr(keyring, "set_password", lambda service, account, value: store.__setitem__((service, account), value))

    def no_network(*a, **k):
        raise AssertionError("tests must not call Google")

    monkeypatch.setattr(google_auth, "fetch_tokeninfo", no_network)
    monkeypatch.setattr(google_auth, "run_login", no_network)
    return store


@pytest.fixture(autouse=True)
def no_osascript(monkeypatch):
    """No test runs osascript against the real Reminders app or Notification Center."""
    from assistant.integrations import notify, reminders

    def blocked(args):
        raise AssertionError("tests must not run osascript")

    monkeypatch.setattr(reminders, "run_osascript", blocked)
    monkeypatch.setattr(notify, "run_osascript", blocked)


@pytest.fixture(autouse=True)
def no_launchd(monkeypatch, tmp_path_factory):
    """No test runs launchctl or writes to the real ~/Library/LaunchAgents."""
    from assistant import cli, schedule

    def blocked(args):
        raise AssertionError("tests must not run launchctl")

    monkeypatch.setattr(schedule, "run_launchctl", blocked)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(cli, "schedule_home", lambda: home)
    return home

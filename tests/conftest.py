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

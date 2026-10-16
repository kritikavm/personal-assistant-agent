import pytest

from assistant.guard import BILLING_VARS


@pytest.fixture(autouse=True)
def _no_billing_variables(monkeypatch):
    """Tests never depend on billing variables in the developer's shell."""
    for name in BILLING_VARS:
        monkeypatch.delenv(name, raising=False)

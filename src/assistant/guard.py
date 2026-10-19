"""Startup guard (ARCHITECTURE §2, §8; SECURITY §3).

Runs before any AI call: lock → billing check → preferences → calendar scope
check. Refuses to start when billing or calendar safety is at risk. No Google
token is not an error: the calendar is treated as unavailable (FR-22).
"""

import fcntl
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from assistant import config
from assistant.config import FILE_MODE
from assistant.schemas import Preferences

LOCK_FILE = ".run.lock"

# --- Single-run lock ------------------------------------------------------------------


class RunLockedError(Exception):
    def __init__(self) -> None:
        super().__init__("another run is in progress")


@contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    """Hold an exclusive `fcntl` lock on `data/.run.lock`, or fail at once.

    The OS releases the lock if the process dies, so a crash never leaves a
    stale lock. The file itself stays; only the lock on it matters.
    """
    fd = os.open(data_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockedError() from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- Billing check (S-7, S-8) ------------------------------------------------------------

# Any of these lets the SDK use billable access instead of my subscription (SECURITY §3).
BILLING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


class BillingCredentialError(Exception):
    """A billing variable is set. Names the variables, never their values."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(
            f"refusing to start: {', '.join(names)} {'is' if len(names) == 1 else 'are'} set. "
            "This agent runs only on the Claude subscription; unset "
            f"{'it' if len(names) == 1 else 'them'} and try again."
        )


def billing_variables(env: Mapping[str, str]) -> list[str]:
    """The billing variables present in `env`, even if empty."""
    return [name for name in BILLING_VARS if name in env]


def check_billing(env: Mapping[str, str]) -> None:
    names = billing_variables(env)
    if names:
        raise BillingCredentialError(names)


def sdk_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for the Agent SDK, with every billing variable removed (backstop)."""
    return {k: v for k, v in env.items() if k not in BILLING_VARS}


# --- Calendar scope check (S-1, S-2) -------------------------------------------------------

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


class TokenInfoSource(Protocol):
    def tokeninfo(self) -> dict[str, Any] | None:
        """Google's tokeninfo response for the stored token, or None if there's no token."""
        ...


class NoGoogleToken:
    """No Google token: the calendar is "not connected" (used by tests and before `auth google`)."""

    def tokeninfo(self) -> dict[str, Any] | None:
        return None


class CalendarAccess(StrEnum):
    connected = "connected"
    not_connected = "not_connected"  # no token in the Keychain yet
    unverified = "unverified"  # token present but its scopes couldn't be read: not used this run
    expired = "expired"  # Google refused the refresh token (revoked or expired): run `assistant auth google`


class GoogleLoginExpired(Exception):
    """The stored Google token was revoked or can't be refreshed."""


class ScopeError(Exception):
    """The token carries a scope beyond calendar.events.readonly. Scope names only."""

    def __init__(self, extra: list[str]):
        self.extra = extra
        super().__init__(
            f"refusing to start: the Google token has scope(s) beyond read-only calendar access: {', '.join(extra)}. "
            "Revoke it and reconnect with calendar.events.readonly only."
        )


def scopes_from_tokeninfo(info: dict[str, Any]) -> set[str] | None:
    """The granted scopes, or None if the response has no readable `scope` field."""
    scope = info.get("scope")
    if not isinstance(scope, str):
        return None
    return set(scope.split())


def check_calendar_scope(source: TokenInfoSource) -> CalendarAccess:
    """Refuse (ScopeError) on any scope other than calendar.events.readonly (decision §4.1).

    No token → not connected. A refused refresh token → expired. A token whose scopes can't be read isn't used.
    """
    try:
        info = source.tokeninfo()
    except GoogleLoginExpired:
        return CalendarAccess.expired
    except Exception:  # noqa: BLE001 - network or Keychain trouble: don't use the token this run
        return CalendarAccess.unverified
    if info is None:
        return CalendarAccess.not_connected
    scopes = scopes_from_tokeninfo(info)
    if scopes is None:
        return CalendarAccess.unverified
    extra = sorted(scopes - {READONLY_SCOPE})
    if extra:
        raise ScopeError(extra)
    if READONLY_SCOPE not in scopes:
        return CalendarAccess.unverified
    return CalendarAccess.connected


# --- The guard -------------------------------------------------------------------------------


@dataclass
class StartupResult:
    prefs: Preferences
    calendar: CalendarAccess
    sdk_env: dict[str, str]

    @property
    def calendar_available(self) -> bool:
        return self.calendar is CalendarAccess.connected


def startup_checks(
    data_dir: Path,
    env: Mapping[str, str] | None = None,
    tokens: TokenInfoSource | None = None,
) -> StartupResult:
    """Billing → preferences → scope, without the lock (for flows that lock only to write)."""
    env = os.environ if env is None else env
    check_billing(env)
    prefs = config.load_preferences(data_dir / config.PREFERENCES_FILE)
    calendar = check_calendar_scope(tokens or NoGoogleToken())
    return StartupResult(prefs, calendar, sdk_env(env))


@contextmanager
def startup_guard(
    data_dir: Path,
    env: Mapping[str, str] | None = None,
    tokens: TokenInfoSource | None = None,
) -> Iterator[StartupResult]:
    """Lock → billing → preferences → scope (ARCHITECTURE §6.1 step 1). Holds the lock throughout.

    Raises RunLockedError, BillingCredentialError, config.PreferencesError or
    ScopeError. Nothing else runs when it raises.
    """
    with run_lock(data_dir):
        yield startup_checks(data_dir, env, tokens)

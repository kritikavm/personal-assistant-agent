"""Google login and the stored token (S-1, S-2, S-10, ARCHITECTURE §11 steps 1–2).

The token (an "authorized user" JSON with the refresh token) lives in the macOS
Keychain, never in a file. The OAuth client file stays in `data/secrets/`.
Only the `calendar.events.readonly` scope is ever requested, and the guard
checks the scopes Google actually granted (tokeninfo) before each run.

Nothing here touches the calendar itself; see `google_calendar.py`.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import keyring
import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from assistant.guard import READONLY_SCOPE, GoogleLoginExpired

KEYCHAIN_SERVICE = "personal-assistant-agent.google-calendar"  # SECURITY §8 kill switch: delete this item
KEYCHAIN_ACCOUNT = "oauth-token"
CLIENT_FILE = Path("secrets") / "google_oauth_client.json"  # under data/
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TIMEOUT = 20  # seconds, for the tokeninfo call
# D4: the OAuth app stays in *Testing*, where Google ends the refresh token 7 days after sign-in.
LOGIN_DAYS = 7
EXPIRY_WARN = timedelta(days=2)  # `doctor` warns this long before the login ends
SIGNED_IN_AT = "signed_in_at"  # our own key in the Keychain JSON (UTC, ISO 8601)


# --- Keychain --------------------------------------------------------------------------------------------


def load_token_info() -> dict[str, Any] | None:
    """The stored authorized-user JSON, or None if there's no token yet."""
    text = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    return json.loads(text) if text else None


def save_token_info(info: dict[str, Any]) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, json.dumps(info))


def login_expires_at(info: dict[str, Any] | None) -> datetime | None:
    """When Google will end this login (sign-in + LOGIN_DAYS), or None if unknown."""
    try:
        signed_in = datetime.fromisoformat(info[SIGNED_IN_AT])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return None
    if signed_in.tzinfo is None:
        return None
    return signed_in + timedelta(days=LOGIN_DAYS)


# --- Credentials -----------------------------------------------------------------------------------------


def load_credentials() -> Credentials | None:
    """Credentials from the Keychain with a fresh access token, or None if not connected.

    Raises GoogleLoginExpired if Google refuses the refresh token, and
    TransportError (or another exception) if Google can't be reached.
    """
    info = load_token_info()
    if info is None:
        return None
    creds = Credentials.from_authorized_user_info(info, scopes=[READONLY_SCOPE])
    if not creds.valid:
        try:
            creds.refresh(Request())  # google-auth applies its own request timeout
        except RefreshError:
            raise GoogleLoginExpired() from None
        refreshed = json.loads(creds.to_json())
        if SIGNED_IN_AT in info:
            refreshed[SIGNED_IN_AT] = info[SIGNED_IN_AT]  # a refresh doesn't restart Google's 7 days
        save_token_info(refreshed)
    return creds


def fetch_tokeninfo(access_token: str) -> dict[str, Any]:
    """Google's view of the token: which scopes were really granted (S-2). Raises on any HTTP problem."""
    response = requests.post(TOKENINFO_URL, data={"access_token": access_token}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


class KeychainTokenInfo:
    """The guard's TokenInfoSource for the real token. None means not connected."""

    def tokeninfo(self) -> dict[str, Any] | None:
        creds = load_credentials()
        if creds is None:
            return None
        return fetch_tokeninfo(creds.token)


# --- Login (`assistant auth google`) ---------------------------------------------------------------------


def run_login(client_file: Path) -> Credentials:
    """Browser login for a desktop OAuth client, asking for the read-only scope only.

    `prompt=consent` and offline access make Google return a refresh token every time.
    The caller checks the granted scopes before anything is stored.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=[READONLY_SCOPE])
    return flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Opening your browser to sign in to Google (read-only calendar access)...",
        success_message="Signed in. You can close this tab and return to the terminal.",
    )


"""OAuth 2.0 PKCE login against the X API, and token storage/refresh.

This is a one-time interactive flow: /auth/login redirects the user's
browser to X, they approve, X redirects back to /auth/callback (which must
be reachable at the exact X_REDIRECT_URI registered on the X app — normally
http://127.0.0.1:8000/auth/callback, so this only works when the app is
running on the same machine as the browser doing the login).
"""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from backend import config

AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
SCOPES = "tweet.read users.read bookmark.read offline.access"

# In-memory store of state -> code_verifier for the PKCE handshake between
# /auth/login and /auth/callback. Fine for a single-user local desktop app
# where both requests happen back-to-back in the same running process.
_pending_logins: dict[str, str] = {}


class XAuthError(Exception):
    pass


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url() -> str:
    if not config.X_CLIENT_ID:
        raise XAuthError("X_CLIENT_ID is not set. Fill it in in .env before logging in.")

    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce_pair()
    _pending_logins[state] = verifier

    params = {
        "response_type": "code",
        "client_id": config.X_CLIENT_ID,
        "redirect_uri": config.X_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    query = httpx.QueryParams(params)
    return f"{AUTHORIZE_URL}?{query}"


def _token_request(data: dict) -> dict:
    auth = None
    if config.X_CLIENT_SECRET:
        auth = (config.X_CLIENT_ID, config.X_CLIENT_SECRET)
    else:
        data = {**data, "client_id": config.X_CLIENT_ID}

    resp = httpx.post(
        TOKEN_URL,
        data=data,
        auth=auth,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise XAuthError(f"X token endpoint returned {resp.status_code}: {resp.text}")
    return resp.json()


def exchange_code_for_tokens(code: str, state: str) -> dict:
    verifier = _pending_logins.pop(state, None)
    if not verifier:
        raise XAuthError("Unknown or expired login state. Start the login flow again at /auth/login.")

    token_data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.X_REDIRECT_URI,
        "code_verifier": verifier,
    })
    _store_tokens(token_data)
    return token_data


def _store_tokens(token_data: dict) -> None:
    from backend import db  # local import to avoid a circular import at module load time

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO oauth_tokens (id, access_token, refresh_token, expires_at, scope, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
            expires_at=excluded.expires_at,
            scope=excluded.scope,
            updated_at=excluded.updated_at
        """,
        (
            token_data["access_token"],
            token_data.get("refresh_token"),
            expires_at.isoformat(),
            token_data.get("scope"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_valid_access_token() -> str:
    """Return a usable access token, transparently refreshing it if it's expired."""
    from backend import db

    conn = db.get_connection()
    row = conn.execute("SELECT * FROM oauth_tokens WHERE id = 1").fetchone()
    conn.close()

    if not row:
        raise XAuthError("Not logged in to X yet. Visit /auth/login first.")

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
        return row["access_token"]

    if not row["refresh_token"]:
        raise XAuthError("Access token expired and no refresh token is stored. Visit /auth/login again.")

    token_data = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": row["refresh_token"],
    })
    _store_tokens(token_data)
    return token_data["access_token"]


def get_logged_in_user() -> dict | None:
    from backend import db

    conn = db.get_connection()
    row = conn.execute("SELECT user_id, username FROM oauth_tokens WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["user_id"]:
        return None
    return {"user_id": row["user_id"], "username": row["username"]}


def store_logged_in_user(user_id: str, username: str) -> None:
    from backend import db

    conn = db.get_connection()
    conn.execute("UPDATE oauth_tokens SET user_id = ?, username = ? WHERE id = 1", (user_id, username))
    conn.commit()
    conn.close()

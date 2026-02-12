from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import webbrowser

import httpx

from google_sync_service.config import Settings
from google_sync_service.schemas import AuthStartResponse, AuthStatusResponse

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class AuthError(RuntimeError):
    """Raised for OAuth flow errors."""


@dataclass(slots=True)
class _PendingAuth:
    state: str
    code_verifier: str
    expires_at: datetime


class GoogleAuthManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._pending_auth_by_state: dict[str, _PendingAuth] = {}
        self._last_error: str | None = None
        self._load_pending_states()

    def status(self) -> AuthStatusResponse:
        configured = bool(self._load_client_credentials(raise_on_missing=False))
        token = self._read_token()
        now = datetime.now(UTC)
        connected = bool(token and token.get("refresh_token"))
        needs_user_action = not connected
        has_refresh_token = bool(token and token.get("refresh_token"))

        # If access token exists and has not expired yet, still count as connected.
        if token and not has_refresh_token:
            expires_at = _parse_expiry(token.get("expiry"))
            connected = bool(token.get("access_token") and expires_at and expires_at > now)
            needs_user_action = not connected

        return AuthStatusResponse(
            configured=configured,
            connected=connected,
            needs_user_action=needs_user_action,
            has_refresh_token=has_refresh_token,
            token_path=str(self._settings.token_path),
            scopes=self._settings.scopes_list,
            last_error=self._last_error,
        )

    def start_authorization(self, *, open_browser: bool = True) -> AuthStartResponse:
        client = self._load_client_credentials(raise_on_missing=True)
        assert client is not None

        state = secrets.token_urlsafe(24)
        code_verifier = _code_verifier()
        code_challenge = _code_challenge(code_verifier)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.oauth_state_ttl_seconds)

        with self._lock:
            self._prune_pending_states(now=datetime.now(UTC))
            self._pending_auth_by_state[state] = _PendingAuth(
                state=state,
                code_verifier=code_verifier,
                expires_at=expires_at,
            )
            self._persist_pending_states()

        params = {
            "client_id": client["client_id"],
            "redirect_uri": self._settings.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._settings.scopes_list),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = httpx.URL(GOOGLE_AUTH_URL).copy_merge_params(params)

        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(str(auth_url), new=2)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to open browser automatically: %s", exc)

        return AuthStartResponse(
            auth_url=str(auth_url),
            state=state,
            expires_at=expires_at,
            opened_browser=opened,
        )

    def complete_authorization(self, *, state: str, code: str | None, error: str | None) -> None:
        if error:
            self._last_error = f"oauth_error:{error}"
            raise AuthError(f"Google OAuth returned error: {error}")

        if not code:
            self._last_error = "oauth_error:missing_code"
            raise AuthError("Missing authorization code from callback.")

        pending = self._consume_pending_state(state)
        if pending is None:
            self._last_error = "oauth_error:invalid_state"
            raise AuthError("Authorization state is missing, expired, or invalid.")

        client = self._load_client_credentials(raise_on_missing=True)
        assert client is not None

        data = {
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": self._settings.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": pending.code_verifier,
        }

        with httpx.Client(timeout=self._settings.oauth_timeout_seconds) as http:
            response = http.post(GOOGLE_TOKEN_URL, data=data)
            response.raise_for_status()
            payload = response.json()

        token = self._read_token() or {}
        token.update(payload)
        if not token.get("refresh_token"):
            self._last_error = "oauth_error:missing_refresh_token"
            raise AuthError(
                "Google did not return a refresh token. Remove prior app access and authorize again."
            )

        expires_in = int(payload.get("expires_in") or 0)
        if expires_in > 0:
            token["expiry"] = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()

        self._write_token(token)
        self._last_error = None

    def get_access_token(self) -> str:
        token = self._read_token()
        if not token:
            raise AuthError("No Google token found. Run auth flow first.")

        access_token = str(token.get("access_token") or "")
        expires_at = _parse_expiry(token.get("expiry"))
        now = datetime.now(UTC)

        if access_token and expires_at and expires_at > now + timedelta(seconds=30):
            return access_token

        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise AuthError("Missing refresh token. Re-authorize Google access.")

        client = self._load_client_credentials(raise_on_missing=True)
        assert client is not None

        data = {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        with httpx.Client(timeout=self._settings.oauth_timeout_seconds) as http:
            response = http.post(GOOGLE_TOKEN_URL, data=data)
            response.raise_for_status()
            payload = response.json()

        next_access_token = str(payload.get("access_token") or "")
        if not next_access_token:
            raise AuthError("Token refresh failed: no access token in response.")

        token["access_token"] = next_access_token
        expires_in = int(payload.get("expires_in") or 0)
        if expires_in > 0:
            token["expiry"] = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
        if payload.get("scope"):
            token["scope"] = payload.get("scope")
        if payload.get("token_type"):
            token["token_type"] = payload.get("token_type")

        self._write_token(token)
        return next_access_token

    def _consume_pending_state(self, state: str) -> _PendingAuth | None:
        now = datetime.now(UTC)
        with self._lock:
            self._prune_pending_states(now=now)
            pending = self._pending_auth_by_state.pop(state, None)
            self._persist_pending_states()
        if pending is None:
            return None
        return pending

    def _load_pending_states(self) -> None:
        path = self._settings.pending_auth_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read pending auth file %s: %s", path, exc)
            return

        if not isinstance(raw, list):
            return
        loaded: dict[str, _PendingAuth] = {}
        now = datetime.now(UTC)
        for item in raw:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "").strip()
            verifier = str(item.get("code_verifier") or "").strip()
            if not state or not verifier:
                continue
            expires_at = _parse_expiry(item.get("expires_at"))
            if expires_at is None or expires_at <= now:
                continue
            loaded[state] = _PendingAuth(
                state=state,
                code_verifier=verifier,
                expires_at=expires_at,
            )
        self._pending_auth_by_state = loaded
        if loaded:
            logger.info("Loaded %d pending OAuth state(s) from disk.", len(loaded))

    def _persist_pending_states(self) -> None:
        path = self._settings.pending_auth_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "state": item.state,
                "code_verifier": item.code_verifier,
                "expires_at": item.expires_at.isoformat(),
            }
            for item in self._pending_auth_by_state.values()
        ]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("Could not chmod pending auth file %s: %s", path, exc)

    def _prune_pending_states(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        expired = [state for state, item in self._pending_auth_by_state.items() if item.expires_at <= current]
        for state in expired:
            self._pending_auth_by_state.pop(state, None)

    def _load_client_credentials(self, *, raise_on_missing: bool) -> dict[str, str] | None:
        client_id = (self._settings.client_id or "").strip()
        client_secret = (self._settings.client_secret or "").strip()
        if client_id and client_secret:
            return {"client_id": client_id, "client_secret": client_secret}

        if self._settings.client_secret_path:
            creds = _read_client_secret_file(self._settings.client_secret_path)
            if creds:
                return creds

        if raise_on_missing:
            raise AuthError(
                "Google client credentials are missing. "
                "Set GOOGLE_SYNC_CLIENT_ID + GOOGLE_SYNC_CLIENT_SECRET "
                "or GOOGLE_SYNC_CLIENT_SECRET_PATH."
            )
        return None

    def _read_token(self) -> dict[str, object] | None:
        path = self._settings.token_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read token file %s: %s", path, exc)
            return None

    def _write_token(self, token: dict[str, object]) -> None:
        path = self._settings.token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("Could not chmod token file %s: %s", path, exc)


def _read_client_secret_file(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "installed" in data:
        data = data.get("installed")
    elif isinstance(data, dict) and "web" in data:
        data = data.get("web")

    if not isinstance(data, dict):
        return None

    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _parse_expiry(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return None
    return None

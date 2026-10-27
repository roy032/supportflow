"""Signed session cookies and CSRF tokens for the dashboard (no server-side session store)."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

COOKIE_NAME = "sf_session"
SESSION_SECONDS = 12 * 3600


@dataclass(frozen=True)
class WebSession:
    agent: str
    expires: int
    # Random per sign-in, so each session has its own CSRF token.
    nonce: str


def _sign(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def encode_session(session: WebSession, secret: str) -> str:
    data = base64.urlsafe_b64encode(
        json.dumps({"a": session.agent, "e": session.expires, "n": session.nonce}).encode()
    )
    return f"{data.decode()}.{_sign(secret, data)}"


def decode_session(value: str | None, secret: str, now: float | None = None) -> WebSession | None:
    if not value or not secret or "." not in value:
        return None
    data, _, signature = value.rpartition(".")
    if not hmac.compare_digest(_sign(secret, data.encode()), signature):
        return None
    try:
        fields = json.loads(base64.urlsafe_b64decode(data.encode()))
        session = WebSession(agent=str(fields["a"]), expires=int(fields["e"]), nonce=str(fields["n"]))
    except (ValueError, KeyError, TypeError):
        return None
    if session.expires < (now or time.time()):
        return None
    return session


def csrf_token(session: WebSession, secret: str) -> str:
    return _sign(secret, f"csrf:{session.nonce}".encode())


def check_csrf(session: WebSession, secret: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(csrf_token(session, secret), token)

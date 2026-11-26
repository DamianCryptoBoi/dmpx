"""Authentication, signed sessions, CSRF tokens, and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass

PBKDF2_ITERATIONS = 260_000


class AuthError(Exception):
    code = "unauthorized"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if not password or not encoded:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(_b64(digest), expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    token: str
    csrf: str
    expires_at: int


class SessionSigner:
    def __init__(self, secret: str, ttl_seconds: int):
        if not secret:
            raise ValueError("session secret must not be empty")
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def issue(self) -> Session:
        csrf = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + self.ttl_seconds
        payload = {"sub": "admin", "exp": expires_at, "csrf": csrf}
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())
        return Session(token=f"{body}.{signature}", csrf=csrf, expires_at=expires_at)

    def verify(self, token: str | None) -> dict | None:
        if not token or "." not in token or not token.isascii():
            return None
        body, signature = token.rsplit(".", 1)
        expected = _b64(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("sub") != "admin":
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if not payload.get("csrf"):
            return None
        return payload


class LoginThrottle:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 900):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        return [stamp for stamp in self._attempts.get(key, []) if stamp >= cutoff]

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            recent = self._recent(key, now)
            self._attempts[key] = recent
            return len(recent) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            recent = self._recent(key, now)
            recent.append(now)
            self._attempts[key] = recent

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def api_key_matches(provided: str | None, expected: str) -> bool:
    if not expected or not provided:
        return False
    # Bytes: compare_digest rejects non-ASCII str, which would surface as a 500.
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def csrf_matches(provided: str | None, expected: str | None) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

import time

from dreamina.security import (
    LoginThrottle,
    SessionSigner,
    api_key_matches,
    bearer_token,
    csrf_matches,
    hash_password,
    verify_password,
)


def test_password_hash_round_trip():
    encoded = hash_password("s3cret-password", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert verify_password("s3cret-password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret-password", "garbage")
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


def test_session_tokens(monkeypatch):
    signer = SessionSigner("secret", ttl_seconds=60)
    issued = signer.issue()
    payload = signer.verify(issued.token)
    assert payload["csrf"] == issued.csrf
    assert signer.verify(issued.token + "x") is None
    assert SessionSigner("other", 60).verify(issued.token) is None
    assert signer.verify(None) is None
    real = time.time
    monkeypatch.setattr("dreamina.security.time.time", lambda: real() + 120)
    assert signer.verify(issued.token) is None


def test_csrf_and_api_key():
    assert csrf_matches("abc", "abc")
    assert not csrf_matches("abc", "abd")
    assert not csrf_matches(None, "abc")
    assert api_key_matches("k", "k")
    assert not api_key_matches("k", "")
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token(None) is None


def test_login_throttle():
    throttle = LoginThrottle(max_attempts=2, window_seconds=60)
    assert not throttle.blocked("ip")
    throttle.record_failure("ip")
    throttle.record_failure("ip")
    assert throttle.blocked("ip")
    assert not throttle.blocked("other")
    throttle.reset("ip")
    assert not throttle.blocked("ip")

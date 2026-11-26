from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from dreamina.app import build_services, create_app
from dreamina.browser import GenerationOutcome, LoginResult
from dreamina.config import Settings
from dreamina.http_client import Credits
from dreamina.security import hash_password

ADMIN_PASSWORD = "correct horse battery staple"
API_KEY = "test-api-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3" + b"\x00" * 64
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64


class FakeBrowsers:
    """Stands in for BrowserManager; records calls instead of driving Chromium."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.generate_calls: list[tuple[str, Any]] = []
        self.login_calls: list[str] = []
        self.generate_error: Exception | None = None
        self.submit_then_error: Exception | None = None
        self.next_submit_id = "submit-1"

    async def login(self, account_id, email, password, profile_id):
        self.login_calls.append(account_id)
        path = self.settings.data_dir / "accounts" / account_id / "cookies.json"
        write_cookies(path)
        return LoginResult(cookies_path=path, user_agent="ua", profile_id=profile_id or f"profile-{account_id}")

    async def generate(self, account_id, profile_id, spec, job_id, *, dry_run=False, on_submit=None):
        self.generate_calls.append((account_id, spec))
        if self.submit_then_error:
            on_submit(self.next_submit_id)
            raise self.submit_then_error
        if self.generate_error:
            raise self.generate_error
        if on_submit:
            on_submit(self.next_submit_id)
        return GenerationOutcome(submit_id=self.next_submit_id, profile_id=profile_id or "p")

    async def export_cookies(self, account_id):
        return None

    async def close(self, account_id):
        pass

    async def close_all(self):
        pass

    async def close_idle(self):
        pass

    def delete_profile(self, profile_id):
        pass


class FakeDreamina:
    """Shared state behind every FakeHTTP client."""

    def __init__(self):
        self.credits: dict[str, int] = {}
        self.history: dict[str, dict[str, Any]] = {}
        self.history_calls = 0
        self.expired = False

    def client(self, path: Path) -> "FakeHTTP":
        return FakeHTTP(self, path.parent.name)


class FakeHTTP:
    def __init__(self, state: FakeDreamina, account_id: str):
        self.state = state
        self.account_id = account_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def credits(self):
        from dreamina.http_client import SessionExpired

        if self.state.expired:
            raise SessionExpired("login required")
        return Credits(total=self.state.credits.get(self.account_id, 1000))

    async def history_record(self, submit_id):
        self.state.history_calls += 1
        return self.state.history.get(submit_id)

    async def download(self, url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video")
        return destination

    async def benefit_prices(self):
        return {}


def write_cookies(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cookies": [{"name": "_tea_web_id", "value": "1234567890123456789"}], "user_agent": "ua"})
    )


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("DREAMINA_", "ADMIN_")):
            monkeypatch.delenv(key)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# test configuration",
                f"DREAMINA_API_KEY={API_KEY}",
                f"ADMIN_PASSWORD_HASH={hash_password(ADMIN_PASSWORD, iterations=1000)}",
                "DREAMINA_SESSION_SECRET=test-session-secret",
                "DREAMINA_INSECURE_COOKIES=1",
                "DREAMINA_ACCOUNT_ALPHA_EMAIL=alpha@example.com",
                "DREAMINA_ACCOUNT_ALPHA_PASSWORD=alpha-pass",
                "DREAMINA_ACCOUNT_BETA_EMAIL=beta@example.com",
                "DREAMINA_ACCOUNT_BETA_PASSWORD=beta-pass",
                "",
            ]
        )
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(tmp_path: Path, env_file: Path) -> Settings:
    base = Settings.from_env(root=tmp_path, env_path=env_file)
    return dataclasses.replace(base, poll_interval_seconds=0, poll_timeout_seconds=5)


@pytest.fixture
def dreamina() -> FakeDreamina:
    return FakeDreamina()


@pytest.fixture
def services(settings, dreamina):
    browsers = FakeBrowsers(settings)
    svc = build_services(settings, browsers=browsers, http_factory=dreamina.client)
    svc.queue.scheduler_interval = 0.05
    yield svc
    svc.db.close()


def make_ready(svc, account_id: str, credits: int = 1000, last_used: str | None = None) -> None:
    path = svc.settings.data_dir / "accounts" / account_id / "cookies.json"
    write_cookies(path)
    svc.db.update_account(
        account_id, session_state="ready", cookies_path=str(path), credits=credits, last_used_at=last_used
    )


@pytest.fixture
def app(settings, services):
    return create_app(settings, services=services)

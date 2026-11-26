"""Account registry: env credentials, session state, credits, and selection."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .browser import BrowserError, BrowserManager, LoginAttention
from .config import Settings
from .db import Database, now_iso
from .envstore import EnvStore, account_env_key
from .http_client import DreaminaHTTP, NoWebId, SessionExpired, UpstreamError, UpstreamRejected

log = logging.getLogger(__name__)

# Single dashes only, so every id maps to a distinct env key (a-b and a--b
# would both become A_B).
ACCOUNT_ID = re.compile(r"^(?=.{1,32}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
HttpFactory = Callable[[Path], DreaminaHTTP]


class AccountError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _short(exc: BaseException) -> str:
    """First line only: Playwright appends call logs that can echo typed values."""
    text = str(getattr(exc, "message", None) or exc or "").strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:300]


class AccountService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        env: EnvStore,
        browsers: BrowserManager,
        http_factory: HttpFactory = DreaminaHTTP.from_cookie_file,
    ):
        self.settings = settings
        self.db = db
        self.env = env
        self.browsers = browsers
        self.http_factory = http_factory
        # Accounts with an in-flight generation; maintained by the job queue.
        self.busy: set[str] = set()
        self._logins: dict[str, asyncio.Task] = {}

    def redact(self, text: str) -> str:
        """Strip any configured account password from text bound for storage or logs."""
        for _account_id, _email, password in self.env.iter_accounts():
            if password and len(password) >= 4:
                text = text.replace(password, "***")
        return text

    def error_text(self, exc: BaseException) -> str:
        return self.redact(_short(exc))

    # registry ----------------------------------------------------------
    def sync_from_env(self) -> None:
        self.env.reload()
        env_ids = set()
        for account_id, email, _password in self.env.iter_accounts():
            env_ids.add(account_id)
            self.db.upsert_account_from_env(account_id, account_id, email, account_env_key(account_id))
        for account in self.db.list_accounts():
            if account["id"] not in env_ids:
                self.db.update_account(account["id"], last_error="credentials missing from .env")
        for account in self.db.list_accounts():
            # A login interrupted by a restart never finished.
            if account["session_state"] == "logging_in":
                self.db.set_account_state(account["id"], "needs_login", "login was interrupted")

    def get(self, account_id: str) -> dict[str, Any]:
        account = self.db.get_account(account_id)
        if account is None:
            raise AccountError("not_found", f"account {account_id} not found", 404)
        return account

    def has_password(self, account_id: str) -> bool:
        return bool(self.env.account_credentials(account_id)[1])

    def state(self, account: dict[str, Any]) -> str:
        return account["session_state"] if account["enabled"] else "disabled"

    def view(self, account: dict[str, Any]) -> dict[str, Any]:
        """Dashboard representation. Passwords are never included."""
        return {
            "id": account["id"],
            "label": account["label"],
            "email": account["email"],
            "enabled": bool(account["enabled"]),
            "state": self.state(account),
            "busy": account["id"] in self.busy,
            "logging_in": account["id"] in self._logins,
            "credits": account["credits"],
            "credits_updated_at": account["credits_updated_at"],
            "last_used_at": account["last_used_at"],
            "last_login_at": account["last_login_at"],
            "last_error": account["last_error"],
            "has_password": self.has_password(account["id"]),
            "has_cookies": bool(account["cookies_path"]) and Path(account["cookies_path"]).exists(),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.view(account) for account in self.db.list_accounts()]

    def add(self, account_id: str, label: str, email: str, password: str) -> dict[str, Any]:
        account_id = account_id.strip().lower()
        if not ACCOUNT_ID.match(account_id):
            raise AccountError("invalid_input", "account id must be 1-32 lowercase letters, digits, or dashes")
        email = email.strip()
        if "@" not in email:
            raise AccountError("invalid_input", "a valid email is required")
        if not password:
            raise AccountError("invalid_input", "a password is required")
        env_key = account_env_key(account_id)
        taken = {account["env_key"] for account in self.db.list_accounts()}
        taken |= {account_env_key(existing) for existing in self.env.account_ids()}
        if self.db.get_account(account_id) or env_key in taken:
            raise AccountError("conflict", f"account {account_id} already exists", 409)
        snapshot = self.env.snapshot()
        self.env.set_account_credentials(account_id, email, password)
        try:
            self.db.create_account(account_id, label.strip() or account_id, email, env_key)
        except Exception:
            self.env.restore(snapshot)
            raise
        return self.get(account_id)

    def update(
        self, account_id: str, *, label: str | None = None, email: str | None = None, password: str | None = None
    ) -> dict[str, Any]:
        account = self.get(account_id)
        email = (email or "").strip() or None
        if email is not None and "@" not in email:
            raise AccountError("invalid_input", "a valid email is required")
        if email or password:
            snapshot = self.env.snapshot()
            self.env.set_account_credentials(account_id, email or account["email"], password or None)
            try:
                self.db.update_account(account_id, email=email or account["email"])
            except Exception:
                self.env.restore(snapshot)
                raise
        if label is not None and label.strip():
            self.db.update_account(account_id, label=label.strip())
        if (email or password) and account["session_state"] == "attention":
            self.db.set_account_state(account_id, "needs_login", None)
        return self.get(account_id)

    def set_enabled(self, account_id: str, enabled: bool) -> None:
        self.get(account_id)
        self.db.update_account(account_id, enabled=1 if enabled else 0)

    async def remove(self, account_id: str) -> None:
        account = self.get(account_id)
        if account_id in self.busy or self.db.active_job_for_account(account_id):
            raise AccountError("conflict", "account has an active generation", 409)
        if account_id in self._logins:
            raise AccountError("conflict", "account is logging in", 409)
        # Take the account out of rotation before the first await so the
        # scheduler cannot start a paid job on it while it is being removed.
        self.busy.add(account_id)
        self.db.update_account(account_id, enabled=0)
        try:
            await self.browsers.close(account_id)
            snapshot = self.env.snapshot()
            self.env.remove_account_credentials(account_id)
            try:
                self.db.delete_account(account_id)
            except Exception:
                self.env.restore(snapshot)
                raise
        except Exception:
            if self.db.get_account(account_id):
                self.db.update_account(account_id, enabled=account["enabled"])
            raise
        finally:
            self.busy.discard(account_id)
        shutil.rmtree(self.settings.data_dir / "accounts" / account_id, ignore_errors=True)
        if account["profile_id"]:
            await asyncio.to_thread(self.browsers.delete_profile, account["profile_id"])

    # sessions ----------------------------------------------------------
    def start_login(self, account_id: str) -> None:
        """Run a login in the background; the dashboard polls the state."""
        self.get(account_id)
        if account_id in self._logins:
            return
        task = asyncio.create_task(self._login_quietly(account_id))
        self._logins[account_id] = task
        task.add_done_callback(lambda _t: self._logins.pop(account_id, None))

    async def _login_quietly(self, account_id: str) -> None:
        try:
            await self.login(account_id)
        except Exception as exc:
            log.warning("login for %s failed: %s", account_id, self.error_text(exc))

    async def login(self, account_id: str) -> dict[str, Any]:
        account = self.get(account_id)
        email, password = self.env.account_credentials(account_id)
        if not email or not password:
            self.db.set_account_state(account_id, "needs_login", "credentials missing from .env")
            raise AccountError("session_expired", "account credentials are missing from .env", 409)
        self.db.set_account_state(account_id, "logging_in", None)
        try:
            result = await self.browsers.login(account_id, email, password, account["profile_id"])
        except LoginAttention as exc:
            self.db.set_account_state(account_id, "attention", self.error_text(exc))
            raise
        except Exception as exc:
            self.db.set_account_state(account_id, "needs_login", self.error_text(exc))
            raise
        self.db.update_account(
            account_id,
            profile_id=result.profile_id,
            cookies_path=str(result.cookies_path),
            user_agent=result.user_agent,
            last_login_at=now_iso(),
        )
        # Promote to ready only after a successful authenticated read.
        self.db.set_account_state(account_id, "needs_login", None)
        return await self.refresh_credits(account_id, renew=False)

    def client(self, account: dict[str, Any]) -> DreaminaHTTP:
        path = account.get("cookies_path")
        if not path or not Path(path).exists():
            raise SessionExpired("account has no saved session")
        return self.http_factory(Path(path))

    async def refresh_credits(self, account_id: str, *, renew: bool = True) -> dict[str, Any]:
        """Read credits; on an auth failure renew the login once and retry."""
        account = self.get(account_id)
        try:
            async with self.client(account) as client:
                credits = await client.credits()
        except (SessionExpired, NoWebId) as exc:
            if renew and self.has_password(account_id) and account["enabled"]:
                return await self.login(account_id)
            self.db.set_account_state(account_id, "needs_login", self.error_text(exc))
            raise
        except UpstreamRejected as exc:
            if "shark" in exc.message.lower():
                self.db.set_account_state(account_id, "attention", self.error_text(exc))
            else:
                self.db.update_account(account_id, last_error=self.error_text(exc))
            raise
        except UpstreamError as exc:
            self.db.update_account(account_id, last_error=self.error_text(exc))
            raise
        fields: dict[str, Any] = {"credits": credits.total, "credits_updated_at": now_iso()}
        # Re-read after the await: a concurrent job may have flagged the account.
        current = self.db.get_account(account_id)
        if current is None:
            raise AccountError("not_found", f"account {account_id} was removed", 404)
        if current["session_state"] in {"needs_login", "logging_in", "ready"}:
            fields.update(session_state="ready", last_error=None)
        self.db.update_account(account_id, **fields)
        return self.get(account_id)

    async def refresh_all(self) -> None:
        for account in self.db.list_accounts():
            if account["enabled"] and account["session_state"] == "ready":
                try:
                    await self.refresh_credits(account["id"], renew=False)
                except Exception as exc:
                    log.info("credit refresh for %s failed: %s", account["id"], self.error_text(exc))

    # selection ---------------------------------------------------------
    def usable(self) -> list[dict[str, Any]]:
        return [
            account
            for account in self.db.list_accounts()
            if account["enabled"]
            and account["session_state"] == "ready"
            and account["cookies_path"]
            and Path(account["cookies_path"]).exists()
            and self.has_password(account["id"])
        ]

    def check_capacity(self, estimate: int) -> None:
        """Fail fast at enqueue time instead of queueing a job nothing can run."""
        usable = self.usable()
        if not usable:
            raise AccountError("no_account_available", "no enabled account has a usable session", 503)
        if not any((account["credits"] or 0) >= estimate for account in usable):
            raise AccountError(
                "insufficient_credits", f"no account has the {estimate} credits this generation needs", 402
            )

    def unavailable_reason(self, estimate: int) -> tuple[str, str] | None:
        """Why no account can take a queued job, or None if one may free up
        (busy with another job or logging in)."""
        candidates = [
            account
            for account in self.db.list_accounts()
            if account["enabled"]
            and self.has_password(account["id"])
            and (
                account["id"] in self._logins
                or account["session_state"] == "logging_in"
                or (
                    account["session_state"] == "ready"
                    and account["cookies_path"]
                    and Path(account["cookies_path"]).exists()
                )
            )
        ]
        if not candidates:
            return "no_account_available", "no enabled account has a usable session"
        if not any(account["credits"] is None or account["credits"] >= estimate for account in candidates):
            return "insufficient_credits", f"no account has the {estimate} credits this generation needs"
        return None

    def pick(self, estimate: int) -> dict[str, Any] | None:
        """Least-recently-used idle account with enough cached credits."""
        candidates = [
            account
            for account in self.usable()
            if account["id"] not in self.busy
            and account["id"] not in self._logins
            and (account["credits"] or 0) >= estimate
        ]
        candidates.sort(key=lambda account: (account["last_used_at"] or "", account["id"]))
        return candidates[0] if candidates else None

    async def wait_for_logins(self) -> None:
        tasks = list(self._logins.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

"""Persistent in-process job queue: scheduling, submission, polling, recovery."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .accounts import AccountService
from .browser import BrowserError, BrowserManager, GenerationRejected, LoginAttention, LoginFailed, SubmitUncertain
from .catalog import PRICES, GenerationSpec, ValidationError, validate_generation
from .config import Settings
from .db import Database, now_iso
from .http_client import SessionExpired, UpstreamError, parse_history
from .media import cleanup_inputs, cleanup_job_dirs

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed"}


# Reasons Dreamina gives when it discards a finished render. These are its
# own content checks; the attempt is not charged.
RENDER_FAILURES = {
    "OutputAudioCopyright": "the generated soundtrack was flagged as copyrighted music",
}


def render_failure(code: str | None, reason: str | None) -> str:
    detail = RENDER_FAILURES.get(reason or "") or (reason and f"reason {reason}")
    message = f"the generation failed while rendering (code {code})"
    if detail:
        message += f": {detail}"
    return message + "; nothing was charged, and retrying usually works" if detail in RENDER_FAILURES.values() else message


def job_spec(job: dict[str, Any]) -> GenerationSpec:
    """Rebuild the validated generation from a stored job row."""
    return validate_generation(
        model_name=job["model"],
        prompt=job["prompt"],
        duration_seconds=int(job["duration_seconds"]),
        resolution=job["resolution"],
        aspect_ratio=job["aspect_ratio"] if job["mode"] == "unified" else None,
        images=job["images_json"],
        audio=job["audio_json"],
        first_frame=job["first_frame_path"],
        last_frame=job["last_frame_path"],
    )


class JobQueue:
    scheduler_interval = 2.0
    maintenance_interval = 60.0
    price_refresh_seconds = 1800.0

    def __init__(self, settings: Settings, db: Database, accounts: AccountService, browsers: BrowserManager):
        self.settings = settings
        self.db = db
        self.accounts = accounts
        self.browsers = browsers
        self._wake = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._submitting = 0
        self._loops: list[asyncio.Task] = []
        self._last_price_refresh = 0.0
        self._last_credit_refresh = 0.0

    # lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.recover()
        self._loops = [
            asyncio.create_task(self._scheduler(), name="dreamina-scheduler"),
            asyncio.create_task(self._maintenance(), name="dreamina-maintenance"),
        ]

    async def stop(self) -> None:
        for task in self._loops + list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._loops, *self._tasks.values(), return_exceptions=True)
        self._loops = []
        self._tasks.clear()

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """Resume work after a restart without ever resubmitting a paid job."""
        # Results are streamed from upstream, never stored; drop files an
        # older version downloaded.
        if self.settings.media_dir.exists():
            shutil.rmtree(self.settings.media_dir, ignore_errors=True)
            log.info("removed stored videos from %s", self.settings.media_dir)
        for job in self.db.jobs_with_status(["submitting", "running"]):
            if job["submit_id"] and job["account_id"]:
                self.db.update_job(job["id"], status="running")
                self._spawn(job["id"], self._resume(job["id"]))
            else:
                self._fail(
                    job["id"],
                    "upstream_timeout",
                    "the service restarted while submitting; the outcome is unknown, so check with the operator before retrying",
                )

    def _spawn(self, job_id: str, coro) -> None:
        task = asyncio.create_task(coro, name=f"dreamina-job-{job_id}")
        self._tasks[job_id] = task

        def done(_task: asyncio.Task) -> None:
            self._tasks.pop(job_id, None)
            self.wake()

        task.add_done_callback(done)

    # scheduling ----------------------------------------------------------
    async def _scheduler(self) -> None:
        while True:
            try:
                self._schedule_once()
            except Exception:
                log.exception("scheduler pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.scheduler_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _schedule_once(self) -> None:
        for job in self.db.jobs_with_status(["queued"]):
            if self._submitting >= self.settings.max_concurrent_jobs:
                return
            if job["id"] in self._tasks:
                continue
            try:
                spec = job_spec(job)
            except ValidationError as exc:
                self._fail(job["id"], "invalid_input", str(exc))
                continue
            estimate = spec.estimate_credits
            account = self.accounts.pick(estimate)
            if account is None:
                # Busy or logging-in accounts may free up; stay queued for them.
                reason = self.accounts.unavailable_reason(estimate)
                if reason:
                    self._fail(job["id"], *reason)
                continue
            self.accounts.busy.add(account["id"])
            try:
                self.db.update_account(account["id"], last_used_at=now_iso())
                self.db.update_job(job["id"], status="submitting", account_id=account["id"], started_at=now_iso())
            except Exception:
                self.accounts.busy.discard(account["id"])
                raise
            self._submitting += 1
            self._spawn(job["id"], self._run(job["id"], account["id"], spec))

    # execution -------------------------------------------------------------
    async def _run(self, job_id: str, account_id: str, spec: GenerationSpec) -> None:
        try:
            outcome = None
            failure: tuple[str, str] | None = None
            definitive = False
            try:
                account = self.accounts.get(account_id)
                outcome = await self.browsers.generate(
                    account_id,
                    account["profile_id"],
                    spec,
                    job_id,
                    # Persist the submit_id the instant the request leaves the page.
                    on_submit=lambda submit_id: self.db.update_job(job_id, submit_id=submit_id),
                )
            except BrowserError as exc:
                failure = self._classify(account_id, exc)
                # Dreamina answered ret != 0: nothing was charged, nothing to poll.
                definitive = isinstance(exc, GenerationRejected)
            except Exception as exc:
                log.exception("job %s submission crashed", job_id)
                failure = ("upstream_rejected", f"{type(exc).__name__}: {self.accounts.error_text(exc)}")
            finally:
                self._submitting -= 1
                cleanup_inputs(self.settings.jobs_dir / job_id)

            job = self.db.get_job(job_id) or {}
            submit_id = outcome.submit_id if outcome else job.get("submit_id")
            if failure and (definitive or not submit_id):
                self._fail(job_id, *failure)
                return
            # From here on the generation may be charged: only ever poll it.
            notes = list(outcome.warnings) if outcome else [f"error after submitting: {failure[1]}"]
            self.db.update_job(job_id, status="running", submit_id=submit_id)
            if notes:
                log.warning("job %s submitted with notes: %s", job_id, "; ".join(notes))
                self.db.update_job(job_id, error_message=self.accounts.redact("; ".join(notes))[:500])
            if outcome and outcome.profile_id:
                self.db.update_account(account_id, profile_id=outcome.profile_id)
            await self._export_cookies_quietly(account_id)
            await self._refresh_credits_quietly(account_id)
            await self._poll(job_id, account_id, submit_id)
        except asyncio.CancelledError:
            # Shutdown: the row keeps its state (and any submit_id) for recovery.
            raise
        except Exception as exc:
            log.exception("job %s crashed", job_id)
            job = self.db.get_job(job_id) or {}
            code = "upstream_timeout" if job.get("submit_id") else "upstream_rejected"
            self._fail(job_id, code, f"{type(exc).__name__}: {self.accounts.error_text(exc)}")
        finally:
            self.accounts.busy.discard(account_id)

    def _classify(self, account_id: str, exc: BrowserError) -> tuple[str, str]:
        message = self.accounts.error_text(exc)
        if isinstance(exc, GenerationRejected):
            if exc.shark:
                self.db.set_account_state(account_id, "attention", message)
            return "upstream_rejected", message
        if isinstance(exc, LoginAttention):
            self.db.set_account_state(account_id, "attention", message)
            return "session_expired", message
        if isinstance(exc, LoginFailed):
            self.db.set_account_state(account_id, "needs_login", message)
            return "session_expired", message
        if isinstance(exc, SubmitUncertain):
            return "upstream_timeout", message
        return "upstream_rejected", message

    async def _export_cookies_quietly(self, account_id: str) -> None:
        try:
            path = await self.browsers.export_cookies(account_id)
        except Exception as exc:
            log.info("cookie export for %s failed: %s", account_id, self.accounts.error_text(exc))
            return
        if path:
            self.db.update_account(account_id, cookies_path=str(path))

    async def _resume(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        account_id = job["account_id"]
        self.accounts.busy.add(account_id)
        try:
            await self._poll(job_id, account_id, job["submit_id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("resumed job %s crashed", job_id)
            self._fail(job_id, "upstream_timeout", f"{type(exc).__name__}: {self.accounts.error_text(exc)}")
        finally:
            self.accounts.busy.discard(account_id)

    async def _poll(self, job_id: str, account_id: str, submit_id: str) -> None:
        deadline = time.monotonic() + self.settings.poll_timeout_seconds
        renewed = False
        while True:
            try:
                account = self.accounts.get(account_id)
                async with self.accounts.client(account) as client:
                    record = await client.history_record(submit_id)
            except SessionExpired as exc:
                # The generation was already submitted and may be charged, so
                # this is an unknown outcome, not a safe-to-retry session error.
                if renewed:
                    self._fail(job_id, "upstream_timeout", f"lost the session while waiting: {self.accounts.error_text(exc)}")
                    return
                renewed = True
                try:
                    await self.accounts.login(account_id)
                except Exception as login_exc:
                    self._fail(
                        job_id, "upstream_timeout", f"lost the session while waiting: {self.accounts.error_text(login_exc)}"
                    )
                    return
                continue
            except UpstreamError as exc:
                log.info("poll for job %s failed transiently: %s", job_id, self.accounts.error_text(exc))
                record = None
            if record:
                state = parse_history(record)
                if state.failed:
                    self.db.update_job(job_id, credits_used=state.credits_used or 0)
                    self._fail(job_id, "upstream_rejected", render_failure(state.fail_code, state.fail_msg))
                    return
                if state.succeeded:
                    self.db.update_job(
                        job_id,
                        status="succeeded",
                        credits_used=state.credits_used,
                        output_url=state.video_url,
                        finished_at=now_iso(),
                    )
                    if not state.video_url:
                        # Maintenance re-reads history until the URL appears.
                        self.db.update_job(job_id, error_code="media_pending", error_message="the video URL is not available yet")
                    await self._refresh_credits_quietly(account_id)
                    return
            if time.monotonic() >= deadline:
                self._fail(
                    job_id,
                    "upstream_timeout",
                    "the generation did not finish in time; it may still complete, so check with the operator before retrying",
                )
                return
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _recover_urls(self) -> None:
        """Re-read history for succeeded jobs whose video URL never appeared."""
        cutoff = time.time() - self.settings.media_retention_days * 86400
        for job in self.db.jobs_missing_output_url():
            if job["id"] in self._tasks or not job["account_id"] or not job["submit_id"]:
                continue
            if _timestamp(job["finished_at"]) < cutoff:
                continue
            try:
                async with self.accounts.client(self.accounts.get(job["account_id"])) as client:
                    record = await client.history_record(job["submit_id"])
            except Exception as exc:
                log.info("video URL lookup for %s failed: %s", job["id"], self.accounts.error_text(exc))
                continue
            url = parse_history(record).video_url if record else None
            if url:
                self.db.update_job(job["id"], output_url=url, error_code=None, error_message=None)

    def _fail(self, job_id: str, code: str, message: str) -> None:
        self.db.update_job(
            job_id,
            status="failed",
            error_code=code,
            error_message=self.accounts.redact(message)[:500],
            finished_at=now_iso(),
        )
        cleanup_inputs(self.settings.jobs_dir / job_id)

    async def _refresh_credits_quietly(self, account_id: str) -> None:
        try:
            await self.accounts.refresh_credits(account_id, renew=False)
        except Exception as exc:
            log.info("credit refresh for %s failed: %s", account_id, self.accounts.error_text(exc))

    # maintenance -----------------------------------------------------------
    async def _maintenance(self) -> None:
        while True:
            try:
                await self.maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maintenance pass failed")
            await asyncio.sleep(self.maintenance_interval)

    async def maintain(self) -> None:
        await self.browsers.close_idle()
        active = {job["id"] for job in self.db.jobs_with_status(["queued", "submitting", "running"])}
        await asyncio.to_thread(
            cleanup_job_dirs, self.settings.jobs_dir, self.settings.media_retention_days, active | set(self._tasks)
        )
        await self._recover_urls()
        now = time.monotonic()
        if now - self._last_price_refresh >= self.price_refresh_seconds:
            await self.refresh_prices()
        if now - self._last_credit_refresh >= self.price_refresh_seconds:
            self._last_credit_refresh = now
            await self.accounts.refresh_all()

    async def refresh_prices(self) -> bool:
        for account in self.accounts.usable():
            try:
                async with self.accounts.client(account) as client:
                    matched = PRICES.update(await client.benefit_prices())
            except Exception as exc:
                log.info("price refresh via %s failed: %s", account["id"], self.accounts.error_text(exc))
                continue
            self._last_price_refresh = time.monotonic()
            return matched > 0
        return False


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0

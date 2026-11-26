import asyncio
import uuid

import pytest

from conftest import make_ready
from dreamina.accounts import AccountError
from dreamina.browser import GenerationRejected, LoginAttention
from dreamina.db import now_iso

SUCCESS = {"status": 50, "item_list": [{"video": {"transcoded_video": {"origin": {"video_url": "https://cdn/x.mp4"}}}}], "credits_consume": 84}


def queue_job(svc, **overrides):
    job = {
        "id": uuid.uuid4().hex,
        "idempotency_key": uuid.uuid4().hex,
        "request_hash": "h",
        "model": "seedance-2.5-flash",
        "prompt": "a paper boat",
        "duration_seconds": 4,
        "resolution": "480p",
        "aspect_ratio": "16:9",
        "mode": "unified",
        "status": "queued",
        "created_at": now_iso(),
    }
    job.update(overrides)
    svc.db.create_job(job)
    return job["id"]


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def status(svc, job_id):
    return svc.db.get_job(job_id)["status"]


def test_least_recently_used_idle_account_is_picked(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha", last_used="2026-01-02T00:00:00+00:00")
    make_ready(services, "beta", last_used="2026-01-01T00:00:00+00:00")
    assert services.accounts.pick(100)["id"] == "beta"
    services.accounts.busy.add("beta")
    assert services.accounts.pick(100)["id"] == "alpha"
    services.db.update_account("alpha", enabled=0)
    assert services.accounts.pick(100) is None


def test_capacity_errors(services):
    services.accounts.sync_from_env()
    with pytest.raises(AccountError) as excinfo:
        services.accounts.check_capacity(84)
    assert (excinfo.value.code, excinfo.value.status) == ("no_account_available", 503)
    make_ready(services, "alpha", credits=10)
    with pytest.raises(AccountError) as excinfo:
        services.accounts.check_capacity(84)
    assert (excinfo.value.code, excinfo.value.status) == ("insufficient_credits", 402)
    make_ready(services, "beta", credits=100)
    services.accounts.check_capacity(84)


async def test_job_runs_to_success(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    dreamina.history["submit-1"] = SUCCESS
    job_id = queue_job(services)
    (services.settings.jobs_dir / job_id / "inputs").mkdir(parents=True)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["account_id"] == "alpha" and job["submit_id"] == "submit-1"
    assert job["credits_used"] == 84 and job["output_url"] == "https://cdn/x.mp4"
    assert not (services.settings.jobs_dir / job_id / "inputs").exists()
    assert not services.settings.media_dir.exists()
    assert not services.accounts.busy
    assert services.db.get_account("alpha")["last_used_at"]


async def test_one_generation_per_account(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    first = queue_job(services)
    second = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, first) == "running")
        await asyncio.sleep(0.2)
        assert status(services, second) == "queued"
        dreamina.history["submit-1"] = SUCCESS
        await wait_for(lambda: status(services, second) != "queued")
    finally:
        await services.queue.stop()


async def test_shark_rejection_marks_account_attention(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.generate_error = GenerationRejected("shark not pass reject", ret="-6")
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert job["error_code"] == "upstream_rejected"
    assert services.db.get_account("alpha")["session_state"] == "attention"
    assert len(services.browsers.generate_calls) == 1


async def test_login_challenge_fails_job_without_retry(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.generate_error = LoginAttention("verification challenge")
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    assert services.db.get_job(job_id)["error_code"] == "session_expired"
    assert services.db.get_account("alpha")["session_state"] == "attention"
    assert len(services.browsers.generate_calls) == 1


async def test_failed_history_marks_job_failed(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    dreamina.history["submit-1"] = {"status": 30, "fail_code": "2038"}
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert job["error_code"] == "upstream_rejected" and "2038" in job["error_message"]


async def test_scheduler_fails_fast_without_usable_accounts(services):
    services.accounts.sync_from_env()
    job_id = queue_job(services)
    services.queue._schedule_once()
    assert services.db.get_job(job_id)["error_code"] == "no_account_available"


async def test_busy_accounts_keep_job_queued(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.accounts.busy.add("alpha")
    job_id = queue_job(services)
    services.queue._schedule_once()
    assert status(services, job_id) == "queued"


async def test_recovery_resumes_polling_and_never_resubmits(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    running = queue_job(services)
    services.db.update_job(running, status="running", submit_id="submit-9", account_id="alpha")
    orphan = queue_job(services)
    services.db.update_job(orphan, status="submitting", account_id="alpha")
    dreamina.history["submit-9"] = SUCCESS
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, running) == "succeeded")
    finally:
        await services.queue.stop()
    assert status(services, running) == "succeeded"
    assert services.db.get_job(orphan)["error_code"] == "upstream_timeout"
    assert services.browsers.generate_calls == []


async def test_refresh_renews_login_once(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    dreamina.expired = True
    real_login = services.browsers.login

    async def login(*args):
        dreamina.expired = False
        return await real_login(*args)

    services.browsers.login = login
    account = await services.accounts.refresh_credits("alpha")
    assert account["session_state"] == "ready"
    assert services.browsers.login_calls == ["alpha"]


# review regressions -----------------------------------------------------------------
from dreamina.browser import BrowserError
from dreamina.http_client import parse_history


async def test_error_after_submit_is_polled_not_failed(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.submit_then_error = BrowserError("page crashed after sending")
    dreamina.history["submit-1"] = SUCCESS
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert job["status"] == "succeeded" and job["submit_id"] == "submit-1"
    assert len(services.browsers.generate_calls) == 1


async def test_rejection_after_request_is_still_final(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.submit_then_error = GenerationRejected("ret=1014 insufficient", ret="1014")
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    assert services.db.get_job(job_id)["error_code"] == "upstream_rejected"


async def test_passwords_are_redacted_from_stored_errors(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.generate_error = BrowserError('Locator.fill: timeout fill("alpha-pass")')
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    message = services.db.get_job(job_id)["error_message"]
    assert "alpha-pass" not in message and "***" in message


async def test_logging_in_account_keeps_jobs_queued(services):
    services.accounts.sync_from_env()
    services.db.set_account_state("alpha", "logging_in")
    job_id = queue_job(services)
    services.queue._schedule_once()
    assert status(services, job_id) == "queued"


async def test_credit_refresh_does_not_clear_attention(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    gate = asyncio.Event()
    original = type(dreamina.client(services.settings.data_dir / "accounts" / "alpha" / "x")).credits

    async def slow_credits(self):
        await gate.wait()
        return await original(self)

    from conftest import FakeHTTP

    FakeHTTP.credits, saved = slow_credits, FakeHTTP.credits
    try:
        task = asyncio.create_task(services.accounts.refresh_credits("alpha", renew=False))
        await asyncio.sleep(0.01)
        services.db.set_account_state("alpha", "attention", "shark")
        gate.set()
        await task
    finally:
        FakeHTTP.credits = saved
    assert services.db.get_account("alpha")["session_state"] == "attention"


async def test_account_leaves_rotation_before_removal_awaits(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    seen = []

    async def close(account_id):
        seen.append(services.accounts.pick(1))

    services.browsers.close = close
    await services.accounts.remove("alpha")
    assert seen == [None]
    assert services.db.get_account("alpha") is None


def test_colliding_account_ids_are_rejected(services):
    services.accounts.sync_from_env()
    with pytest.raises(AccountError) as excinfo:
        services.accounts.add("a--b", "", "x@example.com", "pw-123456")
    assert excinfo.value.code == "invalid_input"
    services.accounts.add("a-b", "", "x@example.com", "pw-123456")
    with pytest.raises(AccountError):
        services.accounts.add("a-b", "", "y@example.com", "pw-123456")


def test_history_skips_empty_video_urls():
    record = {"status": 50, "video_url": "", "item_list": [{"video": {"video_url": "https://cdn/real.mp4"}}]}
    assert parse_history(record).video_url == "https://cdn/real.mp4"


async def test_success_without_url_waits_for_media_instead_of_failing(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    dreamina.history["submit-1"] = {"status": 50, "credits_consume": 84}
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert job["error_code"] == "media_pending" and not job["output_url"]

    dreamina.history["submit-1"] = {"status": 50, "credits_consume": 84, "video_url": "https://cdn/late.mp4"}
    await services.queue.maintain()
    job = services.db.get_job(job_id)
    assert job["output_url"] == "https://cdn/late.mp4" and job["error_code"] is None


# retries before submitting, and models that differ by account -------------------------
from dreamina.browser import ModelUnavailable, NotSubmitted


def fail_first(services, *errors):
    """Make the fake browser raise these errors on its first calls, then succeed."""
    pending = list(errors)
    real = services.browsers.generate

    async def generate(account_id, *args, **kwargs):
        if pending:
            services.browsers.generate_calls.append((account_id, None))
            error = pending.pop(0)
            raise error(account_id) if callable(error) and not isinstance(error, Exception) else error
        return await real(account_id, *args, **kwargs)

    services.browsers.generate = generate


async def test_failure_before_submitting_is_retried_for_free(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    make_ready(services, "beta")
    fail_first(services, BrowserError("reference mode index 0 was not applied"))
    dreamina.history["submit-1"] = SUCCESS
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    first, second = [account for account, _ in services.browsers.generate_calls]
    assert job["attempts"] == 1 and first != second  # moved to the other account
    assert job["error_message"] is None  # the retry note is cleared once submitted


async def test_retry_keeps_the_uploaded_files(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    make_ready(services, "beta")
    job_id = queue_job(services)
    inputs = services.settings.jobs_dir / job_id / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "image1.png").write_bytes(b"png")
    seen = []
    real = services.browsers.generate

    async def generate(account_id, *args, **kwargs):
        seen.append((inputs / "image1.png").exists())
        if len(seen) == 1:
            services.browsers.generate_calls.append((account_id, None))
            raise BrowserError("reference upload did not finish")
        return await real(account_id, *args, **kwargs)

    services.browsers.generate = generate
    dreamina.history["submit-1"] = SUCCESS
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    assert seen == [True, True]  # the retry still has its files
    assert not inputs.exists()  # removed once submitted


async def test_retries_stop_after_three_attempts(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.browsers.generate_error = NotSubmitted("no generate request was sent; nothing was charged")
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "failed")
    finally:
        await services.queue.stop()
    job = services.db.get_job(job_id)
    assert len(services.browsers.generate_calls) == 3 and job["attempts"] == 2
    assert "nothing was charged" in job["error_message"]


async def test_jobs_go_only_to_accounts_offering_the_model(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")  # never used: picked first if it qualified
    make_ready(services, "beta", last_used="2020-01-01T00:00:00+00:00")
    services.accounts.record_models("alpha", ["dreamina_seedance_40_mini"])  # a region without 2.5 Flash
    dreamina.history["submit-1"] = SUCCESS
    job_id = queue_job(services)  # seedance-2.5-flash
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    assert [account for account, _ in services.browsers.generate_calls] == ["beta"]
    assert services.accounts.view(services.db.get_account("alpha"))["models"] == ["dreamina_seedance_40_mini"]


async def test_missing_model_moves_the_job_and_is_remembered(services, dreamina):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")  # never used: picked first
    make_ready(services, "beta", last_used="2020-01-01T00:00:00+00:00")

    def unavailable(account_id):
        services.accounts.record_models(account_id, ["dreamina_seedance_40_mini"])  # what the browser reports
        return ModelUnavailable("model dreamina_seedance_45_flash is not offered to this account")

    fail_first(services, unavailable)
    dreamina.history["submit-1"] = SUCCESS
    job_id = queue_job(services)
    await services.queue.start()
    try:
        await wait_for(lambda: status(services, job_id) == "succeeded")
    finally:
        await services.queue.stop()
    assert [account for account, _ in services.browsers.generate_calls] == ["alpha", "beta"]
    assert services.accounts.pick(84, "dreamina_seedance_45_flash")["id"] == "beta"


def test_capacity_check_refuses_a_model_no_account_offers(services):
    services.accounts.sync_from_env()
    make_ready(services, "alpha")
    services.accounts.record_models("alpha", ["dreamina_seedance_40_mini"])
    with pytest.raises(AccountError) as excinfo:
        services.accounts.check_capacity(84, "dreamina_seedance_45_flash")
    assert (excinfo.value.code, excinfo.value.status) == ("no_account_available", 503)
    services.accounts.check_capacity(52, "dreamina_seedance_40_mini")  # offered: fine

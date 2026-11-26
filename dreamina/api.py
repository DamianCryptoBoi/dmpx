"""Worker-facing /v1 API and the public health check."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException

from .accounts import AccountError
from .catalog import PRICES, ValidationError, estimate_credits, get_model, public_catalog, validate_generation
from .db import now_iso
from .http_client import link_expiry, parse_history
from .media import UploadError, save_upload
from .security import api_key_matches, bearer_token

log = logging.getLogger(__name__)
MAX_FORM_FIELDS = 20
MAX_TEXT_PART_BYTES = 256 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def services(request: Request) -> Any:
    return request.app.state.services


def require_api_key(request: Request) -> None:
    settings = services(request).settings
    if not settings.api_key:
        raise ApiError(503, "api_disabled", "the API key is not configured on the server")
    if not api_key_matches(bearer_token(request.headers.get("authorization")), settings.api_key):
        raise ApiError(401, "unauthorized", "missing or invalid API key")


# generation creation (shared with the dashboard) -----------------------------
def _uploads(form: FormData, name: str) -> list[UploadFile]:
    items = form.getlist(name)
    files = [item for item in items if isinstance(item, UploadFile)]
    if len(files) != len(items):
        raise ApiError(400, "invalid_input", f"{name} must be file uploads")
    return [item for item in files if item.filename]


def _one_upload(form: FormData, name: str) -> UploadFile | None:
    files = _uploads(form, name)
    if len(files) > 1:
        raise ApiError(400, "invalid_input", f"only one {name} is allowed")
    return files[0] if files else None


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    if isinstance(value, UploadFile):
        raise ApiError(400, "invalid_input", f"{name} must be a text field")
    return (value or "").strip()


async def read_form(request: Request, *, max_files: int = 64, max_fields: int = MAX_FORM_FIELDS) -> FormData:
    settings = services(request).settings
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_request_bytes:
        raise ApiError(413, "invalid_input", f"request exceeds {settings.max_request_bytes} bytes")
    try:
        return await request.form(max_files=max_files, max_fields=max_fields, max_part_size=MAX_TEXT_PART_BYTES)
    except MultiPartException as exc:
        raise ApiError(400, "invalid_input", f"malformed form: {exc.message}") from exc


async def create_generation(svc: Any, form: FormData, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
    """Validate, store, and enqueue a generation. Returns (job, created)."""
    key = (idempotency_key or "").strip()
    if not key:
        raise ApiError(400, "invalid_input", "Idempotency-Key header is required")
    if len(key) > 200:
        raise ApiError(400, "invalid_input", "Idempotency-Key must be at most 200 characters")

    model = _text(form, "model")
    prompt = _text(form, "prompt")
    resolution = _text(form, "resolution")
    aspect_ratio = _text(form, "aspect_ratio") or None
    raw_duration = _text(form, "duration_seconds")
    try:
        duration = int(raw_duration)
    except ValueError as exc:
        raise ApiError(400, "invalid_input", "duration_seconds must be an integer") from exc
    images = _uploads(form, "images")
    audio = _uploads(form, "audio")
    first_frame = _one_upload(form, "first_frame")
    last_frame = _one_upload(form, "last_frame")

    def validate(image_paths, audio_paths, first, last):
        try:
            return validate_generation(
                model_name=model,
                prompt=prompt,
                duration_seconds=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                images=image_paths,
                audio=audio_paths,
                first_frame=first,
                last_frame=last,
            )
        except ValidationError as exc:
            raise ApiError(400, "invalid_input", str(exc)) from exc

    # Counts, tokens, and settings are checked before anything touches disk.
    validate(
        [f.filename for f in images],
        [f.filename for f in audio],
        first_frame.filename if first_frame else None,
        last_frame.filename if last_frame else None,
    )

    settings = svc.settings
    job_id = uuid.uuid4().hex
    job_dir = settings.jobs_dir / job_id
    inputs = job_dir / "inputs"
    hashes: dict[str, Any] = {"images": [], "audio": [], "first_frame": None, "last_frame": None}
    try:
        image_paths = []
        for index, upload in enumerate(images, 1):
            path, _mime, _size, digest = await save_upload(
                upload, inputs / f"image{index}", kind="image", max_bytes=settings.max_image_bytes
            )
            image_paths.append(str(path))
            hashes["images"].append(digest)
        audio_paths = []
        for index, upload in enumerate(audio, 1):
            path, _mime, _size, digest = await save_upload(
                upload, inputs / f"audio{index}", kind="audio", max_bytes=settings.max_audio_bytes
            )
            audio_paths.append(str(path))
            hashes["audio"].append(digest)
        frame_paths: dict[str, str | None] = {"first_frame": None, "last_frame": None}
        for name, upload in (("first_frame", first_frame), ("last_frame", last_frame)):
            if upload:
                path, _mime, _size, digest = await save_upload(
                    upload, inputs / name, kind="image", max_bytes=settings.max_image_bytes
                )
                frame_paths[name] = str(path)
                hashes[name] = digest
    except UploadError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise ApiError(400, "invalid_input", str(exc)) from exc
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    spec = validate(image_paths, audio_paths, frame_paths["first_frame"], frame_paths["last_frame"])
    canonical = {
        "model": spec.model.name,
        "prompt": spec.prompt,
        "duration_seconds": spec.duration_seconds,
        "resolution": spec.resolution,
        "aspect_ratio": spec.aspect_ratio,
        "files": hashes,
    }
    request_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()

    def replay() -> tuple[dict[str, Any], bool]:
        shutil.rmtree(job_dir, ignore_errors=True)
        existing = svc.db.get_job_by_idempotency(key)
        if existing["request_hash"] != request_hash:
            raise ApiError(409, "idempotency_conflict", "Idempotency-Key was already used for a different request")
        return existing, False

    if svc.db.get_job_by_idempotency(key):
        return replay()
    try:
        svc.accounts.check_capacity(spec.estimate_credits)
    except AccountError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise ApiError(exc.status, exc.code, exc.message) from exc

    try:
        svc.db.create_job(
            {
                "id": job_id,
                "idempotency_key": key,
                "request_hash": request_hash,
                "model": spec.model.name,
                "prompt": spec.prompt,
                "duration_seconds": spec.duration_seconds,
                "resolution": spec.resolution,
                "aspect_ratio": spec.aspect_ratio,
                "mode": spec.mode,
                "images": image_paths,
                "audio": audio_paths,
                "first_frame_path": frame_paths["first_frame"],
                "last_frame_path": frame_paths["last_frame"],
                "status": "queued",
                "request": {"credits_estimate": spec.estimate_credits, "files": hashes},
                "created_at": now_iso(),
            }
        )
    except sqlite3.IntegrityError:
        return replay()
    svc.queue.wake()
    return svc.db.get_job(job_id), True


# representations ---------------------------------------------------------------
_BRANDS = re.compile(r"dreamina|capcut", re.IGNORECASE)

# Client-facing text for failures whose internal detail means nothing to a caller.
_PUBLIC_ERRORS = {
    "session_expired": "the generation could not start; nothing was charged",
    "no_account_available": "no generation capacity was available",
}


def _neutral(text: str | None) -> str | None:
    """Last line of defence: no upstream product name reaches API clients."""
    return _BRANDS.sub("upstream", text) if text else text


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def public_job(job: dict[str, Any], *, base: str = "/v1/generations", internal: bool = False) -> dict[str, Any]:
    """API view of a job. `internal` adds operator-only detail for the dashboard."""
    try:
        estimate = estimate_credits(get_model(job["model"]), int(job["duration_seconds"]), job["resolution"])
    except ValidationError:
        estimate = None
    media = None
    if job["status"] == "succeeded":
        if job["error_code"] == "media_expired":
            media = "expired"
        elif job["output_url"]:
            media = "ready"
        else:
            media = "pending"
    error = None
    if job["status"] == "failed":
        code = job["error_code"] or "upstream_rejected"
        message = job["error_message"] or ""
        if not internal:
            message = _PUBLIC_ERRORS.get(code) or _neutral(message)
        error = {"code": code, "message": message}
    notes = job["error_message"] if job["status"] != "failed" else None
    view = {
        "id": job["id"],
        "status": job["status"],
        "model": job["model"],
        "prompt": job["prompt"],
        "duration_seconds": job["duration_seconds"],
        "resolution": job["resolution"],
        "aspect_ratio": job["aspect_ratio"],
        "mode": job["mode"],
        "references": {
            "images": len(job["images_json"]),
            "audio": len(job["audio_json"]),
            "first_frame": bool(job["first_frame_path"]),
            "last_frame": bool(job["last_frame_path"]),
        },
        "credits_estimate": estimate,
        "credits_used": job["credits_used"],
        "media": media,
        "content_url": f"{base}/{job['id']}/content" if media == "ready" else None,
        "video_url": job["output_url"] if media == "ready" else None,
        "video_url_expires_at": _iso(link_expiry(job["output_url"])) if media == "ready" else None,
        "error": error,
        "notes": notes if internal else _neutral(notes),
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    if internal:
        view.update(account_id=job["account_id"], submit_id=job["submit_id"])
    return view


LINK_MARGIN = timedelta(minutes=30)
MEDIA_GONE = "the video is no longer available"


async def refresh_link(svc: Any, job: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Return the job with a result link valid for at least LINK_MARGIN.

    Result links are pre-signed and expire (about six hours), so a link that
    is unknown, expired or about to expire is renewed from the generation
    history. With `strict`, a renewal failure raises (410 when the video is
    gone, 502 when upstream is unreachable and the old link is unusable);
    otherwise the job is returned as it is.
    """
    if job["status"] != "succeeded" or not job["output_url"] or job["error_code"] == "media_expired":
        return job
    expires = link_expiry(job["output_url"])
    now = datetime.now(timezone.utc)
    if expires and expires - now > LINK_MARGIN:
        return job
    try:
        await _renew_link(svc, job)
    except ApiError as exc:
        if exc.code == "media_expired":
            if strict:
                raise
        elif strict and not (expires and expires > now):
            raise
        else:
            log.info("could not renew the result link for %s: %s", job["id"], exc.message)
    return svc.db.get_job(job["id"]) or job


async def media_redirect(svc: Any, job: dict[str, Any] | None) -> RedirectResponse:
    """Send the client to the result itself; the video never passes through here."""
    if job is None:
        raise ApiError(404, "not_found", "generation not found")
    if job["status"] != "succeeded":
        raise ApiError(409, "not_ready", f"generation is {job['status']}")
    if job["error_code"] == "media_expired":
        raise ApiError(410, "media_expired", MEDIA_GONE)
    if not job["output_url"]:
        raise ApiError(409, "not_ready", "the video link is not available yet")
    job = await refresh_link(svc, job, strict=True)
    return RedirectResponse(job["output_url"], status_code=302, headers={"cache-control": "no-store"})


async def _renew_link(svc: Any, job: dict[str, Any]) -> str:
    try:
        account = svc.accounts.get(job["account_id"])
    except AccountError:
        account = None
    if account is None or not job["submit_id"]:
        svc.db.update_job(job["id"], error_code="media_expired", error_message=MEDIA_GONE)
        raise ApiError(410, "media_expired", MEDIA_GONE)
    try:
        async with svc.accounts.client(account) as client:
            record = await client.history_record(job["submit_id"])
    except Exception as exc:
        raise ApiError(502, "upstream_unavailable", "the video link could not be renewed right now; try again") from exc
    url = parse_history(record).video_url if record else None
    if not url:
        svc.db.update_job(job["id"], error_code="media_expired", error_message=MEDIA_GONE)
        raise ApiError(410, "media_expired", MEDIA_GONE)
    svc.db.update_job(job["id"], output_url=url)
    return url


def credit_summary(svc: Any) -> dict[str, Any]:
    accounts = svc.accounts.list()
    usable = {account["id"] for account in svc.accounts.usable()}
    return {
        "total_available": sum(account["credits"] or 0 for account in accounts if account["id"] in usable),
        "accounts": [
            {
                "id": account["id"],
                "label": account["label"],
                "state": account["state"],
                "busy": account["busy"],
                "credits": account["credits"],
                "credits_updated_at": account["credits_updated_at"],
            }
            for account in accounts
        ],
    }


# routes -------------------------------------------------------------------------
router = APIRouter()
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@v1.get("/models")
async def models() -> dict[str, Any]:
    return {"models": public_catalog(), "pricing_source": PRICES.source, "pricing_updated_at": PRICES.updated_at}


@v1.post("/generations")
async def post_generation(request: Request) -> JSONResponse:
    svc = services(request)
    form = await read_form(request)
    try:
        job, created = await create_generation(svc, form, request.headers.get("idempotency-key"))
    finally:
        await form.close()
    location = f"/v1/generations/{job['id']}"
    return JSONResponse(public_job(job), status_code=202 if created else 200, headers={"Location": location})


@v1.get("/generations")
async def list_generations(request: Request, limit: int = 20, offset: int = 0, status: str | None = None):
    svc = services(request)
    limit = max(1, min(100, limit))
    offset = max(0, offset)
    if status and status not in {"queued", "submitting", "running", "succeeded", "failed"}:
        raise ApiError(400, "invalid_input", "unknown status filter")
    jobs = svc.db.list_jobs(limit=limit, offset=offset, status=status)
    return {
        "data": [public_job(job) for job in jobs],
        "limit": limit,
        "offset": offset,
        "total": svc.db.count_jobs(status),
    }


@v1.get("/generations/{job_id}")
async def get_generation(request: Request, job_id: str):
    svc = services(request)
    job = svc.db.get_job(job_id)
    if job is None:
        raise ApiError(404, "not_found", "generation not found")
    return public_job(await refresh_link(svc, job))


@v1.get("/generations/{job_id}/content")
async def get_generation_content(request: Request, job_id: str):
    svc = services(request)
    return await media_redirect(svc, svc.db.get_job(job_id))


@v1.get("/credits")
async def credits(request: Request):
    return credit_summary(services(request))

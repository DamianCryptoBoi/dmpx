"""Server-rendered admin dashboard."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .accounts import AccountError
from .api import ApiError, create_generation, credit_summary, media_response, public_job, read_form, services
from .catalog import ASPECT_RATIOS, MODELS, PRICES, public_catalog
from .security import csrf_matches, verify_password

COOKIE = "dreamina_admin"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/admin")


class LoginRequired(Exception):
    pass


def client_ip(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    # cloudflared connects from loopback; only then is the header trustworthy.
    if host in {"127.0.0.1", "::1"}:
        return request.headers.get("cf-connecting-ip", host)
    return host


def session(request: Request) -> dict[str, Any]:
    payload = services(request).signer.verify(request.cookies.get(COOKIE))
    if payload is None:
        raise LoginRequired()
    return payload


async def checked_form(request: Request):
    """Parse a mutation's form and enforce its CSRF token."""
    payload = session(request)
    form = await read_form(request)
    token = form.get("csrf")
    if not csrf_matches(token if isinstance(token, str) else None, payload["csrf"]):
        await form.close()
        raise ApiError(403, "csrf_failed", "the form expired; reload the page and try again")
    return payload, form


def flash(request: Request, payload: dict[str, Any], kind: str, message: str) -> None:
    services(request).flashes[payload["csrf"]] = (kind, message)


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def render(request: Request, name: str, payload: dict[str, Any], **context: Any) -> HTMLResponse:
    svc = services(request)
    return templates.TemplateResponse(
        request,
        name,
        {
            "csrf": payload["csrf"],
            "flash": svc.flashes.pop(payload["csrf"], None),
            "nav": name.split(".")[0],
            **context,
        },
    )


# authentication ---------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    svc = services(request)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "disabled": not svc.settings.admin_password_hash}
    )


@router.post("/login")
async def login(request: Request):
    svc = services(request)
    disabled = not svc.settings.admin_password_hash
    ip = client_ip(request)
    # Unauthenticated: accept no file parts, so nothing is spooled to disk.
    form = await read_form(request, max_files=0, max_fields=4)
    password = form.get("password")
    await form.close()
    error = None
    if disabled:
        error = "Admin login is disabled until ADMIN_PASSWORD_HASH is set."
    elif svc.throttle.blocked(ip):
        error = "Too many failed attempts. Try again later."
    elif not isinstance(password, str) or not verify_password(password, svc.settings.admin_password_hash):
        svc.throttle.record_failure(ip)
        error = "Incorrect password."
    if error:
        return templates.TemplateResponse(
            request, "login.html", {"error": error, "disabled": disabled}, status_code=401
        )
    svc.throttle.reset(ip)
    issued = svc.signer.issue()
    response = back("/admin")
    response.set_cookie(
        COOKIE,
        issued.token,
        max_age=svc.settings.session_ttl_seconds,
        httponly=True,
        secure=svc.settings.secure_cookies,
        samesite="strict",
        path="/admin",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    _payload, form = await checked_form(request)
    await form.close()
    response = back("/admin/login")
    response.delete_cookie(COOKIE, path="/admin")
    return response


# pages ----------------------------------------------------------------------------
def overview_data(svc: Any) -> dict[str, Any]:
    counts = svc.db.counts_by_status()
    accounts = svc.accounts.list()
    return {
        "credits": credit_summary(svc)["total_available"],
        "accounts_ready": sum(1 for account in accounts if account["state"] == "ready"),
        "accounts_total": len(accounts),
        "active": counts.get("submitting", 0) + counts.get("running", 0),
        "queued": counts.get("queued", 0),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
    }


@router.get("", response_class=HTMLResponse)
async def overview(request: Request):
    payload = session(request)
    svc = services(request)
    return render(
        request,
        "overview.html",
        payload,
        stats=overview_data(svc),
        failures=[public_job(job) for job in svc.db.recent_failures(5)],
        recent=[public_job(job) for job in svc.db.list_jobs(limit=8)],
        warnings=svc.settings.warnings,
    )


@router.get("/status.json")
async def status_json(request: Request):
    try:
        session(request)
    except LoginRequired:
        return JSONResponse({"error": "login required"}, status_code=401)
    return overview_data(services(request))


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    payload = session(request)
    return render(request, "accounts.html", payload, accounts=services(request).accounts.list())


@router.post("/accounts")
async def add_account(request: Request):
    payload, form = await checked_form(request)
    await form.close()
    svc = services(request)
    try:
        account = svc.accounts.add(
            str(form.get("account_id") or ""),
            str(form.get("label") or ""),
            str(form.get("email") or ""),
            str(form.get("password") or ""),
        )
    except AccountError as exc:
        flash(request, payload, "error", exc.message)
        return back("/admin/accounts")
    svc.accounts.start_login(account["id"])
    flash(request, payload, "ok", f"Added {account['id']}; logging in now.")
    return back("/admin/accounts")


async def _account_action(request: Request, account_id: str, action: str) -> Response:
    payload, form = await checked_form(request)
    await form.close()
    svc = services(request)
    try:
        if action == "update":
            svc.accounts.update(
                account_id,
                label=str(form.get("label") or "") or None,
                email=str(form.get("email") or "") or None,
                password=str(form.get("password") or "") or None,
            )
            flash(request, payload, "ok", f"Updated {account_id}.")
        elif action == "toggle":
            enabled = not bool(svc.accounts.get(account_id)["enabled"])
            svc.accounts.set_enabled(account_id, enabled)
            flash(request, payload, "ok", f"{'Enabled' if enabled else 'Disabled'} {account_id}.")
        elif action == "login":
            svc.accounts.start_login(account_id)
            flash(request, payload, "ok", f"Logging in {account_id}; this takes about a minute.")
        elif action == "refresh":
            account = await svc.accounts.refresh_credits(account_id, renew=False)
            flash(request, payload, "ok", f"{account_id} has {account['credits']} credits.")
        elif action == "remove":
            if form.get("confirm") != account_id:
                flash(request, payload, "error", "Type the account id to confirm removal.")
            else:
                await svc.accounts.remove(account_id)
                flash(request, payload, "ok", f"Removed {account_id}.")
    except AccountError as exc:
        flash(request, payload, "error", exc.message)
    except Exception as exc:
        flash(request, payload, "error", f"{action} failed: {svc.accounts.error_text(exc)}")
    return back("/admin/accounts")


@router.post("/accounts/{account_id}/{action}")
async def account_action(request: Request, account_id: str, action: str):
    if action not in {"update", "toggle", "login", "refresh", "remove"}:
        raise ApiError(404, "not_found", "unknown action")
    return await _account_action(request, account_id, action)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, page: int = 1, status: str | None = None):
    payload = session(request)
    svc = services(request)
    page = max(1, page)
    per_page = 25
    status = status if status in {"queued", "submitting", "running", "succeeded", "failed"} else None
    total = svc.db.count_jobs(status)
    jobs = [
        public_job(job, base="/admin/jobs")
        for job in svc.db.list_jobs(limit=per_page, offset=(page - 1) * per_page, status=status)
    ]
    return render(
        request,
        "jobs.html",
        payload,
        jobs=jobs,
        page=page,
        pages=max(1, (total + per_page - 1) // per_page),
        status=status,
        models=list(MODELS.values()),
        aspect_ratios=ASPECT_RATIOS,
        resolutions=sorted({r for model in MODELS.values() for r in model.resolutions}, key=_resolution_order),
        idempotency_key=uuid.uuid4().hex,
    )


def _resolution_order(value: str) -> int:
    return {"480p": 0, "720p": 1, "1080p": 2, "2k": 3, "4k": 4}.get(value, 9)


@router.post("/jobs")
async def create_job(request: Request):
    payload, form = await checked_form(request)
    svc = services(request)
    try:
        key = form.get("idempotency_key")
        job, _created = await create_generation(svc, form, key if isinstance(key, str) else None)
    except ApiError as exc:
        flash(request, payload, "error", exc.message)
        return back("/admin/jobs")
    finally:
        await form.close()
    return back(f"/admin/jobs/{job['id']}")


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str):
    payload = session(request)
    job = services(request).db.get_job(job_id)
    if job is None:
        raise ApiError(404, "not_found", "generation not found")
    return render(request, "job.html", payload, job=public_job(job, base="/admin/jobs"))


@router.get("/jobs/{job_id}/content")
async def job_content(request: Request, job_id: str):
    session(request)
    return media_response(services(request).db.get_job(job_id))


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    payload = session(request)
    return render(
        request,
        "models.html",
        payload,
        models=public_catalog(),
        pricing_source=PRICES.source,
        pricing_updated_at=PRICES.updated_at,
    )

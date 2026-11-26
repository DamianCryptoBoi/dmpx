"""Application factory: wires settings, persistence, browsers, queue, and routes.

Run with exactly one process: `uvicorn --factory dreamina.app:create_app`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import admin, api
from .accounts import AccountError, AccountService, HttpFactory
from .browser import BrowserManager
from .config import Settings
from .db import Database
from .envstore import EnvStore
from .http_client import DreaminaHTTP
from .jobs import JobQueue
from .security import LoginThrottle, SessionSigner

log = logging.getLogger("dreamina")


@dataclass
class Services:
    settings: Settings
    db: Database
    env: EnvStore
    browsers: Any
    accounts: AccountService
    queue: JobQueue
    signer: SessionSigner
    throttle: LoginThrottle
    flashes: dict[str, tuple[str, str]] = field(default_factory=dict)


def build_services(
    settings: Settings,
    *,
    browsers: Any | None = None,
    http_factory: HttpFactory = DreaminaHTTP.from_cookie_file,
) -> Services:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    env = EnvStore(settings.env_path)
    browsers = browsers or BrowserManager(settings, env)
    accounts = AccountService(settings, db, env, browsers, http_factory)
    return Services(
        settings=settings,
        db=db,
        env=env,
        browsers=browsers,
        accounts=accounts,
        queue=JobQueue(settings, db, accounts, browsers),
        signer=SessionSigner(settings.session_secret, settings.session_ttl_seconds),
        throttle=LoginThrottle(settings.login_throttle_attempts, settings.login_throttle_seconds),
    )


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or (services.settings if services else Settings.from_env())
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every upstream URL at INFO; keep upstream details out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc: Services = app.state.services
        for warning in settings.warnings:
            log.warning(warning)
        svc.accounts.sync_from_env()
        await svc.queue.start()
        try:
            yield
        finally:
            await svc.queue.stop()
            await svc.accounts.wait_for_logins()
            await svc.browsers.close_all()
            svc.db.close()

    app = FastAPI(title="Dreamina proxy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services or build_services(settings)
    app.mount("/admin/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(api.router)
    app.include_router(api.v1)
    app.include_router(admin.router)

    def is_admin(request: Request) -> bool:
        return request.url.path.startswith("/admin")

    @app.exception_handler(api.ApiError)
    async def api_error(request: Request, exc: api.ApiError):
        if is_admin(request):
            return admin.templates.TemplateResponse(
                request, "error.html", {"code": exc.code, "message": exc.message}, status_code=exc.status
            )
        return api.error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(AccountError)
    async def account_error(request: Request, exc: AccountError):
        return api.error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(admin.LoginRequired)
    async def login_required(request: Request, exc: admin.LoginRequired):
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()) if part not in {"query", "path", "body"})
        return api.error_response(400, "invalid_input", f"{where}: {first.get('msg', 'invalid value')}".strip(": "))

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin", status_code=303)

    return app

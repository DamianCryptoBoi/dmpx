# Dreamina Proxy Implementation Plan

Date: 2026-11-26
Spec: `docs/superpowers/specs/2026-11-26-dreamina-proxy-design.md`
Status: ready to execute

## Goal

Ship a single-process FastAPI service that exposes an authenticated `/v1` video-generation API backed by multiple Dreamina accounts, with a password-protected admin dashboard, a persistent SQLite queue, ShardBrowser UI automation, and PM2/cloudflared deployment files.

## Non-negotiable constraints

- No Docker, Redis, Celery, PostgreSQL, or frontend build step.
- One Uvicorn process; queue and browser coordination stay in-process.
- Passwords live only in `.env` (mode `0600`), never in SQLite, templates, logs, or API responses.
- Automation never branches on visible/localized text. Control flow uses `data-*` attributes, language-neutral input values, indices, and DOM relationships only.
- Paid generation is never retried after a generate request may have been sent. Recovery uses `submit_id` + history.
- Live generation tests are opt-in and cost credits.

## Architecture

```
Cloudflare Worker ── Bearer key ─┐
                                 ├─ cloudflared → 127.0.0.1:8000 → Uvicorn
Admin browser ── signed cookie ──┘
                                      FastAPI
                    ┌──────────────────┼──────────────────┐
                 SQLite             Job queue          ShardX + Playwright
            accounts / jobs     scheduler + workers     login + UI generation
                    │                  │                      │
              data/jobs inputs     HTTPX read-only       per-account profile
              media outputs        credits/history       cookies (0600)
```

## File map

```
dreamina/
  __init__.py
  config.py          settings, paths, env parsing helpers
  envstore.py        atomic .env read/write with flock, account credentials
  db.py              SQLite schema, WAL, account/job repositories
  catalog.py         model catalog, validation, pricing, prompt tokens
  security.py        API key, PBKDF2 admin hash, signed sessions, CSRF, throttle
  http_client.py     signed read-only Dreamina calls: credits, history, download
  browser.py         ShardX launch, login, language-neutral UI generation
  accounts.py        account CRUD, session states, credits, eligibility
  queue.py           scheduler, workers, polling, media retention, recovery
  media.py           safe file storage, MIME checks, retention cleanup
  api.py             /health and /v1 routes
  admin.py           dashboard routes and form actions
  app.py             application factory, lifespan, router wiring
  templates/         base, login, overview, accounts, jobs, models
  static/app.css     dashboard styling
  static/app.js      small progressive enhancements
scripts/
  setup_admin.py     generate ADMIN_PASSWORD_HASH
  smoke_api.py       non-generating API smoke test
tests/
  test_catalog.py
  test_envstore.py
  test_security.py
  test_scheduler.py
  test_api.py
  test_browser_flow.py   mocked DOM/request validation
requirements.txt
.env.example
ecosystem.config.cjs
README.md
```

## Milestones and tasks

### 1. Configuration and persistence

1. `config.py`: dataclass `Settings` loaded from env. Defaults: data dir `.data`, DB `data/dreamina.db`, media retention 7 days, max concurrent 2, browser idle 600 s, session TTL 12 h, secure cookies on unless `DREAMINA_INSECURE_COOKIES=1`. Resolve relative to repo root.
2. `envstore.py`: line-preserving parser; `get`, `set_many`, `remove`; `flock` on `.env.lock`; temp-file + `os.replace`; `0600`; `reload` after write. Account keys `DREAMINA_ACCOUNT_<KEY>_EMAIL|PASSWORD`; legacy `DREAMINA_EMAIL|PASSWORD` maps to account `default`.
3. `db.py`: WAL, `busy_timeout=5000`, `foreign_keys=ON`. Tables `accounts` and `jobs` exactly as in spec §8. Repository methods for create/update/list, idempotency lookup, queue claim, recovery queries.
4. Tests: env round-trip, rollback on failed write, permissions, idempotency uniqueness.

### 2. Security

1. PBKDF2-HMAC-SHA256 hash format `pbkdf2_sha256$iterations$salt_b64$hash_b64`; constant-time verify.
2. Signed admin session token `base64url(json).hmac_sha256`; payload `{sub, exp, csrf}`; cookie `HttpOnly`, `SameSite=Strict`, `Secure` configurable.
3. CSRF token in session, required on every admin mutation via form field or `X-CSRF-Token`.
4. API key `Authorization: Bearer` with `secrets.compare_digest`; if unset, `/v1` returns `503 api_disabled`.
5. In-memory login throttle: 5 failures per 15 minutes per client IP, cleared on success.
6. Tests: wrong key, wrong CSRF, expired session, hash round-trip, throttle.

### 3. Catalog and validation

1. Four models with Dreamina keys, duration ranges, resolutions, aspect ratios, prompt/reference limits, and verified per-second fallback prices.
2. `validate_request()` checks model, duration, resolution, aspect, prompt length, image/audio counts and sizes, first/last-frame exclusivity, and reference token indices.
3. Prompt parser returns ordered segments and tokens (`@image1`, `@audio1`); tokens without files are invalid.
4. `estimate_credits()` = per-second price × duration; first/last-frame and unified references use the same no-input-video price.
5. Tests: every boundary, mode mixing, missing token targets, per-model limits, price estimates.

### 4. Read-only Dreamina client

1. Build cookies from a saved cookie JSON; derive 19-digit `webId`, region, and UA.
2. Signed POST helper exactly as verified: md5 of `9e2c|{path[-7:]}|7|8.4.0|{device_time}||11ac`, required headers/query.
3. Methods: `credits()`, `history_by_ids(ids)`, `download(url, dest)`, `find_record_value()`.
4. Classify `ret=-6`/Shark, auth expiry, transient network/5xx. Three attempts with jitter for transport/5xx only.
5. Tests: signature vector, cookie parsing, retry classification, history extraction.

### 5. Browser driver

1. `BrowserManager` per account: open/create ShardX profile, launch `headless=True, cdp=True`, connect Patchright, reuse page, export cookies/UA after login, close on idle/shutdown.
2. Login: guest login control via `data-main-frame-2026-guest-top-actions-ready`; email `input[type=email][name=username]`; continue `button[class*="continue-button"]`; password `input[type=password][name=password]`; verify with credits call. Missing password step or CAPTCHA iframe → `attention`.
3. Generation flow: fresh workspace, dismiss overlays, select mode by option index (`0` unified, `1` first/last; env overrides), select model by `data-model-key`, upload by drag/drop into `[id*="reference-upload"]`, set settings by discovering popovers via radio/spinbutton types, insert mention chips filtered by `li[role="option"]:has(img|svg)`, type prompt with keyboard events, submit with one guarded retry only when no generate response was observed.
4. Validate captured `/aigc_draft/generate` request: model key, duration ms, resolution, first/end frame objects, or unified material/meta types and counts. Persist `submit_id` before returning.
5. Tests with a fake page object: mode/model selection, chip mapping, localized-text ban, guarded submit, request validation.

### 6. Accounts, scheduling, queue

1. `AccountService`: sync env accounts into DB; add/update/remove; write-only password; login/retry; refresh credits; states `needs_login|logging_in|ready|attention|disabled`; busy set.
2. Eligibility: enabled + ready + cookies present + not busy + cached credits ≥ estimate (refresh if stale); sort least-recently-used.
3. `JobQueue`: scheduler loop; max concurrent; one job per account; `queued → submitting → running → succeeded|failed`; stable error codes; credits refresh after submit; media download; retention cleanup.
4. Startup recovery: queued re-enqueue; submitting with `submit_id` poll; running poll; succeeded missing media re-download; failed unchanged.
5. Tests: LRU selection, insufficient credits, no account, state transitions, recovery, no duplicate submit.

### 7. API and dashboard

1. `/health`, `/v1/models`, `/v1/generations` (POST multipart, GET list/detail/content), `/v1/credits`.
2. Idempotency: required header; canonical request hash including file hashes; replay returns original; mismatch 409.
3. Upload limits and MIME sniffing; store under `data/jobs/<id>/inputs`; remove temp uploads after browser use.
4. Admin: login/logout, overview, accounts (add/toggle/login/refresh/set-password/remove), jobs (create/detail/download), models.
5. Templates: server-rendered, no build; CSRF hidden fields; status refresh via small JS.
6. Tests: full mocked API flow, auth failures, idempotency replay/conflict, content authorization, dashboard login/CSRF.

### 8. Deployment and verification

1. `requirements.txt` pinned; `.env.example`; `ecosystem.config.cjs` fork mode, one instance, `127.0.0.1:8000`.
2. `scripts/setup_admin.py` prompts once and prints only the hash.
3. README: install, ShardX/Chromium libs, PM2 startup/save, cloudflared ingress, backups, API examples, live-test checklist, security notes.
4. Run unit/mocked tests locally. Live acceptance remains opt-in.

## Verification commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/setup_admin.py --check-env
.venv/bin/python scripts/smoke_api.py
```

## Risks

- Dreamina DOM changes: selectors are centralized in `browser.py`; diagnostics capture a screenshot on failure.
- Linux headless differences: acceptance requires the VPS smoke test before production traffic.
- Mixed image/audio chips are implemented but not yet live-submitted; the first paid acceptance test must verify `meta_list` contains both types.
- Multiple Chromium instances are memory-heavy; default concurrency is 2 with idle shutdown.

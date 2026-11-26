# Dreamina Multi-Account Proxy and Admin Dashboard

Date: 2026-11-26
Status: Approved design, pending implementation planning

## 1. Purpose and scope

Build a single-admin service on an Ubuntu/Debian x86_64 VPS that:

- Exposes an HTTPS API called by the owner's Cloudflare Workers.
- Automatically selects among multiple Dreamina accounts.
- Supports text, image-reference, first/last-frame, and audio-reference video generation.
- Supports Dreamina Seedance 2.5, 2.5 Flash, 2.0 Fast, and 2.0 Mini.
- Provides an admin dashboard for accounts, sessions, credits, generation jobs, and generated media.
- Runs without Docker using PM2, Uvicorn, and an existing cloudflared ingress.

This is a single-operator application, not a multi-tenant SaaS. The first release excludes reference video clips, video editing, Recreate, long-video mode, public unauthenticated access, horizontal scaling, and any CAPTCHA or Shark bypass.

## 2. Verified feasibility and remaining validation

### Verified with the owner's account

- Direct HTTP with the current cookies, user agent, and request signing succeeded for credits and history.
- Direct HTTP generation was rejected with `ret=-6`, `shark not pass reject`; no credits were charged. Direct generation submission is therefore not used.
- Headed ShardBrowser UI generation succeeded for text-only Seedance 2.5 Flash.
- Headless ShardBrowser first/last-frame generation succeeded for Seedance 2.5 Flash and consumed 84 credits.
- Headless image/audio generation succeeded and included both material types. Its `meta_list` contained only text, so proper @-mention construction is required in the implementation.
- Proper image/audio mention chips were subsequently created and inspected in the UI, but no additional tagged generation was submitted.
- Direct ImageX and VOD uploads succeeded after correcting VOD `FileType` to `video` for MP3 uploads and handling IPv4/IPv6 connectivity differences.
- All four models passed live text-only headless generation: Seedance 2.5 Flash (84 credits), Seedance 2.5 (156 credits, 4 s, 480p), Seedance 2.0 Fast (72 credits, 4 s, 720p), and Seedance 2.0 Mini (52 credits, 4 s, 720p). Outputs were H.264/AAC and matched the prompt.
- The three new model tests ran under Polish, Vietnamese, and Portuguese UI locales. Control flow used only `data-selector-option`/`data-model-key`, numeric radio values, numeric duration values, and `data-testid`; no localized label or ARIA text was read. Each captured `aigc_draft/generate` request validated the expected `model_req_key`, 4-second duration, resolution, and reference mode.

### Validation still required

- A properly tagged mixed image/audio generation through the production browser driver.
- Linux x64 deployment and headless behavior on the VPS.
- Session renewal, account addition through the dashboard, and restart recovery.

Live tests are always opt-in and require an explicit credit-cost confirmation from the owner.

## 3. Architecture

```text
Cloudflare Worker ──Bearer API key──┐
                                   ├─ Cloudflare HTTPS hostname
Admin browser ──admin login────────┘                │
                                              cloudflared
                                                   │
                                        127.0.0.1:8000 / Uvicorn
                                                   │
                                              FastAPI + PM2
                                                   │
                    ┌──────────────────────────────┼───────────────────────────┐
                    │                              │                           │
                 SQLite                    Headless ShardBrowser         HTTPX client
            accounts/jobs/idempotency       login + generation        credits/history/status
                    │                              │                           │
             Persistent data volume       Per-account profiles/cookies   Dreamina read-only APIs
```

### Main components

1. **FastAPI application**: Worker API, admin dashboard, authentication, validation, and static media delivery.
2. **Account manager**: env-backed credentials, cookies, Shard profiles, credit/session state, and account eligibility.
3. **Browser driver**: normal Dreamina UI automation for login, uploads, model/options selection, and generation submission.
4. **Read-only HTTP client**: credits, history, job status, model configuration, and pricing metadata.
5. **Persistent job queue**: automatic account selection, one active generation per account, restart recovery, and idempotency.
6. **Admin dashboard**: server-rendered templates and vanilla JavaScript, without a frontend build system.
7. **cloudflared**: the only public ingress; FastAPI and CDP remain on loopback/private interfaces.

## 4. Model catalog

Public API model names map to Dreamina as follows:

| Public name | Dreamina `model_req_key` | Duration scope | Resolutions |
|---|---|---:|---|
| `seedance-2.5` | `dreamina_seedance_45_pro` | 4–30 s | `480p`, `720p`, `1080p`, `4k` |
| `seedance-2.5-flash` | `dreamina_seedance_45_flash` | 4–30 s | `480p`, `720p` |
| `seedance-2.0-fast` | `dreamina_seedance_40` | 4–15 s | `720p`, `1080p`, `2k`, `4k` |
| `seedance-2.0-mini` | `dreamina_seedance_40_mini` | 4–15 s | `720p`, `1080p`, `2k`, `4k` |

All models support `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, and `9:16` where the selected UI mode permits them. First/last-frame mode uses adaptive aspect ratio.

Reference limits from the live configuration:

- Seedance 2.5: up to 30 images and 10 audio files.
- Seedance 2.5 Flash: up to 10 images and 5 audio files.
- Seedance 2.0 Fast/Mini: up to 9 images and 3 audio files.
- Audio is MP3/WAV, at most 15 MB per file. Duration limits are 30.2 seconds for 2.5 models and 15 seconds for 2.0 models.
- Prompt limits are 15,000 characters for 2.5 models and 4,000 for 2.0 models.

A model adapter owns each model key, constraints, commerce benefit selection, and payload/UI differences. Prices are fetched from Dreamina metadata and cached briefly; they are not hardcoded. The live configuration is checked periodically for changes, with the table above as a conservative fallback.

## 5. Worker API

All `/v1` endpoints except the minimal health check require `Authorization: Bearer <API_KEY>` and constant-time credential comparison.

| Endpoint | Behavior |
|---|---|
| `GET /health` | Liveness only; no sensitive information |
| `GET /v1/models` | Models, constraints, reference limits, and current pricing |
| `POST /v1/generations` | Validate and queue a generation; return `202 Accepted` |
| `GET /v1/generations` | Paginated recent jobs |
| `GET /v1/generations/{id}` | Job status, settings, result metadata, or stable error |
| `GET /v1/generations/{id}/content` | Stream the downloaded MP4 |
| `GET /v1/credits` | Aggregate and per-account credit availability without email addresses |

### Generation request

`POST /v1/generations` accepts `multipart/form-data`:

- `model`: required public model name.
- `prompt`: required text, with optional `@image1`, `@image2`, `@audio1`, etc.
- `duration_seconds`: validated per model.
- `resolution`: validated per model.
- `aspect_ratio`: validated for unified-reference mode.
- `images`: ordered image files for unified references.
- `audio`: ordered MP3/WAV files for unified references.
- `first_frame`: optional image used only in first/last-frame mode.
- `last_frame`: optional image used only with `first_frame`.

The API distinguishes unified-reference mode from first/last-frame mode based on the supplied frame fields; mixing modes returns `400 invalid_input`.

`Idempotency-Key` is required. The service stores a request hash with the key. A retry with the same key and identical request returns the original job; the same key with different content returns `409 Conflict`.

Successful enqueue returns a job ID, `queued` status, and a `Location` header. Job states are `queued`, `submitting`, `running`, `succeeded`, and `failed`. Public errors use stable codes including `invalid_input`, `unauthorized`, `insufficient_credits`, `no_account_available`, `session_expired`, `upstream_rejected`, `upstream_timeout`, and `not_found`.

Completed videos are downloaded promptly and exposed through the authenticated content endpoint. Generated media retention defaults to seven days and is configurable. Temporary upload files are removed after the browser finishes with them.

## 6. Accounts, credentials, and sessions

### Credential storage

Each account has an internal ID and env entries:

```dotenv
DREAMINA_ACCOUNT_<ID>_EMAIL=...
DREAMINA_ACCOUNT_<ID>_PASSWORD=...
```

Passwords are never stored in SQLite, returned by an API, rendered in the dashboard, or written to logs. SQLite stores account labels, email addresses, enabled/session state, cached credits, Shard profile IDs, last-use timestamps, and sanitized errors.

The dashboard writes env changes atomically with a file lock and `0600` permissions, then reloads affected account configuration without a service restart. Write failures leave the previous env content intact. Account addition is transactional from the operator's perspective: any database failure restores the previous env content.

The data directory defaults to `.data` locally and `/var/lib/dreamina` in production. It has `0700` permissions and contains SQLite, per-account cookies, Shard profiles, temporary inputs, and generated media.

### Session lifecycle

Account session states are `needs_login`, `logging_in`, `ready`, `attention`, and `disabled`; busy state is derived from active jobs.

- Login uses the account's dedicated ShardBrowser profile and env credentials.
- Cookies are persisted per account with `0600` permissions.
- Authentication failures trigger one login renewal and one safe read-only retry.
- CAPTCHA, verification, or Shark rejection marks the account `attention` and removes it from selection. No bypass is attempted.
- Credits are refreshed after each completed submission and periodically when accounts are viewed or selected.

### Automatic account selection

Callers do not choose accounts. The scheduler considers enabled accounts with `ready` sessions and sufficient estimated credits. It prefers accounts with no active generation, then the least-recently-used account. Busy accounts may be queued behind. If no enabled account has a usable session, the request fails with `503 no_account_available`; if usable accounts exist but none has enough credits, it fails with `402 insufficient_credits`. Neither case silently waits indefinitely.

## 7. Generation browser flow

The browser driver uses normal ShardBrowser UI automation with persistent profiles and `headless=True`.

1. Open a fresh Dreamina video workspace.
2. Dismiss ordinary promotional overlays.
3. Select the model using `data-model-key` and select unified-reference or first/last-frame mode using language-neutral option keys or configuration-derived indices.
4. Set duration, resolution, and aspect ratio using control types, numeric values, and state assertions rather than localized labels.
5. Supply files through Dreamina's drag-and-drop handlers when the native file picker does not expose an input node.
6. Build proper image/audio mention chips for unified references so both `material_list` and `meta_list` contain the intended references.
7. Enter prompts using real keyboard input events; do not rely on form value assignment alone.
8. Assert model, settings, files, and submit-enabled state before spending credits.
9. Handle new-workspace initialization without duplicating submission, then capture exactly one `/aigc_draft/generate` response.
10. Persist `submit_id` immediately and use read-only history polling for completion.

**Localization is a hard requirement:** automation must never match exact visible text, button labels, placeholders, localized ARIA labels, or localized error copy. Selector priority is stable `data-*` attributes (`data-model-key`, `data-testid`, `data-selector-option`, `data-native-reference-*`), language-neutral input attributes and numeric values, DOM relationships, then stable class prefixes as a final fallback. Localized text is allowed only in diagnostics, never in control flow. Uploaded asset IDs or reference indices—not asset-name text—drive mention selection. The driver verifies accepted material types and reference metadata in the captured request. Paid generation is never blindly retried after transport uncertainty; history is queried by `submit_id` first.

## 8. Persistence and restart recovery

SQLite uses WAL mode. Two tables are sufficient initially:

- `accounts`: metadata, session state, credits, profile ID, usage timestamps, and errors.
- `jobs`: public/internal IDs, unique idempotency key and request hash, model/settings/prompt/reference metadata, selected account, `submit_id`, status, upstream history ID, credit consumption, output path, sanitized errors, and timestamps.

On PM2 restart:

- `queued` jobs re-enter scheduling.
- `submitting` jobs first query history by `submit_id`; they are not resubmitted automatically.
- `running` jobs resume polling.
- `succeeded` jobs with missing media are marked for recovery rather than regenerated.
- `failed` jobs remain failed with their stable error information.

One Uvicorn process is required because the queue and browser/session coordination are in-process.

## 9. Admin dashboard and security

Server-rendered pages use vanilla JavaScript for small interactions and periodic status refresh.

### Pages

- **Overview**: total credits, ready accounts, active/queued jobs, and recent failures.
- **Accounts**: add/edit, enable/disable, refresh credits, retry login, and confirmed removal. Password fields are write-only.
- **Jobs**: create a generation and inspect prompt, model, settings, selected account, status, credits, errors, and media playback/download.
- **Models**: all four models, capabilities, current prices, and reference limits.

Admin login verifies against `ADMIN_PASSWORD_HASH` from env. A setup command generates the salted hash from a one-time password prompt; the plaintext admin password is not stored in the database or env file. Sessions are signed, expiring, Secure, HttpOnly, and `SameSite=Strict`. Mutations require a CSRF token, and login attempts are throttled.

Other security requirements:

- Constant-time API-key and password comparisons.
- Request body/file/count limits and MIME/content validation.
- No public CDP, Shard profile, env, cookie, or SQLite endpoint.
- No secrets or full upstream payloads in logs or error responses.
- Atomic env writes and restricted filesystem permissions.
- Generated media requires Worker API-key or admin-session authorization.

## 10. Timeout, retry, and network policy

- Connect timeout: 15–20 seconds.
- Read-only request timeout: 60 seconds.
- Image/audio upload timeout: 300 seconds; larger future video uploads: 600 seconds.
- Generation polling: 60 seconds per request with a 15-minute overall limit.
- Connection failures and transient 5xx responses: at most three attempts with backoff and jitter.
- Support IPv4/IPv6 fallback based on connectivity rather than hardcoding either family.
- Refresh expired upload tokens.
- Check upload/job state before retrying when bytes may already have been accepted.
- Never retry validation, authentication, CAPTCHA, or Shark failures.

## 11. Testing

### Unit tests

- Model/duration/resolution/reference validation.
- Prompt reference parsing and mention construction.
- Env parsing, atomic updates, rollback, and permissions.
- API-key/admin authentication and CSRF checks.
- Idempotency replay and conflicting request hashes.
- Account eligibility and least-recently-used scheduling.
- Job-state transitions and restart recovery.

### Mocked integration tests

- FastAPI endpoints with mocked account/session services.
- Dreamina read-only responses, pricing changes, and errors.
- Browser-driver state assertions and captured request validation.
- Identical browser-flow fixtures in multiple UI languages to prove that no selector depends on localized text.
- Upload retry behavior and timeout classification.
- Media download and authenticated content streaming.

### Opt-in live acceptance tests

- Flash text-only, first/last-frame, and properly tagged image/audio generation.
- One lowest-cost job for each of the other three models.
- Dashboard account addition and session renewal.
- Idempotency replay against the live service.
- PM2 restart while a job is running, followed by recovery without duplicate charges.

Live tests are never run automatically in CI.

## 12. PM2 and Cloudflare deployment

Production uses no Docker. Required host components are Python 3.12, Node.js/PM2, cloudflared, and Chromium runtime libraries available through the distribution package manager.

The repository includes:

- Pinned Python requirements and the FastAPI application.
- `ecosystem.config.cjs` running one Uvicorn process on `127.0.0.1:8000` in fork mode.
- ShardBrowser installation/setup instructions using its Linux x64 SDK and persistent cache.
- A production env example with API key, admin password hash, session secret, data directory, media retention, and account entries.
- `pm2 startup` / `pm2 save` instructions and log handling.
- cloudflared ingress instructions for a dedicated Dreamina subdomain or a path on the existing hostname. The existing application and its port remain unchanged.
- README documentation for setup, API examples, dashboard use, backups, and live tests.

Deployment acceptance requires health checks, admin login, account enrollment, credit reads, all four model smoke tests, reference-mode checks, media downloads, idempotency verification, and restart recovery.

## 13. Implementation boundaries

Implementation should remain a small modular Python application:

- Configuration, env updates, and secret handling.
- SQLite access and schema initialization.
- Authentication and CSRF middleware.
- Account/session management and scheduling.
- Read-only Dreamina HTTP client.
- ShardBrowser login/generation driver.
- Persistent job queue and media handling.
- Worker API routes and admin routes/templates/static assets.
- Tests, PM2 configuration, and deployment documentation.

No Redis, Celery, PostgreSQL, React, Docker, or additional browser-evasion subsystem is introduced in this version.

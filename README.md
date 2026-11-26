<img src="assets/icon.svg" width="64" height="64" alt="">

# Dreamina proxy

A private HTTP API for Dreamina (CapCut) video generation, backed by several
Dreamina accounts, with an admin dashboard. Dreamina has no public API, so the
service drives the normal Dreamina web UI in headless ShardX browsers to submit
generations, and uses Dreamina's signed read-only endpoints for credits,
prices, job status, and downloads.

- Models: Seedance 2.5, 2.5 Flash, 2.0 Fast, 2.0 Mini
- Modes: text only; unified references (images + audio tagged in the prompt
  with `@image1`, `@audio1`, …); first/last frame
- Picks an account automatically (least recently used, one generation per
  account, enough credits), queues jobs in SQLite, and survives restarts
  without ever submitting a paid job twice
- Design: [docs/superpowers/specs/2026-11-26-dreamina-proxy-design.md](docs/superpowers/specs/2026-11-26-dreamina-proxy-design.md)

The browser automation never matches visible or localized text: it uses
`data-*` attributes, input values, and DOM structure, so it works in any UI
language. A test fails if text-based selectors appear in `dreamina/browser.py`.

## Run locally

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (`pyproject.toml`
+ `uv.lock`); uv also provides Python 3.12 if it is missing.

```bash
uv sync                                          # .venv with app + test dependencies
cp .env.example .env && chmod 600 .env
uv run scripts/setup_admin.py --write            # admin password, API key, session secret
DREAMINA_INSECURE_COOKIES=1 uv run uvicorn --factory dreamina.app:create_app --port 8000
```

Open <http://127.0.0.1:8000/admin> and add an account; it logs in automatically. The
first ShardX launch downloads the browser engine and fingerprint library into
`DREAMINA_SHARDX_CACHE_DIR`.

```bash
uv run pytest -q                                  # unit + mocked integration tests
uv run scripts/setup_admin.py --check-env
uv run scripts/smoke_api.py --base-url http://127.0.0.1:8000
```

`uv run scripts/dry_run.py` drives the whole UI flow for one account (uploads, mode,
model, settings, prompt, mention chips) and stops before submitting, so it
costs nothing. Use it after Dreamina ships UI changes. It needs the service to
be stopped (it opens the account's browser profile itself).

## API

Every `/v1` call needs `Authorization: Bearer $DREAMINA_API_KEY`. Errors are
`{"error": {"code": "...", "message": "..."}}` with stable codes:
`invalid_input`, `unauthorized`, `api_disabled`, `idempotency_conflict`,
`insufficient_credits` (402), `no_account_available` (503), `not_found`,
`not_ready`, `media_expired` (410), `upstream_unavailable` (502),
`session_expired`, `upstream_rejected`, `upstream_timeout`.

| Method | Path | |
|---|---|---|
| GET | `/health` | Liveness; no auth |
| GET | `/v1/models` | Models, limits, live per-second prices |
| POST | `/v1/generations` | Queue a generation → `202` + `Location` |
| GET | `/v1/generations?limit=&offset=&status=` | Recent jobs |
| GET | `/v1/generations/{id}` | Status, credits, errors |
| GET | `/v1/generations/{id}/content` | The MP4, streamed from upstream (supports `Range`) |
| GET | `/v1/credits` | Per-account credits (no emails) |

`POST /v1/generations` takes `multipart/form-data` and an `Idempotency-Key`
header. Retrying with the same key and the same request returns the original
job (`200`); the same key with a different request is `409`.

| Field | |
|---|---|
| `model` | `seedance-2.5`, `seedance-2.5-flash`, `seedance-2.0-fast`, `seedance-2.0-mini` |
| `prompt` | Text; `@image1`, `@audio2`, … tag uploaded references |
| `duration_seconds` | 4–30 (2.5 models) or 4–15 (2.0 models) |
| `resolution` | 2.5: `480p` `720p` `1080p` `4k` · Flash: `480p` `720p` · 2.0: `720p` `1080p` `2k` `4k` |
| `aspect_ratio` | `21:9` `16:9` `4:3` `1:1` `3:4` `9:16` (unified mode) |
| `images`, `audio` | Repeatable files: PNG/JPEG/WebP, MP3/WAV (≤ 15 MB) |
| `first_frame`, `last_frame` | First/last-frame mode instead of references |

```bash
curl -X POST https://dreamina.example.com/v1/generations \
  -H "Authorization: Bearer $DREAMINA_API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F model=seedance-2.5-flash -F duration_seconds=4 -F resolution=480p -F aspect_ratio=16:9 \
  -F prompt='@image1 dances to @audio1 at sunset' \
  -F images=@dancer.png -F audio=@song.mp3
```

From a Cloudflare Worker:

```js
const form = new FormData();
form.append("model", "seedance-2.0-mini");
form.append("prompt", "The boat drifts across the pond");
form.append("duration_seconds", "5");
form.append("resolution", "720p");
form.append("first_frame", new File([firstBytes], "first.png", { type: "image/png" }));
form.append("last_frame", new File([lastBytes], "last.png", { type: "image/png" }));

const created = await fetch("https://dreamina.example.com/v1/generations", {
  method: "POST",
  headers: { Authorization: `Bearer ${env.DREAMINA_API_KEY}`, "Idempotency-Key": crypto.randomUUID() },
  body: form,
}).then((r) => r.json());
// Poll GET /v1/generations/{created.id} until status is "succeeded" or "failed",
// then fetch content_url with the same Authorization header.
```

Jobs move `queued → submitting → running → succeeded | failed`. Status is
polled every 15 s for up to 15 minutes (`DREAMINA_POLL_*`).

Videos are never stored on the server. Each `content_url` request streams the
file from Dreamina's CDN; when the signed CDN link has expired it is refreshed
from the account's history first. If the video is gone upstream the endpoint
returns `410 media_expired`, and a temporary CDN failure returns
`502 upstream_unavailable` (safe to retry). Uploaded references are deleted
right after submission; failure screenshots are kept for
`DREAMINA_MEDIA_RETENTION_DAYS` (7).

## Dashboard

`/admin` (password from `ADMIN_PASSWORD_HASH`):

- **Overview**: available credits, ready accounts, active/queued jobs, failures
- **Accounts**: add (writes `.env` and logs in), edit, enable/disable,
  refresh credits, log in again, remove (type the id to confirm). Passwords
  are write-only and live only in `.env`.
- **Jobs**: queue a generation, follow status, play/download results
- **Models**: limits and current prices

Account states: `ready`, `needs_login`, `logging_in`, `attention` (CAPTCHA,
verification, or Dreamina's anti-bot rejection; log in again from the
dashboard after resolving it in a normal browser), `disabled`.

## Deploy with Docker (recommended)

Needs an x86_64 Linux server (ShardX has no Linux ARM engine) with Docker
Engine and the compose plugin, e.g. `curl -fsSL https://get.docker.com | sh`.

```bash
git clone https://github.com/DamianCryptoBoi/dmpx.git sdbf && cd sdbf
docker compose build
docker compose run --rm app python scripts/setup_admin.py --write  # admin password; prints the Worker API key
docker compose up -d
```

The image holds Python, Chromium's system libraries and the ShardX engine; the
`sdbf_data` volume holds everything that must survive (`/data/.env` with the
secrets and account passwords, the job database, browser profiles). The
container runs as a non-root user with `init`, 1 GB of shared memory for
Chromium, and rotated logs. Chromium runs with `--no-sandbox`: on Linux hosts
Docker's default seccomp profile refuses the namespaces its sandbox needs
(seen on Ubuntu 22.04 with Docker 29), so the unprivileged container is the
isolation boundary. On a host where the sandbox works, set
`DREAMINA_BROWSER_ARGS=` (empty) in `.env`.

The dashboard listens on the server's `127.0.0.1:8000` only. Without Cloudflare,
reach it with `ssh -N -L 8000:127.0.0.1:8000 <server>` and open
http://localhost:8000/admin/login in Chrome or Firefox.

### Public HTTPS (Cloudflare Tunnel)

1. Cloudflare Zero Trust → Networks → Tunnels → **Create a tunnel**
   (Cloudflared) and copy its token.
2. Add a public hostname such as `sdbf.example.com` with service **HTTP** and
   URL `app:8000`.
3. Next to `compose.yaml` on the server, create `.env`:
   ```
   COMPOSE_PROFILES=tunnel
   CLOUDFLARE_TUNNEL_TOKEN=<token>
   ```
4. `docker compose up -d`

Cloudflare terminates HTTPS, and no port is open to the internet. Only the
`cloudflared` container may report the visitor's address
(`DREAMINA_TRUSTED_PROXIES`), so login throttling stays per visitor.

### Operations (Docker)

- **Update**: `git pull && docker compose up -d --build`
- **Logs**: `docker compose logs -f app`
- **Settings**: compose-level knobs (`DREAMINA_MAX_CONCURRENT_JOBS`,
  `DREAMINA_BROWSER_IDLE_SECONDS`, `DREAMINA_BROWSER_ARGS`) go in
  `.env` next to `compose.yaml`; secrets and accounts are in the volume, set
  through `setup_admin.py` and the dashboard.
- **Backup**: `docker compose stop app`, then
  `docker run --rm -v sdbf_data:/data -v "$PWD":/backup debian:bookworm-slim tar czf /backup/sdbf-data.tgz -C /data .`,
  then `docker compose start app`.
- **Remove**: `docker compose down` keeps the data; `docker compose down -v`
  also deletes the volume with the accounts and history.

### Sizing

Measured on a 2 vCPU / 4 GB VPS: one browser on Dreamina's page takes about
1.1 GB of RAM and more than one CPU core while the page loads. Rendering
happens upstream and is only polled over HTTP. Each account runs one
generation at a time, so N simultaneous generations need N accounts.

At most `DREAMINA_MAX_CONCURRENT_JOBS` browsers are open at once: opening
another first closes the least recently used idle one. Memory therefore
stays near `1.1 GB × DREAMINA_MAX_CONCURRENT_JOBS` plus ~0.3 GB however
many accounts you add.

| Server | Setting | Result |
|---|---|---|
| 2 vCPU / 4 GB | `DREAMINA_MAX_CONCURRENT_JOBS=2` (default) | Two submissions at a time; with four accounts all four render at once, the 3rd and 4th starting about a minute later |
| 4 vCPU / 8 GB | `DREAMINA_MAX_CONCURRENT_JOBS=4` | Four submissions at once |

## Deploy without Docker (Ubuntu/Debian x86_64)

```bash
sudo git clone https://github.com/DamianCryptoBoi/dmpx.git /opt/dreamina
sudo /opt/dreamina/scripts/setup_server.sh --hostname dreamina.example.com
```

`scripts/setup_server.sh` does the whole install and is safe to re-run:

1. Installs base packages, creates the `dreamina` service user, and installs uv.
2. `uv sync --frozen --no-dev` (uv fetches Python 3.12 if the system lacks it).
3. Installs Chromium's system libraries (`patchright install-deps chromium`).
4. Creates `/var/lib/dreamina` (0700) and `.env` (0600) with the data paths.
5. Prompts once for the admin password and prints the new Worker API key.
6. Downloads the ShardX browser engine and fingerprint library.
7. Starts the service with PM2, saves it, and registers `pm2-dreamina` with
   systemd so it starts on boot; then checks `/health`.
8. Prints the cloudflared ingress rule to add (it never edits your tunnel).

Update (pull as root, then re-run the script; it restores file ownership):

```bash
sudo git -C /opt/dreamina -c safe.directory=/opt/dreamina pull
sudo /opt/dreamina/scripts/setup_server.sh
```

Logs: `sudo -u dreamina pm2 logs dreamina-proxy`. `ecosystem.config.cjs` runs
**one** Uvicorn process on `127.0.0.1:8000` in fork mode. Never scale it out:
the queue and browser coordination are in-process.

### cloudflared

Add a dedicated hostname to the existing tunnel's ingress (above the catch-all)
and leave the existing app's rule unchanged:

```yaml
ingress:
  - hostname: dreamina.example.com
    service: http://127.0.0.1:8000
  - hostname: existing.example.com
    service: http://127.0.0.1:3000
  - service: http_status:404
```

Then `cloudflared tunnel route dns <tunnel> dreamina.example.com` and restart
cloudflared. Neither port 8000 nor any browser debugging port is exposed
publicly. The app trusts `CF-Connecting-IP` only for loopback connections
(login throttling).

### Operations

- **Backups**: `.env` and `/var/lib/dreamina/dreamina.db` (use
  `sqlite3 dreamina.db ".backup backup.db"` while running). Profiles under
  `/var/lib/dreamina/profiles` hold browser sessions; losing them only costs a
  re-login.
- **Restarts** are safe: queued jobs are picked up again, running jobs resume
  polling by `submit_id`, and a job interrupted mid-submission is marked
  failed (`upstream_timeout`) rather than resubmitted. Check Dreamina history
  before retrying such a job yourself.
- **Memory**: each active account runs one Chromium. Browsers close after
  `DREAMINA_BROWSER_IDLE_SECONDS`; `DREAMINA_MAX_CONCURRENT_JOBS` caps
  simultaneous submissions. See Sizing above.
- **Prices** are refreshed from Dreamina every 30 minutes; the verified table
  in `dreamina/catalog.py` is the fallback.

## Live acceptance (opt-in, spends credits)

Never automated. Run once per new deployment, after `dry_run.py` passes:

1. Text-only Seedance 2.5 Flash, 4 s, 480p (84 credits)
2. Unified references: one image + one audio tagged in the prompt; check the
   job notes show no warnings (the captured request is checked for both
   reference types)
3. First/last frame on Seedance 2.0 Mini, 4 s, 720p (52 credits)
4. Replay the same `Idempotency-Key`; expect the same job id and no new charge
5. Restart the service (`docker compose restart app` or
   `pm2 restart dreamina-proxy`) while a job is running; it must finish once
6. Add a second account from the dashboard and confirm it reaches `ready`

## Security notes

- Passwords are only in `.env` (0600); never in SQLite, logs, API responses,
  or pages. The data directory is 0700.
- API key and password checks are constant-time; dashboard sessions are
  signed, expiring, `HttpOnly`, `Secure`, `SameSite=Strict`; every dashboard
  mutation checks a CSRF token; failed logins are throttled.
- Uploads are size-limited and checked by content, not by extension.
- The service never tries to bypass CAPTCHAs or Dreamina's anti-bot checks; an
  account that hits one is set to `attention` and taken out of rotation.

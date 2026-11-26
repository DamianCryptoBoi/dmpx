#!/usr/bin/env bash
# Install or update the Dreamina proxy on an Ubuntu/Debian x86_64 server.
#
#   sudo scripts/setup_server.sh [--hostname dreamina.example.com]
#
# Run it from the checkout (for example /opt/dreamina). It is safe to re-run
# after `git pull`: it re-syncs dependencies and restarts the service, and it
# never overwrites existing secrets or accounts in .env.
#
# Steps: system packages, service user, uv + Python deps, Chromium system
# libraries, data directory, .env defaults, admin password/API key (prompted
# once), ShardX engine download, PM2 process + boot service, health check.
# cloudflared is not modified; the ingress rule to add is printed at the end.
#
# Environment overrides: DREAMINA_USER (default dreamina),
# DREAMINA_DATA_DIR (default /var/lib/dreamina).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${DREAMINA_USER:-dreamina}"
DATA_DIR="${DREAMINA_DATA_DIR:-/var/lib/dreamina}"
ENV_FILE="$APP_DIR/.env"
PORT=8000 # must match ecosystem.config.cjs
HOSTNAME_HINT="dreamina.example.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_HINT="${2:?--hostname needs a value}"; shift 2 ;;
    -h | --help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
as_user() { sudo -u "$SERVICE_USER" -H "$@"; }
in_app() { as_user bash -c "cd \"$APP_DIR\" && $1"; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || die "the ShardX browser engine needs Linux x86_64"
command -v apt-get >/dev/null || die "only Debian/Ubuntu (apt) is supported"
[[ -f "$APP_DIR/pyproject.toml" && -f "$APP_DIR/uv.lock" ]] || die "run this from the dreamina-proxy checkout"

step "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates sudo >/dev/null

step "Service user: $SERVICE_USER"
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --create-home --shell /bin/bash "$SERVICE_USER"
fi
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
as_user test -x "$APP_DIR" || die "$SERVICE_USER cannot reach $APP_DIR; move the checkout (e.g. to /opt/dreamina)"

step "uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

step "Python dependencies (uv sync)"
# uv installs a managed Python 3.12 for the service user if the system lacks one.
in_app "uv sync --frozen --no-dev"

step "Chromium system libraries"
"$APP_DIR/.venv/bin/patchright" install-deps chromium

step "Data directory: $DATA_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$DATA_DIR"

step "Configuration: $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 /dev/null "$ENV_FILE"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
set_default() {
  grep -q "^$1=" "$ENV_FILE" || printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
}
set_default DREAMINA_DATA_DIR "$DATA_DIR"
set_default DREAMINA_SHARDX_CACHE_DIR "$DATA_DIR/shardx"

if grep -q '^ADMIN_PASSWORD_HASH=.' "$ENV_FILE"; then
  echo "admin password already set"
elif [[ -t 0 ]]; then
  step "Admin password and API key"
  in_app ".venv/bin/python scripts/setup_admin.py --write"
else
  warn "no terminal: set the admin password later with"
  warn "  sudo -u $SERVICE_USER -H bash -c 'cd $APP_DIR && uv run scripts/setup_admin.py --write'"
fi

step "ShardX browser engine and fingerprints"
in_app ".venv/bin/python -c 'from dreamina.config import Settings; from shardx import ShardX; s = Settings.from_env(); s.ensure_dirs(); ShardX(cache_dir=str(s.shardx_cache_dir), profiles_dir=str(s.profiles_dir)).runtime.install(); print(\"ShardX ready in\", s.shardx_cache_dir)'"

step "PM2"
if ! command -v pm2 >/dev/null; then
  command -v npm >/dev/null || apt-get install -y -qq nodejs npm >/dev/null
  npm install -g pm2 >/dev/null
fi
in_app "pm2 startOrRestart ecosystem.config.cjs --update-env && pm2 save"
if [[ -d /run/systemd/system ]]; then
  env PATH="$PATH" pm2 startup systemd -u "$SERVICE_USER" --hp "$SERVICE_HOME" >/dev/null
  echo "boot service: pm2-$SERVICE_USER.service"
else
  warn "systemd is not running; PM2 will not start on boot"
fi

step "Health check"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "service is up on 127.0.0.1:$PORT"
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || die "service did not answer; check: sudo -u $SERVICE_USER pm2 logs dreamina-proxy"
in_app ".venv/bin/python scripts/setup_admin.py --check-env" || true

cat <<EOF

Done. Next:
  1. Add this rule to your cloudflared ingress, above the catch-all, then
     restart cloudflared (the existing rules stay as they are):
       - hostname: $HOSTNAME_HINT
         service: http://127.0.0.1:$PORT
     and route DNS: cloudflared tunnel route dns <tunnel> $HOSTNAME_HINT
  2. Open https://$HOSTNAME_HINT/admin and add your Dreamina accounts.
  3. Give your Worker the API key (DREAMINA_API_KEY in $ENV_FILE).

Logs:    sudo -u $SERVICE_USER pm2 logs dreamina-proxy
Update:  sudo -u $SERVICE_USER git -C $APP_DIR pull && sudo $APP_DIR/scripts/setup_server.sh
EOF

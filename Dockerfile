# syntax=docker/dockerfile:1
# SDBF: the FastAPI service plus the ShardX browser engine. linux/amd64 only,
# because ShardX ships no Linux ARM engine. Run it with compose.yaml.
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm

# --- dependencies ---------------------------------------------------------------
FROM --platform=linux/amd64 ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev --no-install-project

# --- runtime --------------------------------------------------------------------
FROM --platform=linux/amd64 ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/DamianCryptoBoi/dmpx" \
      org.opencontainers.image.title="SDBF"
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DREAMINA_DATA_DIR=/data \
    DREAMINA_ENV_FILE=/data/.env \
    DREAMINA_SHARDX_CACHE_DIR=/opt/shardx \
    DREAMINA_BROWSER_ARGS=--no-sandbox
WORKDIR /app
COPY --from=build /app/.venv /app/.venv

# Chromium's system libraries (the browser itself comes from ShardX), and
# unzip, which ShardX uses to unpack its engine.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates unzip \
    && patchright install-deps chromium

RUN groupadd --system --gid 10001 sdbf \
    && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/sdbf sdbf \
    && install -d -o 10001 -g 10001 -m 700 /data \
    && install -d -o 10001 -g 10001 /opt/shardx
USER 10001:10001

# Bake the engine and fingerprint library into the image so a container
# starts without a download; it only changes when the shardx pin does.
RUN python -c "from shardx import ShardX; ShardX(cache_dir='/opt/shardx', profiles_dir='/tmp/shardx-profiles').runtime.install()" \
    && rm -rf /tmp/shardx-profiles

COPY --chown=10001:10001 dreamina ./dreamina
COPY --chown=10001:10001 scripts ./scripts

# Accounts, browser profiles, the job database and .env live here.
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# Exactly one process: the queue and browser sessions live in memory.
CMD ["uvicorn", "--factory", "dreamina.app:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-proxy-headers", "--timeout-graceful-shutdown", "30"]

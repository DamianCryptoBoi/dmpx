"""Application configuration loaded from .env and the process environment."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .envstore import read_env_file

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE_URL = (
    "https://dreamina.capcut.com/ai-tool/generate"
    "?enter_from=hover_function_click&from_page=create&ai_feature_name=video&type=video"
)


DESKTOP_PLATFORMS = ("windows", "mac", "linux")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    root: Path
    env_path: Path
    data_dir: Path
    db_path: Path
    media_dir: Path
    jobs_dir: Path
    profiles_dir: Path
    shardx_cache_dir: Path
    api_key: str
    admin_password_hash: str
    session_secret: str
    session_ttl_seconds: int
    secure_cookies: bool
    media_retention_days: int
    max_concurrent_jobs: int
    browser_idle_seconds: int
    headless: bool
    shard_platform: str
    workspace_url: str
    generation_timeout_seconds: int
    poll_interval_seconds: int
    poll_timeout_seconds: int
    max_image_bytes: int
    max_audio_bytes: int
    max_request_bytes: int
    mode_index_unified: int
    mode_index_first_last: int
    login_throttle_attempts: int
    login_throttle_seconds: int
    log_level: str
    trusted_proxies: tuple[str, ...] = ()
    browser_args: tuple[str, ...] = ()
    browser_language: str = "en-US"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, root: Path | None = None, env_path: Path | None = None) -> "Settings":
        root = (root or ROOT).resolve()
        env_path = (env_path or Path(os.environ.get("DREAMINA_ENV_FILE", root / ".env"))).resolve()
        values = read_env_file(env_path)
        values.update({key: value for key, value in os.environ.items() if key.startswith(("DREAMINA_", "ADMIN_"))})

        def get(name: str, default: str | None = None) -> str | None:
            return values.get(name, default)

        warnings: list[str] = []
        data_dir = Path(get("DREAMINA_DATA_DIR", str(root / ".data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = (root / data_dir).resolve()
        db_path = Path(get("DREAMINA_DB_PATH", str(data_dir / "dreamina.db"))).expanduser()
        if not db_path.is_absolute():
            db_path = (root / db_path).resolve()

        api_key = (get("DREAMINA_API_KEY") or "").strip()
        if not api_key:
            warnings.append("DREAMINA_API_KEY is not set; /v1 endpoints are disabled.")

        admin_hash = (get("ADMIN_PASSWORD_HASH") or "").strip()
        if not admin_hash:
            warnings.append("ADMIN_PASSWORD_HASH is not set; admin login is disabled. Run scripts/setup_admin.py.")

        session_secret = (get("DREAMINA_SESSION_SECRET") or "").strip()
        if not session_secret:
            session_secret = secrets.token_urlsafe(48)
            warnings.append("DREAMINA_SESSION_SECRET is not set; generated an ephemeral secret for this process.")

        media_retention = max(1, _as_int(get("DREAMINA_MEDIA_RETENTION_DAYS"), 7))
        max_concurrent = max(1, min(8, _as_int(get("DREAMINA_MAX_CONCURRENT_JOBS"), 2)))

        # New profiles must be desktop fingerprints: the library also holds
        # Android templates, which get Dreamina's mobile UI.
        shard_platform = (get("DREAMINA_SHARD_PLATFORM") or "windows").strip().lower()
        if shard_platform not in DESKTOP_PLATFORMS:
            warnings.append(f"DREAMINA_SHARD_PLATFORM={shard_platform} is not a desktop platform; using windows.")
            shard_platform = "windows"

        def resolve(raw: str) -> Path:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (root / path).resolve()

        return cls(
            root=root,
            env_path=env_path,
            data_dir=data_dir,
            db_path=db_path,
            media_dir=data_dir / "media",
            jobs_dir=data_dir / "jobs",
            profiles_dir=resolve(get("DREAMINA_PROFILES_DIR") or str(data_dir / "profiles")),
            shardx_cache_dir=resolve(get("DREAMINA_SHARDX_CACHE_DIR") or str(root / ".shardx")),
            api_key=api_key,
            admin_password_hash=admin_hash,
            session_secret=session_secret,
            session_ttl_seconds=max(300, _as_int(get("DREAMINA_SESSION_TTL_HOURS"), 12) * 3600),
            secure_cookies=_as_bool(get("DREAMINA_INSECURE_COOKIES"), False) is False,
            media_retention_days=media_retention,
            max_concurrent_jobs=max_concurrent,
            browser_idle_seconds=max(60, _as_int(get("DREAMINA_BROWSER_IDLE_SECONDS"), 600)),
            headless=_as_bool(get("DREAMINA_HEADLESS"), True),
            shard_platform=shard_platform,
            workspace_url=get("DREAMINA_WORKSPACE_URL", DEFAULT_WORKSPACE_URL) or DEFAULT_WORKSPACE_URL,
            generation_timeout_seconds=max(60, _as_int(get("DREAMINA_GENERATION_TIMEOUT_SECONDS"), 600)),
            poll_interval_seconds=max(5, _as_int(get("DREAMINA_POLL_INTERVAL_SECONDS"), 15)),
            poll_timeout_seconds=max(60, _as_int(get("DREAMINA_POLL_TIMEOUT_SECONDS"), 900)),
            max_image_bytes=max(1, _as_int(get("DREAMINA_MAX_IMAGE_BYTES"), 20 * 1024 * 1024)),
            max_audio_bytes=max(1, _as_int(get("DREAMINA_MAX_AUDIO_BYTES"), 15 * 1024 * 1024)),
            max_request_bytes=max(1, _as_int(get("DREAMINA_MAX_REQUEST_BYTES"), 60 * 1024 * 1024)),
            mode_index_unified=max(0, _as_int(get("DREAMINA_MODE_INDEX_UNIFIED"), 0)),
            mode_index_first_last=max(0, _as_int(get("DREAMINA_MODE_INDEX_FIRST_LAST"), 1)),
            login_throttle_attempts=max(1, _as_int(get("DREAMINA_LOGIN_THROTTLE_ATTEMPTS"), 5)),
            login_throttle_seconds=max(60, _as_int(get("DREAMINA_LOGIN_THROTTLE_SECONDS"), 900)),
            log_level=(get("DREAMINA_LOG_LEVEL") or "INFO").upper(),
            trusted_proxies=tuple(part.strip() for part in (get("DREAMINA_TRUSTED_PROXIES") or "").split(",") if part.strip()),
            browser_args=tuple((get("DREAMINA_BROWSER_ARGS") or "").split()),
            browser_language=(get("DREAMINA_BROWSER_LANGUAGE", "en-US") or "").strip(),
            warnings=tuple(warnings),
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.jobs_dir, self.profiles_dir, self.shardx_cache_dir):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass

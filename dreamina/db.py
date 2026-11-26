"""SQLite persistence for accounts and generation jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACCOUNT_FIELDS = {
    "label",
    "email",
    "env_key",
    "enabled",
    "session_state",
    "credits",
    "credits_updated_at",
    "profile_id",
    "cookies_path",
    "user_agent",
    "last_used_at",
    "last_login_at",
    "last_error",
    "updated_at",
}
JOB_FIELDS = {
    "account_id",
    "status",
    "submit_id",
    "history_id",
    "credits_used",
    "output_path",
    "output_url",
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
    "updated_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    email TEXT NOT NULL,
                    env_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    session_state TEXT NOT NULL DEFAULT 'needs_login',
                    credits INTEGER,
                    credits_updated_at TEXT,
                    profile_id TEXT,
                    cookies_path TEXT,
                    user_agent TEXT,
                    last_used_at TEXT,
                    last_login_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    resolution TEXT NOT NULL,
                    aspect_ratio TEXT,
                    mode TEXT NOT NULL,
                    images_json TEXT NOT NULL DEFAULT '[]',
                    audio_json TEXT NOT NULL DEFAULT '[]',
                    first_frame_path TEXT,
                    last_frame_path TEXT,
                    status TEXT NOT NULL,
                    submit_id TEXT,
                    history_id TEXT,
                    credits_used INTEGER,
                    output_path TEXT,
                    output_url TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_account_status ON jobs(account_id, status);
                CREATE INDEX IF NOT EXISTS idx_accounts_enabled_state ON accounts(enabled, session_state);
                """
            )

    # accounts ---------------------------------------------------------
    def upsert_account_from_env(self, account_id: str, label: str, email: str, env_key: str) -> None:
        timestamp = now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO accounts (id, label, email, env_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET email=excluded.email, env_key=excluded.env_key, updated_at=excluded.updated_at
                """,
                (account_id, label, email, env_key, timestamp, timestamp),
            )

    def create_account(self, account_id: str, label: str, email: str, env_key: str) -> None:
        timestamp = now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO accounts (id, label, email, env_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, label, email, env_key, timestamp, timestamp),
            )

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]

    def update_account(self, account_id: str, **fields: Any) -> None:
        values = {key: value for key, value in fields.items() if key in ACCOUNT_FIELDS}
        if not values:
            return
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE accounts SET {assignments} WHERE id = ?", (*values.values(), account_id))

    def delete_account(self, account_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def set_account_state(self, account_id: str, state: str, error: str | None = None) -> None:
        self.update_account(account_id, session_state=state, last_error=error)

    # jobs -------------------------------------------------------------
    def create_job(self, job: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, account_id, idempotency_key, request_hash, model, prompt,
                    duration_seconds, resolution, aspect_ratio, mode, images_json,
                    audio_json, first_frame_path, last_frame_path, status,
                    request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    job.get("account_id"),
                    job["idempotency_key"],
                    job["request_hash"],
                    job["model"],
                    job["prompt"],
                    job["duration_seconds"],
                    job["resolution"],
                    job.get("aspect_ratio"),
                    job["mode"],
                    json.dumps(job.get("images", [])),
                    json.dumps(job.get("audio", [])),
                    job.get("first_frame_path"),
                    job.get("last_frame_path"),
                    job.get("status", "queued"),
                    json.dumps(job.get("request", {})),
                    job["created_at"],
                    job["created_at"],
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_job(row)

    def get_job_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
        return self._decode_job(row)

    def list_jobs(self, limit: int = 50, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._decode_job(row) for row in rows]

    def count_jobs(self, status: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row["count"])

    def jobs_with_status(self, statuses: list[str]) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at", statuses
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def next_queued_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return self._decode_job(row)

    def update_job(self, job_id: str, **fields: Any) -> None:
        values = {key: value for key, value in fields.items() if key in JOB_FIELDS}
        if not values:
            return
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values.values(), job_id))

    def active_job_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('submitting', 'running')"
            ).fetchone()
        return int(row["count"])

    def active_job_for_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id = ? AND status IN ('submitting', 'running') LIMIT 1",
                (account_id,),
            ).fetchone()
        return self._decode_job(row)

    def counts_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def recent_failures(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'failed' ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def jobs_with_missing_media(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'succeeded' AND (output_path IS NULL OR output_path = '')"
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    @staticmethod
    def _decode_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        job = dict(row)
        for key in ("images_json", "audio_json", "request_json"):
            raw = job.get(key)
            try:
                job[key] = json.loads(raw) if raw else ([] if key != "request_json" else {})
            except (TypeError, json.JSONDecodeError):
                job[key] = [] if key != "request_json" else {}
        return job

"""Line-preserving, atomic .env credential storage."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+=,\-\[\]]*$")
_ACCOUNT_KEY = re.compile(r"^DREAMINA_ACCOUNT_([A-Z0-9_]+)_(EMAIL|PASSWORD)$")


def _decode_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _encode_value(value: str) -> str:
    if "\n" not in value and "\r" not in value and _SAFE_VALUE.match(value):
        return value
    return json.dumps(value)


def read_env_file(path: Path) -> dict[str, str]:
    """Read a dotenv file without mutating it. Missing files are empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = _decode_value(match.group(2))
    return values


def account_env_key(account_id: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", account_id).strip("_").upper()
    if not key:
        raise ValueError("account id has no usable characters")
    return key


class EnvStore:
    """Small dotenv store with flock-protected atomic writes.

    Only lines belonging to keys being changed are rewritten; unrelated
    comments and ordering are preserved.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self._lines: list[str] = []
        self._values: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        self._lines = text.splitlines()
        self._values = {}
        for line in self._lines:
            match = _ASSIGNMENT.match(line)
            if match:
                self._values[match.group(1)] = _decode_value(match.group(2))

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def snapshot(self) -> list[str]:
        return list(self._lines)

    def restore(self, snapshot: list[str]) -> None:
        self._write(snapshot)
        self.reload()

    def set_many(self, values: dict[str, str]) -> None:
        if not values:
            return
        with self._locked():
            self.reload()
            lines = list(self._lines)
            positions: dict[str, int] = {}
            for index, line in enumerate(lines):
                match = _ASSIGNMENT.match(line)
                if match:
                    positions[match.group(1)] = index
            for key, value in values.items():
                encoded = f"{key}={_encode_value(str(value))}"
                if key in positions:
                    lines[positions[key]] = encoded
                else:
                    lines.append(encoded)
            self._write(lines)
            self.reload()

    def remove(self, keys: set[str] | list[str]) -> None:
        keys = set(keys)
        if not keys:
            return
        with self._locked():
            self.reload()
            kept: list[str] = []
            for line in self._lines:
                match = _ASSIGNMENT.match(line)
                if match and match.group(1) in keys:
                    continue
                kept.append(line)
            self._write(kept)
            self.reload()

    def _locked(self):
        store = self

        class _Lock:
            def __enter__(self):
                store.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = open(store.lock_path, "a+", encoding="utf-8")
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(self, exc_type, exc, tb):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()

        return _Lock()

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines)
        if content and not content.endswith("\n"):
            content += "\n"
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def account_credentials(self, account_id: str) -> tuple[str | None, str | None]:
        env_key = account_env_key(account_id)
        email = self.get(f"DREAMINA_ACCOUNT_{env_key}_EMAIL")
        password = self.get(f"DREAMINA_ACCOUNT_{env_key}_PASSWORD")
        if account_id == "default":
            email = email or self.get("DREAMINA_EMAIL")
            password = password or self.get("DREAMINA_PASSWORD")
        return email, password

    def account_ids(self) -> list[str]:
        ids = {
            match.group(1).lower().replace("_", "-")
            for key in self._values
            if (match := _ACCOUNT_KEY.match(key))
        }
        if "default" not in ids and self.get("DREAMINA_EMAIL") and self.get("DREAMINA_PASSWORD"):
            ids.add("default")
        return sorted(ids)

    def set_account_credentials(self, account_id: str, email: str, password: str | None = None) -> None:
        env_key = account_env_key(account_id)
        values = {f"DREAMINA_ACCOUNT_{env_key}_EMAIL": email}
        if password is not None:
            values[f"DREAMINA_ACCOUNT_{env_key}_PASSWORD"] = password
        self.set_many(values)

    def remove_account_credentials(self, account_id: str) -> None:
        env_key = account_env_key(account_id)
        keys = {
            f"DREAMINA_ACCOUNT_{env_key}_EMAIL",
            f"DREAMINA_ACCOUNT_{env_key}_PASSWORD",
        }
        if account_id == "default":
            keys.update({"DREAMINA_EMAIL", "DREAMINA_PASSWORD"})
        self.remove(keys)

    def iter_accounts(self) -> Iterator[tuple[str, str, str | None]]:
        for account_id in self.account_ids():
            email, password = self.account_credentials(account_id)
            if email:
                yield account_id, email, password

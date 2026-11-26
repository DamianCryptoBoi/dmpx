"""Upload validation, hashing, and media retention helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from fastapi import UploadFile

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}


class UploadError(ValueError):
    code = "invalid_input"


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WAVE":
        return "audio/wav"
    return None


async def save_upload(
    upload: UploadFile,
    destination: Path,
    *,
    kind: str,
    max_bytes: int,
) -> tuple[Path, str, int, str]:
    """Save an upload with magic-byte validation.

    Returns (path, detected_mime, size, sha256).
    """
    # The declared Content-Type is ignored: clients send image/jpg,
    # binary/octet-stream or nothing at all. The bytes decide (see _sniff).
    allowed = IMAGE_TYPES if kind == "image" else AUDIO_TYPES

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    head = b""
    temp = destination.with_suffix(destination.suffix + ".upload")
    try:
        with temp.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError(f"{kind} file exceeds the {max_bytes} byte limit")
                digest.update(chunk)
                handle.write(chunk)
        detected = _sniff(head)
        if detected not in allowed:
            raise UploadError(f"file does not look like a supported {kind}")
        suffix = allowed[detected]
        final = destination.with_suffix(suffix)
        os.replace(temp, final)
        os.chmod(final, 0o600)
        return final, detected, size, digest.hexdigest()
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    finally:
        await upload.close()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_inputs(job_dir: Path) -> None:
    shutil.rmtree(job_dir / "inputs", ignore_errors=True)


def cleanup_media(media_dir: Path, retention_days: int) -> int:
    if not media_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in media_dir.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    for path in sorted(media_dir.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return removed


def cleanup_job_dirs(jobs_dir: Path, retention_days: int, keep: set[str]) -> int:
    """Remove per-job folders (inputs, failure screenshots) past retention."""
    if not jobs_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in jobs_dir.iterdir():
        if path.is_dir() and path.name not in keep and path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed

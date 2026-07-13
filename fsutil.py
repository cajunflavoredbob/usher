"""Crash-safe small-file writes, shared by every first-boot secret/key writer.

The pattern (temp file -> flush+fsync -> atomic rename -> fsync parent dir)
matches settings.py's settings.json writer. A bare write_bytes can be torn by
power loss, and a truncated encryption.key or session secret crash-loops the
container until someone deletes the file by hand.
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, chmod: int | None = 0o600) -> None:
    """Write data to path atomically and durably. Creates parent dirs.
    chmod is applied to the temp file BEFORE the rename so the final file
    never exists with looser permissions; pass None to skip."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 chmod if chmod is not None else 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, *, chmod: int | None = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), chmod=chmod)


def _fsync_dir(dir_path: Path) -> None:
    """fsync a directory so a rename inside it is durable. Best-effort:
    some platforms/filesystems can't open a directory for fsync."""
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass

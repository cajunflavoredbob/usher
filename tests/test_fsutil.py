"""Tests for fsutil's crash-safe writes (a torn encryption.key
or session secret crash-loops the container until manually deleted)."""
from __future__ import annotations

import os
from pathlib import Path

from fsutil import atomic_write_bytes, atomic_write_text


def test_writes_content_and_creates_parents(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "encryption.key"
    atomic_write_bytes(target, b"key-material")
    assert target.read_bytes() == b"key-material"


def test_default_chmod_0600(tmp_path: Path):
    target = tmp_path / "secret"
    atomic_write_bytes(target, b"s")
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)


def test_chmod_none_skips_restriction(tmp_path: Path):
    target = tmp_path / "client_id"
    atomic_write_text(target, "uuid", chmod=None)
    assert (target.stat().st_mode & 0o777) != 0o600
    assert target.read_text() == "uuid"


def test_replaces_existing_file(tmp_path: Path):
    target = tmp_path / "f"
    target.write_bytes(b"old")
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"


def test_no_temp_file_left_behind(tmp_path: Path):
    target = tmp_path / "f"
    atomic_write_bytes(target, b"data")
    assert [p.name for p in tmp_path.iterdir()] == ["f"]

"""Tests for settings.py: round-trip, validation, password helpers, store I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from settings import (
    AdminAccount,
    Settings,
    SettingsStore,
    DEFAULT_DAILY_AUTOFIX_LIMIT,
    hash_password,
    load_or_create_session_secret,
    validate_public_url,
    verify_password,
)


def test_from_dict_to_dict_roundtrip_full():
    s = Settings(
        telegram_bot_token="abc",
        admin_telegram_id=12345,
        hermes_public_url="https://hermes.example.com",
        seerr_url="http://seerr:5056",
        seerr_api_key="key",
        seerr_public_url="https://seerr.example.com",
        radarr_url="http://radarr:7878",
        radarr_api_key="rkey",
        sonarr_url="http://sonarr:8989",
        sonarr_api_key="skey",
        allowed_autofix_telegram_ids=[1, 2, 3],
        daily_autofix_limit=10,
        webhook_secret="wsecret",
        admin=AdminAccount(username="user1", password_hash="pbkdf2_sha256$x$y$z"),
    )
    s2 = Settings.from_dict(s.to_dict())
    assert s == s2


def test_from_dict_handles_empty_input():
    s = Settings.from_dict({})
    assert s.telegram_bot_token == ""
    assert s.admin_telegram_id == 0
    assert s.daily_autofix_limit == DEFAULT_DAILY_AUTOFIX_LIMIT
    assert s.allowed_autofix_telegram_ids == []
    assert s.admin.username == ""


def test_autofix_flags_default_false():
    s = Settings.from_dict({})
    assert s.autofix_allow_all is False
    assert s.daily_autofix_unlimited is False


def test_autofix_flags_roundtrip_when_set():
    s = Settings(
        allowed_autofix_telegram_ids=[10, 20],
        autofix_allow_all=True,
        daily_autofix_limit=7,
        daily_autofix_unlimited=True,
    )
    s2 = Settings.from_dict(s.to_dict())
    assert s2.autofix_allow_all is True
    assert s2.daily_autofix_unlimited is True
    # The list and number are retained even with the override flags on.
    assert s2.allowed_autofix_telegram_ids == [10, 20]
    assert s2.daily_autofix_limit == 7


def test_autofix_flags_coerce_truthy_values():
    s = Settings.from_dict({"autofix_allow_all": 1, "daily_autofix_unlimited": "yes"})
    assert s.autofix_allow_all is True
    assert s.daily_autofix_unlimited is True


def test_from_dict_invalid_admin_id_becomes_zero():
    s = Settings.from_dict({"admin_telegram_id": "not-an-int"})
    assert s.admin_telegram_id == 0


def test_from_dict_invalid_daily_limit_falls_back():
    s = Settings.from_dict({"daily_autofix_limit": "five"})
    assert s.daily_autofix_limit == DEFAULT_DAILY_AUTOFIX_LIMIT


def test_from_dict_negative_daily_limit_falls_back():
    s = Settings.from_dict({"daily_autofix_limit": -3})
    assert s.daily_autofix_limit == DEFAULT_DAILY_AUTOFIX_LIMIT


def test_is_bot_configured():
    assert not Settings().is_bot_configured()
    assert not Settings(telegram_bot_token="x").is_bot_configured()
    assert not Settings(admin_telegram_id=1).is_bot_configured()
    assert Settings(telegram_bot_token="x", admin_telegram_id=1).is_bot_configured()


# --- password helpers ---


def test_hash_then_verify():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("wrong", h)


def test_verify_rejects_malformed_hash():
    assert not verify_password("x", "not-a-real-hash")
    assert not verify_password("x", "")
    assert not verify_password("x", None)  # type: ignore[arg-type]


def test_verify_rejects_wrong_algo():
    h = hash_password("x").replace("pbkdf2_sha256", "scrypt")
    assert not verify_password("x", h)


# --- URL validation ---


@pytest.mark.parametrize("url", ["", "http://x", "https://x.example.com:8765/admin"])
def test_validate_public_url_accepts(url):
    assert validate_public_url(url) is None


@pytest.mark.parametrize("url", ["ftp://x", "x.example.com", "//x", "javascript:alert(1)"])
def test_validate_public_url_rejects(url):
    err = validate_public_url(url)
    assert err is not None
    assert "http://" in err and "https://" in err


# --- SettingsStore round-trip ---


def test_store_seeds_and_persists_webhook_secret(tmp_settings_path: Path, monkeypatch):
    # Ensure no env webhook secret leaks in.
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TELEGRAM_ID", raising=False)

    store = SettingsStore(tmp_settings_path)
    assert store.settings.webhook_secret  # auto-generated
    # Should be persisted to disk too.
    data = json.loads(tmp_settings_path.read_text())
    assert data["webhook_secret"] == store.settings.webhook_secret


def test_store_auto_generates_secret_on_upgrade(tmp_settings_path: Path, monkeypatch):
    # Simulate a pre-0.11.0 settings.json with no webhook_secret.
    tmp_settings_path.write_text(json.dumps({"telegram_bot_token": "x", "admin_telegram_id": 1}))
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)

    store = SettingsStore(tmp_settings_path)
    assert store.settings.webhook_secret
    data = json.loads(tmp_settings_path.read_text())
    assert data["webhook_secret"]


def test_store_preserves_existing_secret(tmp_settings_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    existing = "preserved-secret-abc"
    tmp_settings_path.write_text(json.dumps({"webhook_secret": existing}))

    store = SettingsStore(tmp_settings_path)
    assert store.settings.webhook_secret == existing


def test_store_save_roundtrip(tmp_settings_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    store = SettingsStore(tmp_settings_path)
    store.settings.telegram_bot_token = "new-token"
    store.save()
    reread = SettingsStore(tmp_settings_path)
    assert reread.settings.telegram_bot_token == "new-token"


# --- crash-safe write + corrupt-file preservation (v0.11.20) ---


def _quiet_env(monkeypatch):
    for var in ("HERMES_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "ADMIN_TELEGRAM_ID"):
        monkeypatch.delenv(var, raising=False)


def test_write_calls_fsync_and_atomic_replace(tmp_settings_path: Path, monkeypatch):
    """_write must fsync the temp file before an atomic os.replace, and fsync
    the parent dir after -- otherwise an unsafe shutdown can truncate the file."""
    _quiet_env(monkeypatch)
    import settings as settings_mod

    fsynced_fds: list = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        settings_mod.os, "fsync", lambda fd: (fsynced_fds.append(fd), real_fsync(fd))[1]
    )
    replaced: list = []
    real_replace = os.replace
    monkeypatch.setattr(
        settings_mod.os, "replace",
        lambda src, dst: (replaced.append((str(src), str(dst))), real_replace(src, dst))[1],
    )

    store = SettingsStore(tmp_settings_path)  # seed triggers a _write
    store.settings.telegram_bot_token = "tok"
    fsynced_fds.clear()
    replaced.clear()
    store.save()

    # At least two fsyncs: the temp file fd and the parent directory fd.
    assert len(fsynced_fds) >= 2
    # The final landing is an atomic replace onto the real path.
    assert any(dst == str(tmp_settings_path) for _, dst in replaced)
    assert json.loads(tmp_settings_path.read_text())["telegram_bot_token"] == "tok"
    # No leftover temp file.
    assert not tmp_settings_path.with_suffix(".tmp").exists()


def test_write_survives_dir_fsync_unsupported(tmp_settings_path: Path, monkeypatch):
    """Parent-dir fsync is best-effort; an OSError there must not break save()."""
    _quiet_env(monkeypatch)
    import settings as settings_mod

    store = SettingsStore(tmp_settings_path)

    def boom(path, *a, **k):
        raise OSError("no dir fd here")

    monkeypatch.setattr(settings_mod.os, "open", boom)
    store.settings.telegram_bot_token = "still-works"
    store.save()  # must not raise
    assert json.loads(tmp_settings_path.read_text())["telegram_bot_token"] == "still-works"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "{not valid json", '{"admin_telegram_id":', "\x00\x00\x00"],
)
def test_corrupt_file_is_preserved_not_destroyed(tmp_settings_path: Path, monkeypatch, bad):
    """A corrupt/truncated settings.json is moved to a .corrupt.N sidecar and a
    fresh config seeded -- the original bytes are never silently overwritten."""
    _quiet_env(monkeypatch)
    tmp_settings_path.write_text(bad)

    store = SettingsStore(tmp_settings_path)

    sidecar = tmp_settings_path.parent / f"{tmp_settings_path.name}.corrupt.1"
    assert sidecar.exists()
    assert sidecar.read_text() == bad  # original bytes preserved verbatim
    # The live file is now valid, freshly seeded JSON.
    reloaded = json.loads(tmp_settings_path.read_text())
    assert "webhook_secret" in reloaded
    assert store.settings.webhook_secret


def test_repeated_corruption_keeps_numbered_backups(tmp_settings_path: Path, monkeypatch):
    """The same bad file reappearing must not clobber an earlier rescue copy."""
    _quiet_env(monkeypatch)

    tmp_settings_path.write_text("{corrupt-one")
    SettingsStore(tmp_settings_path)
    tmp_settings_path.write_text("{corrupt-two")
    SettingsStore(tmp_settings_path)

    base = tmp_settings_path.parent
    assert (base / f"{tmp_settings_path.name}.corrupt.1").read_text() == "{corrupt-one"
    assert (base / f"{tmp_settings_path.name}.corrupt.2").read_text() == "{corrupt-two"


def test_missing_file_seeds_silently_no_sidecar(tmp_settings_path: Path, monkeypatch):
    """A genuinely absent file is a legitimate fresh install: seed, no .corrupt."""
    _quiet_env(monkeypatch)
    assert not tmp_settings_path.exists()

    store = SettingsStore(tmp_settings_path)
    assert store.settings.webhook_secret
    assert not (tmp_settings_path.parent / f"{tmp_settings_path.name}.corrupt.1").exists()


def test_valid_file_loads_unchanged(tmp_settings_path: Path, monkeypatch):
    """A valid existing file is loaded as-is, with no corrupt sidecar created."""
    _quiet_env(monkeypatch)
    secret = "keep-this-secret"
    tmp_settings_path.write_text(json.dumps({"webhook_secret": secret, "telegram_bot_token": "t"}))

    store = SettingsStore(tmp_settings_path)
    assert store.settings.webhook_secret == secret
    assert store.settings.telegram_bot_token == "t"
    assert not (tmp_settings_path.parent / f"{tmp_settings_path.name}.corrupt.1").exists()


# --- session secret ---


def test_session_secret_persists(tmp_path: Path):
    p = tmp_path / "session_secret"
    a = load_or_create_session_secret(p)
    b = load_or_create_session_secret(p)
    assert a == b
    assert len(a) == 32


def test_session_secret_preserves_whitespace_bytes(tmp_path: Path):
    """Regression: bytes 0x0a / 0x20 / 0x09 at either end must NOT be stripped.
    CI failure on v0.11.7: a random secret starting with `\\n` lost its first
    byte on the second read because the loader used .strip()."""
    p = tmp_path / "session_secret"
    # Plant a secret whose ends are ASCII-whitespace bytes.
    seeded = b"\n" + b"x" * 30 + b" "
    p.write_bytes(seeded)
    loaded = load_or_create_session_secret(p)
    assert loaded == seeded
    assert len(loaded) == 32

"""Tests for settings.py: round-trip, validation, password helpers, store I/O."""
from __future__ import annotations

import json
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
        admin=AdminAccount(username="kenny", password_hash="pbkdf2_sha256$x$y$z"),
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


# --- session secret ---


def test_session_secret_persists(tmp_path: Path):
    p = tmp_path / "session_secret"
    a = load_or_create_session_secret(p)
    b = load_or_create_session_secret(p)
    assert a == b
    assert len(a) == 32

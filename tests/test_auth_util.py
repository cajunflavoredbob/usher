"""Tests for auth_util.py: throttle, CSRF, setup token, secure-cookie detection."""
from __future__ import annotations

import time as _time
from pathlib import Path
from types import SimpleNamespace

import pytest

from auth_util import (
    LoginThrottle,
    THROTTLE_MAX_FAILURES,
    THROTTLE_WINDOW_S,
    clear_setup_token,
    client_ip,
    load_or_create_setup_token,
    request_is_secure,
    validate_csrf,
)


# --- LoginThrottle ---


def test_throttle_starts_unlocked():
    t = LoginThrottle()
    assert t.is_locked("1.2.3.4") is None


def test_throttle_locks_after_max_failures():
    t = LoginThrottle()
    for _ in range(THROTTLE_MAX_FAILURES):
        t.record_failure("a")
    locked = t.is_locked("a")
    assert locked is not None
    assert 0 < locked <= THROTTLE_WINDOW_S


def test_throttle_independent_keys():
    t = LoginThrottle()
    for _ in range(THROTTLE_MAX_FAILURES):
        t.record_failure("a")
    assert t.is_locked("a") is not None
    assert t.is_locked("b") is None


def test_throttle_success_resets():
    t = LoginThrottle()
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        t.record_failure("c")
    t.record_success("c")
    assert t.is_locked("c") is None
    # And a fresh round can fail again without immediate lock.
    t.record_failure("c")
    assert t.is_locked("c") is None


def test_throttle_window_expiry():
    """Stale failures age out of the window."""
    t = LoginThrottle()
    # Inject ancient timestamps directly to simulate failures outside the
    # window without sleeping.
    old = _time.monotonic() - (THROTTLE_WINDOW_S + 10)
    for _ in range(THROTTLE_MAX_FAILURES):
        t._failures["d"].append(old)
    assert t.is_locked("d") is None


# --- CSRF validation ---


def _fake_request(*, cookies: dict, scheme: str = "http", headers: dict | None = None,
                  remote: str = "1.2.3.4"):
    return SimpleNamespace(
        cookies=cookies,
        scheme=scheme,
        headers=headers or {},
        remote=remote,
    )


def test_validate_csrf_matches():
    req = _fake_request(cookies={"hermes_csrf": "secret-abc"})
    assert validate_csrf(req, "secret-abc") is True


def test_validate_csrf_mismatch_returns_false():
    req = _fake_request(cookies={"hermes_csrf": "secret-abc"})
    assert validate_csrf(req, "other-value") is False


def test_validate_csrf_missing_cookie_returns_false():
    req = _fake_request(cookies={})
    assert validate_csrf(req, "abc") is False


def test_validate_csrf_missing_form_value_returns_false():
    req = _fake_request(cookies={"hermes_csrf": "abc"})
    assert validate_csrf(req, None) is False
    assert validate_csrf(req, "") is False


# --- Setup token ---


def test_setup_token_generated_on_first_call(tmp_path: Path, caplog):
    caplog.set_level("WARNING", logger="hermes.auth")
    token = load_or_create_setup_token(tmp_path)
    assert token
    assert (tmp_path / "setup_token").read_text().strip() == token


def test_setup_token_persists_across_calls(tmp_path: Path):
    a = load_or_create_setup_token(tmp_path)
    b = load_or_create_setup_token(tmp_path)
    assert a == b


def test_clear_setup_token_removes_file(tmp_path: Path):
    load_or_create_setup_token(tmp_path)
    assert (tmp_path / "setup_token").exists()
    clear_setup_token(tmp_path)
    assert not (tmp_path / "setup_token").exists()


def test_clear_setup_token_idempotent(tmp_path: Path):
    clear_setup_token(tmp_path)  # no-op when file missing


def test_setup_token_returns_new_after_clear(tmp_path: Path):
    a = load_or_create_setup_token(tmp_path)
    clear_setup_token(tmp_path)
    b = load_or_create_setup_token(tmp_path)
    assert a != b


# --- Secure-cookie detection + client_ip ---


def test_request_is_secure_https_scheme():
    req = _fake_request(cookies={}, scheme="https")
    assert request_is_secure(req) is True


def test_request_is_secure_x_forwarded_proto():
    req = _fake_request(cookies={}, scheme="http",
                        headers={"X-Forwarded-Proto": "https"})
    assert request_is_secure(req) is True


def test_request_is_not_secure_default():
    req = _fake_request(cookies={}, scheme="http")
    assert request_is_secure(req) is False


def test_client_ip_uses_remote_when_no_xff():
    req = _fake_request(cookies={}, remote="10.0.0.5")
    assert client_ip(req) == "10.0.0.5"


def test_client_ip_uses_xff_when_present():
    req = _fake_request(cookies={}, remote="10.0.0.5",
                        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.5"})
    assert client_ip(req) == "203.0.113.7"

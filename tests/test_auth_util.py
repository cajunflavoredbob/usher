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
    parse_trusted_proxies,
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
    from collections import deque
    old = _time.monotonic() - (THROTTLE_WINDOW_S + 10)
    t._failures["d"] = deque([old] * THROTTLE_MAX_FAILURES)
    assert t.is_locked("d") is None
    # Fully-expired buckets are dropped, not kept as empty entries.
    assert "d" not in t._failures


def test_is_locked_does_not_insert_a_bucket():
    """The old defaultdict minted an entry for every key merely checked,
    letting unauthenticated probes grow the map."""
    t = LoginThrottle()
    t.is_locked("probe-1")
    t.is_locked("probe-2")
    assert t._failures == {}


def test_throttle_key_count_is_capped(monkeypatch):
    import auth_util
    monkeypatch.setattr(auth_util, "THROTTLE_MAX_KEYS", 3)
    t = LoginThrottle()
    for i in range(10):
        t.record_failure(f"ip-{i}")
    assert len(t._failures) <= 3
    assert "ip-9" in t._failures  # newest key always lands


# --- CSRF validation ---


def _fake_request(*, cookies: dict, scheme: str = "http", headers: dict | None = None,
                  remote: str = "1.2.3.4", trusted_proxies: str = ""):
    """trusted_proxies mimics app["trusted_proxies"] (a plain dict stands in
    for the aiohttp Application mapping)."""
    return SimpleNamespace(
        cookies=cookies,
        scheme=scheme,
        headers=headers or {},
        remote=remote,
        app={"trusted_proxies": parse_trusted_proxies(trusted_proxies)},
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


def test_request_is_secure_xfp_ignored_from_untrusted_peer():
    """X-Forwarded-Proto is attacker-supplied on a direct connection."""
    req = _fake_request(cookies={}, scheme="http",
                        headers={"X-Forwarded-Proto": "https"})
    assert request_is_secure(req) is False


def test_request_is_secure_xfp_honored_from_trusted_proxy():
    req = _fake_request(cookies={}, scheme="http", remote="10.0.0.2",
                        headers={"X-Forwarded-Proto": "https"},
                        trusted_proxies="10.0.0.2/32")
    assert request_is_secure(req) is True


def test_request_is_not_secure_default():
    req = _fake_request(cookies={}, scheme="http")
    assert request_is_secure(req) is False


def test_client_ip_uses_remote_when_no_xff():
    req = _fake_request(cookies={}, remote="10.0.0.5")
    assert client_ip(req) == "10.0.0.5"


def test_client_ip_ignores_xff_without_trusted_proxy():
    """The audit's throttle bypass: rotating XFF must not change the key."""
    req = _fake_request(cookies={}, remote="10.0.0.5",
                        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.5"})
    assert client_ip(req) == "10.0.0.5"


def test_client_ip_from_trusted_proxy_takes_rightmost_untrusted():
    """A client-prepended spoof stays left of the hop the proxy appended;
    walking right-to-left past trusted hops must land on the real client."""
    req = _fake_request(cookies={}, remote="10.0.0.2",
                        headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7, 10.0.0.2"},
                        trusted_proxies="10.0.0.0/24")
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_all_trusted_hops_falls_back_to_peer():
    req = _fake_request(cookies={}, remote="10.0.0.2",
                        headers={"X-Forwarded-For": "10.0.0.9"},
                        trusted_proxies="10.0.0.0/24")
    assert client_ip(req) == "10.0.0.2"


def test_client_ip_garbage_xff_hop_is_returned_verbatim_but_bounded():
    """A non-IP hop from a trusted proxy is still used as a throttle key
    (it can't match a trusted network, so the walk stops there)."""
    req = _fake_request(cookies={}, remote="10.0.0.2",
                        headers={"X-Forwarded-For": "not-an-ip"},
                        trusted_proxies="10.0.0.0/24")
    assert client_ip(req) == "not-an-ip"


def test_client_ip_tolerates_request_without_app():
    req = SimpleNamespace(cookies={}, scheme="http", headers={}, remote="9.9.9.9")
    assert client_ip(req) == "9.9.9.9"


def test_parse_trusted_proxies_skips_invalid_entries():
    nets = parse_trusted_proxies("10.0.0.0/24, bogus, 192.168.1.5")
    assert len(nets) == 2  # bare IP becomes a /32; bogus dropped

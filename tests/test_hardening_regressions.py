"""Regression tests for the 0.13.0 hardening changes.

Each test pins a specific fix so it can't silently regress.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import auth_util
import webui
from auth_util import (
    LoginThrottle,
    THROTTLE_IP_MAX_FAILURES,
    THROTTLE_MAX_FAILURES,
    _SETUP_TOKEN_UNREADABLE,
    generate_csrf_token,
    load_or_create_setup_token,
    validate_csrf,
)
from bot.shared import format_media_label, format_status

SECRET = b"0123456789abcdef0123456789abcdef"


# --- CSRF: signed + session-bound -------------------------------------------

def _req(cookies: dict):
    return SimpleNamespace(cookies=cookies, app={"session_secret": SECRET})


def test_csrf_token_bound_to_session_rejected_for_other_session():
    """A token minted for session A (or none) must not validate against a
    different session B: the cookie-planting attack the binding closes."""
    session_a = webui._make_session_cookie(SECRET, "admin", 0)
    session_b = webui._make_session_cookie(SECRET, "admin", 1)
    bind_a = session_a.rsplit(".", 1)[-1]
    token_a = generate_csrf_token(SECRET, bind_a)

    # Same session: valid.
    ok = _req({webui.SESSION_COOKIE: session_a, "usher_csrf": token_a})
    assert validate_csrf(ok, token_a) is True

    # Planted against a different session: rejected.
    bad = _req({webui.SESSION_COOKIE: session_b, "usher_csrf": token_a})
    assert validate_csrf(bad, token_a) is False


def test_csrf_unsigned_token_rejected():
    req = _req({"usher_csrf": "planted-value"})
    assert validate_csrf(req, "planted-value") is False


# --- Login throttle: per-IP aggregate cap -----------------------------------

def test_ip_throttle_locks_after_aggregate_cap():
    """The coarse per-IP throttle must lock a single IP after
    THROTTLE_IP_MAX_FAILURES regardless of how the username field varies."""
    t = LoginThrottle(max_failures=THROTTLE_IP_MAX_FAILURES)
    ip = "203.0.113.7"
    for _ in range(THROTTLE_IP_MAX_FAILURES):
        assert t.is_locked(ip) is None
        t.record_failure(ip)
    assert t.is_locked(ip) is not None


def test_eviction_prefers_nonlocked_bucket():
    """Flooding filler keys must not evict a locked bucket (which would hand
    the attacker fresh guesses)."""
    t = LoginThrottle()
    # Lock one real key.
    for _ in range(THROTTLE_MAX_FAILURES):
        t.record_failure("victim")
    assert t.is_locked("victim") is not None
    # Fill to the key cap with single-failure filler keys, forcing eviction.
    for i in range(auth_util.THROTTLE_MAX_KEYS + 50):
        t.record_failure(f"filler-{i}")
    # The locked victim bucket must survive.
    assert t.is_locked("victim") is not None


# --- Setup token fails closed on unreadable ---------------------------------

def test_setup_token_unreadable_returns_sentinel(tmp_path, monkeypatch):
    p = tmp_path / "setup_token"
    p.write_text("real-token")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    assert load_or_create_setup_token(tmp_path) == _SETUP_TOKEN_UNREADABLE


def test_setup_token_empty_file_regenerates(tmp_path):
    p = tmp_path / "setup_token"
    p.write_text("   ")  # whitespace-only == empty after strip
    token = load_or_create_setup_token(tmp_path)
    assert token and token != _SETUP_TOKEN_UNREADABLE
    # A fresh token was written back.
    assert p.read_text().strip() == token


# --- format_media_label: specials episode vs all-seasons --------------------

def test_specials_episode_renders_s00exx():
    assert format_media_label("Foo", "", season=0, episode=3) == "Foo — S00E03"


def test_bare_season_zero_renders_nothing():
    assert format_media_label("Foo", "", season=0, episode=None) == "Foo"


# --- format_status is HTML (no leftover markdown asterisks) ------------------

def test_format_status_emits_html_not_markdown():
    out = format_status({"Seerr": "✅ 1.2.3"})
    assert "<b>Seerr</b>" in out
    assert "*" not in out


def test_format_status_escapes_values():
    out = format_status({"Seerr": "<script>&"})
    assert "&lt;script&gt;&amp;" in out
    assert "<script>" not in out


# --- Sonarr null-season guard (destructive-fix safety) ----------------------

async def test_fix_context_rejects_tv_with_episode_but_null_season():
    """An issue with episode set but season None must be rejected before the
    delete workflow: get_episodes(series, None) serializes an empty
    seasonNumber filter = every episode in the series, and an episode-only
    match could delete the wrong file."""
    from unittest.mock import AsyncMock
    from bot.tickets import _resolve_fix_context

    seerr = AsyncMock()
    seerr.get_issue.return_value = SimpleNamespace(
        media_type="tv", tmdb_id=1, problem_season=None, problem_episode=9)

    fix, err = await _resolve_fix_context(seerr, 42, action_name="Fix")
    assert fix is None
    assert err is not None and "individual episodes" in err


async def test_fix_context_allows_specials_season_zero():
    """Season 0 (specials) with an episode is a valid individual-episode fix
    and must NOT be rejected by the null-season guard."""
    from unittest.mock import AsyncMock
    from bot.tickets import _resolve_fix_context

    seerr = AsyncMock()
    seerr.get_issue.return_value = SimpleNamespace(
        media_type="tv", tmdb_id=1, problem_season=0, problem_episode=3)
    seerr.get_media_title.return_value = ("Foo", "2020")
    seerr.get_tv_seasons.return_value = ([], 55)

    fix, err = await _resolve_fix_context(seerr, 42, action_name="Fix")
    assert err is None
    assert fix is not None

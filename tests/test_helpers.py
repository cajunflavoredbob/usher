"""Tests for pure helpers in bot.py: format_age, _derive_parent_name."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.issue_flow import _derive_parent_name
from bot.shared import format_age as format_age


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat().replace("+00:00", "Z")


def test_format_age_just_now():
    assert format_age(_iso(timedelta(seconds=5))) == "just now"


def test_format_age_minutes():
    assert format_age(_iso(timedelta(minutes=5))) == "5m ago"


def test_format_age_hours():
    assert format_age(_iso(timedelta(hours=3))) == "3h ago"


def test_format_age_days():
    assert format_age(_iso(timedelta(days=2))) == "2d ago"


def test_format_age_old_returns_date():
    out = format_age(_iso(timedelta(days=14)))
    # Looks like YYYY-MM-DD.
    assert len(out) == 10 and out[4] == "-" and out[7] == "-"


def test_format_age_invalid_returns_qmark():
    assert format_age("not-a-date") == "?"


def test_format_age_warns_once_per_prefix(caplog):
    """Regression for an unparseable timestamp should log
    WARN once per distinct 20-char prefix so a Seerr format change doesn't
    pass silently, but repeat hits don't spam the log."""
    # Clear the module-level seen set so this test is order-independent.
    from bot.shared import _FORMAT_AGE_WARNED
    _FORMAT_AGE_WARNED.clear()
    caplog.set_level("WARNING", logger="hermes")
    # First two calls share the same prefix -> one WARN.
    format_age("garbage-1234567890")
    format_age("garbage-1234567890")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "format_age" in r.getMessage()]
    assert len(warnings) == 1
    # A different prefix -> another WARN.
    format_age("totally-different-prefix")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "format_age" in r.getMessage()]
    assert len(warnings) == 2


# --- _derive_parent_name ---


@pytest.mark.parametrize("query,expected", [
    ("Demon Slayer - Infinity Castle", "Demon Slayer"),
    ("Demon Slayer — Infinity Castle", "Demon Slayer"),
    ("Avatar | The Last Airbender", "Avatar"),
    ("Star Wars: A New Hope", "Star Wars"),
])
def test_derive_parent_name_extracts(query, expected):
    assert _derive_parent_name(query) == expected


@pytest.mark.parametrize("query", [
    "Inception",                   # no separator
    "A - B",                       # parent too short
])
def test_derive_parent_name_returns_none(query):
    assert _derive_parent_name(query) is None

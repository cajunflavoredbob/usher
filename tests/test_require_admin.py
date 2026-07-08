"""Tests for bot.tickets._require_admin: admin pass-through, non-admin toast + audit."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.tickets import _require_admin


def _make_q(user_id: int):
    answers: list[tuple[str, bool]] = []

    async def _answer(text="", show_alert=False):
        answers.append((text, show_alert))

    q = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        answer=_answer,
    )
    q.answers = answers  # type: ignore[attr-defined]
    return q


def _make_ctx(admin_id: int):
    return SimpleNamespace(bot_data={"admin_id": admin_id})


# --- pass-through ---


async def test_admin_passes_through():
    q = _make_q(user_id=999)
    ctx = _make_ctx(admin_id=999)
    assert await _require_admin(q, ctx, action_label="any") is True
    assert q.answers == []  # no toast issued


# --- non-admin ---


async def test_non_admin_toasts_and_returns_false(caplog):
    q = _make_q(user_id=42)
    ctx = _make_ctx(admin_id=999)
    caplog.set_level(logging.WARNING, logger="hermes.audit")
    assert await _require_admin(q, ctx, action_label="tk_close_direct") is False
    assert q.answers == [("Admin only.", False)]
    audit_records = [r for r in caplog.records if r.name == "hermes.audit"]
    assert audit_records, "expected an audit log entry for the blocked admin callback"
    entry = audit_records[-1].getMessage()
    assert "admin_callback_blocked" in entry
    assert "tk_close_direct" in entry
    assert "user=42" in entry


async def test_missing_admin_id_blocks():
    """Defensive: if admin_id is somehow None, treat all callers as non-admin."""
    q = _make_q(user_id=42)
    ctx = SimpleNamespace(bot_data={})
    assert await _require_admin(q, ctx, action_label="x") is False


async def test_anonymous_user_blocked():
    """No from_user -> not the admin."""
    q = SimpleNamespace(from_user=None, answer=AsyncMock())
    q.answers = []  # type: ignore[attr-defined]
    ctx = _make_ctx(admin_id=999)
    assert await _require_admin(q, ctx, action_label="x") is False

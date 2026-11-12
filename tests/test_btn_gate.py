"""Tests for the recent-N button-history tracking in bot.shared:
record_btn maintains a bounded list per user; the gate's snapshot-then-decide
flow admits callbacks whose source message matches any live recent entry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.shared import BTN_HISTORY_MAX, BTN_TTL_SECONDS, global_btn_gate, record_btn


def _make_app(initial: dict | None = None):
    return SimpleNamespace(bot_data=dict(initial or {}))


def _make_message(message_id: int, chat_id: int = 100):
    return SimpleNamespace(message_id=message_id, chat_id=chat_id)


def _make_update(user_id: int, message_id: int, chat_id: int = 100,
                 data: str | None = None):
    """Build a stub Update with a callback_query carrying the given message."""
    msg = _make_message(message_id, chat_id)
    q = SimpleNamespace(
        message=msg,
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answers=[],
        edits=[],
    )

    async def _answer(text="", show_alert=False):
        q.answers.append(text)

    async def _edit_reply_markup(reply_markup=None):
        q.edits.append(reply_markup)

    q.answer = _answer
    q.edit_message_reply_markup = _edit_reply_markup
    return SimpleNamespace(callback_query=q)


class _Ctx:
    def __init__(self, app):
        self.application = app


# --- record_btn ---


def test_record_btn_adds_entry():
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    entries = app.bot_data["btn_msgs"][42]
    assert len(entries) == 1
    assert entries[0]["message_id"] == 1001


def test_record_btn_caps_at_history_max():
    app = _make_app()
    for i in range(BTN_HISTORY_MAX + 3):
        record_btn(app, 42, _make_message(1000 + i))
    entries = app.bot_data["btn_msgs"][42]
    assert len(entries) == BTN_HISTORY_MAX
    # Oldest should be evicted; most recent retained.
    assert entries[0]["message_id"] == 1000 + 3
    assert entries[-1]["message_id"] == 1000 + BTN_HISTORY_MAX + 2


def test_record_btn_independent_per_user():
    app = _make_app()
    record_btn(app, 42, _make_message(1))
    record_btn(app, 99, _make_message(2))
    assert app.bot_data["btn_msgs"][42][0]["message_id"] == 1
    assert app.bot_data["btn_msgs"][99][0]["message_id"] == 2


def test_record_btn_skips_none_message():
    app = _make_app()
    record_btn(app, 42, None)
    assert "btn_msgs" not in app.bot_data or app.bot_data["btn_msgs"].get(42) is None


def test_record_btn_ignores_non_message_returns():
    """Inline-message edits return bare True; anything without a message_id
    must be ignored, not crash or pollute the history."""
    app = _make_app()
    record_btn(app, 42, True)
    record_btn(app, 42, SimpleNamespace())
    assert "btn_msgs" not in app.bot_data or app.bot_data["btn_msgs"].get(42) is None


def test_record_btn_refreshes_existing_entry():
    """Re-recording an already-tracked message (a flow step that edits its
    menu in place) moves it to the newest slot instead of duplicating it."""
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    record_btn(app, 42, _make_message(1002))
    record_btn(app, 42, _make_message(1001))
    entries = app.bot_data["btn_msgs"][42]
    assert [e["message_id"] for e in entries] == [1002, 1001]


# --- global_btn_gate ---


async def test_gate_allows_when_no_history():
    """Gradual rollout: gate is permissive for users with no recorded history."""
    app = _make_app()
    upd = _make_update(user_id=42, message_id=1)
    await global_btn_gate(upd, _Ctx(app))  # no raise


async def test_gate_allows_latest_message():
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    upd = _make_update(user_id=42, message_id=1001)
    await global_btn_gate(upd, _Ctx(app))  # no raise


async def test_gate_admits_any_of_recent_n():
    """Buttons on each of the last BTN_HISTORY_MAX messages stay live."""
    app = _make_app()
    for i in range(BTN_HISTORY_MAX):
        record_btn(app, 42, _make_message(1000 + i))
    # Tap the OLDEST of the recent entries; should still pass.
    upd = _make_update(user_id=42, message_id=1000)
    await global_btn_gate(upd, _Ctx(app))


async def test_gate_blocks_evicted_message():
    """Once a message falls off the end of the history, its buttons stop working."""
    app = _make_app()
    for i in range(BTN_HISTORY_MAX + 2):
        record_btn(app, 42, _make_message(1000 + i))
    # Tap an evicted ID.
    upd = _make_update(user_id=42, message_id=1000)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(app))
    # Toast text indicates staleness, not expiry.
    assert upd.callback_query.answers
    assert "older one" in upd.callback_query.answers[0].lower()


async def test_gate_blocks_expired_latest():
    """If the latest entry exists but is past the TTL, the gate rejects with
    an expiry toast (not a staleness one)."""
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    # Backdate the entry past TTL.
    entry = app.bot_data["btn_msgs"][42][-1]
    old = datetime.now(timezone.utc) - timedelta(seconds=BTN_TTL_SECONDS + 60)
    entry["sent_at"] = old.isoformat()
    upd = _make_update(user_id=42, message_id=1001)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(app))
    assert upd.callback_query.answers
    assert "expired" in upd.callback_query.answers[0].lower()


async def test_gate_ignores_other_users():
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    # Different user with no record -> gate allows.
    upd = _make_update(user_id=99, message_id=1001)
    await global_btn_gate(upd, _Ctx(app))


async def test_gate_no_callback_query_passes_through():
    """Updates without a callback_query (e.g., text messages) skip the gate."""
    app = _make_app()
    upd = SimpleNamespace(callback_query=None)
    await global_btn_gate(upd, _Ctx(app))  # no raise


# --- exempt prefixes and chat matching ---


async def test_gate_admits_bell_tap_with_recorded_pick_list():
    """Detail cards are never recorded, so the Notify-me button must pass."""
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    upd = _make_update(42, 5555, data="subadd:movie:550")
    await global_btn_gate(upd, _Ctx(app))  # no raise


@pytest.mark.parametrize("prefix", ["rqap", "rqdc", "rqrt"])
async def test_gate_admits_admin_request_dm_when_old_and_buried(prefix):
    app = _make_app()
    record_btn(app, 42, _make_message(2000))
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    app.bot_data["btn_msgs"][42][0]["sent_at"] = old
    for i in range(3):
        record_btn(app, 42, _make_message(3000 + i))
    upd = _make_update(42, 2000, data=f"{prefix}:77")
    await global_btn_gate(upd, _Ctx(app))  # no raise


async def test_gate_still_blocks_stale_fix_button():
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    upd = _make_update(42, 5555, data="tkf:9")
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(app))


async def test_gate_refuses_same_message_id_from_other_chat():
    app = _make_app()
    record_btn(app, 42, _make_message(1001, chat_id=100))
    upd = _make_update(42, 1001, chat_id=200)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(app))


async def test_gate_admits_entry_without_chat_id_by_message_id():
    app = _make_app()
    record_btn(app, 42, _make_message(1001))
    del app.bot_data["btn_msgs"][42][0]["chat_id"]
    upd = _make_update(42, 1001, chat_id=200)
    await global_btn_gate(upd, _Ctx(app))  # no raise

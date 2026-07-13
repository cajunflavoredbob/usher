"""Tests for the stage-3 audit fixes: double-tap / per-media serialization
(P1-6, P2-4), the expired-keyboard catch-all (P1-4), the cross-conversation
text-capture guard (P1-5), and the HTML escaping conversions (P1-3)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler

from bot.issue_flow import _issue_conversation, _resume_submit_issue, _submit_issue
from bot.link_flow import _finalize_link
from bot.resolve_flow import resolve_start
from bot.shared import (
    end_action,
    media_action_key,
    try_begin_action,
    unmatched_callback,
    user_in_conversation,
)
from bot.tickets import _apply_fix, tk_close_direct
from fix_result import FixResult
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_mapping, make_update


def _issue(*, media_type: str = "movie", tmdb_id: int = 12345) -> IssueListItem:
    return IssueListItem(
        id=42, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=tmdb_id, media_type=media_type,
        problem_season=None, problem_episode=None,
        created_by="someone",
    )


def _fake_conv(active_keys):
    return SimpleNamespace(_conversations={k: 1 for k in active_keys})


# --- in-flight primitive -------------------------------------------------------


def test_try_begin_action_claims_once():
    ctx = make_ctx()
    assert try_begin_action(ctx, "movie:1") is True
    assert try_begin_action(ctx, "movie:1") is False
    end_action(ctx, "movie:1")
    assert try_begin_action(ctx, "movie:1") is True


# --- P1-6 / P2-4: destructive-action guards -------------------------------------


async def test_apply_fix_strips_buttons_before_network_calls():
    """The FIRST edit must be the working state (no keyboard), before any
    Seerr/Arr call resolves."""
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue()
    ctx.bot_data["radarr"].auto_fix.return_value = FixResult.success(
        "ok", steps_done=["delete", "search"], poll_info={"movie_id": 5})
    await _apply_fix(upd, ctx, strategy="redownload")
    first = upd.callback_query.edits[0]
    assert "working" in first["text"]
    assert first.get("reply_markup") is None


async def test_apply_fix_refuses_when_media_action_in_flight():
    upd = make_update(callback_data="tkfm:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(tmdb_id=777)
    ctx.bot_data["inflight_actions"] = {"movie:777"}
    await _apply_fix(upd, ctx, strategy="mark_failed")
    ctx.bot_data["radarr"].mark_failed.assert_not_called()
    assert "already" in upd.callback_query.edits[-1]["text"]
    # The pre-claimed key must NOT be released by the refused attempt.
    assert "movie:777" in ctx.bot_data["inflight_actions"]


async def test_apply_fix_releases_key_after_run():
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(tmdb_id=777)
    ctx.bot_data["radarr"].auto_fix.return_value = FixResult.success(
        "ok", steps_done=["delete", "search"], poll_info={"movie_id": 5})
    await _apply_fix(upd, ctx, strategy="redownload")
    assert "movie:777" not in ctx.bot_data.get("inflight_actions", set())


async def test_close_direct_second_tap_is_noop():
    upd = make_update(callback_data="tkcd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["inflight_actions"] = {"close:42"}
    await tk_close_direct(upd, ctx)
    ctx.bot_data["seerr"].resolve_issue.assert_not_called()


async def test_double_submit_is_noop():
    """Second concurrent confirm tap must not file a duplicate Seerr issue."""
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="tok"),
                   user_data={"submitting_issue": True,
                              "media": {"type": "movie", "tmdb_id": 1,
                                        "seerr_media_id": 5, "title": "M",
                                        "year": ""},
                              "issue_type": 1, "description": "d"})
    ctx.bot_data["seerr"].create_issue = AsyncMock()
    state = await _submit_issue(upd, ctx, autofix=False)
    assert state == ConversationHandler.END
    ctx.bot_data["seerr"].create_issue.assert_not_called()


# --- P1-4: expired-keyboard catch-all -------------------------------------------


async def test_unmatched_callback_answers_and_strips_keyboard():
    upd = make_update(callback_data="season:3", user_id=42)  # dead mid-flow tap
    ctx = make_ctx()
    await unmatched_callback(upd, ctx)
    texts = [t for t, _alert in upd.callback_query.answers]
    assert any("expired" in t for t in texts)
    assert upd.callback_query.markup_edits == [None]


# --- P1-5: cross-conversation text-capture guard ---------------------------------


def test_user_in_conversation_matches_chat_user_key():
    ctx = make_ctx()
    ctx.bot_data["conversations"] = {"issue": _fake_conv({(100, 42)})}
    active = make_update(callback_data="x", user_id=42, chat_id=100)
    other = make_update(callback_data="x", user_id=7, chat_id=100)
    assert user_in_conversation(ctx, active, "issue") is True
    assert user_in_conversation(ctx, other, "issue") is False
    assert user_in_conversation(ctx, active, "resolve") is False


def test_ptb_conversation_key_shape_is_chat_then_user():
    """Pins the private PTB detail user_in_conversation depends on: for a
    per_chat+per_user handler the key is (chat_id, user_id). If a PTB
    upgrade changes _get_key or _conversations, this fails loudly."""
    conv = _issue_conversation()
    assert hasattr(conv, "_conversations")
    upd = make_update(callback_data="x", user_id=42, chat_id=100)
    assert conv._get_key(upd) == (100, 42)


async def test_resolve_comment_refused_while_issue_flow_active():
    upd = make_update(callback_data="resolve:42:no", user_id=42, chat_id=100)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="tok"))
    ctx.bot_data["conversations"] = {"issue": _fake_conv({(100, 42)})}
    state = await resolve_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "awaiting_comment_for" not in ctx.user_data
    replies = upd.callback_query.message.reply_calls
    assert replies and "middle of another flow" in replies[0]["text"]
    # Original message (with its buttons) must NOT have been edited away.
    assert upd.callback_query.edits == []


async def test_resume_submit_refused_while_new_issue_flow_active():
    """A new /issue started mid-relink owns user_data; auto-submitting would
    file that half-built draft."""
    upd = make_update(text="", user_id=42, chat_id=100)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="tok"))
    ctx.bot_data["conversations"] = {"issue": _fake_conv({(100, 42)})}
    ctx.bot_data["seerr"].create_issue = AsyncMock()
    await _resume_submit_issue(upd, ctx, {"autofix": False})
    ctx.bot_data["seerr"].create_issue.assert_not_called()
    assert "didn't auto-submit" in upd.effective_message.reply_calls[0]["text"]


# --- P1-3: HTML escaping ----------------------------------------------------------


async def test_finalize_link_uses_html_and_escapes_display_name():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx()
    ctx.bot_data["plex"] = SimpleNamespace(get_user=AsyncMock(return_value=None))
    ctx.bot_data["seerr"].login_with_plex = AsyncMock(
        return_value=(7, "we_ird*<name>"))
    ctx.bot_data["store"].link_with_plex = AsyncMock()
    await _finalize_link(upd, ctx, "tok")
    kwargs = ctx.bot.send_message.call_args_list[0].kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "we_ird*&lt;name&gt;" in kwargs["text"]

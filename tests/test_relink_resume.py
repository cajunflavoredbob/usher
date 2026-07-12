"""Tests for the relink-resume flow: an action gated by a revoked Plex token
(PlexTokenInvalidError) stashes a marker, and _finalize_link picks it back up
after the re-link succeeds. Covers the marker lifecycle (stash at each gated
surface, TTL expiry, voluntary-unlink abandonment) and the executors."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import shared
from bot.link_flow import _finalize_link, cmd_unlink
from bot.resolve_flow import resolve_comment, resolve_start
from bot.shared import RELINK_RESUME_EXECUTORS, run_relink_resume
from bot.tickets import tk_reply_text
from seerr import CreatedIssue, PlexTokenInvalidError
from tests._handler_harness import make_ctx, make_mapping, make_update


def _fresh_marker(kind: str, payload: dict) -> dict:
    return {"kind": kind, "payload": payload, "saved_at": time.time()}


# --- run_relink_resume mechanics ---------------------------------------------


async def test_resume_dispatches_and_pops_marker(monkeypatch):
    ran = []
    monkeypatch.setitem(RELINK_RESUME_EXECUTORS, "fake",
                        AsyncMock(side_effect=lambda u, c, p: ran.append(p)))
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(user_data={"relink_resume": _fresh_marker("fake", {"x": 1})})
    assert await run_relink_resume(upd, ctx) is True
    assert ran == [{"x": 1}]
    assert "relink_resume" not in ctx.user_data
    assert "Picking up where you left off" in upd.effective_message.reply_calls[0]["text"]


async def test_resume_noop_without_marker():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx()
    assert await run_relink_resume(upd, ctx) is False
    assert upd.effective_message.reply_calls == []


async def test_resume_expired_marker_is_dropped(monkeypatch):
    executor = AsyncMock()
    monkeypatch.setitem(RELINK_RESUME_EXECUTORS, "fake", executor)
    upd = make_update(text="", user_id=42)
    stale = {"kind": "fake", "payload": {}, "saved_at": time.time() - 9999}
    ctx = make_ctx(user_data={"relink_resume": stale})
    assert await run_relink_resume(upd, ctx) is False
    executor.assert_not_called()
    assert "relink_resume" not in ctx.user_data  # popped, not left to rot


async def test_resume_unknown_kind_is_dropped():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(user_data={"relink_resume": _fresh_marker("no-such-kind", {})})
    assert await run_relink_resume(upd, ctx) is False


# --- markers are stashed at the gated surfaces --------------------------------


async def test_gated_close_stashes_resume_marker():
    upd = make_update(callback_data="resolve:42:yes", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="plex-abc"))
    ctx.bot_data["seerr"].resolve_issue.side_effect = PlexTokenInvalidError()
    await resolve_start(upd, ctx)
    marker = ctx.user_data["relink_resume"]
    assert marker["kind"] == "resolve_close"
    assert marker["payload"] == {"issue_id": 42}
    assert "pick up where you left off" in upd.callback_query.edits[-1]["text"]


async def test_gated_comment_stashes_typed_text():
    upd = make_update(text="the audio is still german", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="plex-abc"),
                   user_data={"awaiting_comment_for": 42})
    ctx.bot_data["seerr"].add_issue_comment.side_effect = PlexTokenInvalidError()
    await resolve_comment(upd, ctx)
    marker = ctx.user_data["relink_resume"]
    assert marker["kind"] == "resolve_comment"
    assert marker["payload"] == {"issue_id": 42,
                                 "comment": "the audio is still german"}


async def test_gated_ticket_reply_stashes_typed_text():
    upd = make_update(text="any update on this?", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="plex-abc"),
                   user_data={"tk_reply_id": 7})
    ctx.bot_data["seerr"].add_issue_comment.side_effect = PlexTokenInvalidError()
    await tk_reply_text(upd, ctx)
    marker = ctx.user_data["relink_resume"]
    assert marker["kind"] == "ticket_reply"
    assert marker["payload"] == {"issue_id": 7, "text": "any update on this?"}


# --- executors -----------------------------------------------------------------


async def test_resume_close_uses_fresh_token():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="fresh-tok"),
                   user_data={"relink_resume": _fresh_marker(
                       "resolve_close", {"issue_id": 42})})
    assert await run_relink_resume(upd, ctx) is True
    ctx.bot_data["seerr"].resolve_issue.assert_called_once_with(
        42, as_plex_token="fresh-tok")
    assert "closed" in upd.effective_message.reply_calls[-1]["text"]


async def test_resume_comment_posts_saved_text():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="fresh-tok"),
                   user_data={"relink_resume": _fresh_marker(
                       "resolve_comment", {"issue_id": 42, "comment": "still bad"})})
    assert await run_relink_resume(upd, ctx) is True
    ctx.bot_data["seerr"].add_issue_comment.assert_called_once_with(
        42, "still bad", as_plex_token="fresh-tok")


async def test_resume_ticket_reply_posts_saved_text():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="fresh-tok"),
                   user_data={"relink_resume": _fresh_marker(
                       "ticket_reply", {"issue_id": 7, "text": "any update?"})})
    assert await run_relink_resume(upd, ctx) is True
    ctx.bot_data["seerr"].add_issue_comment.assert_called_once_with(
        7, "any update?", as_plex_token="fresh-tok")
    assert "Replied to ticket #7" in upd.effective_message.reply_calls[-1]["text"]


async def test_resume_submit_issue_reuses_draft_from_user_data():
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="fresh-tok"),
                   user_data={
                       "relink_resume": _fresh_marker("submit_issue",
                                                      {"autofix": False}),
                       "media": {"type": "movie", "tmdb_id": 1,
                                 "seerr_media_id": 5, "title": "Avatar",
                                 "year": "2009"},
                       "issue_type": 4,
                       "description": "wrong subs",
                   })
    ctx.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=99, url="http://seerr.example/issues/99"))
    assert await run_relink_resume(upd, ctx) is True
    kwargs = ctx.bot_data["seerr"].create_issue.call_args.kwargs
    assert kwargs["as_plex_token"] == "fresh-tok"
    assert kwargs["message"] == "wrong subs"
    assert any("Reported as issue #99" in c["text"]
               for c in upd.effective_message.reply_calls)


# --- marker lifecycle around link/unlink ----------------------------------------


async def test_voluntary_unlink_abandons_marker():
    upd = make_update(text="/unlink", user_id=42)
    ctx = make_ctx(user_data={"relink_resume": _fresh_marker("resolve_close",
                                                             {"issue_id": 42})})
    await cmd_unlink(upd, ctx)
    assert "relink_resume" not in ctx.user_data


async def test_finalize_link_runs_resume(monkeypatch):
    executor = AsyncMock()
    monkeypatch.setitem(RELINK_RESUME_EXECUTORS, "fake", executor)
    upd = make_update(text="", user_id=42)
    ctx = make_ctx(user_data={"relink_resume": _fresh_marker("fake", {"x": 1})})
    ctx.bot_data["plex"] = SimpleNamespace(get_user=AsyncMock(return_value=None))
    ctx.bot_data["seerr"].login_with_plex = AsyncMock(return_value=(7, "User1", None))
    ctx.bot_data["store"].link_with_plex = AsyncMock()
    await _finalize_link(upd, ctx, "fresh-tok")
    executor.assert_called_once()
    assert "relink_resume" not in ctx.user_data

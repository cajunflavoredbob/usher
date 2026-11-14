"""Tests for bot.ticket_flow.ticket_pick_media: version-tag enforcement (the
v0.11.10 fix) and the malformed-callback fallback."""
from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from bot.ticket_flow import ticket_pick_media
from bot.shared import PICK_MEDIA, PICK_TYPE
from seerr import MediaResult
from tests._handler_harness import make_ctx, make_link, make_update


def _seed(ctx, version: int, results: list[MediaResult]):
    """Populate search_results in the v0.11.10 dict shape:
    {"version": N, "by_key": {(media_type, tmdb_id): MediaResult}}.
    """
    ctx.user_data["search_version"] = version
    ctx.user_data["search_results"] = {
        "version": version,
        "by_key": {(r.media_type, r.tmdb_id): r for r in results},
    }


# --- happy path ---


async def test_version_match_movie_advances_to_pick_type():
    upd = make_update(callback_data="media:5:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    _seed(ctx, version=5, results=[
        MediaResult(media_type="movie", tmdb_id=42, title="Inception",
                    year="2010", seerr_media_id=100),
    ])
    state = await ticket_pick_media(upd, ctx)
    assert state == PICK_TYPE
    assert ctx.user_data["media"]["tmdb_id"] == 42
    assert ctx.user_data["media"]["type"] == "movie"


# --- search-results version-tag regression ---


async def test_version_mismatch_shows_search_context_changed():
    """callback_data carries version 3 but current search_version is 5
    (user kicked off a new /issue search before tapping). The in-flight
    pick must NOT resolve against the new search_results dict."""
    upd = make_update(callback_data="media:3:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    _seed(ctx, version=5, results=[
        MediaResult(media_type="movie", tmdb_id=42, title="X", year="",
                    seerr_media_id=1),
    ])
    state = await ticket_pick_media(upd, ctx)
    # Refused, not ended: the newer report's conversation stays live.
    assert state is None
    assert upd.callback_query.edits
    text = upd.callback_query.edits[0]["text"]
    assert "Search context changed" in text
    # No media commitment in user_data, and the newer search is untouched.
    assert "media" not in ctx.user_data
    assert ctx.user_data["search_results"]["version"] == 5


async def test_older_list_tap_leaves_the_newer_list_working():
    ctx = make_ctx(admin_id=999)
    _seed(ctx, version=5, results=[
        MediaResult(media_type="movie", tmdb_id=42, title="X", year="",
                    seerr_media_id=1),
    ])
    old = make_update(callback_data="media:3:movie:42", user_id=999)
    assert await ticket_pick_media(old, ctx) is None
    new = make_update(callback_data="media:5:movie:42", user_id=999)
    state = await ticket_pick_media(new, ctx)
    assert state == PICK_TYPE
    assert ctx.user_data["media"]["tmdb_id"] == 42
    assert ctx.user_data["season"] is None and ctx.user_data["episode"] is None


async def test_no_search_results_at_all_treats_as_mismatch():
    """Empty user_data (e.g., handler crashed earlier, or conversation
    timeout fired before the tap)."""
    upd = make_update(callback_data="media:1:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    state = await ticket_pick_media(upd, ctx)
    assert state is None
    assert "Search context changed" in upd.callback_query.edits[0]["text"]


# --- malformed callback_data ---


async def test_malformed_callback_too_few_parts():
    upd = make_update(callback_data="media:5:bad", user_id=999)  # only 3 parts
    ctx = make_ctx(admin_id=999)
    state = await ticket_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    assert "Couldn't parse selection" in upd.callback_query.edits[0]["text"]


async def test_malformed_callback_non_int_version():
    upd = make_update(callback_data="media:NOPE:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    state = await ticket_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    assert "Couldn't parse selection" in upd.callback_query.edits[0]["text"]


async def test_not_in_library_offers_request_jump_and_ends():
    """The dead end shows a Request-it-instead button (a request-conversation
    entry point that works after END) and ENDs unless a parent re-search is
    offered -- staying parked produced a spurious timeout notice."""
    ctx = make_ctx(admin_id=999, user_data={
        "search_results": {"version": 1, "by_key": {
            ("movie", 77): MediaResult("movie", 77, "Not In Lib", "2026", None)}},
        "search_version": 1,
    })
    upd = make_update(callback_data="media:1:movie:77", user_id=42)
    state = await ticket_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    callbacks = [b.callback_data for row in edit["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqfi:movie:77:i" in callbacks
    # Ended with no parent offered: no stash left to bypass a later check.
    assert "rq_jump_media" not in ctx.user_data


async def test_dead_end_leaves_no_stash_that_skips_the_trending_refusal():
    """End to end: after the dead end ENDs, a /trending tap on the same title,
    now Available in a browse list, is still refused."""
    from seerr import MEDIA_STATUS_AVAILABLE
    from bot.request_flow import request_from_ticket
    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=42), user_data={
        "search_results": {"version": 1, "by_key": {
            ("movie", 77): MediaResult("movie", 77, "Not In Lib", "2026", None)}},
        "search_version": 1,
    })
    await ticket_pick_media(
        make_update(callback_data="media:1:movie:77", user_id=42), ctx)
    ctx.user_data["dv_results"] = [MediaResult(
        "movie", 77, "Not In Lib", "2026", 5, status=MEDIA_STATUS_AVAILABLE)]
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    ctx.bot_data["seerr"].get_my_permissions.return_value = 0
    upd = make_update(callback_data="rqfi:movie:77", user_id=42)
    state = await request_from_ticket(upd, ctx)
    assert state == ConversationHandler.END
    assert "Already available" in upd.effective_message.reply_calls[0]["text"]
    assert "rq_media" not in ctx.user_data


async def test_dead_end_with_a_parent_search_keeps_the_stash_for_the_jump():
    ctx = make_ctx(admin_id=999, user_data={
        "search_results": {"version": 1, "by_key": {
            ("movie", 77): MediaResult("movie", 77, "Not In Lib", "2026", None)}},
        "search_version": 1, "research_parent": "Parent Show",
    })
    upd = make_update(callback_data="media:1:movie:77", user_id=42)
    state = await ticket_pick_media(upd, ctx)
    assert state == PICK_MEDIA
    assert ctx.user_data["rq_jump_media"]["tmdb_id"] == 77

"""Tests for bot.issue_flow.issue_pick_media: version-tag enforcement (the
v0.11.10 fix) and the malformed-callback fallback."""
from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from bot.issue_flow import issue_pick_media
from bot.shared import PICK_TYPE
from seerr import MediaResult
from tests._handler_harness import make_ctx, make_update


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
    state = await issue_pick_media(upd, ctx)
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
    state = await issue_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    assert upd.callback_query.edits
    text = upd.callback_query.edits[0]["text"]
    assert "Search context changed" in text
    # No media commitment in user_data
    assert "media" not in ctx.user_data


async def test_no_search_results_at_all_treats_as_mismatch():
    """Empty user_data (e.g., handler crashed earlier, or conversation
    timeout fired before the tap)."""
    upd = make_update(callback_data="media:1:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    state = await issue_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    assert "Search context changed" in upd.callback_query.edits[0]["text"]


# --- malformed callback_data ---


async def test_malformed_callback_too_few_parts():
    upd = make_update(callback_data="media:5:bad", user_id=999)  # only 3 parts
    ctx = make_ctx(admin_id=999)
    state = await issue_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    assert "Couldn't parse selection" in upd.callback_query.edits[0]["text"]


async def test_malformed_callback_non_int_version():
    upd = make_update(callback_data="media:NOPE:movie:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    state = await issue_pick_media(upd, ctx)
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
    state = await issue_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    callbacks = [b.callback_data for row in edit["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqfi:movie:77" in callbacks
    assert ctx.user_data["rq_jump_media"]["tmdb_id"] == 77

"""The "Plex watchlist" button on detail cards: shown to viewers with a Link,
and the tap that looks the title up on Plex Discover and adds it with the
viewer's own Plex token."""
from __future__ import annotations

from unittest.mock import AsyncMock

from seerr import MediaResult
from plex import PlexTokenRejectedError
from bot.callback_prefixes import RELINK
from bot.media_card import card_keyboard, send_detail_card
from bot.shared import DECRYPT_FAILED_MSG
from bot.watchlist import watchlist_add

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
MOVIE = 60935


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _ctx(link=None, key="rk-1"):
    ctx = make_ctx(admin_id=ADMIN_ID, link=link)
    ctx.bot_data["seerr"].get_media_title.return_value = ("The Thing", "2011")
    plex = ctx.bot_data["plex"]
    plex.find_discover_key = AsyncMock(return_value=key)
    plex.add_to_watchlist = AsyncMock()
    return ctx


def _tap(media_type="movie", tmdb_id=MOVIE):
    return make_update(callback_data=f"pwl:{media_type}:{tmdb_id}", user_id=USER_ID)


# --- The button -----------------------------------------------------------------

def test_card_keyboard_has_the_button_only_when_asked():
    movie = MediaResult("movie", MOVIE, "T", "2011", None)
    assert f"pwl:movie:{MOVIE}" in _cbs(card_keyboard(movie, watchlist=True))
    assert not any(c.startswith("pwl") for c in _cbs(card_keyboard(movie)))


async def test_detail_card_shows_the_button_to_a_linked_viewer():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    await send_detail_card(ctx, USER_ID, MediaResult("tv", 1396, "T", "2008", None))
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert "pwl:tv:1396" in _cbs(kb)


async def test_detail_card_has_no_button_for_an_unlinked_viewer():
    ctx = _ctx(link=None)
    await send_detail_card(ctx, USER_ID, MediaResult("movie", MOVIE, "T", "2011", None))
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("pwl") for c in _cbs(kb))


async def test_detail_card_checks_the_owner_not_the_chat():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    await send_detail_card(ctx, -100123, MediaResult("movie", MOVIE, "T", "2011", None),
                           USER_ID)
    ctx.bot_data["store"].get.assert_any_await(USER_ID)


# --- The tap --------------------------------------------------------------------

async def test_found_title_is_added_with_the_users_token_and_confirmed():
    ctx = _ctx(link=make_link(telegram_id=USER_ID, plex_token="plex-abc"))
    upd = _tap()
    await watchlist_add(upd, ctx)
    plex = ctx.bot_data["plex"]
    plex.find_discover_key.assert_awaited_once_with(
        "plex-abc", "movie", MOVIE, "The Thing", "2011")
    plex.add_to_watchlist.assert_awaited_once_with("plex-abc", "rk-1")
    reply = upd.callback_query.message.reply_calls[-1]["text"]
    assert "Added" in reply and "The Thing (2011)" in reply


async def test_title_not_found_says_add_it_in_plex_and_sends_no_put():
    ctx = _ctx(link=make_link(telegram_id=USER_ID), key=None)
    upd = _tap()
    await watchlist_add(upd, ctx)
    ctx.bot_data["plex"].add_to_watchlist.assert_not_awaited()
    reply = upd.callback_query.message.reply_calls[-1]["text"]
    assert "add it in Plex" in reply


async def test_revoked_token_on_lookup_shows_the_relink_prompt():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    ctx.bot_data["plex"].find_discover_key.side_effect = PlexTokenRejectedError()
    upd = _tap()
    await watchlist_add(upd, ctx)
    reply = upd.callback_query.message.reply_calls[-1]
    assert reply["text"].startswith("⚠️ Plex says your sign-in is no longer valid.")
    assert _cbs(reply["reply_markup"]) == [RELINK]
    # The card itself is left alone (it may be a photo, which can't be edited
    # into text).
    assert upd.callback_query.edits == []


async def test_revoked_token_on_the_put_shows_the_relink_prompt():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    ctx.bot_data["plex"].add_to_watchlist.side_effect = PlexTokenRejectedError()
    upd = _tap()
    await watchlist_add(upd, ctx)
    reply = upd.callback_query.message.reply_calls[-1]
    assert "no longer valid" in reply["text"]
    assert _cbs(reply["reply_markup"]) == [RELINK]


async def test_unlinked_tap_asks_for_a_link():
    ctx = _ctx(link=None)
    upd = _tap()
    await watchlist_add(upd, ctx)
    assert "/link" in upd.callback_query.message.reply_calls[-1]["text"]
    ctx.bot_data["plex"].find_discover_key.assert_not_awaited()


async def test_admin_without_a_link_is_asked_for_one_too():
    ctx = _ctx(link=None)
    upd = make_update(callback_data=f"pwl:movie:{MOVIE}", user_id=ADMIN_ID)
    await watchlist_add(upd, ctx)
    assert "/link" in upd.callback_query.message.reply_calls[-1]["text"]
    ctx.bot_data["plex"].find_discover_key.assert_not_awaited()


async def test_undecryptable_link_says_so():
    ctx = _ctx(link=make_link(telegram_id=USER_ID, decrypt_failed=True))
    upd = _tap()
    await watchlist_add(upd, ctx)
    assert upd.callback_query.message.reply_calls[-1]["text"] == DECRYPT_FAILED_MSG
    ctx.bot_data["plex"].find_discover_key.assert_not_awaited()


async def test_a_plex_error_is_reported_without_a_put():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    ctx.bot_data["plex"].find_discover_key.side_effect = RuntimeError("down")
    upd = _tap()
    await watchlist_add(upd, ctx)
    ctx.bot_data["plex"].add_to_watchlist.assert_not_awaited()
    assert "Couldn't add" in upd.callback_query.message.reply_calls[-1]["text"]


async def test_bad_callback_data_does_nothing():
    ctx = _ctx(link=make_link(telegram_id=USER_ID))
    for data in ("pwl:movie:x", "pwl:book:5", "pwl"):
        upd = make_update(callback_data=data, user_id=USER_ID)
        await watchlist_add(upd, ctx)
        assert upd.callback_query.message.reply_calls == []
    ctx.bot_data["plex"].find_discover_key.assert_not_awaited()

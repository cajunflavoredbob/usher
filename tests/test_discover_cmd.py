"""Tests for /trending browse: rendering, category switching, paging,
detail cards, and the request-jump pick buttons."""
from __future__ import annotations

from seerr import MEDIA_STATUS_AVAILABLE, MediaResult
from bot import hub
from bot.discover_cmd import (cmd_trending, dv_category, dv_genre,
                              dv_genre_type, dv_info, dv_page,
                              dv_similar)
from bot.requests_hub import REQUESTS_HUB

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42


def _results(n=7):
    return [MediaResult("movie" if i % 2 == 0 else "tv", 100 + i,
                        f"Title {i + 1}", "2026", None,
                        status=MEDIA_STATUS_AVAILABLE if i == 0 else None,
                        poster_path="/p.jpg", overview="A thing happens.")
            for i in range(n)]


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_trending_renders_list_with_request_jump_buttons():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    ctx.args = []
    ctx.bot_data["seerr"].discover.return_value = _results(7)
    upd = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    text = call["text"]
    assert "🔥 Trending" in text
    assert "1. " in text and "5. " in text and "6. " not in text
    assert "✅ available" in text  # availability annotation
    callbacks = _callbacks(call["reply_markup"])
    # number buttons jump into the request conversation
    assert "rqfi:movie:100" in callbacks
    # detail cards + paging + category switch present
    assert any(cb.startswith("dvi:1:") for cb in callbacks)
    assert "dvp:1:1" in callbacks
    assert "dvc:movies" in callbacks and "dvc:trending" not in callbacks


async def test_trending_arg_selects_category():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    ctx.args = ["movies"]
    ctx.bot_data["seerr"].discover.return_value = _results(2)
    upd = make_update(text="/trending movies", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    assert ctx.bot_data["seerr"].discover.call_args.args[0] == "movies"


async def test_category_switch_refetches_and_bumps_version():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID),
                   user_data={"dv_version": 3})
    ctx.bot_data["seerr"].discover.return_value = _results(2)
    upd = make_update(callback_data="dvc:tv", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert ctx.user_data["dv_version"] == 4
    assert ctx.user_data["dv_cat"] == "tv"
    assert "📺 Shows" in upd.callback_query.edits[0]["text"]


async def test_stale_page_toasts():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID),
                   user_data={"dv_version": 2, "dv_results": _results(7),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    assert upd.callback_query.edits == []
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_page_two_renders_next_slice():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID),
                   user_data={"dv_version": 1, "dv_results": _results(7),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "6. " in text and "7. " in text and "1. " not in text


async def test_info_sends_detail_card():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID),
                   user_data={"dv_version": 1, "dv_results": _results(3),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvi:1:movie:100", user_id=USER_ID)
    await dv_info(upd, ctx)
    kwargs = ctx.bot.send_photo.call_args.kwargs
    assert "Title 1" in kwargs["caption"]


async def test_unknown_category_ignored():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    upd = make_update(callback_data="dvc:hax", user_id=USER_ID)
    await dv_category(upd, ctx)
    ctx.bot_data["seerr"].discover.assert_not_awaited()


async def test_stale_popular_tap_after_tautulli_is_gone_toasts():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    ctx.bot_data["tautulli"] = None
    upd = make_update(callback_data="dvc:popular", user_id=USER_ID)
    await dv_category(upd, ctx)
    ctx.bot_data["seerr"].discover.assert_not_awaited()
    assert upd.callback_query.edits == []
    text, alert = upd.callback_query.answers[-1]
    assert text and alert


def _flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def _from_hub(upd):
    upd.callback_query.message.reply_markup = hub.end_markup(REQUESTS_HUB)
    return upd


def _genre_ctx(**user_data):
    ctx = make_ctx(link=make_link(telegram_id=USER_ID), user_data=user_data)
    ctx.bot_data["seerr"].genres.return_value = [(28, "Action"), (35, "Comedy")]
    ctx.bot_data["seerr"].discover_genre.return_value = _results(7)
    return ctx


async def test_trending_list_offers_genres_button():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    ctx.args = []
    ctx.bot_data["seerr"].discover.return_value = _results(2)
    upd = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    markup = upd.effective_message.reply_calls[-1]["reply_markup"]
    assert ("🎭 Genres", "dvc:genres") in _flat(markup)


async def test_genres_button_opens_type_picker_and_bumps_version():
    ctx = _genre_ctx(dv_version=3)
    upd = _from_hub(make_update(callback_data="dvc:genres", user_id=USER_ID))
    await dv_category(upd, ctx)
    ctx.bot_data["seerr"].discover.assert_not_awaited()
    assert ctx.user_data["dv_version"] == 4
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert ("🎬 Movies", "dvg:4:movie") in flat
    assert ("📺 Shows", "dvg:4:tv") in flat
    assert flat[-1] == ("⬅️ Requests", "hubo:requests")


async def test_genre_picker_lists_seerr_genres_for_chosen_type():
    ctx = _genre_ctx(dv_version=4)
    upd = _from_hub(make_update(callback_data="dvg:4:tv", user_id=USER_ID))
    await dv_genre_type(upd, ctx)
    ctx.bot_data["seerr"].genres.assert_awaited_once_with("tv")
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert ("Action", "dvr:4:tv:28") in flat
    assert ("Comedy", "dvr:4:tv:35") in flat
    assert flat[-1] == ("⬅️ Requests", "hubo:requests")


async def test_picking_a_genre_calls_genre_path_and_renders_cards():
    ctx = _genre_ctx(dv_version=4, dv_genres={"movie": {28: "Action"}})
    upd = _from_hub(make_update(callback_data="dvr:4:movie:28",
                                user_id=USER_ID))
    await dv_genre(upd, ctx)
    call = ctx.bot_data["seerr"].discover_genre.call_args
    assert call.args[:2] == ("movie", 28)
    assert ctx.user_data["dv_version"] == 5
    edit = upd.callback_query.edits[0]
    assert "🎭 Action (Movies)" in edit["text"]
    flat = _flat(edit["reply_markup"])
    callbacks = [c for _, c in flat]
    assert "rqfi:movie:100" in callbacks
    assert any(c.startswith("dvi:5:") for c in callbacks)
    assert ("Page 2 ➡️", "dvp:5:1") in flat
    assert ("🎭 Genres", "dvc:genres") in flat
    assert flat[-1] == ("⬅️ Requests", "hubo:requests")


async def test_genre_results_page():
    ctx = _genre_ctx(dv_version=4, dv_genres={"movie": {28: "Action"}})
    await dv_genre(make_update(callback_data="dvr:4:movie:28",
                               user_id=USER_ID), ctx)
    upd = make_update(callback_data="dvp:5:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "🎭 Action (Movies)" in text and "6. " in text


async def test_stale_genre_taps_toast():
    ctx = _genre_ctx(dv_version=5, dv_genres={"movie": {28: "Action"}})
    for data, fn in (("dvg:4:movie", dv_genre_type),
                     ("dvr:4:movie:28", dv_genre)):
        upd = make_update(callback_data=data, user_id=USER_ID)
        await fn(upd, ctx)
        assert upd.callback_query.edits == []
        assert upd.callback_query.answers[-1][1] is True
    ctx.bot_data["seerr"].genres.assert_not_awaited()
    ctx.bot_data["seerr"].discover_genre.assert_not_awaited()


async def test_genre_taps_refuse_unlinked_users():
    ctx = make_ctx(link=None)
    for data, fn in (("dvc:genres", dv_category),
                     ("dvg:1:movie", dv_genre_type),
                     ("dvr:1:movie:28", dv_genre)):
        upd = make_update(callback_data=data, user_id=USER_ID)
        await fn(upd, ctx)
        assert upd.callback_query.edits == []
    ctx.bot_data["seerr"].genres.assert_not_awaited()
    ctx.bot_data["seerr"].discover_genre.assert_not_awaited()


async def test_genre_taps_allow_the_admin_without_a_link():
    ctx = make_ctx(admin_id=USER_ID, link=None, user_data={"dv_version": 1})
    ctx.bot_data["seerr"].genres.return_value = [(28, "Action")]
    upd = make_update(callback_data="dvg:1:movie", user_id=USER_ID)
    await dv_genre_type(upd, ctx)
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert ("Action", "dvr:1:movie:28") in flat


async def test_genre_picker_fetch_failure_says_so():
    ctx = _genre_ctx(dv_version=1)
    ctx.bot_data["seerr"].genres.side_effect = RuntimeError("boom")
    upd = _from_hub(make_update(callback_data="dvg:1:movie", user_id=USER_ID))
    await dv_genre_type(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "Couldn't fetch the genres" in edit["text"]
    assert ("🔁 Try again", "dvg:1:movie") in _flat(edit["reply_markup"])


async def test_genre_results_fetch_failure_retries_that_genre():
    ctx = _genre_ctx(dv_version=4, dv_genres={"movie": {28: "Action"}})
    ctx.bot_data["seerr"].discover_genre.side_effect = RuntimeError("boom")
    upd = _from_hub(make_update(callback_data="dvr:4:movie:28",
                                user_id=USER_ID))
    await dv_genre(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "Couldn't fetch the list" in edit["text"]
    assert ("🔁 Try again", "dvr:4:movie:28") in _flat(edit["reply_markup"])


# --- More like this -------------------------------------------------------

def _similar_ctx(**user_data):
    ctx = make_ctx(link=make_link(telegram_id=USER_ID), user_data=user_data)
    ctx.bot_data["seerr"].similar_titles.return_value = _results(7)
    return ctx


async def test_more_like_this_lists_titles_like_trending():
    ctx = _similar_ctx(dv_version=2)
    upd = make_update(callback_data="dvs:tv:555", user_id=USER_ID)
    await dv_similar(upd, ctx)
    call = ctx.bot_data["seerr"].similar_titles.call_args
    assert call.args[:2] == ("tv", 555)
    sent = upd.callback_query.message.reply_calls[-1]
    assert "🔁 More like this" in sent["text"]
    assert "1. " in sent["text"] and "5. " in sent["text"]
    cbs = _callbacks(sent["reply_markup"])
    assert "rqfi:movie:100" in cbs
    assert "dvi:3:movie:100" in cbs and "dvp:3:1" in cbs
    assert ctx.user_data["dv_version"] == 3


async def test_more_like_this_results_open_detail_cards_and_page():
    ctx = _similar_ctx()
    await dv_similar(make_update(callback_data="dvs:movie:9", user_id=USER_ID),
                     ctx)
    upd = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    assert "6. " in upd.callback_query.edits[0]["text"]
    assert "🔁 More like this" in upd.callback_query.edits[0]["text"]


async def test_more_like_this_empty_says_so():
    ctx = _similar_ctx()
    ctx.bot_data["seerr"].similar_titles.return_value = []
    upd = make_update(callback_data="dvs:movie:9", user_id=USER_ID)
    await dv_similar(upd, ctx)
    assert "Nothing similar" in upd.callback_query.message.reply_calls[-1]["text"]


async def test_more_like_this_fetch_failure_says_so():
    ctx = _similar_ctx()
    ctx.bot_data["seerr"].similar_titles.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="dvs:movie:9", user_id=USER_ID)
    await dv_similar(upd, ctx)
    sent = upd.callback_query.message.reply_calls[-1]
    assert "Couldn't fetch the list" in sent["text"]


async def test_more_like_this_refuses_unlinked_users_and_bad_data():
    ctx = make_ctx(link=None)
    await dv_similar(make_update(callback_data="dvs:movie:9", user_id=USER_ID),
                     ctx)
    ctx = _similar_ctx()
    for data in ("dvs:person:9", "dvs:movie:x", "dvs:movie"):
        await dv_similar(make_update(callback_data=data, user_id=USER_ID), ctx)
    ctx.bot_data["seerr"].similar_titles.assert_not_awaited()


def test_detail_card_offers_more_like_this_for_both_types():
    from bot.media_card import card_keyboard
    for mt in ("movie", "tv"):
        r = MediaResult(mt, 77, "T", "2026", None)
        assert f"dvs:{mt}:77" in _callbacks(card_keyboard(r))

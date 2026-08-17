"""Tests for /trending browse: rendering, category switching, paging,
detail cards, and the request-jump pick buttons."""
from __future__ import annotations

from seerr import MEDIA_STATUS_AVAILABLE, MediaResult
from bot.discover_cmd import cmd_trending, dv_category, dv_info, dv_page

from tests._handler_harness import make_ctx, make_mapping, make_update

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
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID))
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
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID))
    ctx.args = ["movies"]
    ctx.bot_data["seerr"].discover.return_value = _results(2)
    upd = make_update(text="/trending movies", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    assert ctx.bot_data["seerr"].discover.call_args.args[0] == "movies"


async def test_category_switch_refetches_and_bumps_version():
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID),
                   user_data={"dv_version": 3})
    ctx.bot_data["seerr"].discover.return_value = _results(2)
    upd = make_update(callback_data="dvc:tv", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert ctx.user_data["dv_version"] == 4
    assert ctx.user_data["dv_cat"] == "tv"
    assert "📺 Shows" in upd.callback_query.edits[0]["text"]


async def test_stale_page_toasts():
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID),
                   user_data={"dv_version": 2, "dv_results": _results(7),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    assert upd.callback_query.edits == []
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_page_two_renders_next_slice():
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID),
                   user_data={"dv_version": 1, "dv_results": _results(7),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "6. " in text and "7. " in text and "1. " not in text


async def test_info_sends_detail_card():
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID),
                   user_data={"dv_version": 1, "dv_results": _results(3),
                              "dv_cat": "trending", "dv_screen": 0})
    upd = make_update(callback_data="dvi:1:movie:100", user_id=USER_ID)
    await dv_info(upd, ctx)
    kwargs = ctx.bot.send_photo.call_args.kwargs
    assert "Title 1" in kwargs["caption"]


async def test_unknown_category_ignored():
    ctx = make_ctx(mapping=make_mapping(telegram_id=USER_ID))
    upd = make_update(callback_data="dvc:hax", user_id=USER_ID)
    await dv_category(upd, ctx)
    ctx.bot_data["seerr"].discover.assert_not_awaited()

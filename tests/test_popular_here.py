"""Trending's "Popular here this week" category, with a fake Tautulli."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.discover_cmd import dv_category, dv_info
from tautulli import tmdb_id_from_metadata

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42

# Rows as Tautulli sends them: they carry names and counts the bot must drop.
_MOVIE_ROWS = [
    {"title": "Alpha Film", "year": 2024, "rating_key": 1, "total_plays": 99,
     "users_watched": "7", "users": ["SecretViewer"]},
    {"title": "No Guid Film", "year": 2020, "rating_key": 2, "total_plays": 55,
     "users_watched": "3"},
]
_TV_ROWS = [
    {"title": "Beta Show", "year": 2023, "rating_key": 3, "total_plays": 44,
     "users_watched": "5"},
]
_GUIDS = {1: {"guids": ["imdb://tt1", "tmdb://111"]},
          2: {"guids": ["imdb://tt2"]},
          3: {"guids": ["tmdb://333"]}}


def _tautulli():
    async def home(**kw):
        rows = _MOVIE_ROWS if kw["stat_id"] == "popular_movies" else _TV_ROWS
        return [{"stat_id": kw["stat_id"], "rows": rows}]

    async def meta(key):
        return _GUIDS[key]

    return SimpleNamespace(get_home_stats=AsyncMock(side_effect=home),
                           get_metadata=AsyncMock(side_effect=meta))


def _ctx(tau):
    return make_ctx(link=make_link(telegram_id=USER_ID),
                    bot_data_overrides={"tautulli": tau})


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _open_popular(ctx):
    upd = make_update(callback_data="dvc:popular", user_id=USER_ID)
    await dv_category(upd, ctx)
    return upd.callback_query.edits[-1]


def test_tmdb_id_from_guids_new_and_legacy_forms():
    assert tmdb_id_from_metadata({"guids": ["tmdb://5"]}) == 5
    assert tmdb_id_from_metadata(
        {"guid": "com.plexapp.agents.themoviedb://6?lang=en"}) == 6
    assert tmdb_id_from_metadata({"guids": ["imdb://tt1"]}) is None


async def test_popular_lists_both_stats_in_order_with_tmdb_buttons():
    tau = _tautulli()
    edit = await _open_popular(_ctx(tau))
    text = edit["text"]
    assert "🏆 Popular here this week" in text
    assert (text.index("Alpha Film") < text.index("No Guid Film")
            < text.index("Beta Show"))
    kwargs = [c.kwargs for c in tau.get_home_stats.call_args_list]
    assert {k["stat_id"] for k in kwargs} == {"popular_movies", "popular_tv"}
    assert all(k["time_range"] == 7 for k in kwargs)
    cbs = _cbs(edit["reply_markup"])
    assert "rqfi:movie:111" in cbs and "rqfi:tv:333" in cbs
    assert any(c.startswith("dvi:") and c.endswith(":movie:111") for c in cbs)


async def test_title_without_tmdb_guid_is_listed_with_no_buttons():
    edit = await _open_popular(_ctx(_tautulli()))
    assert "No Guid Film" in edit["text"]
    cbs = _cbs(edit["reply_markup"])
    assert not any(c.endswith(":0") for c in cbs)
    assert len([c for c in cbs if c.startswith("rqfi:")]) == 2


async def test_popular_text_has_no_names_or_counts():
    edit = await _open_popular(_ctx(_tautulli()))
    text = edit["text"]
    for leak in ("SecretViewer", "users_watched", "99", "55", "44", "play",
                 "watched by"):
        assert leak not in text


async def test_detail_button_opens_card_for_matched_title():
    ctx = _ctx(_tautulli())
    await _open_popular(ctx)
    version = ctx.user_data["dv_version"]
    upd = make_update(callback_data=f"dvi:{version}:movie:111",
                      user_id=USER_ID)
    await dv_info(upd, ctx)
    sent = ctx.bot.send_message.call_args.kwargs["text"]
    assert "Alpha Film" in sent


async def test_popular_hidden_without_tautulli():
    ctx = make_ctx(link=make_link(telegram_id=USER_ID))
    ctx.bot_data["seerr"].discover.return_value = []
    upd = make_update(callback_data="dvc:movies", user_id=USER_ID)
    await dv_category(upd, ctx)
    # no Tautulli: asking for the category does nothing
    upd = make_update(callback_data="dvc:popular", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert upd.callback_query.edits == []
    from seerr import MediaResult
    ctx.bot_data["seerr"].discover.return_value = [
        MediaResult("movie", 100, "T", "2026", None)]
    upd = make_update(callback_data="dvc:movies", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert "dvc:popular" not in _cbs(upd.callback_query.edits[-1]["reply_markup"])


async def test_tautulli_failure_reports_cant_fetch():
    tau = _tautulli()
    tau.get_home_stats = AsyncMock(side_effect=RuntimeError("down"))
    edit = await _open_popular(_ctx(tau))
    assert "Couldn't fetch the list" in edit["text"]


def _unlinked_ctx(tau, admin_id=999):
    ctx = make_ctx(admin_id=admin_id, bot_data_overrides={"tautulli": tau})
    ctx.bot_data["seerr"].discover.return_value = _tv_results()
    return ctx


def _tv_results():
    from seerr import MediaResult
    return [MediaResult("movie", 100, "T", "2026", None)]


async def test_unlinked_user_gets_link_first_from_trending_command():
    from bot.discover_cmd import cmd_trending
    ctx = _unlinked_ctx(_tautulli())
    ctx.args = []
    upd = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    assert "/link first" in upd.effective_message.reply_calls[-1]["text"]
    ctx.bot_data["seerr"].discover.assert_not_called()


async def test_unlinked_user_refused_on_category_page_and_info_taps():
    from bot.discover_cmd import dv_page
    ctx = _unlinked_ctx(_tautulli())
    for fn, data in ((dv_category, "dvc:movies"), (dv_page, "dvp:1:1"),
                     (dv_info, "dvi:1:movie:100")):
        upd = make_update(callback_data=data, user_id=USER_ID)
        await fn(upd, ctx)
        assert upd.callback_query.edits == []
    ctx.bot_data["seerr"].discover.assert_not_called()
    ctx.bot.send_message.assert_not_called()


async def test_unlinked_user_hub_trending_button_says_link_first():
    from bot.discover_cmd import trending_hub
    ctx = _unlinked_ctx(_tautulli())
    upd = make_update(callback_data="rqht", user_id=USER_ID)
    await trending_hub(upd, ctx)
    assert "/link first" in upd.callback_query.edits[-1]["text"]
    ctx.bot_data["seerr"].discover.assert_not_called()


async def test_forged_popular_tap_from_unlinked_user_is_refused():
    tau = _tautulli()
    ctx = _unlinked_ctx(tau)
    upd = make_update(callback_data="dvc:popular", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert upd.callback_query.edits == []
    tau.get_home_stats.assert_not_called()


async def test_linked_user_sees_popular_and_my_stats_buttons():
    from bot.discover_cmd import cmd_trending
    from bot.callback_prefixes import MYSTATS
    ctx = _ctx(_tautulli())
    ctx.bot_data["seerr"].discover.return_value = _tv_results()
    ctx.args = []
    upd = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    cbs = _cbs(upd.effective_message.reply_calls[-1]["reply_markup"])
    assert "dvc:popular" in cbs and MYSTATS in cbs


async def test_admin_without_link_still_gets_popular():
    tau = _tautulli()
    ctx = _unlinked_ctx(tau, admin_id=USER_ID)
    upd = make_update(callback_data="dvc:popular", user_id=USER_ID)
    await dv_category(upd, ctx)
    assert "Alpha Film" in upd.callback_query.edits[-1]["text"]

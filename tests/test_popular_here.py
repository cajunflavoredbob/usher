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

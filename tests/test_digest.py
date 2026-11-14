"""Tests for the daily Digest (who gets it, what it lists, when it runs), its
per-user opt-in on the Account Hub's Notices card, and the Admin's panel
settings."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot import hub
from bot.digest import (account_notices, digest_info, notice_toggle,
                        run_digest_if_due, send_digest)
from bot.link_flow import render_account
from seerr import MediaResult
from settings import Settings

from tests._handler_harness import make_ctx, make_link, make_update

ADMIN_ID = 999
# The test box's time zone is unknown, so every "now" is built in UTC: the
# window is then the previous UTC day, 2026-11-13.
NOW = datetime(2026, 11, 14, 18, 5, tzinfo=timezone.utc)
YESTERDAY = "2026-11-13 12:00:00"
TODAY = "2026-11-14 09:00:00"


@pytest.fixture(autouse=True)
def _restore_hubs(monkeypatch):
    # register_hub writes the module-level HUBS dict; keep a stub from
    # leaking into later test files.
    monkeypatch.setattr(hub, "HUBS", dict(hub.HUBS))


async def _link(store, telegram_id):
    await store.link_with_plex(
        telegram_id=telegram_id, seerr_id=telegram_id, seerr_display="U",
        plex_token="tok", plex_uuid=f"u{telegram_id}",
        plex_username=f"p{telegram_id}")


def _ctx(store, *, enabled=True, hour=18, dm=True, url="https://plex.example/x"):
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["store"] = store
    cfg = ctx.bot_data["settings_store"].settings
    cfg.digest_enabled, cfg.digest_hour, cfg.tg_notify_digest = enabled, hour, dm
    ctx.bot_data["seerr"].get_media_url.return_value = url
    return ctx


async def _seed(store):
    await store.add_new_content("radarr:1", "movie", 550, "Fight Club",
                                added_at=YESTERDAY)
    await store.add_new_content("sonarr:1", "episode", 1396, "Breaking Bad",
                                1, 1, added_at=YESTERDAY)
    await store.add_new_content("sonarr:2", "episode", 1396, "Breaking Bad",
                                1, 2, added_at=YESTERDAY)
    await store.add_new_content("radarr:2", "movie", 680, "Pulp Fiction",
                                added_at=TODAY)


def _sends(ctx):
    return ctx.application.bot.send_message.call_args_list


# --- who gets it, what it lists ----------------------------------------------

async def test_only_opted_in_linked_users_get_the_digest(fresh_store):
    for uid in (1, 2, 3):
        await _link(fresh_store, uid)
    await fresh_store.set_pref(1, "digest", True)
    await fresh_store.set_pref(3, "digest", True)
    await fresh_store.set_pref(3, "digest", False)
    await fresh_store.set_pref(77, "digest", True)  # opted in but never Linked
    await _seed(fresh_store)
    ctx = _ctx(fresh_store)
    assert await send_digest(ctx.application, NOW) == 1
    assert [c.kwargs["chat_id"] for c in _sends(ctx)] == [1]


async def test_digest_lists_yesterdays_rows_grouped_movies_then_shows(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await _seed(fresh_store)
    ctx = _ctx(fresh_store)
    await send_digest(ctx.application, NOW)
    text = _sends(ctx)[0].kwargs["text"]
    assert text.index("Movies") < text.index("Fight Club") < text.index("Shows")
    assert text.index("Shows") < text.index("Breaking Bad")
    assert "Breaking Bad: 2 new episodes" in text
    assert "Pulp Fiction" not in text  # added today, not yesterday


async def test_each_entry_has_watch_now_or_a_detail_card_button(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await _seed(fresh_store)
    ctx = _ctx(fresh_store)
    urls = {"movie": "https://plex.example/m", "tv": None}
    ctx.bot_data["seerr"].get_media_url.side_effect = (
        lambda media_type, tmdb_id: urls[media_type])
    await send_digest(ctx.application, NOW)
    btns = [b for row in _sends(ctx)[0].kwargs["reply_markup"].inline_keyboard
            for b in row]
    assert [(b.url, b.callback_data) for b in btns] == [
        ("https://plex.example/m", None), (None, "dgi:tv:1396")]


# --- empty day, switches, hour -------------------------------------------------

async def test_an_empty_day_sends_nothing(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await fresh_store.add_new_content("radarr:2", "movie", 680, "Pulp Fiction",
                                      added_at=TODAY)
    ctx = _ctx(fresh_store)
    assert await run_digest_if_due(ctx.application, NOW) == 0
    assert _sends(ctx) == []


async def test_the_feature_switch_off_sends_nothing(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await _seed(fresh_store)
    for kwargs in ({"enabled": False}, {"dm": False}):
        ctx = _ctx(fresh_store, **kwargs)
        assert await run_digest_if_due(ctx.application, NOW) == 0
        assert _sends(ctx) == []


async def test_the_job_runs_only_in_the_configured_hour_and_once_a_day(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await _seed(fresh_store)
    ctx = _ctx(fresh_store, hour=7)
    assert await run_digest_if_due(ctx.application, NOW) == 0  # hour 18 != 7
    ctx.bot_data["settings_store"].settings.digest_hour = 18
    assert await run_digest_if_due(ctx.application, NOW) == 1
    later = NOW.replace(minute=45)
    assert await run_digest_if_due(ctx.application, later) == 0  # same day
    # A restart (fresh app, same store) inside the hour does not resend.
    again = _ctx(fresh_store)
    assert await run_digest_if_due(again.application, later) == 0
    assert len(_sends(ctx)) == 1


async def test_the_next_day_sends_again(fresh_store):
    await _link(fresh_store, 1)
    await fresh_store.set_pref(1, "digest", True)
    await _seed(fresh_store)
    ctx = _ctx(fresh_store)
    await run_digest_if_due(ctx.application, NOW)
    await fresh_store.add_new_content("radarr:3", "movie", 603, "The Matrix",
                                      added_at=TODAY)
    assert await run_digest_if_due(
        ctx.application, NOW.replace(day=15)) == 1
    assert "The Matrix" in _sends(ctx)[1].kwargs["text"]


# --- the detail-card button -----------------------------------------------------

async def test_digest_info_sends_the_detail_card():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_media_result = AsyncMock(return_value=MediaResult(
        "tv", 1396, "Breaking Bad", "2008", None))
    await digest_info(make_update(callback_data="dgi:tv:1396", user_id=1), ctx)
    ctx.bot_data["seerr"].get_media_result.assert_awaited_once_with("tv", 1396)
    assert "Breaking Bad" in ctx.bot.send_message.call_args.kwargs["text"]


# --- Account Hub Notices -------------------------------------------------------

async def test_account_hub_offers_notices_to_linked_users():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=1))
    view = await render_account(make_update(text="/account", user_id=1), ctx)
    assert "accnt" in [b.callback_data for b in view.buttons]


async def test_notices_toggles_persist(fresh_store):
    await _link(fresh_store, 1)
    hub.register_hub("account", "Account", None)
    ctx = _ctx(fresh_store)
    assert await fresh_store.get_prefs(1) == {"digest": False, "auto_follow": True}
    await account_notices(make_update(callback_data="accnt", user_id=1), ctx)
    await notice_toggle(make_update(callback_data="ntg:digest", user_id=1), ctx)
    await notice_toggle(make_update(callback_data="ntg:auto", user_id=1), ctx)
    assert await fresh_store.get_prefs(1) == {"digest": True, "auto_follow": False}
    upd = make_update(callback_data="ntg:auto", user_id=1)
    await notice_toggle(upd, ctx)
    assert await fresh_store.get_prefs(1) == {"digest": True, "auto_follow": True}
    shown = upd.callback_query.edits[-1]["text"].replace("<b>", "").replace("</b>", "")
    assert "Daily digest: on" in shown


async def test_an_unlinked_user_cannot_change_notices(fresh_store):
    hub.register_hub("account", "Account", None)
    ctx = _ctx(fresh_store)
    await notice_toggle(make_update(callback_data="ntg:digest", user_id=5), ctx)
    assert await fresh_store.get_prefs(5) == {"digest": False, "auto_follow": True}


# --- panel settings ---------------------------------------------------------------

def test_digest_settings_default_off_at_18_and_round_trip():
    s = Settings.from_dict({})
    assert (s.digest_enabled, s.digest_hour, s.tg_notify_digest) == (False, 18, True)
    s = Settings.from_dict({"digest_enabled": True, "digest_hour": 7,
                            "tg_notify_digest": False})
    assert (s.digest_enabled, s.digest_hour, s.tg_notify_digest) == (True, 7, False)
    assert Settings.from_dict({"digest_hour": 30}).digest_hour == 18

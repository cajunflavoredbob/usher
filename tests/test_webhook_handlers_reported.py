"""Tests for bot.webhook_handlers.handle_seerr_reported (the new-issue admin
DM). v0.11.24: TV issues now show the affected scope line, and the message
carries a History button alongside Reply/Fix/Close.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from bot.webhook_handlers import handle_seerr_reported
from bot.callback_prefixes import TK_CLOSE, TK_FIX, TK_OPEN, TK_REPLY
from tests._handler_harness import make_ctx, make_mapping


def _app(admin_id=999, admin_mapping=None):
    app = make_ctx(admin_id=admin_id).application
    # Admin is NOT the reporter, so the DM is sent.
    app.bot_data["store"].get = AsyncMock(return_value=admin_mapping)
    return app


def _payload(*, media=None, extra=None, reporter="user2"):
    p = {
        "notification_type": "ISSUE_CREATED",
        "message": "audio is wrong",
        "issue": {
            "issue_id": 38,
            "issue_type": "AUDIO",
            "reportedBy_username": reporter,
        },
        "media": media if media is not None else {"media_type": "tv", "tmdbId": 555},
    }
    if extra is not None:
        p["extra"] = extra
    return p


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_new_issue_shows_episode_scope():
    app = _app(admin_mapping=make_mapping(telegram_id=999, plex_username="user1plex"))
    await handle_seerr_reported(app, _payload(extra=[
        {"name": "Affected Season", "value": "1"},
        {"name": "Affected Episode", "value": "5"},
    ]))
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Season 1, Episode 5" in text


async def test_new_issue_shows_all_seasons_when_no_season():
    app = _app(admin_mapping=make_mapping(telegram_id=999, plex_username="user1plex"))
    await handle_seerr_reported(app, _payload(extra=[]))
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "All seasons" in text


async def test_movie_issue_has_no_scope_line():
    app = _app(admin_mapping=make_mapping(telegram_id=999, plex_username="user1plex"))
    await handle_seerr_reported(app, _payload(media={"media_type": "movie", "tmdbId": 99}))
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Season" not in text
    assert "All seasons" not in text


async def test_new_issue_has_history_button():
    app = _app(admin_mapping=make_mapping(telegram_id=999, plex_username="user1plex"))
    await handle_seerr_reported(app, _payload())
    cbs = _callbacks(app.bot.send_message.call_args.kwargs["reply_markup"])
    assert cbs == [
        f"{TK_REPLY}:38", f"{TK_FIX}:38", f"{TK_CLOSE}:38", f"{TK_OPEN}:38",
    ]

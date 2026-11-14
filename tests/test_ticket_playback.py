"""Reporter's playback details (from Tautulli) on the Admin's new-Ticket DM and
the Admin's Ticket card, never on a Reporter's view."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.tickets import tk_open
from bot.webhook_handlers import handle_seerr_reported
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_link, make_update

USERS = [{"user_id": 9, "username": "reporterplex", "email": "rep@example.com"}]


def _issue(**kw) -> IssueListItem:
    d = dict(id=38, issue_type=1, status=1, created_at="2026-06-16T00:00:00Z",
             tmdb_id=555, media_type="movie", problem_season=None,
             problem_episode=None, created_by="Rep", description="stutters",
             rating_key="100", created_by_email="rep@example.com",
             created_by_plex_username="ReporterPlex")
    d.update(kw)
    return IssueListItem(**d)


def _tau(rows=None, stream=None, users=USERS):
    rows = [{"row_id": 7, "date": 1781568000, "player": "Living Room",
             "platform": "Roku", "parent_media_index": 1, "media_index": 5}
            ] if rows is None else rows
    stream = {"transcode_decision": "transcode", "video_codec": "hevc",
              "audio_codec": "eac3"} if stream is None else stream
    return SimpleNamespace(
        get_users=AsyncMock(return_value=users),
        get_history=AsyncMock(return_value=rows),
        get_stream_data=AsyncMock(return_value=stream),
    )


def _app(tau, issue):
    app = make_ctx(admin_id=999, bot_data_overrides={"tautulli": tau}).application
    app.bot_data["store"].get = AsyncMock(return_value=make_link(
        telegram_id=999, plex_username="adminplex"))
    app.bot_data["seerr"].get_issue.return_value = issue
    return app


def _payload():
    return {"notification_type": "ISSUE_CREATED", "message": "stutters",
            "issue": {"issue_id": 38, "issue_type": "VIDEO",
                      "reportedBy_username": "Rep"},
            "media": {"media_type": "movie", "tmdbId": 555}}


async def test_dm_shows_playback_lines():
    tau = _tau()
    app = _app(tau, _issue())
    await handle_seerr_reported(app, _payload())
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Living Room" in text and "Roku" in text
    assert "Transcode" in text
    assert "video hevc, audio eac3" in text
    assert "2026-06-16" in text
    kw = tau.get_history.call_args.kwargs
    assert kw["user_id"] == 9 and kw["rating_key"] == "100"
    assert tau.get_stream_data.call_args.args[0] == 7


async def test_dm_without_match_has_no_lines():
    app = _app(_tau(rows=[]), _issue())
    await handle_seerr_reported(app, _payload())
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Player" not in text and "Transcode" not in text
    assert "New ticket" in text


async def test_dm_unmapped_reporter_has_no_lines():
    tau = _tau(users=[{"user_id": 1, "username": "other"}])
    app = _app(tau, _issue())
    await handle_seerr_reported(app, _payload())
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Player" not in text
    tau.get_history.assert_not_called()


async def test_dm_tautulli_error_still_sends():
    tau = _tau()
    tau.get_stream_data.side_effect = RuntimeError("down")
    app = _app(tau, _issue())
    await handle_seerr_reported(app, _payload())
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "New ticket" in text and "Player" not in text


async def test_episode_ticket_matches_only_its_season_and_episode():
    rows = [
        {"row_id": 1, "date": 1781568000, "player": "Wrong", "platform": "X",
         "parent_media_index": 1, "media_index": 6},
        {"row_id": 2, "date": 1781568000, "player": "WrongSeason",
         "platform": "X", "parent_media_index": 2, "media_index": 5},
        {"row_id": 3, "date": 1781568000, "player": "Right", "platform": "Y",
         "parent_media_index": 1, "media_index": 5},
    ]
    tau = _tau(rows=rows)
    issue = _issue(media_type="tv", problem_season=1, problem_episode=5)
    app = _app(tau, issue)
    await handle_seerr_reported(app, _payload())
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Right" in text and "Wrong" not in text
    assert tau.get_history.call_args.kwargs["grandparent_rating_key"] == "100"
    assert tau.get_stream_data.call_args.args[0] == 3


async def test_episode_ticket_with_no_matching_episode_has_no_lines():
    rows = [{"row_id": 1, "date": 1781568000, "player": "Wrong",
             "platform": "X", "parent_media_index": 1, "media_index": 6}]
    issue = _issue(media_type="tv", problem_season=1, problem_episode=5)
    app = _app(_tau(rows=rows), issue)
    await handle_seerr_reported(app, _payload())
    assert "Player" not in app.bot.send_message.call_args.kwargs["text"]


async def _card(ctx, user_id):
    upd = make_update(callback_data="tkopen:38", user_id=user_id)
    await tk_open(upd, ctx)
    return upd.callback_query.message.reply_calls[0]["text"]


async def test_admin_card_shows_playback_but_reporter_view_does_not():
    tau = _tau()
    ctx = make_ctx(admin_id=999, bot_data_overrides={"tautulli": tau})
    ctx.bot_data["seerr"].get_issue.return_value = _issue()
    assert "Living Room" in await _card(ctx, 999)

    tau.get_history.reset_mock()
    ctx2 = make_ctx(admin_id=999, bot_data_overrides={"tautulli": tau})
    ctx2.bot_data["seerr"].get_issue.return_value = _issue()
    text = await _card(ctx2, 42)
    assert "Living Room" not in text and "Transcode" not in text
    tau.get_history.assert_not_called()


async def test_season_zero_ticket_is_the_whole_show():
    rows = [{"row_id": 4, "date": 1781568000, "player": "Den", "platform": "Y",
             "parent_media_index": 2, "media_index": 3}]
    issue = _issue(media_type="tv", problem_season=0, problem_episode=0)
    app = _app(_tau(rows=rows), issue)
    await handle_seerr_reported(app, _payload())
    assert "Den" in app.bot.send_message.call_args.kwargs["text"]

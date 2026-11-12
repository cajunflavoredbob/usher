"""Ticket DMs are routed by Seerr user id, not by the user-editable display
name in the webhook payload. The id comes from fetching the issue with the
admin key (same approach as the MEDIA path's _resolve_requester)."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from bot.webhook_handlers import (
    handle_seerr_comment,
    handle_seerr_reported,
    handle_seerr_resolved,
)
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_link

ADMIN_TG, REPORTER_TG, VICTIM_TG = 999, 111, 222
ADMIN_SID, REPORTER_SID, VICTIM_SID = 1, 5, 8


def _issue(*, reporter_id=REPORTER_SID, commenter_id=ADMIN_SID):
    return IssueListItem(
        id=42, issue_type=1, status=1, created_at="", tmdb_id=1,
        media_type="movie", problem_season=None, problem_episode=None,
        created_by="x", created_by_id=reporter_id,
        last_comment_user_id=commenter_id,
    )


def _app(issue, *, admin_sid=ADMIN_SID):
    admin = replace(make_link(telegram_id=ADMIN_TG, plex_username="adminplex"),
                    seerr_id=admin_sid)
    reporter = replace(make_link(telegram_id=REPORTER_TG, plex_username="reporterplex"),
                       seerr_id=REPORTER_SID)
    victim = replace(make_link(telegram_id=VICTIM_TG, plex_username="victimplex"),
                     seerr_id=VICTIM_SID)
    by_sid = {ADMIN_SID: admin, REPORTER_SID: reporter, VICTIM_SID: victim}
    by_name = {"adminplex": admin, "reporterplex": reporter, "victimplex": victim}
    app = make_ctx(admin_id=ADMIN_TG).application
    store = app.bot_data["store"]
    store.get = AsyncMock(return_value=admin)
    store.find_by_seerr_id = AsyncMock(side_effect=lambda i: by_sid.get(i))
    store.find_by_plex_username = AsyncMock(side_effect=lambda n: by_name.get(n))
    seerr = app.bot_data["seerr"]
    if isinstance(issue, Exception):
        seerr.get_issue = AsyncMock(side_effect=issue)
    else:
        seerr.get_issue = AsyncMock(return_value=issue)
    seerr.get_admin_user_id = AsyncMock(return_value=ADMIN_SID)
    return app


def _payload(*, reporter, commenter=None):
    p = {
        "issue": {"issue_id": 42, "reportedBy_username": reporter,
                  "issue_status": "OPEN", "issue_type": "VIDEO"},
        "media": {},
        "message": "broken",
    }
    if commenter is not None:
        p["comment"] = {"commentedBy_username": commenter,
                        "comment_message": "hello"}
    return p


def _sent(app):
    return sorted(c.kwargs["chat_id"] for c in app.bot.send_message.call_args_list)


async def test_comment_reaches_reporter_with_custom_display_name():
    app = _app(_issue())
    await handle_seerr_comment(app, _payload(reporter="Cool Name", commenter="Admin Display"))
    assert _sent(app) == [REPORTER_TG]


async def test_comment_spoofed_reporter_name_does_not_reroute_to_victim():
    # Payload display name claims to be the victim's plex username.
    app = _app(_issue())
    await handle_seerr_comment(app, _payload(reporter="victimplex", commenter="Admin Display"))
    assert VICTIM_TG not in _sent(app)
    assert _sent(app) == [REPORTER_TG]


async def test_comment_spoofed_commenter_name_does_not_hide_dm():
    # A third party (id 8) names themselves like the reporter; the reporter
    # must still be told because the ids differ.
    app = _app(_issue(commenter_id=VICTIM_SID))
    await handle_seerr_comment(app, _payload(reporter="reporterplex", commenter="reporterplex"))
    assert _sent(app) == sorted([ADMIN_TG, REPORTER_TG])


async def test_admins_own_comment_never_dms_admin_even_with_other_display_name():
    # Admin comments through the API key (no linked plex name match).
    app = _app(_issue(commenter_id=ADMIN_SID))
    app.bot_data["store"].get = AsyncMock(return_value=None)
    await handle_seerr_comment(app, _payload(reporter="x", commenter="Whatever"))
    assert ADMIN_TG not in _sent(app)
    assert _sent(app) == [REPORTER_TG]


async def test_reporters_own_comment_goes_to_admin_only():
    app = _app(_issue(commenter_id=REPORTER_SID))
    await handle_seerr_comment(app, _payload(reporter="Cool Name", commenter="Cool Name"))
    assert _sent(app) == [ADMIN_TG]


async def test_comment_falls_back_to_username_when_fetch_fails():
    app = _app(RuntimeError("seerr down"))
    await handle_seerr_comment(app, _payload(reporter="reporterplex", commenter="adminplex"))
    assert _sent(app) == [REPORTER_TG]


async def test_resolved_reaches_reporter_with_custom_display_name():
    app = _app(_issue())
    await handle_seerr_resolved(app, _payload(reporter="Cool Name"))
    assert _sent(app) == sorted([ADMIN_TG, REPORTER_TG])


async def test_resolved_spoofed_name_does_not_dm_victim():
    app = _app(_issue())
    await handle_seerr_resolved(app, _payload(reporter="victimplex"))
    assert VICTIM_TG not in _sent(app)


async def test_reported_by_admin_with_custom_display_name_is_not_dmed():
    app = _app(_issue(reporter_id=ADMIN_SID))
    await handle_seerr_reported(app, _payload(reporter="Boss Man"))
    assert _sent(app) == []


async def test_reported_by_someone_named_like_admin_is_still_dmed():
    app = _app(_issue(reporter_id=REPORTER_SID))
    await handle_seerr_reported(app, _payload(reporter="adminplex"))
    assert _sent(app) == [ADMIN_TG]


async def test_admin_replying_under_key_id_on_own_ticket_dms_nobody():
    # Admin filed the Ticket as their linked account (id 3) and replied
    # through the API key (id 1): same person, so no self-DM.
    app = _app(_issue(reporter_id=3, commenter_id=ADMIN_SID), admin_sid=3)
    await handle_seerr_comment(app, _payload(reporter="x", commenter="y"))
    assert _sent(app) == []

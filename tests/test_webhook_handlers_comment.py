"""Tests for bot.webhook_handlers.handle_seerr_comment notification routing.

The handler notifies the *other* party in the conversation: the reporter when
someone else comments, the admin when the reporter (or a third party) comments.
Whoever wrote the comment is never notified about their own comment. The
reporter-followup -> admin path is the v0.11.22 fix (Nathan's followup wasn't
reaching Kenny because commenter == reporter short-circuited the whole handler).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.webhook_handlers import handle_seerr_comment
from bot.callback_prefixes import TK_OPEN, TK_REPLY
from tests._handler_harness import make_ctx, make_mapping


def _app(*, admin_id=999, admin_mapping=None, reporter_mapping=None):
    """Build a fake Application for the comment handler. `admin_mapping` is what
    store.get(admin_id) returns; `reporter_mapping` is what
    store.find_by_plex_username(reporter) returns."""
    app = make_ctx(admin_id=admin_id).application
    app.bot_data["store"].get = AsyncMock(return_value=admin_mapping)
    app.bot_data["store"].find_by_plex_username = AsyncMock(return_value=reporter_mapping)
    return app


def _payload(*, commenter, reporter="nathan", status="OPEN", text="any update?"):
    return {
        "notification_type": "ISSUE_COMMENT",
        "issue": {
            "issue_id": 42,
            "reportedBy_username": reporter,
            "issue_status": status,
        },
        "comment": {
            "commentedBy_username": commenter,
            "comment_message": text,
        },
        "media": {},
    }


def _sent_chat_ids(app):
    return [c.kwargs["chat_id"] for c in app.bot.send_message.call_args_list]


# --- the v0.11.22 fix: reporter follows up -> admin gets notified -----------


async def test_reporter_followup_notifies_admin_not_reporter():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    # Nathan (the reporter) comments on his own issue.
    await handle_seerr_comment(app, _payload(commenter="nathan", reporter="nathan"))

    ids = _sent_chat_ids(app)
    assert ids == [999], "only the admin should be DMed; reporter wrote it"


# --- admin replies -> reporter notified, admin not echoed -------------------


async def test_admin_reply_notifies_reporter_not_admin():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    # Kenny (admin) comments; commenter == admin's plex username.
    await handle_seerr_comment(app, _payload(commenter="kennyplex", reporter="nathan"))

    ids = _sent_chat_ids(app)
    assert ids == [111], "only the reporter should be DMed; admin wrote it"


# --- third party comments -> both reporter and admin notified ---------------


async def test_third_party_comment_notifies_both():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    await handle_seerr_comment(app, _payload(commenter="someoneelse", reporter="nathan"))

    ids = sorted(_sent_chat_ids(app))
    assert ids == [111, 999]


# --- admin is the reporter -> no double DM ----------------------------------


async def test_admin_is_reporter_no_double_dm():
    # Admin filed the issue themselves; a third party comments.
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=admin)

    await handle_seerr_comment(app, _payload(commenter="someoneelse", reporter="kennyplex"))

    ids = _sent_chat_ids(app)
    assert ids == [999], "admin/reporter is the same chat -- notify once, not twice"


# --- buttons: Reply only when OPEN, History always --------------------------


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_reply_and_history_present_when_open():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    await handle_seerr_comment(app, _payload(commenter="nathan", status="OPEN"))

    cbs = _callbacks(app.bot.send_message.call_args.kwargs["reply_markup"])
    assert f"{TK_REPLY}:42" in cbs
    assert f"{TK_OPEN}:42" in cbs


async def test_history_present_no_reply_when_resolved():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    await handle_seerr_comment(app, _payload(commenter="nathan", status="RESOLVED"))

    cbs = _callbacks(app.bot.send_message.call_args.kwargs["reply_markup"])
    assert cbs == [f"{TK_OPEN}:42"]   # History only, no Reply on a resolved ticket


# --- affected season/episode scope line -------------------------------------


async def test_scope_line_from_extra_array():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)
    # get_media_title is mocked to ("Movie Title", "2026") in the harness.
    payload = {
        "notification_type": "ISSUE_COMMENT",
        "issue": {"issue_id": 42, "reportedBy_username": "nathan", "issue_status": "OPEN"},
        "comment": {"commentedBy_username": "nathan", "comment_message": "still broken"},
        "media": {"media_type": "tv", "tmdbId": 555},
        "extra": [
            {"name": "Affected Season", "value": "1"},
            {"name": "Affected Episode", "value": "5"},
        ],
    }
    await handle_seerr_comment(app, payload)
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Season 1, Episode 5" in text


# --- guards -----------------------------------------------------------------


async def test_empty_comment_dropped():
    app = _app()
    await handle_seerr_comment(app, _payload(commenter="nathan", text="   "))
    app.bot.send_message.assert_not_called()


async def test_reporter_followup_admin_unlinked_nobody_notified():
    """Reporter comments but no admin_id is configured and reporter wrote it,
    so there's no one to route to."""
    app = _app(admin_id=None, admin_mapping=None, reporter_mapping=None)
    await handle_seerr_comment(app, _payload(commenter="nathan", reporter="nathan"))
    app.bot.send_message.assert_not_called()

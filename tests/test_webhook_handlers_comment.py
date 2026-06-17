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
from bot.callback_prefixes import TK_REPLY
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


# --- reply button only when OPEN --------------------------------------------


async def test_reply_button_present_when_open():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    await handle_seerr_comment(app, _payload(commenter="nathan", status="OPEN"))

    kb = app.bot.send_message.call_args.kwargs["reply_markup"]
    assert kb is not None
    assert kb.inline_keyboard[0][0].callback_data == f"{TK_REPLY}:42"


async def test_no_reply_button_when_resolved():
    admin = make_mapping(telegram_id=999, plex_username="kennyplex")
    reporter = make_mapping(telegram_id=111, plex_username="nathan")
    app = _app(admin_id=999, admin_mapping=admin, reporter_mapping=reporter)

    await handle_seerr_comment(app, _payload(commenter="nathan", status="RESOLVED"))

    assert app.bot.send_message.call_args.kwargs["reply_markup"] is None


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

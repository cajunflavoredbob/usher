"""Tests for handle_seerr_media: who gets DMed per MEDIA_* event, the
requester-is-admin suppression, the null-request/notifyuser fallback, and
the requested-seasons note."""
from __future__ import annotations

from unittest.mock import AsyncMock

from bot.webhook_handlers import handle_seerr_media

from tests._handler_harness import make_ctx, make_mapping

USER_ID = 42
ADMIN_ID = 999


def _app(*, requester_mapping=None, admin_id=ADMIN_ID):
    ctx = make_ctx(admin_id=admin_id)
    app = ctx.application
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=requester_mapping)
    return app


def _payload(nt: str, *, username="user1plex", request_block=True, extra=None,
             notifyuser=None):
    p = {
        "notification_type": nt,
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550, "status": "AVAILABLE"},
        "request": ({"request_id": "12", "requestedBy_username": username}
                    if request_block else None),
    }
    if extra is not None:
        p["extra"] = extra
    if notifyuser is not None:
        p["notifyuser_username"] = notifyuser
    return p


def _sent_chat_ids(app) -> list[int]:
    return [c.kwargs["chat_id"] for c in app.bot.send_message.call_args_list]


async def test_pending_notifies_admin_only():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    assert _sent_chat_ids(app) == [ADMIN_ID]
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "New request" in text
    assert "user1plex" in text


async def test_approved_notifies_requester_only():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_APPROVED"))
    assert _sent_chat_ids(app) == [USER_ID]
    assert "approved" in app.bot.send_message.call_args.kwargs["text"]


async def test_auto_approved_notifies_admin_only():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_AUTO_APPROVED"))
    assert _sent_chat_ids(app) == [ADMIN_ID]
    assert "auto-approved" in app.bot.send_message.call_args.kwargs["text"]


async def test_declined_notifies_requester_only():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_DECLINED"))
    assert _sent_chat_ids(app) == [USER_ID]


async def test_available_notifies_requester():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_AVAILABLE"))
    assert _sent_chat_ids(app) == [USER_ID]
    assert "available in Plex" in app.bot.send_message.call_args.kwargs["text"]


async def test_failed_notifies_both():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    assert sorted(_sent_chat_ids(app)) == [USER_ID, ADMIN_ID]


async def test_admin_requester_not_double_notified():
    """Admin requested it themselves: the 'New request' admin FYI is noise."""
    app = _app(requester_mapping=make_mapping(telegram_id=ADMIN_ID))
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    assert app.bot.send_message.call_args_list == []


async def test_unlinked_requester_gets_no_dm():
    app = _app(requester_mapping=None)
    await handle_seerr_media(app, _payload("MEDIA_APPROVED"))
    assert app.bot.send_message.call_args_list == []


async def test_available_null_request_uses_notifyuser_fallback():
    """Scan-triggered MEDIA_AVAILABLE can carry request: null; notifyuser_*
    then identifies the target."""
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload(
        "MEDIA_AVAILABLE", request_block=False, notifyuser="user1plex"))
    assert _sent_chat_ids(app) == [USER_ID]
    app.bot_data["store"].find_by_plex_username.assert_awaited_with("user1plex")


async def test_requested_seasons_note_included():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload(
        "MEDIA_APPROVED",
        extra=[{"name": "Requested Seasons", "value": "1, 3"}]))
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Seasons: 1, 3" in text


async def test_auto_requested_is_silent():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload("MEDIA_AUTO_REQUESTED"))
    assert app.bot.send_message.call_args_list == []


async def test_title_falls_back_to_subject():
    """When the TMDb lookup fails, the DM carries Seerr's subject line
    instead of no title at all."""
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    app.bot_data["seerr"].get_media_title.side_effect = RuntimeError("down")
    await handle_seerr_media(app, _payload("MEDIA_APPROVED"))
    assert "Some Movie (2026)" in app.bot.send_message.call_args.kwargs["text"]


# --- id-based requester resolution -------------------------------------------

def _stub_request_lookup(app, requested_by_id):
    from seerr import RequestListItem, REQUEST_STATUS_PENDING
    app.bot_data["seerr"].get_request = AsyncMock(return_value=RequestListItem(
        id=12, status=REQUEST_STATUS_PENDING, media_type="movie",
        tmdb_id=550, requested_by_id=requested_by_id))


async def test_requester_resolved_by_id_beats_spoofed_display_name():
    """Display names are user-editable in Seerr; the numeric id from the
    request lookup wins over the payload's username."""
    victim = make_mapping(telegram_id=777, plex_username="victimplex")
    app = _app(requester_mapping=victim)  # username match would hit victim
    real = make_mapping(telegram_id=USER_ID)
    _stub_request_lookup(app, requested_by_id=42)
    app.bot_data["store"].find_by_seerr_id = AsyncMock(return_value=real)
    await handle_seerr_media(app, _payload("MEDIA_APPROVED",
                                           username="victimplex"))
    app.bot_data["store"].find_by_seerr_id.assert_awaited_with(42)
    assert _sent_chat_ids(app) == [USER_ID]  # the real requester, not victim


async def test_unlinked_admin_request_does_not_self_notify():
    """The admin can request without a mapping (admin-key attribution);
    matching the request's Seerr user id against the API key's own id
    suppresses the 'New request' self-DM."""
    app = _app(requester_mapping=None)
    _stub_request_lookup(app, requested_by_id=1)
    app.bot_data["store"].find_by_seerr_id = AsyncMock(return_value=None)
    app.bot_data["seerr"].get_admin_user_id = AsyncMock(return_value=1)
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    assert app.bot.send_message.call_args_list == []


async def test_linked_admin_failed_request_drops_notified_claim():
    """'The admin has been notified' is a lie when the requester IS the
    admin; the clause is dropped for them."""
    app = _app(requester_mapping=make_mapping(telegram_id=ADMIN_ID))
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    texts = [c.kwargs["text"] for c in app.bot.send_message.call_args_list]
    assert len(texts) == 1
    assert "failed to download" in texts[0]
    assert "admin has been notified" not in texts[0]


async def test_garbage_extra_entry_does_not_kill_dms():
    app = _app(requester_mapping=make_mapping(telegram_id=USER_ID))
    await handle_seerr_media(app, _payload(
        "MEDIA_APPROVED", extra=["garbage-string", None,
                                 {"name": "Requested Seasons", "value": "2"}]))
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Seasons: 2" in text


async def test_unlinked_admin_gets_lifecycle_dms():
    """The unlinked admin (admin-key attribution) has no mapping; their
    approved/declined/available/failed DMs go to the admin chat instead of
    vanishing -- MEDIA_FAILED previously notified NOBODY."""
    app = _app(requester_mapping=None)
    _stub_request_lookup(app, requested_by_id=1)
    app.bot_data["store"].find_by_seerr_id = AsyncMock(return_value=None)
    app.bot_data["seerr"].get_admin_user_id = AsyncMock(return_value=1)
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    assert _sent_chat_ids(app) == [ADMIN_ID]
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "failed to download" in text
    assert "admin has been notified" not in text  # requester IS the admin

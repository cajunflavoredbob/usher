"""Tests for availability subscriptions: card keyboard gating, add/remove
handlers, the /subscriptions list, one-shot store semantics, and the
MEDIA_AVAILABLE fan-out (privacy: per-user only)."""
from __future__ import annotations

from unittest.mock import AsyncMock

from seerr import MEDIA_STATUS_AVAILABLE, MEDIA_STATUS_BLOCKLISTED, MediaResult
from bot.media_card import card_keyboard
from bot.subscriptions import (
    cmd_subscriptions,
    fan_out_availability,
    subscription_add,
    subscription_del,
)

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_offers_notify_only_when_availability_can_change():
    requestable = MediaResult("movie", 1, "T", "2026", None, status=None)
    available = MediaResult("movie", 2, "T", "2026", 5,
                            status=MEDIA_STATUS_AVAILABLE)
    blocked = MediaResult("movie", 3, "T", "2026", None,
                          status=MEDIA_STATUS_BLOCKLISTED)
    assert any(cb.startswith("subadd:") for cb in _callbacks(card_keyboard(requestable)))
    assert not any(cb.startswith("subadd:") for cb in _callbacks(card_keyboard(available)))
    assert not any(cb.startswith("subadd:") for cb in _callbacks(card_keyboard(blocked)))


async def test_subscription_add_stores_with_title():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await subscription_add(upd, ctx)
    store = ctx.bot_data["store"]
    assert store.add_subscription.call_args.args == (USER_ID, "movie", 550,
                                                     "TMDb 550")
    store.set_subscription_label.assert_awaited_once_with(
        USER_ID, "movie", 550, "Movie Title (2026)")
    assert any("Added" in a[0] for a in upd.callback_query.answers)


async def test_subscription_add_answers_before_the_title_lookup():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    answered_first = []

    async def lookup(*_a, **_k):
        answered_first.append(bool(upd.callback_query.answers))
        return "Movie Title", 2026

    ctx.bot_data["seerr"].get_media_title = lookup
    await subscription_add(upd, ctx)
    assert answered_first == [True]


async def test_subscription_add_gated_for_unlinked():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await subscription_add(upd, ctx)
    ctx.bot_data["store"].add_subscription.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_subscription_add_duplicate_says_already_listed():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["store"].add_subscription.return_value = False
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await subscription_add(upd, ctx)
    assert any("Already" in a[0] for a in upd.callback_query.answers)


async def test_subscriptions_list_and_delete_own_only():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["store"].list_subscriptions.return_value = [
        (7, "movie", 550, "Fight Club (1999)"),
        (9, "tv", 1399, "Game of Thrones (2011)"),
    ]
    upd = make_update(text="/subscriptions", user_id=USER_ID)
    await cmd_subscriptions(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    assert "Fight Club" in call["text"]
    assert "subdel:7" in _callbacks(call["reply_markup"])

    del_upd = make_update(callback_data="subdel:7", user_id=USER_ID)
    await subscription_del(del_upd, ctx)
    # Owner-scoped delete: telegram_id travels with the id.
    assert ctx.bot_data["store"].remove_subscription.call_args.args == (7, USER_ID)


async def test_empty_list_hint():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    upd = make_update(text="/subscriptions", user_id=USER_ID)
    await cmd_subscriptions(upd, ctx)
    assert "You have no Subscriptions" in upd.effective_message.reply_calls[-1]["text"]


async def test_fan_out_dms_subscribers_except_already_notified():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[42, 77, 88])
    await fan_out_availability(app, "movie", 550, "🎬 Fight Club (1999)",
                               already_notified={77})
    chat_ids = [c.kwargs["chat_id"] for c in app.bot.send_message.call_args_list]
    assert sorted(chat_ids) == [42, 88]
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "Now available" in text and "Fight Club" in text


async def test_available_webhook_triggers_fanout():
    from bot.webhook_handlers import handle_seerr_media
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=USER_ID))
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[88])
    payload = {
        "notification_type": "MEDIA_AVAILABLE",
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550, "status": "AVAILABLE"},
        "request": {"request_id": "12", "requestedBy_username": "user1plex"},
    }
    await handle_seerr_media(app, payload)
    chat_ids = [c.kwargs["chat_id"] for c in app.bot.send_message.call_args_list]
    # requester DM + subscriber DM, no double to the requester
    assert sorted(chat_ids) == [USER_ID, 88]


# --- real-store round trip ----------------------------------------------------

async def test_store_subscription_round_trip(fresh_store):
    assert await fresh_store.add_subscription(1, "movie", 550, "Fight Club")
    assert not await fresh_store.add_subscription(1, "movie", 550, "Fight Club")
    assert await fresh_store.add_subscription(2, "movie", 550, "Fight Club")
    assert await fresh_store.add_subscription(1, "tv", 1399, "GoT")
    subscriptions = await fresh_store.list_subscriptions(1)
    assert [(s[1], s[2]) for s in subscriptions] == [("movie", 550), ("tv", 1399)]
    # one-shot pop consumes every subscriber of the title, both users
    popped = await fresh_store.pop_subscribers("movie", 550)
    assert sorted(popped) == [1, 2]
    assert await fresh_store.pop_subscribers("movie", 550) == []
    # remove is owner-scoped
    subscriptions = await fresh_store.list_subscriptions(1)
    subscription_id = subscriptions[0][0]
    assert not await fresh_store.remove_subscription(subscription_id, telegram_id=2)
    assert await fresh_store.remove_subscription(subscription_id, telegram_id=1)
    assert await fresh_store.list_subscriptions(1) == []


async def test_fanout_disabled_keeps_rows_unconsumed():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["settings_store"].settings.tg_notify_subscriptions = False
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[42])
    await fan_out_availability(app, "movie", 550, "T", already_notified=set())
    app.bot_data["store"].pop_subscribers.assert_not_awaited()
    app.bot.send_message.assert_not_awaited()


async def test_long_subscription_list_stays_under_telegram_limits():
    from bot.subscriptions import SUBSCRIPTIONS_SHOWN_MAX, cmd_subscriptions
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["store"].list_subscriptions.return_value = [
        (i, "movie", i, "A Rather Long Movie Title Number %d (2026)" % i)
        for i in range(1, 121)]
    upd = make_update(text="/subscriptions", user_id=USER_ID)
    await cmd_subscriptions(upd, ctx)
    sent = upd.effective_message.reply_calls[0]
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert len(buttons) == SUBSCRIPTIONS_SHOWN_MAX < 100
    assert len(sent["text"]) <= 4096
    assert "Showing the first" in sent["text"]


# --- Watch now button ---------------------------------------------------------

PLEX_URL = "https://app.plex.tv/desktop/#!/server/abc/details?key=%2Flibrary%2Fmetadata%2F9"


def _buttons(call):
    markup = call.kwargs.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.url) for row in markup.inline_keyboard for b in row]


async def test_fan_out_carries_watch_now_when_media_url_found():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["seerr"].get_media_url = AsyncMock(return_value=PLEX_URL)
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[88])
    await fan_out_availability(app, "movie", 550, "T", already_notified=set())
    app.bot_data["seerr"].get_media_url.assert_awaited_once_with("movie", 550)
    assert _buttons(app.bot.send_message.call_args) == [("▶️ Watch now", PLEX_URL)]


async def test_fan_out_has_no_button_without_media_url():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[88])
    await fan_out_availability(app, "movie", 550, "T", already_notified=set())
    assert _buttons(app.bot.send_message.call_args) == []
    assert app.bot.send_message.await_count == 1


async def test_fan_out_sends_without_button_when_lookup_errors():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["seerr"].get_media_url = AsyncMock(side_effect=RuntimeError("down"))
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[88])
    await fan_out_availability(app, "movie", 550, "T", already_notified=set())
    assert _buttons(app.bot.send_message.call_args) == []
    assert "Now available" in app.bot.send_message.call_args.kwargs["text"]


async def test_requester_available_dm_carries_watch_now():
    from bot.webhook_handlers import handle_seerr_media
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["seerr"].get_media_url = AsyncMock(return_value=PLEX_URL)
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=USER_ID))
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[])
    payload = {
        "notification_type": "MEDIA_AVAILABLE",
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550, "status": "AVAILABLE"},
        "request": {"request_id": "12", "requestedBy_username": "user1plex"},
    }
    await handle_seerr_media(app, payload)
    assert _buttons(app.bot.send_message.call_args) == [("▶️ Watch now", PLEX_URL)]


async def test_requester_available_dm_has_no_button_without_media_url():
    from bot.webhook_handlers import handle_seerr_media
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["seerr"].get_media_url = AsyncMock(side_effect=RuntimeError("x"))
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=USER_ID))
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[])
    payload = {
        "notification_type": "MEDIA_AVAILABLE",
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550, "status": "AVAILABLE"},
        "request": {"request_id": "12", "requestedBy_username": "user1plex"},
    }
    await handle_seerr_media(app, payload)
    assert _buttons(app.bot.send_message.call_args) == []
    assert "available in Plex" in app.bot.send_message.call_args.kwargs["text"]


async def test_client_get_media_url_reads_media_info(monkeypatch):
    import httpx
    import seerr as seerr_mod

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/movie/550"):
            return httpx.Response(200, json={"mediaInfo": {"mediaUrl": PLEX_URL}})
        return httpx.Response(200, json={"mediaInfo": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", lambda *a, **k: real(
        *a, **{**k, "transport": httpx.MockTransport(handler)}))
    client = seerr_mod.SeerrClient("http://seerr.test", "key")
    assert await client.get_media_url("movie", 550) == PLEX_URL
    assert await client.get_media_url("tv", 1399) is None
    assert seen[0].endswith("/movie/550") and seen[1].endswith("/tv/1399")
    await client.close()

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
    sub_add,
    sub_del,
)

from tests._handler_harness import make_ctx, make_mapping, make_update

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


async def test_sub_add_stores_with_title():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await sub_add(upd, ctx)
    args = ctx.bot_data["store"].add_subscription.call_args.args
    assert args == (USER_ID, "movie", 550, "Movie Title (2026)")
    assert any("notified" in a[0].lower() or "DMed" in a[0]
               for a in upd.callback_query.answers)


async def test_sub_add_gated_for_unlinked():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await sub_add(upd, ctx)
    ctx.bot_data["store"].add_subscription.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_sub_add_duplicate_says_already_listed():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["store"].add_subscription.return_value = False
    upd = make_update(callback_data="subadd:movie:550", user_id=USER_ID)
    await sub_add(upd, ctx)
    assert any("Already" in a[0] for a in upd.callback_query.answers)


async def test_subscriptions_list_and_delete_own_only():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
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
    await sub_del(del_upd, ctx)
    # Owner-scoped delete: telegram_id travels with the id.
    assert ctx.bot_data["store"].remove_subscription.call_args.args == (7, USER_ID)


async def test_empty_list_hint():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    upd = make_update(text="/subscriptions", user_id=USER_ID)
    await cmd_subscriptions(upd, ctx)
    assert "not watching anything" in upd.effective_message.reply_calls[-1]["text"]


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
        return_value=make_mapping(telegram_id=USER_ID))
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
    subs = await fresh_store.list_subscriptions(1)
    assert [(s[1], s[2]) for s in subs] == [("movie", 550), ("tv", 1399)]
    # one-shot pop consumes every subscriber of the title, both users
    popped = await fresh_store.pop_subscribers("movie", 550)
    assert sorted(popped) == [1, 2]
    assert await fresh_store.pop_subscribers("movie", 550) == []
    # remove is owner-scoped
    subs = await fresh_store.list_subscriptions(1)
    sub_id = subs[0][0]
    assert not await fresh_store.remove_subscription(sub_id, telegram_id=2)
    assert await fresh_store.remove_subscription(sub_id, telegram_id=1)
    assert await fresh_store.list_subscriptions(1) == []


async def test_fanout_disabled_keeps_rows_unconsumed():
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["settings_store"].settings.tg_notify_subscriptions = False
    app.bot_data["store"].pop_subscribers = AsyncMock(return_value=[42])
    await fan_out_availability(app, "movie", 550, "T", already_notified=set())
    app.bot_data["store"].pop_subscribers.assert_not_awaited()
    app.bot.send_message.assert_not_awaited()

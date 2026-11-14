"""The Share Expiry job (reminders, removal, retry) and the buttons on its
DMs, driven against a real store and a fake Plex client."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.callback_prefixes import (SHARE_EXTEND, SHARE_KEEP, SHARE_LET_END,
                                   SHARE_REMOVE_NOW, SHARE_REMOVE_RETRY,
                                   SHARE_REMOVE_YES, SHARE_RETRY)
from bot.share_expiry_job import run_share_expiries, share_expiry_action
from bot.shared import BTN_HISTORY_MAX, global_btn_gate, record_btn
from http_util import NotFoundAPIError, TransientAPIError
from plex import PlexShare
from store import UserStore

from tests._handler_harness import make_ctx, make_message, make_update

ADMIN_ID = 999
HOLDER_ID = 555
NOW = datetime(2030, 6, 10, 12, 0, tzinfo=timezone.utc)


def _at(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%d %H:%M:%S")


def _share(id, email="a@example.com", username="alice"):
    return PlexShare(id=id, email=email, username=username, accepted=True,
                     all_libraries=True)


async def _setup(store: UserStore, shares=()):
    await store.link_with_plex(
        telegram_id=ADMIN_ID, seerr_id=1, seerr_display="Admin",
        plex_token="admin-tok", plex_uuid="ua", plex_username="admin")
    plex = AsyncMock()
    plex.get_owned_server.return_value = ("machine-1", "server1")
    plex.list_shares.return_value = list(shares)
    app = SimpleNamespace(
        bot=AsyncMock(),
        bot_data={"store": store, "plex": plex, "admin_id": ADMIN_ID})
    return app, plex


def _sent_to(app, chat_id):
    return [c.kwargs for c in app.bot.send_message.await_args_list
            if c.kwargs["chat_id"] == chat_id]


def _buttons(markup):
    return [cb.callback_data for row in markup.inline_keyboard for cb in row]


async def _holder(store):
    await store.link_with_plex(
        telegram_id=HOLDER_ID, seerr_id=2, seerr_display="Alice",
        plex_token="t", plex_uuid="uh", plex_username="Alice")


async def test_reminder_goes_out_once_to_admin_and_holder(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await _holder(fresh_store)
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(timedelta(days=2, hours=23)))
    await run_share_expiries(app, NOW)
    await run_share_expiries(app, NOW + timedelta(hours=1))
    admin = _sent_to(app, ADMIN_ID)
    assert len(admin) == 1
    assert _buttons(admin[0]["reply_markup"]) == [
        f"{SHARE_EXTEND}:1", f"{SHARE_LET_END}:1", f"{SHARE_REMOVE_NOW}:1"]
    holder = _sent_to(app, HOLDER_ID)
    assert len(holder) == 1
    assert "ask the Admin" in holder[0]["text"]
    assert "a@example.com" not in holder[0]["text"]
    plex.remove_share.assert_not_awaited()


async def test_nothing_is_sent_before_the_reminder_window(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(timedelta(days=4)))
    await run_share_expiries(app, NOW)
    app.bot.send_message.assert_not_awaited()


async def test_unmatched_holder_gets_no_dm(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(timedelta(days=2)))
    await run_share_expiries(app, NOW)
    assert len(_sent_to(app, ADMIN_ID)) == 1
    assert len(app.bot.send_message.await_args_list) == 1


async def test_expired_share_is_removed_and_admin_told(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(minutes=1)))
    await run_share_expiries(app, NOW)
    plex.remove_share.assert_awaited_once_with("admin-tok", 1)
    assert await fresh_store.get_share_expiry(1) is None
    assert "Removed" in _sent_to(app, ADMIN_ID)[0]["text"]


async def test_share_already_gone_deletes_row_without_a_delete_call(
        fresh_store):
    app, plex = await _setup(fresh_store, [])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(hours=2)))
    await run_share_expiries(app, NOW)
    plex.remove_share.assert_not_awaited()
    assert await fresh_store.get_share_expiry(1) is None
    app.bot.send_message.assert_not_awaited()


async def test_failing_removal_keeps_row_retries_and_alerts_once_a_day(
        fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    plex.remove_share.side_effect = TransientAPIError("down", service="Plex")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(minutes=1)))
    await run_share_expiries(app, NOW)
    row = await fresh_store.get_share_expiry(1)
    assert row is not None and row.last_error and row.last_attempt_at
    first = _sent_to(app, ADMIN_ID)
    assert len(first) == 1
    assert _buttons(first[0]["reply_markup"]) == [f"{SHARE_RETRY}:1"]
    for hours in (1, 2, 23):
        await run_share_expiries(app, NOW + timedelta(hours=hours))
    assert plex.remove_share.await_count == 4
    assert len(_sent_to(app, ADMIN_ID)) == 1
    await run_share_expiries(app, NOW + timedelta(hours=24))
    assert len(_sent_to(app, ADMIN_ID)) == 2
    assert await fresh_store.get_share_expiry(1) is not None


async def test_failed_listing_keeps_every_due_row(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    plex.list_shares.side_effect = TransientAPIError("down", service="Plex")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(minutes=1)))
    await run_share_expiries(app, NOW)
    assert await fresh_store.get_share_expiry(1) is not None
    plex.remove_share.assert_not_awaited()
    assert len(_sent_to(app, ADMIN_ID)) == 1


async def test_failed_dm_is_retried_next_tick(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    app.bot.send_message.side_effect = [RuntimeError("tg down"), None]
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(timedelta(days=2)))
    await run_share_expiries(app, NOW)
    await run_share_expiries(app, NOW + timedelta(hours=1))
    assert app.bot.send_message.await_count == 2
    assert (await fresh_store.get_share_expiry(1)).admin_reminded


# --- the buttons -------------------------------------------------------------

def _tap(ctx_app, data, user_id=ADMIN_ID):
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["store"] = ctx_app.bot_data["store"]
    ctx.bot_data["plex"] = ctx_app.bot_data["plex"]
    return ctx, make_update(callback_data=data, user_id=user_id)


async def _gated_tap(app, data):
    """A tap on a DM that was never recorded, with the Admin's button history
    full of newer messages: through the button gate, then the handler."""
    ctx, upd = _tap(app, data)
    ctx.application = SimpleNamespace(bot_data=ctx.bot_data)
    for i in range(BTN_HISTORY_MAX + 1):
        record_btn(ctx.application, ADMIN_ID,
                   make_message(message_id=5000 + i))
    await global_btn_gate(upd, ctx)
    await share_expiry_action(upd, ctx)
    return upd


async def test_extend_gets_through_the_button_gate(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_EXTEND}:1")
    assert upd.callback_query.markup_edits == []
    row = await fresh_store.get_share_expiry(1)
    assert row.expires_at == "2031-01-31 00:00:00"


async def test_let_it_end_gets_through_the_button_gate(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_LET_END}:1")
    assert "ends on Jan 1, 2031" in upd.callback_query.edits[-1]["text"]


async def test_retry_gets_through_the_button_gate(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2020-01-01 00:00:00")
    await _gated_tap(app, f"{SHARE_RETRY}:1")
    plex.remove_share.assert_awaited_once_with("admin-tok", 1)


async def test_remove_now_gets_through_the_button_gate(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_REMOVE_NOW}:1")
    assert upd.callback_query.markup_edits == []
    assert upd.callback_query.edits


async def test_extend_moves_the_date_and_rearms(fresh_store):
    app, _ = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    await fresh_store.mark_share_expiry_reminded(1, admin=True, holder=True)
    ctx, upd = _tap(app, f"{SHARE_EXTEND}:1")
    await share_expiry_action(upd, ctx)
    row = await fresh_store.get_share_expiry(1)
    assert row.expires_at == "2031-01-31 00:00:00"
    assert not row.admin_reminded and not row.holder_reminded
    assert "now ends on Jan 31, 2031" in upd.callback_query.edits[-1]["text"]


async def test_let_it_end_changes_nothing(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    before = await fresh_store.get_share_expiry(1)
    ctx, upd = _tap(app, f"{SHARE_LET_END}:1")
    await share_expiry_action(upd, ctx)
    assert await fresh_store.get_share_expiry(1) == before
    plex.remove_share.assert_not_awaited()


async def test_remove_now_asks_first_and_removes_nothing(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_REMOVE_NOW}:1")
    await share_expiry_action(upd, ctx)
    plex.remove_share.assert_not_awaited()
    assert await fresh_store.get_share_expiry(1) is not None
    edit = upd.callback_query.edits[-1]
    assert "Remove <b>a@example.com</b>'s access (accepted share)?" in edit["text"]
    assert "They lose the server immediately" in edit["text"]
    assert _buttons(edit["reply_markup"]) == [
        f"{SHARE_REMOVE_YES}:1", f"{SHARE_KEEP}:1"]


async def test_remove_now_confirm_for_a_pending_invite_says_so(fresh_store):
    pending = PlexShare(id=1, email="a@example.com", username="alice",
                        accepted=False, all_libraries=True)
    app, _ = await _setup(fresh_store, [pending])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_REMOVE_NOW}:1")
    await share_expiry_action(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "PENDING invite" in text
    assert "cancelled before they accept it" in text


async def test_remove_now_when_the_share_is_gone_drops_the_row(fresh_store):
    app, plex = await _setup(fresh_store, [])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_REMOVE_NOW}:1")
    await share_expiry_action(upd, ctx)
    assert await fresh_store.get_share_expiry(1) is None
    assert "Already removed" in upd.callback_query.edits[-1]["text"]
    plex.remove_share.assert_not_awaited()


async def test_remove_now_listing_failure_offers_try_again(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    plex.list_shares.side_effect = TransientAPIError("down", service="Plex")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_REMOVE_NOW}:1")
    await share_expiry_action(upd, ctx)
    assert _buttons(upd.callback_query.edits[-1]["reply_markup"]) == [
        f"{SHARE_REMOVE_NOW}:1"]
    plex.remove_share.assert_not_awaited()


async def test_confirmed_remove_removes_the_share(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_REMOVE_YES}:1")
    plex.remove_share.assert_awaited_once_with("admin-tok", 1)
    assert await fresh_store.get_share_expiry(1) is None
    assert "Removed" in upd.callback_query.edits[-1]["text"]


async def test_keep_puts_the_reminder_back_and_removes_nothing(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_KEEP}:1")
    plex.remove_share.assert_not_awaited()
    assert await fresh_store.get_share_expiry(1) is not None
    edit = upd.callback_query.edits[-1]
    assert "access ends on" in edit["text"]
    assert _buttons(edit["reply_markup"]) == [
        f"{SHARE_EXTEND}:1", f"{SHARE_LET_END}:1", f"{SHARE_REMOVE_NOW}:1"]


async def test_retry_failure_keeps_row_and_offers_retry_again(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    plex.remove_share.side_effect = TransientAPIError("down", service="Plex")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2020-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_RETRY}:1")
    await share_expiry_action(upd, ctx)
    row = await fresh_store.get_share_expiry(1)
    assert row is not None and row.last_error
    assert _buttons(upd.callback_query.edits[-1]["reply_markup"]) == [
        f"{SHARE_REMOVE_RETRY}:1"]


async def test_retry_when_plex_says_gone_drops_the_row(fresh_store):
    app, plex = await _setup(fresh_store, [])
    plex.remove_share.side_effect = NotFoundAPIError("gone", status_code=404)
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2020-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_RETRY}:1")
    await share_expiry_action(upd, ctx)
    assert await fresh_store.get_share_expiry(1) is None
    assert "Already removed" in upd.callback_query.edits[-1]["text"]


async def test_buttons_are_admin_only(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    for prefix in (SHARE_EXTEND, SHARE_LET_END, SHARE_REMOVE_NOW,
                   SHARE_REMOVE_YES, SHARE_KEEP,
                   SHARE_RETRY, SHARE_REMOVE_RETRY):
        ctx, upd = _tap(app, f"{prefix}:1", user_id=12345)
        await share_expiry_action(upd, ctx)
    plex.remove_share.assert_not_awaited()
    row = await fresh_store.get_share_expiry(1)
    assert row.expires_at == "2031-01-01 00:00:00"


async def test_missing_admin_token_alerts_and_keeps_row(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.unlink(ADMIN_ID)
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(minutes=1)))
    await run_share_expiries(app, NOW)
    assert await fresh_store.get_share_expiry(1) is not None
    plex.remove_share.assert_not_awaited()
    assert len(_sent_to(app, ADMIN_ID)) == 1


async def test_row_extended_after_the_first_read_is_not_removed(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(-timedelta(minutes=1)))

    async def extend_during_listing(*args):
        await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                           _at(timedelta(days=30)))
        return [_share(1)]
    plex.list_shares.side_effect = extend_during_listing
    await run_share_expiries(app, NOW)
    plex.remove_share.assert_not_awaited()
    assert await fresh_store.get_share_expiry(1) is not None


async def test_retry_after_the_date_was_extended_leaves_the_share(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    ctx, upd = _tap(app, f"{SHARE_RETRY}:1")
    await share_expiry_action(upd, ctx)
    plex.remove_share.assert_not_awaited()
    assert await fresh_store.get_share_expiry(1) is not None
    assert "Jan 1, 2031" in upd.callback_query.edits[-1]["text"]


async def test_reminder_states_the_real_days_left(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       _at(timedelta(days=1, hours=2)))
    await run_share_expiries(app, NOW)
    text = _sent_to(app, ADMIN_ID)[0]["text"]
    assert "in about 1 day." in text and "3 days" not in text


async def test_keep_restores_the_reminder_with_the_real_days_left(
        fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    later = (datetime.now(timezone.utc) + timedelta(days=2, hours=1)
             ).strftime("%Y-%m-%d %H:%M:%S")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice", later)
    upd = await _gated_tap(app, f"{SHARE_KEEP}:1")
    text = upd.callback_query.edits[-1]["text"]
    assert "in about 2 days." in text and "3 days" not in text


async def test_confirm_and_keep_taps_reach_the_share_expiry_handler():
    """shnoy and shnok are matched by the registered pattern and no earlier
    handler takes them (the real handler registration)."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from telegram.ext import TypeHandler

    import bot.app as app_mod
    settings = MagicMock()
    settings.settings.telegram_bot_token = "123:abc"
    settings.settings.admin_telegram_id = ADMIN_ID
    app = app_mod._build_app(settings, b"secret", MagicMock(), MagicMock(),
                             Path("."), Path("settings.json"), ":memory:",
                             8080, "127.0.0.1")
    for prefix in (SHARE_REMOVE_YES, SHARE_KEEP, SHARE_REMOVE_RETRY):
        from telegram import CallbackQuery, Update, User
        upd = Update(update_id=1, callback_query=CallbackQuery(
            id="1", from_user=User(ADMIN_ID, "a", False),
            chat_instance="c", data=f"{prefix}:7"))
        # One handler per group runs; the gates (TypeHandlers) match every
        # update and pass it on, so only the other handlers count.
        taken = []
        for group in sorted(app.handlers):
            hit = next((h for h in app.handlers[group]
                        if not isinstance(h, TypeHandler)
                        and h.check_update(upd)), None)
            if hit is not None:
                taken.append(hit.callback)
        assert taken == [share_expiry_action], prefix


async def test_remove_retry_gets_through_the_button_gate(fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_REMOVE_RETRY}:1")
    assert upd.callback_query.markup_edits == []
    plex.remove_share.assert_awaited_once_with("admin-tok", 1)


async def test_failed_remove_now_retry_removes_a_share_that_is_not_due(
        fresh_store):
    """Remove now fails on a Share whose date is 3 days off; the Retry on
    that failure DM must remove it, not answer "left as it is"."""
    app, plex = await _setup(fresh_store, [_share(1)])
    plex.remove_share.side_effect = TransientAPIError("down", service="Plex")
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_REMOVE_YES}:1")
    failed = upd.callback_query.edits[-1]
    assert _buttons(failed["reply_markup"]) == [f"{SHARE_REMOVE_RETRY}:1"]
    plex.remove_share.side_effect = None
    upd = await _gated_tap(app, f"{SHARE_REMOVE_RETRY}:1")
    plex.remove_share.assert_awaited_with("admin-tok", 1)
    assert await fresh_store.get_share_expiry(1) is None
    assert "Removed" in upd.callback_query.edits[-1]["text"]


async def test_job_alert_retry_still_leaves_a_share_that_is_not_due(
        fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_RETRY}:1")
    plex.remove_share.assert_not_awaited()
    assert "left as it is" in upd.callback_query.edits[-1]["text"]


async def test_remove_now_without_a_link_keeps_the_reminder_buttons(
        fresh_store):
    app, plex = await _setup(fresh_store, [_share(1)])
    await fresh_store.unlink(ADMIN_ID)
    await fresh_store.set_share_expiry(1, "a@example.com", "alice",
                                       "2031-01-01 00:00:00")
    upd = await _gated_tap(app, f"{SHARE_REMOVE_NOW}:1")
    edit = upd.callback_query.edits[-1]
    assert "/link first" in edit["text"]
    assert _buttons(edit["reply_markup"]) == [
        f"{SHARE_EXTEND}:1", f"{SHARE_LET_END}:1", f"{SHARE_REMOVE_NOW}:1"]


async def test_tap_on_an_old_reminder_after_the_job_removed_the_share(
        fresh_store):
    app, plex = await _setup(fresh_store, [])
    upd = await _gated_tap(app, f"{SHARE_EXTEND}:1")
    assert "already removed" in upd.callback_query.edits[-1]["text"]

"""Share Expiry: the table, the Invite flow's Expiry row, the Share view's Set
expiry and Clear, the "⏳ <date>" mark on the Shares list, and a backup that
carries the rows."""
from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram.ext import ConversationHandler

import webui
from bot.callback_prefixes import INV_EXPIRY, SHARE_EXPIRY, SHARE_VIEW
from bot.invite_flow import (admin_shares, invite_expiry, invite_libs_done,
                             invite_send, share_expiry, share_view,
                             uninvite_go)
from bot.share_expiry import expires_at_text, expiry_date_text
from bot.shared import INV_CONFIRM
from plex import PlexLibrarySection, PlexShare
from store import UserStore

from tests._handler_harness import make_ctx, make_link, make_update

ADMIN_ID = 999


def _share(id, email, username="", accepted=True):
    return PlexShare(id=id, email=email, username=username,
                     accepted=accepted, all_libraries=True)


def _ctx(store, shares=(), **kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=ADMIN_ID),
                   **kwargs)
    ctx.args = []
    ctx.bot_data["store"].get_share_expiry = store.get_share_expiry
    ctx.bot_data["store"].list_share_expiries = store.list_share_expiries
    ctx.bot_data["store"].set_share_expiry = store.set_share_expiry
    ctx.bot_data["store"].clear_share_expiry = store.clear_share_expiry
    plex = AsyncMock()
    plex.get_owned_server.return_value = ("machine-1", "server1")
    plex.get_library_sections.return_value = [
        PlexLibrarySection(id=101, title="Movies", type="movie")]
    plex.list_shares.return_value = list(shares)
    ctx.bot_data["plex"] = plex
    return ctx


def _draft(store, shares=(), version=1):
    return _ctx(store, shares, user_data={
        "inv_sections": [PlexLibrarySection(id=101, title="Movies",
                                            type="movie")],
        "inv_selected": {101}, "inv_version": version,
        "inv_email": "friend@example.com", "inv_machine": "machine-1",
        "inv_server_name": "server1"})


def _days_until(expires_at: str) -> float:
    when = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds() / 86400


def _buttons(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


# --- the table ---------------------------------------------------------------

async def test_set_get_list_clear_roundtrip(fresh_store: UserStore):
    await fresh_store.set_share_expiry(7, "a@example.com", "a", "2030-01-02 03:04:05")
    await fresh_store.set_share_expiry(5, "b@example.com", "", "2029-01-01 00:00:00")
    row = await fresh_store.get_share_expiry(7)
    assert (row.email, row.username, row.expires_at) == (
        "a@example.com", "a", "2030-01-02 03:04:05")
    assert not row.admin_reminded and not row.holder_reminded
    assert row.last_attempt_at is None and row.last_error is None
    assert [r.share_id for r in await fresh_store.list_share_expiries()] == [5, 7]
    assert await fresh_store.clear_share_expiry(7) is True
    assert await fresh_store.get_share_expiry(7) is None
    assert await fresh_store.clear_share_expiry(7) is False


async def test_moving_an_expiry_rearms_reminders_and_forgets_the_error(
        fresh_store: UserStore):
    await fresh_store.set_share_expiry(7, "a@example.com", "a", "2030-01-02 03:04:05")
    with sqlite3.connect(fresh_store.path) as c:
        c.execute("UPDATE share_expiries SET admin_reminded = 1, "
                  "holder_reminded = 1, last_attempt_at = 'x', "
                  "last_error = 'boom', last_alerted_at = 'y'")
    await fresh_store.set_share_expiry(7, "a@example.com", "a", "2031-01-01 00:00:00")
    row = await fresh_store.get_share_expiry(7)
    assert row.expires_at == "2031-01-01 00:00:00"
    assert not row.admin_reminded and not row.holder_reminded
    assert (row.last_attempt_at, row.last_error, row.last_alerted_at) == (
        None, None, None)


def test_expiry_text_helpers():
    now = datetime(2026, 11, 14, 9, 30, tzinfo=timezone.utc)
    assert expires_at_text(30, now) == "2026-12-14 09:30:00"
    assert expiry_date_text("2026-12-14 09:30:00") == "Dec 14, 2026"


# --- the Invite flow ---------------------------------------------------------

async def test_confirm_card_has_expiry_row_with_never_selected(
        fresh_store: UserStore):
    ctx = _draft(fresh_store)
    upd = make_update(callback_data="invd:1", user_id=ADMIN_ID)
    assert await invite_libs_done(upd, ctx) == INV_CONFIRM
    markup = upd.callback_query.edits[0]["reply_markup"]
    assert _buttons(markup)[:4] == [
        ("☑ Never", f"{INV_EXPIRY}:1:0"), ("7 days", f"{INV_EXPIRY}:1:7"),
        ("30 days", f"{INV_EXPIRY}:1:30"), ("90 days", f"{INV_EXPIRY}:1:90")]


async def test_invite_with_30_days_stores_the_expiry_on_the_new_share(
        fresh_store: UserStore):
    ctx = _draft(fresh_store, shares=[
        _share(3, "other@example.com"),
        _share(42, "Friend@Example.com", username="friend", accepted=False)])
    pick = make_update(callback_data=f"{INV_EXPIRY}:1:30", user_id=ADMIN_ID)
    assert await invite_expiry(pick, ctx) == INV_CONFIRM
    edit = pick.callback_query.edits[-1]
    assert "Ends on" in edit["text"]
    assert ("☑ 30 days", f"{INV_EXPIRY}:1:30") in _buttons(edit["reply_markup"])

    go = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    assert await invite_send(go, ctx) == ConversationHandler.END
    row = await fresh_store.get_share_expiry(42)
    assert row is not None and row.email == "Friend@Example.com"
    assert row.username == "friend"
    assert 29.9 < _days_until(row.expires_at) < 30.1
    assert await fresh_store.get_share_expiry(3) is None
    assert "Ends on" in go.callback_query.edits[-1]["text"]


async def test_invite_with_never_stores_nothing_and_skips_the_listing(
        fresh_store: UserStore):
    ctx = _draft(fresh_store, shares=[_share(42, "friend@example.com")])
    go = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    await invite_send(go, ctx)
    assert await fresh_store.list_share_expiries() == []
    ctx.bot_data["plex"].list_shares.assert_not_awaited()


async def test_choosing_never_again_drops_a_picked_expiry(
        fresh_store: UserStore):
    ctx = _draft(fresh_store, shares=[_share(42, "friend@example.com")])
    await invite_expiry(make_update(callback_data=f"{INV_EXPIRY}:1:90",
                                    user_id=ADMIN_ID), ctx)
    await invite_expiry(make_update(callback_data=f"{INV_EXPIRY}:1:0",
                                    user_id=ADMIN_ID), ctx)
    await invite_send(make_update(callback_data="invgo:1", user_id=ADMIN_ID),
                      ctx)
    assert await fresh_store.list_share_expiries() == []


async def test_stale_expiry_pick_toasts_and_changes_nothing(
        fresh_store: UserStore):
    ctx = _draft(fresh_store, version=2)
    pick = make_update(callback_data=f"{INV_EXPIRY}:1:30", user_id=ADMIN_ID)
    await invite_expiry(pick, ctx)
    assert "inv_expiry_days" not in ctx.user_data
    assert pick.callback_query.answers[-1][1] is True


async def test_invite_sent_but_share_not_found_says_to_set_it_by_hand(
        fresh_store: UserStore):
    ctx = _draft(fresh_store, shares=[])
    await invite_expiry(make_update(callback_data=f"{INV_EXPIRY}:1:7",
                                    user_id=ADMIN_ID), ctx)
    go = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    await invite_send(go, ctx)
    text = go.callback_query.edits[-1]["text"]
    assert "Invite sent" in text and "couldn't set the expiry" in text
    assert await fresh_store.list_share_expiries() == []


# --- the Share view ----------------------------------------------------------

async def test_set_expiry_then_clear_on_the_share_view(fresh_store: UserStore):
    ctx = _ctx(fresh_store, [_share(71, "friend@example.com", "friend")])
    picker = make_update(callback_data=f"{SHARE_EXPIRY}:71", user_id=ADMIN_ID)
    await share_expiry(picker, ctx)
    buttons = _buttons(picker.callback_query.edits[0]["reply_markup"])
    assert (f"7 days", f"{SHARE_EXPIRY}:71:7") in buttons
    assert not any(cb.endswith(":0") for _, cb in buttons)  # nothing to clear

    pick = make_update(callback_data=f"{SHARE_EXPIRY}:71:90", user_id=ADMIN_ID)
    await share_expiry(pick, ctx)
    row = await fresh_store.get_share_expiry(71)
    assert (row.email, row.username) == ("friend@example.com", "friend")
    assert 89.9 < _days_until(row.expires_at) < 90.1
    assert "Ends on" in pick.callback_query.edits[-1]["text"]

    again = make_update(callback_data=f"{SHARE_EXPIRY}:71", user_id=ADMIN_ID)
    await share_expiry(again, ctx)
    assert (("Clear expiry", f"{SHARE_EXPIRY}:71:0")
            in _buttons(again.callback_query.edits[0]["reply_markup"]))

    clear = make_update(callback_data=f"{SHARE_EXPIRY}:71:0", user_id=ADMIN_ID)
    await share_expiry(clear, ctx)
    assert await fresh_store.get_share_expiry(71) is None
    assert "Ends on" not in clear.callback_query.edits[-1]["text"]


async def test_share_view_shows_the_date(fresh_store: UserStore):
    ctx = _ctx(fresh_store, [_share(71, "friend@example.com")])
    await fresh_store.set_share_expiry(71, "friend@example.com", "",
                                       "2030-03-04 05:06:07")
    view = make_update(callback_data=f"{SHARE_VIEW}:71", user_id=ADMIN_ID)
    await share_view(view, ctx)
    assert "⏳ Ends on Mar 4, 2030." in view.callback_query.edits[0]["text"]


async def test_set_expiry_is_admin_only(fresh_store: UserStore):
    ctx = _ctx(fresh_store, [_share(71, "friend@example.com")])
    upd = make_update(callback_data=f"{SHARE_EXPIRY}:71:30", user_id=12345)
    await share_expiry(upd, ctx)
    assert await fresh_store.get_share_expiry(71) is None


async def test_removing_a_share_forgets_its_expiry(fresh_store: UserStore):
    ctx = _ctx(fresh_store, [_share(71, "friend@example.com")])
    await fresh_store.set_share_expiry(71, "friend@example.com", "",
                                       "2030-03-04 05:06:07")
    go = make_update(callback_data="uninv:71:a", user_id=ADMIN_ID)
    await uninvite_go(go, ctx)
    assert await fresh_store.get_share_expiry(71) is None


# --- the Shares list ---------------------------------------------------------

async def test_shares_list_marks_shares_with_an_expiry(fresh_store: UserStore):
    ctx = _ctx(fresh_store, [_share(71, "a@example.com"),
                             _share(72, "b@example.com", accepted=False)])
    await fresh_store.set_share_expiry(72, "b@example.com", "",
                                       "2030-03-04 05:06:07")
    upd = make_update(callback_data="adms:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    labels = {cb: text for text, cb in
              _buttons(upd.callback_query.edits[0]["reply_markup"])}
    assert labels[f"{SHARE_VIEW}:71"] == "a@example.com"
    assert labels[f"{SHARE_VIEW}:72"] == "⏳ b@example.com (pending) ⏳ Mar 4, 2030"


# --- backup ------------------------------------------------------------------

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])


async def test_backup_carries_the_expiry_rows(tmp_path, fresh_token_crypto):
    db = tmp_path / "mappings.sqlite"
    store = UserStore(db, crypto=fresh_token_crypto)
    await store.set_share_expiry(71, "a@example.com", "a", "2030-03-04 05:06:07")

    settings_path = tmp_path / "settings.json"
    sstore = webui.SettingsStore(settings_path)
    sstore.settings.admin.username = "admin"
    sstore.settings.admin.password_hash = "x"
    sstore.save()
    app = web.Application()
    webui.attach_webui(app, settings_store=sstore, session_secret=SECRET,
                       data_dir=tmp_path, settings_path=settings_path,
                       db_path=db)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/backup",
            data={"csrf_token": CSRF, "passphrase": "", "unencrypted_ok": "1"},
            cookies={webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF})
        assert resp.status == 200
        blob = await resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        restored = tmp_path / "restored" / "mappings.sqlite"
        restored.parent.mkdir()
        restored.write_bytes(zf.read("mappings.sqlite"))
    back = UserStore(restored, crypto=fresh_token_crypto)
    row = await back.get_share_expiry(71)
    assert (row.email, row.username, row.expires_at) == (
        "a@example.com", "a", "2030-03-04 05:06:07")

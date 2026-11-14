"""The Admin Hub's Shares list: the Inactive button and its list of Shares
with no Tautulli activity for `inactive_share_days`."""
from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from bot.callback_prefixes import (ADMIN_INACTIVE, ADMIN_SHARES, SHARE_VIEW,
                                   UNINV_GO)
from bot.invite_flow import (admin_inactive, admin_shares, share_view,
                             uninvite_go)
from http_util import TransientAPIError
from tautulli import TautulliClient

from tests._handler_harness import make_update
from tests.test_invite_flow import (ADMIN_ID, BACK, _flat, _share,
                                    _shares_ctx)

DAY = 86400


def _days_ago(n):
    return int(time.time() - n * DAY)


def _ctx(shares, rows, days=60):
    ctx = _shares_ctx(shares=shares)
    ctx.bot_data["settings_store"].settings.inactive_share_days = days
    tau = AsyncMock()
    tau.get_users_table.return_value = rows
    ctx.bot_data["tautulli"] = tau
    return ctx


SHARES = [_share(1, "old@example.com"), _share(2, "never@example.com"),
          _share(3, "recent@example.com"),
          _share(4, "", username="ByName")]
ROWS = [
    {"email": "old@example.com", "username": "old", "last_seen": _days_ago(90)},
    {"email": "never@example.com", "username": "never", "last_seen": None},
    {"email": "recent@example.com", "username": "recent",
     "last_seen": _days_ago(10)},
    {"email": "", "username": "byname", "last_seen": _days_ago(100)},
]


async def _inactive(ctx, page=0):
    upd = make_update(callback_data=f"{ADMIN_INACTIVE}:{page}",
                      user_id=ADMIN_ID)
    await admin_inactive(upd, ctx)
    return upd.callback_query.edits[0]


async def test_lists_old_and_never_but_not_recent():
    edit = await _inactive(_ctx(SHARES, ROWS))
    flat = _flat(edit["reply_markup"])
    views = [(t, d) for t, d in flat if d.startswith(f"{SHARE_VIEW}:")]
    assert [d for _, d in views] == [f"{SHARE_VIEW}:2", f"{SHARE_VIEW}:4",
                                     f"{SHARE_VIEW}:1"]
    text = dict((d, t) for t, d in views)
    assert text[f"{SHARE_VIEW}:1"] == "old@example.com: last watched 90 days ago"
    assert text[f"{SHARE_VIEW}:2"] == "never@example.com: never watched"
    assert text[f"{SHARE_VIEW}:4"] == "ByName: last watched 100 days ago"
    assert f"{SHARE_VIEW}:3" not in text
    assert flat[-2:] == [("⬅️ Shares", f"{ADMIN_SHARES}:0"), BACK]
    assert "(3)" in edit["text"] and "60 days" in edit["text"]


async def test_share_without_a_tautulli_row_counts_as_never():
    edit = await _inactive(_ctx([_share(7, "ghost@example.com")], []))
    flat = _flat(edit["reply_markup"])
    assert ("ghost@example.com: never watched",
            f"{SHARE_VIEW}:7") in flat


async def test_days_setting_is_used():
    ctx = _ctx(SHARES, ROWS, days=5)
    ids = {d for _, d in _flat((await _inactive(ctx))["reply_markup"])
           if d.startswith(f"{SHARE_VIEW}:")}
    assert f"{SHARE_VIEW}:3" in ids  # 10 days ago is now inactive
    ctx = _ctx(SHARES, ROWS, days=95)
    ids = {d for _, d in _flat((await _inactive(ctx))["reply_markup"])
           if d.startswith(f"{SHARE_VIEW}:")}
    assert ids == {f"{SHARE_VIEW}:2", f"{SHARE_VIEW}:4"}  # 90 days is not


async def test_empty_last_seen_and_the_day_boundary():
    shares = [_share(1, "blank@example.com"), _share(2, "zero@example.com"),
              _share(3, "edge@example.com"), _share(4, "inside@example.com")]
    rows = [{"email": "blank@example.com", "last_seen": ""},
            {"email": "zero@example.com", "last_seen": 0},
            {"email": "edge@example.com", "last_seen": _days_ago(60) - 5},
            {"email": "inside@example.com", "last_seen": _days_ago(59)}]
    flat = _flat((await _inactive(_ctx(shares, rows)))["reply_markup"])
    ids = {d for _, d in flat if d.startswith(f"{SHARE_VIEW}:")}
    assert ids == {f"{SHARE_VIEW}:1", f"{SHARE_VIEW}:2", f"{SHARE_VIEW}:3"}


async def test_nobody_inactive_says_so():
    ctx = _ctx([SHARES[2]], ROWS)
    edit = await _inactive(ctx)
    assert "Every Share has watched" in edit["text"]
    assert _flat(edit["reply_markup"]) == [("⬅️ Shares", f"{ADMIN_SHARES}:0")]


async def test_pages_of_eight():
    shares = [_share(i, f"p{i:02d}@example.com") for i in range(1, 10)]
    ctx = _ctx(shares, [])
    edit = await _inactive(ctx)
    flat = _flat(edit["reply_markup"])
    assert ("Next ▶️", f"{ADMIN_INACTIVE}:1") in flat
    assert "Page 1 of 2." in edit["text"]
    edit2 = await _inactive(ctx, page=1)
    flat2 = _flat(edit2["reply_markup"])
    assert ("◀️ Prev", f"{ADMIN_INACTIVE}:0") in flat2
    assert len([1 for _, d in flat2 if d.startswith(f"{SHARE_VIEW}:")]) == 1


async def test_row_opens_share_view_and_remove_still_works():
    ctx = _ctx(SHARES, ROWS)
    view = make_update(callback_data=f"{SHARE_VIEW}:1", user_id=ADMIN_ID)
    await share_view(view, ctx)
    assert "old@example.com" in view.callback_query.edits[0]["text"]
    go = make_update(callback_data=f"{UNINV_GO}:1:a", user_id=ADMIN_ID)
    await uninvite_go(go, ctx)
    assert ctx.bot_data["plex"].remove_share.call_args.args[1] == 1


async def test_button_shown_only_with_tautulli():
    ctx = _ctx(SHARES, ROWS)
    upd = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert flat[0] == ("💤 Inactive", f"{ADMIN_INACTIVE}:0")

    ctx.bot_data["tautulli"] = None
    upd = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert not any(d.startswith(ADMIN_INACTIVE) for _, d in flat)

    ctx.bot_data.pop("tautulli")
    upd = make_update(callback_data=f"{ADMIN_INACTIVE}:0", user_id=ADMIN_ID)
    await admin_inactive(upd, ctx)
    assert upd.callback_query.edits[0]["text"] == "Tautulli isn't linked."


async def test_tautulli_failure_offers_try_again():
    ctx = _ctx(SHARES, ROWS)
    ctx.bot_data["tautulli"].get_users_table.side_effect = TransientAPIError(
        "down", status_code=503, service="Tautulli")
    edit = await _inactive(ctx)
    assert edit["text"].startswith("Couldn't ask Tautulli.")
    assert ("🔁 Try again", f"{ADMIN_INACTIVE}:0") in _flat(
        edit["reply_markup"])


async def test_non_admin_refused():
    ctx = _ctx(SHARES, ROWS)
    upd = make_update(callback_data=f"{ADMIN_INACTIVE}:0", user_id=42)
    await admin_inactive(upd, ctx)
    ctx.bot_data["tautulli"].get_users_table.assert_not_awaited()


async def test_client_get_users_table_unwraps_and_lifts_page_size():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"response": {
            "result": "success", "message": None,
            "data": {"data": [{"user_id": 1, "last_seen": 5}, "junk"]}}})

    c = TautulliClient("http://tau.test:8181", "k")
    c._client = httpx.AsyncClient(base_url="http://tau.test:8181",
                                  transport=httpx.MockTransport(handler))
    assert await c.get_users_table() == [{"user_id": 1, "last_seen": 5}]
    assert seen["cmd"] == "get_users_table" and seen["length"] == "1000"
    await c.close()


def test_inactive_handler_registered_before_the_catch_all():
    src = Path("bot/app.py").read_text(encoding="utf-8")
    found = re.search(
        r"CallbackQueryHandler\(admin_inactive,\s*"
        r"pattern=fr\"\^\{ADMIN_INACTIVE\}:\\d\+\$\"\)", src)
    assert found and found.start() < src.index(
        "CallbackQueryHandler(unmatched_callback)")

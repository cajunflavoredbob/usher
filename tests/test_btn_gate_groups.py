"""In a group chat the button gate refuses a tap by anyone other than the
person the bot message was sent to, for every standalone callback family.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from bot import callback_prefixes as cp
from bot.shared import global_btn_gate, record_btn

OWNER = 42
OTHER = 77

# One entry per handler family named in the ticket.
FAMILIES = {
    "rq_list_cancel": f"{cp.RQ_LIST_CANCEL}:5",
    "requests_hub_mine": cp.REQUESTS_MINE,
    "trending_hub": cp.REQUESTS_TRENDING,
    "coming_week_hub": cp.REQUESTS_WEEK,
    "dv_category": f"{cp.DV_CATEGORY}:popular",
    "dv_page": f"{cp.DV_PAGE}:1:2",
    "dv_info": f"{cp.DV_INFO}:1:movie:550",
    "subscription_del": f"{cp.SUBSCRIPTION_DEL}:3",
    "subscriptions_hub": cp.REQUESTS_SUBSCRIPTIONS,
    "subscription_add": f"{cp.SUBSCRIPTION_ADD}:movie:550",
    "follow_add": f"{cp.FOLLOW_ADD}:550",
    "follow_del": f"{cp.FOLLOW_DEL}:550",
    "remaining_ask": f"{cp.RQ_REMAINING}:550",
    "remaining_send": f"{cp.RQ_REMAINING_GO}:550",
    "follow_remove": f"{cp.FOLLOW_REMOVE}:4",
    "follows_hub": cp.REQUESTS_FOLLOWS,
    "tk_open": f"{cp.TK_OPEN}:9",
    "tk_close_menu": f"{cp.TK_CLOSE}:9",
    "tk_close_direct": f"{cp.TK_CLOSE_DIRECT}:9",
    "tk_fix": f"{cp.TK_FIX}:9",
    "tk_fix_redownload": f"{cp.TK_FIX_REDOWNLOAD}:9",
    "tk_fix_mark_failed": f"{cp.TK_FIX_MARK_FAILED}:9",
    "tk_back": f"{cp.TK_BACK}:9",
    "tickets_hub_list": cp.TICKETS_LIST,
    "account_unlink": cp.ACCOUNT_UNLINK,
    "account_notices": cp.ACCOUNT_NOTICES,
    "notice_toggle": f"{cp.NOTICE_TOGGLE}:digest",
    "digest_info": f"{cp.DIGEST_INFO}:movie:550",
    "hub_open_callback": f"{cp.HUB_OPEN}:requests",
    "mystats_button": cp.MYSTATS,
    "cmd_link_didnt_work": cp.LINK_HELP,
    "request_info_dismiss": cp.RQ_INFO_DISMISS,
}


def _update(tapper: int, data: str, *, chat_type="supergroup", replied_to=OWNER,
            replied_bot=False, message_id=900, chat_id=-100):
    reply = None
    if replied_to is not None:
        reply = SimpleNamespace(
            from_user=SimpleNamespace(id=replied_to, is_bot=replied_bot))
    msg = SimpleNamespace(
        message_id=message_id, chat_id=chat_id,
        chat=SimpleNamespace(type=chat_type), reply_to_message=reply)
    q = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=tapper),
                        data=data, answers=[], edits=[])

    async def _answer(text="", show_alert=False):
        q.answers.append(text)

    async def _edit(reply_markup=None):
        q.edits.append(reply_markup)

    q.answer = _answer
    q.edit_message_reply_markup = _edit
    return SimpleNamespace(callback_query=q)


class _Ctx:
    def __init__(self, app):
        self.application = app


def _app():
    return SimpleNamespace(bot_data={})


@pytest.mark.parametrize("data", list(FAMILIES.values()), ids=list(FAMILIES))
async def test_other_member_tap_is_refused(data):
    upd = _update(OTHER, data)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(_app()))
    q = upd.callback_query
    assert q.answers == ["This menu isn't yours."]
    assert q.edits == []


@pytest.mark.parametrize("data", list(FAMILIES.values()), ids=list(FAMILIES))
async def test_recipient_tap_is_admitted(data):
    upd = _update(OWNER, data)
    await global_btn_gate(upd, _Ctx(_app()))  # no raise
    assert upd.callback_query.answers == []


async def test_recipient_with_recorded_menu_admitted_when_command_gone():
    app = _app()
    record_btn(app, OWNER, SimpleNamespace(message_id=900, chat_id=-100))
    upd = _update(OWNER, cp.REQUESTS_MINE, replied_to=None)
    await global_btn_gate(upd, _Ctx(app))


async def test_unattributed_message_refused_for_user_without_history():
    upd = _update(OTHER, cp.RQ_INFO_DISMISS, replied_to=None)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(upd, _Ctx(_app()))
    assert upd.callback_query.answers == ["This menu isn't yours."]


async def test_reply_to_bot_falls_back_to_tapper_history():
    app = _app()
    record_btn(app, OWNER, SimpleNamespace(message_id=900, chat_id=-100))
    ok = _update(OWNER, cp.REQUESTS_MINE, replied_bot=True)
    await global_btn_gate(ok, _Ctx(app))
    bad = _update(OTHER, cp.REQUESTS_MINE, replied_bot=True)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(bad, _Ctx(app))


@pytest.mark.parametrize("data", [cp.RQ_INFO_DISMISS, cp.REQUESTS_MINE])
async def test_private_chat_keeps_todays_behaviour(data):
    upd = _update(OTHER, data, chat_type="private", replied_to=OWNER)
    await global_btn_gate(upd, _Ctx(_app()))  # no raise, no history needed
    assert upd.callback_query.answers == []


@pytest.mark.parametrize("prefix", [cp.RQ_ADMIN_APPROVE, cp.ACCESS_APPROVE])
async def test_admin_dm_buttons_still_pass(prefix):
    upd = _update(OTHER, f"{prefix}:5", chat_type="private", replied_to=None)
    await global_btn_gate(upd, _Ctx(_app()))


@pytest.mark.parametrize("data", [cp.RQ_INFO_DISMISS, f"{cp.SUBSCRIPTION_ADD}:movie:550",
                                  f"{cp.FOLLOW_ADD}:550", f"{cp.FOLLOW_DEL}:550",
                                  f"{cp.RQ_REMAINING}:550", f"{cp.DV_SIMILAR}:tv:550",
                                  f"{cp.PLEX_WATCHLIST_ADD}:movie:550"])
async def test_detail_card_owner_admitted_and_other_refused(data):
    from bot.shared import record_card_owner
    app = _app()
    record_card_owner(app, OWNER, SimpleNamespace(message_id=900, chat_id=-100))
    ok = _update(OWNER, data, replied_to=None)
    await global_btn_gate(ok, _Ctx(app))
    bad = _update(OTHER, data, replied_to=None)
    with pytest.raises(ApplicationHandlerStop):
        await global_btn_gate(bad, _Ctx(app))


async def test_card_owner_ignores_private_chats():
    from bot.shared import record_card_owner
    app = _app()
    record_card_owner(app, OWNER, SimpleNamespace(message_id=1, chat_id=5))
    assert "card_owners" not in app.bot_data


async def test_anonymous_admin_reply_falls_back_to_history():
    app = _app()
    record_btn(app, OWNER, SimpleNamespace(message_id=900, chat_id=-100))
    upd = _update(OWNER, cp.REQUESTS_MINE, replied_bot=True)
    await global_btn_gate(upd, _Ctx(app))

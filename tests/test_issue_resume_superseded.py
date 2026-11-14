"""A relink resume that a newer /issue or Report a problem superseded never
submits and never says the link is incomplete. Driven through a real PTB
Application: the real link and /issue conversations, reset_stale_flows and
global_btn_gate wired as in bot/app.py, with only the Telegram network calls
and the Plex/Seerr clients faked."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import (Application, ApplicationHandlerStop,
                          CallbackContext, CallbackQueryHandler, ExtBot,
                          TypeHandler)

import bot.link_flow as link_flow
from bot.callback_prefixes import LINK_PLATFORM, RELINK, TICKETS_LIST, TICKETS_REPORT
from bot.link_flow import _link_conversation
from bot.shared import (global_btn_gate, guard_flow_steps, prompt_plex_relink,
                        reset_stale_flows)
from bot.ticket_flow import _new_ticket_conversation
from bot.tickets import tickets_hub_list
from seerr import CreatedIssue
from tests._handler_harness import make_ctx, make_link

USER_ID = 42
CHAT_ID = 42
SUPERSEDED_LINE = "didn't send the earlier report"
INCOMPLETE = "link is incomplete"
PICKING_UP = "Picking up where you left off"
SUBMITTED = "Reported as ticket"


class _Harness:
    def __init__(self, app, monkeypatch):
        self.app = app
        self.sent: list[str] = []
        self._ids = 1000
        self.hub_msg = None

        harness = self

        def _msg(message_id=None):
            if message_id is None:
                harness._ids += 1
                message_id = harness._ids
            m = Message(message_id=message_id, date=_now(),
                        chat=Chat(id=CHAT_ID, type="private"))
            m.set_bot(app.bot)
            return m

        async def send_message(self_, chat_id=None, text=None, *a, **kw):
            harness.sent.append(text)
            return _msg()

        async def edit_message_text(self_, text=None, *a, message_id=None, **kw):
            harness.sent.append(text)
            return _msg(message_id)

        async def noop(self_, *a, **kw):
            return True

        monkeypatch.setattr(ExtBot, "send_message", send_message)
        monkeypatch.setattr(ExtBot, "edit_message_text", edit_message_text)
        monkeypatch.setattr(ExtBot, "answer_callback_query", noop)
        monkeypatch.setattr(ExtBot, "edit_message_reply_markup", noop)
        monkeypatch.setattr(ExtBot, "send_chat_action", noop)

    @property
    def user_data(self) -> dict:
        return self.app.user_data[USER_ID]

    def _update(self, data: dict) -> Update:
        return Update.de_json(data, self.app.bot)

    def _message_dict(self, message_id: int, text: str = "") -> dict:
        d = {"message_id": message_id, "date": int(time.time()),
             "chat": {"id": CHAT_ID, "type": "private"},
             "from": {"id": USER_ID, "is_bot": False, "first_name": "U"}}
        if text:
            d["text"] = text
            if text.startswith("/"):
                d["entities"] = [{"type": "bot_command", "offset": 0,
                                  "length": len(text.split()[0])}]
        return d

    async def say(self, text: str) -> None:
        self._ids += 1
        await self.app.process_update(self._update({
            "update_id": self._ids,
            "message": self._message_dict(self._ids, text)}))

    async def tap(self, data: str, message_id: int) -> None:
        self._ids += 1
        msg = self._message_dict(message_id)
        msg["from"] = {"id": 1, "is_bot": True, "first_name": "Bot"}
        await self.app.process_update(self._update({
            "update_id": self._ids,
            "callback_query": {
                "id": str(self._ids), "chat_instance": "ci", "data": data,
                "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
                "message": msg}}))

    async def interrupted_submit(self, **draft) -> int:
        """The state a submit leaves when Plex rejects the token: the draft in
        user_data and the relink prompt on screen. Returns the prompt's id."""
        self._ids += 1
        upd = self._update({"update_id": self._ids,
                            "message": self._message_dict(self._ids, "ok")})
        ctx = CallbackContext.from_update(upd, self.app)
        self.user_data.update(draft)
        before = len(self.sent)
        await prompt_plex_relink(upd, ctx, resume_kind="submit_ticket",
                                 resume_payload={"autofix": False}, reply=True)
        assert len(self.sent) == before + 1
        return self._ids  # the id the prompt was just sent under

    async def relink(self, prompt_id: int) -> list[str]:
        """Tap the prompt's button and finish the sign-in. Returns what the
        bot said from the tap on."""
        start = len(self.sent)
        await self.tap(RELINK, prompt_id)
        await self.tap(f"{LINK_PLATFORM}:desktop", prompt_id)
        return self.sent[start:]


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@pytest.fixture
async def h(monkeypatch):
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    app = Application.builder().token("1:x").build()
    app.bot._bot_user = User(id=1, first_name="Usher", is_bot=True,
                             username="UsherBot")
    app._initialized = True
    harness = _Harness(app, monkeypatch)

    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=USER_ID))
    app.bot_data.update(ctx.bot_data)
    app.bot_data["store"].link_with_plex = AsyncMock()
    seerr = app.bot_data["seerr"]
    seerr.login_with_plex = AsyncMock(return_value=(7, "User1"))
    seerr.create_issue = AsyncMock(return_value=CreatedIssue(id=5, url="u"))
    plex = app.bot_data["plex"]
    plex.request_pin = AsyncMock(return_value=SimpleNamespace(
        id=1, code="ABCD", auth_url="https://plex.example/auth"))
    plex.poll_pin = AsyncMock(return_value="new-token")
    plex.get_user = AsyncMock(return_value=SimpleNamespace(
        uuid="u", username="user1plex"))

    link_conv = _link_conversation()
    issue_conv = _new_ticket_conversation()
    app.bot_data["conversations"] = {"link": link_conv, "issue": issue_conv}
    app.bot_data["flow_convs"] = [link_conv, issue_conv]
    for conv in app.bot_data["flow_convs"]:
        guard_flow_steps(conv)
    app.add_handler(TypeHandler(Update, reset_stale_flows), group=-2)
    app.add_handler(TypeHandler(Update, global_btn_gate), group=-1)
    app.add_handler(link_conv)
    app.add_handler(CallbackQueryHandler(tickets_hub_list,
                                         pattern=f"^{TICKETS_LIST}$"))
    app.add_handler(issue_conv)
    yield harness


_DRAFT = dict(media={"title": "Movie", "type": "movie", "seerr_media_id": 3},
              issue_type=1, description="no audio")


def _assert_only_the_superseded_line(texts: list[str]) -> None:
    assert sum(SUPERSEDED_LINE in t for t in texts) == 1, texts
    assert not any(PICKING_UP in t or INCOMPLETE in t for t in texts), texts
    assert not any(SUBMITTED in t for t in texts), texts
    assert any("Linked as" in t for t in texts), texts


async def test_a_new_issue_ended_by_a_command_then_relink(h):
    prompt = await h.interrupted_submit(**_DRAFT)
    await h.say("/issue")
    await h.say("/tickets")  # ends the new report
    texts = await h.relink(prompt)
    _assert_only_the_superseded_line(texts)
    h.app.bot_data["seerr"].create_issue.assert_not_called()
    assert "relink_resume" not in h.user_data


async def test_b_new_issue_still_open_then_relink(h):
    prompt = await h.interrupted_submit(**_DRAFT)
    await h.say("/issue")
    texts = await h.relink(prompt)
    _assert_only_the_superseded_line(texts)
    h.app.bot_data["seerr"].create_issue.assert_not_called()


async def test_d_new_issue_ended_by_a_hub_tap_then_relink(h):
    prompt = await h.interrupted_submit(**_DRAFT)
    await h.say("/issue")
    # A Hub's list button, as the Tickets Hub offers it, on a recorded message.
    from bot.shared import record_btn
    hub = SimpleNamespace(message_id=777, chat_id=CHAT_ID)
    record_btn(h.app, USER_ID, hub)
    await h.tap(TICKETS_LIST, 777)
    texts = await h.relink(prompt)
    _assert_only_the_superseded_line(texts)
    h.app.bot_data["seerr"].create_issue.assert_not_called()


async def test_e_report_a_problem_ended_by_a_command_then_relink(h):
    prompt = await h.interrupted_submit(**_DRAFT)
    from bot.shared import record_btn
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=778, chat_id=CHAT_ID))
    await h.tap(TICKETS_REPORT, 778)
    await h.say("/tickets")  # ends the Hub report
    texts = await h.relink(prompt)
    _assert_only_the_superseded_line(texts)
    h.app.bot_data["seerr"].create_issue.assert_not_called()


async def test_f_no_second_issue_resumes_the_submit(h):
    prompt = await h.interrupted_submit(**_DRAFT)
    texts = await h.relink(prompt)
    assert any(PICKING_UP in t for t in texts), texts
    assert any(SUBMITTED in t for t in texts), texts
    assert not any(SUPERSEDED_LINE in t or INCOMPLETE in t for t in texts)
    h.app.bot_data["seerr"].create_issue.assert_awaited_once()

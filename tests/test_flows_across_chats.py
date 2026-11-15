"""One person's flows in two chats. user_data is per user while each
conversation is keyed per (chat, user), so a flow left open in one chat used
to act on what a flow in another chat picked. Driven through the real
Application from bot.app._build_app (every conversation, reset_stale_flows
and global_btn_gate as in production); only the Telegram network calls and
the Seerr, Radarr and store clients are faked. The DM's chat id is the user's
id; the group is a supergroup with a negative id, and the bot's group
messages quote the user, as real replies do."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot

import bot.app as app_mod
from bot.callback_prefixes import (ISSUE_TYPE, TICKET_AUTOFIX_CONFIRM,
                                   TICKET_AUTOFIX_OFFER, TICKET_MEDIA, TK_REPLY)
from seerr import CreatedIssue, MediaResult
from tests._handler_harness import make_ctx, make_link

USER_ID = 42
DM_ID = USER_ID
GROUP_ID = -1001234


def _chat(chat_id: int) -> dict:
    if chat_id < 0:
        return {"id": chat_id, "type": "supergroup", "title": "g"}
    return {"id": chat_id, "type": "private"}


class _Rig:
    """The production Application with the network calls faked. Every bot
    message lands in the chat it was sent to, so the button gate sees the
    real (chat, message) pairs."""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(app_mod, "_build_clients_from_settings",
                            lambda app: None)
        settings = MagicMock()
        settings.settings.telegram_bot_token = "123:abc"
        settings.settings.admin_telegram_id = USER_ID
        self.app = app_mod._build_app(
            settings, b"secret", MagicMock(), MagicMock(), Path("."),
            Path("settings.json"), ":memory:", 8080, "127.0.0.1")
        self.app.bot._bot_user = User(id=1, first_name="Usher", is_bot=True,
                                      username="UsherBot")
        self.app._initialized = True
        ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID))
        self.app.bot_data.update(ctx.bot_data)
        self.app.bot_data["autofix_allow_all"] = True
        self.seerr = self.app.bot_data["seerr"]
        self.radarr = self.app.bot_data["radarr"]
        self.seerr.create_issue = AsyncMock(
            return_value=CreatedIssue(id=5, url="https://seerr.example/5"))
        titles = {"movie x": MediaResult("movie", 100, "Movie X", "2001", 10),
                  "alien": MediaResult("movie", 348, "Alien", "1979", 20)}

        async def search(query, limit=None, **kw):
            return [titles[query.lower()]]

        self.seerr.search = AsyncMock(side_effect=search)
        store = self.app.bot_data["store"]
        store.recent_fix_event = AsyncMock(return_value=None)
        # (chat_id, text) of every message the bot sent or edited.
        self.sent: list[tuple[int, str]] = []
        self._ids = 1000
        rig = self

        def _msg(chat_id, message_id=None):
            if message_id is None:
                rig._ids += 1
                message_id = rig._ids
            m = Message(message_id=message_id, date=datetime.now(timezone.utc),
                        chat=Chat(**_chat(chat_id)))
            m.set_bot(rig.app.bot)
            return m

        async def send_message(self_, chat_id=None, text=None, *a, **kw):
            rig.sent.append((chat_id, text))
            return _msg(chat_id)

        async def edit_message_text(self_, text=None, chat_id=None,
                                    message_id=None, *a, **kw):
            rig.sent.append((chat_id, text))
            return _msg(chat_id, message_id)

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

    def texts(self, chat_id: int) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]

    def last_menu(self, chat_id: int) -> int:
        """The newest recorded button message in that chat."""
        entries = self.app.bot_data["btn_msgs"][USER_ID]
        return [e for e in entries if e["chat_id"] == chat_id][-1]["message_id"]

    def _user_message(self, chat_id: int, text: str) -> dict:
        self._ids += 1
        d = {"message_id": self._ids, "date": int(time.time()),
             "chat": _chat(chat_id),
             "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
             "text": text}
        if text.startswith("/"):
            d["entities"] = [{"type": "bot_command", "offset": 0,
                              "length": len(text.split()[0])}]
        return d

    async def say(self, chat_id: int, text: str) -> None:
        self._ids += 1
        await self.app.process_update(Update.de_json(
            {"update_id": self._ids,
             "message": self._user_message(chat_id, text)}, self.app.bot))

    async def tap(self, chat_id: int, data: str, message_id: int) -> None:
        self._ids += 1
        msg = {"message_id": message_id, "date": int(time.time()),
               "chat": _chat(chat_id),
               "from": {"id": 1, "is_bot": True, "first_name": "Bot"}}
        if chat_id < 0:
            # A bot reply in a group quotes the user it answers.
            msg["reply_to_message"] = self._user_message(chat_id, "x")
        await self.app.process_update(Update.de_json({
            "update_id": self._ids,
            "callback_query": {
                "id": str(self._ids), "chat_instance": "ci", "data": data,
                "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
                "message": msg}}, self.app.bot))

    def recorded(self, chat_id: int, message_id: int) -> None:
        """Record a Ticket card or resolve DM as the bot would have."""
        from types import SimpleNamespace
        from bot.shared import record_btn
        record_btn(self.app, USER_ID,
                   SimpleNamespace(message_id=message_id, chat_id=chat_id))

    async def issue_to_description(self, chat_id: int, title: str,
                                   issue_type: int) -> None:
        """/issue, the title, its one result, the issue type: the flow then
        waits for the description."""
        await self.say(chat_id, "/issue")
        await self.say(chat_id, title)
        version = self.user_data["search_version"]
        result = self.user_data["search_results"]["by_key"]
        (media_type, tmdb_id), = result.keys()
        await self.tap(chat_id, f"{TICKET_MEDIA}:{version}:{media_type}:{tmdb_id}",
                       self.last_menu(chat_id))
        await self.tap(chat_id, f"{ISSUE_TYPE}:{version}:{issue_type}",
                       self.last_menu(chat_id))
        assert self.user_data["issue_type"] == issue_type


@pytest.fixture
def rig(monkeypatch):
    return _Rig(monkeypatch)



# --- one flow per user across chats ------------------------------------------


async def test_a_group_issue_ends_the_dm_issue_so_alien_is_never_filed_or_fixed(rig):
    await rig.issue_to_description(DM_ID, "movie x", 1)
    await rig.issue_to_description(GROUP_ID, "alien", 2)
    await rig.say(DM_ID, "the picture is green")
    # The DM text is not part of any flow: nothing offered, nothing filed.
    version = rig.user_data.get("search_version")
    await rig.tap(DM_ID, f"{TICKET_AUTOFIX_OFFER}:{version}:yes",
                  rig.last_menu(DM_ID))
    await rig.tap(DM_ID, f"{TICKET_AUTOFIX_CONFIRM}:{version}:yes",
                  rig.last_menu(DM_ID))
    rig.seerr.create_issue.assert_not_awaited()
    rig.radarr.auto_fix.assert_not_awaited()
    assert not any("auto-fix" in t.lower() for t in rig.texts(DM_ID))
    # The group report is still open and still files Alien.
    assert rig.user_data["media"]["title"] == "Alien"


async def test_a_command_in_a_group_ends_the_dm_issue(rig):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.say(GROUP_ID, "/status")
    assert "media" not in rig.user_data
    await rig.say(DM_ID, "the picture is green")
    rig.seerr.create_issue.assert_not_awaited()


async def test_a_dm_only_issue_still_files(rig):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.say(DM_ID, "the picture is green")
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 10


async def test_a_group_only_issue_still_files(rig):
    await rig.issue_to_description(GROUP_ID, "alien", 4)
    await rig.say(GROUP_ID, "no sound at all")
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 20


async def test_a_command_in_a_group_leaves_another_users_dm_flow(rig):
    """The force-end matches the user part of each key, not any key."""
    issue_conv = rig.app.bot_data["conversations"]["issue"]
    issue_conv._conversations[(777, 777)] = 1
    await rig.say(GROUP_ID, "/status")
    assert issue_conv._conversations.get((777, 777)) == 1


async def test_a_command_in_the_dm_ends_a_ticket_reply_left_open_in_a_group(rig):
    rig.recorded(GROUP_ID, 600)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:7", 600)
    assert rig.user_data["tk_reply_id"] == 7
    await rig.say(DM_ID, "/status")
    conv = rig.app.bot_data["conversations"]["ticket_reply"]
    assert (GROUP_ID, USER_ID) not in conv._conversations
    await rig.say(GROUP_ID, "a reply that is now just chat")
    rig.seerr.add_issue_comment.assert_not_awaited()


def test_a_force_end_names_each_conversation_once(rig):
    from bot.shared import force_end_flow_conversations
    from telegram.ext import CallbackContext
    conv = rig.app.bot_data["conversations"]["ticket_reply"]
    conv._conversations[(DM_ID, USER_ID)] = 1
    conv._conversations[(GROUP_ID, USER_ID)] = 1
    upd = Update.de_json({"update_id": 1,
                          "message": rig._user_message(DM_ID, "/status")},
                         rig.app.bot)
    ended = force_end_flow_conversations(
        CallbackContext.from_update(upd, rig.app), upd)
    assert ended == [conv]
    assert conv._conversations == {}

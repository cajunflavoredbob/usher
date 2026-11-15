"""One person's flows in two chats. user_data is per user while each
conversation is keyed per (chat, user), so a flow left open in one chat used
to act on what a flow in another chat picked. Driven through the real
Application from bot.app._build_app (every conversation, reset_stale_flows
and global_btn_gate as in production); only the Telegram network calls and
the Seerr, Radarr and store clients are faked. The DM's chat id is the user's
id; the group is a supergroup with a negative id, and the bot's group
messages quote the user, as real replies do. A command or Hub button ends
only its own chat's flows; each draft records the chat that owns it, and a
flow in another chat refuses to act on a draft a newer flow took over."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot

import bot.app as app_mod
from bot.callback_prefixes import (ISSUE_TYPE, REQUESTS_MINE, RESOLVE, RQ_GO,
                                   RQ_MEDIA, TICKET_AUTOFIX_CONFIRM,
                                   TICKET_AUTOFIX_OFFER, TICKET_MEDIA,
                                   TICKETS_REPORT, TK_CLOSE_WITH_COMMENT,
                                   TK_REPLY)
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


# --- each draft belongs to the chat whose flow started it --------------------


ISSUE_REPLACED = "That report was replaced by one you started in another chat."
RQ_REPLACED = "That request was replaced by one you started in another chat."


def _live(rig, name: str, chat_id: int) -> bool:
    conv = rig.app.bot_data["conversations"][name]
    return (chat_id, USER_ID) in conv._conversations


async def test_a_group_issue_takes_over_the_draft_so_the_dm_files_and_fixes_nothing(rig):
    """The DM report waits for its description while a group
    /issue picks Alien. The DM text must not file against Alien, and its
    Auto-fix taps must not delete Alien's file."""
    await rig.issue_to_description(DM_ID, "movie x", 1)
    await rig.issue_to_description(GROUP_ID, "alien", 2)
    await rig.say(DM_ID, "the picture is green")
    assert rig.texts(DM_ID)[-1].startswith(ISSUE_REPLACED)
    assert not _live(rig, "issue", DM_ID)
    version = rig.user_data.get("search_version")
    await rig.tap(DM_ID, f"{TICKET_AUTOFIX_OFFER}:{version}:yes",
                  rig.last_menu(DM_ID))
    await rig.tap(DM_ID, f"{TICKET_AUTOFIX_CONFIRM}:{version}:yes",
                  rig.last_menu(DM_ID))
    rig.seerr.create_issue.assert_not_awaited()
    rig.radarr.auto_fix.assert_not_awaited()
    assert not any("auto-fix" in t.lower() for t in rig.texts(DM_ID))
    # The group report keeps its draft and still files Alien.
    assert rig.user_data["media"]["title"] == "Alien"
    assert rig.user_data.get("description") is None
    await rig.say(GROUP_ID, "no sound at all")
    await rig.tap(GROUP_ID, f"{TICKET_AUTOFIX_OFFER}:{version}:no",
                  rig.last_menu(GROUP_ID))
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 20
    assert rig.seerr.create_issue.call_args.kwargs["message"] == "no sound at all"


async def test_another_bots_command_in_a_group_leaves_the_dm_issue(rig):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.say(GROUP_ID, "/weather")
    assert _live(rig, "issue", DM_ID)
    await rig.say(DM_ID, "the picture is green")
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 10


async def test_a_hub_tap_in_a_group_leaves_the_dm_issue(rig):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    rig.recorded(GROUP_ID, 610)
    await rig.tap(GROUP_ID, REQUESTS_MINE, 610)
    assert _live(rig, "issue", DM_ID)
    await rig.say(DM_ID, "the picture is green")
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 10


async def test_a_command_in_the_dm_still_ends_the_dm_issue(rig):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.say(DM_ID, "/status")
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


async def test_a_command_in_the_group_ending_its_own_issue_leaves_the_dm_draft(rig):
    """The group's /issue is live but the DM's newer /issue owns the draft:
    ending the group's conversation must not clear the DM's report."""
    await rig.issue_to_description(GROUP_ID, "alien", 4)
    await rig.issue_to_description(DM_ID, "movie x", 4)
    assert _live(rig, "issue", GROUP_ID)
    await rig.say(GROUP_ID, "/status")
    assert not _live(rig, "issue", GROUP_ID)
    assert rig.user_data["media"]["title"] == "Movie X"
    await rig.say(DM_ID, "the picture is green")
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 10


async def test_a_command_in_a_group_leaves_another_users_dm_flow(rig):
    issue_conv = rig.app.bot_data["conversations"]["issue"]
    issue_conv._conversations[(777, 777)] = 1
    await rig.say(GROUP_ID, "/status")
    assert issue_conv._conversations.get((777, 777)) == 1


async def test_a_command_in_the_dm_leaves_a_ticket_reply_open_in_a_group(rig):
    rig.recorded(GROUP_ID, 600)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:7", 600)
    await rig.say(DM_ID, "/status")
    assert _live(rig, "ticket_reply", GROUP_ID)
    assert rig.user_data["tk_reply_chat"] == GROUP_ID
    await rig.say(GROUP_ID, "a reply for seven")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (7, "a reply for seven")


async def test_a_command_in_the_reply_chat_still_ends_the_reply(rig):
    rig.recorded(GROUP_ID, 600)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:7", 600)
    await rig.say(GROUP_ID, "/status")
    assert "tk_reply_id" not in rig.user_data
    await rig.say(GROUP_ID, "just chat now")
    rig.seerr.add_issue_comment.assert_not_awaited()


def test_a_force_end_ends_only_its_own_chats_conversation(rig):
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
    assert conv._conversations == {(GROUP_ID, USER_ID): 1}


# --- a DM /link PIN poll and a command in a group -----------------------------


async def test_a_dm_link_poll_finishes_after_a_command_in_a_group(rig, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from bot import link_flow
    from bot.callback_prefixes import LINK_CONSENT, LINK_PLATFORM
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    started, release = asyncio.Event(), asyncio.Event()
    plex = rig.app.bot_data["plex"]
    plex.request_pin = AsyncMock(return_value=SimpleNamespace(
        id=1, auth_url="https://plex.example/auth", code="abcd"))

    async def poll_pin(pin_id):
        started.set()
        await release.wait()
        return "tok"

    plex.poll_pin = poll_pin
    plex.get_user = AsyncMock(return_value=SimpleNamespace(uuid="u", username="un"))
    rig.seerr.login_with_plex = AsyncMock(return_value=(7, "U"))
    rig.app.bot_data["store"].link_with_plex = AsyncMock()
    await rig.say(DM_ID, "/link")
    await rig.tap(DM_ID, f"{LINK_CONSENT}:yes", rig.last_menu(DM_ID))
    task = asyncio.create_task(
        rig.tap(DM_ID, f"{LINK_PLATFORM}:desktop", rig.last_menu(DM_ID)))
    await asyncio.wait_for(started.wait(), 5)
    await rig.say(GROUP_ID, "/weather")
    assert rig.user_data.get("link_active_loop")
    release.set()
    await asyncio.wait_for(task, 5)
    rig.app.bot_data["store"].link_with_plex.assert_awaited_once()
    # The poll step's own END stands: a command in another chat is no
    # overtake, so the DM's link conversation does not stay open.
    assert not _live(rig, "link", DM_ID)


# --- a report cut off by a revoked sign-in, resumed by a DM /link -------------


async def _relink_in_dm(rig, monkeypatch) -> None:
    """/link in the DM, through the PIN poll, to _finalize_link (which runs
    the relink resume)."""
    from types import SimpleNamespace
    from bot import link_flow
    from bot.callback_prefixes import LINK_CONSENT, LINK_PLATFORM
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    plex = rig.app.bot_data["plex"]
    plex.request_pin = AsyncMock(return_value=SimpleNamespace(
        id=1, auth_url="https://plex.example/auth", code="abcd"))
    plex.poll_pin = AsyncMock(return_value="fresh-tok")
    plex.get_user = AsyncMock(return_value=SimpleNamespace(uuid="u", username="un"))
    rig.seerr.login_with_plex = AsyncMock(return_value=(7, "U"))
    rig.app.bot_data["store"].link_with_plex = AsyncMock()
    await rig.say(DM_ID, "/link")
    await rig.tap(DM_ID, f"{LINK_CONSENT}:yes", rig.last_menu(DM_ID))
    await rig.tap(DM_ID, f"{LINK_PLATFORM}:desktop", rig.last_menu(DM_ID))
    rig.app.bot_data["store"].link_with_plex.assert_awaited_once()


def _revoked_then_filed(rig) -> None:
    from seerr import PlexTokenInvalidError
    rig.seerr.create_issue = AsyncMock(side_effect=[
        PlexTokenInvalidError(),
        CreatedIssue(id=5, url="https://seerr.example/5")])


@pytest.mark.parametrize("chat_id,title,media_id", [
    (GROUP_ID, "alien", 20), (DM_ID, "movie x", 10)])
async def test_a_report_cut_off_by_a_revoked_sign_in_is_filed_once_after_a_dm_link(
        rig, monkeypatch, chat_id, title, media_id):
    """The pass-8 case is the group report: the resume runs in the DM, where
    the group's draft is not the DM's own, and it must still be filed."""
    _revoked_then_filed(rig)
    await rig.issue_to_description(chat_id, title, 4)
    await rig.say(chat_id, "no sound at all")
    assert rig.user_data["relink_resume"]["kind"] == "submit_ticket"
    await _relink_in_dm(rig, monkeypatch)
    assert rig.seerr.create_issue.await_count == 2
    kwargs = rig.seerr.create_issue.call_args.kwargs
    assert (kwargs["seerr_media_id"], kwargs["message"]) == (media_id,
                                                            "no sound at all")
    assert any("Reported as ticket #5" in t for t in rig.texts(DM_ID))
    assert not any(t.startswith(ISSUE_REPLACED) for t in rig.texts(DM_ID))
    # The filed report's draft is gone, so nothing files it a second time.
    assert "media" not in rig.user_data and "relink_resume" not in rig.user_data


async def test_a_resume_whose_report_is_no_longer_the_draft_files_nothing():
    """The marker names the draft it was written for: another draft in its
    place (even one this chat owns) is not filed by the resume."""
    from bot.shared import run_relink_resume
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_issue_draft(), "issue_chat": DM_ID,
                              "issue_chat_draft": 8, "relink_resume": {
                                  "kind": "submit_ticket", "saved_at": time.time(),
                                  "payload": {"autofix": False, "draft": 7}}})
    ctx.bot_data["seerr"].create_issue = AsyncMock()
    upd = make_update(text="", user_id=USER_ID, chat_id=DM_ID)
    await run_relink_resume(upd, ctx)
    ctx.bot_data["seerr"].create_issue.assert_not_awaited()
    assert "didn't send the earlier report" in upd.effective_message.reply_calls[-1]["text"]
    assert ctx.user_data["media"]["title"] == "Alien"


# --- /request and /invite drafts ----------------------------------------------


def _requests(rig) -> list:
    return [c.kwargs["tmdb_id"] for c in rig.seerr.create_request.call_args_list]


async def _request_to_confirm(rig, chat_id: int, title: str) -> int:
    await rig.say(chat_id, f"/request {title}")
    version = rig.user_data["rq_search_version"]
    tmdb = {"movie x": 100, "alien": 348}[title]
    await rig.tap(chat_id, f"{RQ_MEDIA}:{version}:movie:{tmdb}",
                  rig.last_menu(chat_id))
    assert "Request <b>" in rig.texts(chat_id)[-1]
    return version


@pytest.fixture
def rq_rig(rig):
    from seerr import CreatedRequest, REQUEST_STATUS_APPROVED
    rig.seerr.create_request = AsyncMock(return_value=CreatedRequest(
        id=9, status=REQUEST_STATUS_APPROVED))
    return rig


async def test_a_group_request_takes_over_so_the_dm_confirm_files_nothing(rq_rig):
    rig = rq_rig
    dm_version = await _request_to_confirm(rig, DM_ID, "movie x")
    dm_confirm = rig.last_menu(DM_ID)
    group_version = await _request_to_confirm(rig, GROUP_ID, "alien")
    await rig.tap(DM_ID, f"{RQ_GO}:{dm_version}", dm_confirm)
    assert _requests(rig) == []
    assert rig.texts(DM_ID)[-1].startswith(RQ_REPLACED)
    await rig.tap(GROUP_ID, f"{RQ_GO}:{group_version}", rig.last_menu(GROUP_ID))
    assert _requests(rig) == [348]


async def test_another_bots_command_in_a_group_leaves_the_dm_request(rq_rig):
    rig = rq_rig
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    confirm = rig.last_menu(DM_ID)
    await rig.say(GROUP_ID, "/weather")
    rig.recorded(GROUP_ID, 611)
    await rig.tap(GROUP_ID, REQUESTS_MINE, 611)
    await rig.tap(DM_ID, f"{RQ_GO}:{version}", confirm)
    assert _requests(rig) == [100]


async def test_another_bots_command_in_a_group_leaves_the_dm_invite(rig):
    from types import SimpleNamespace
    from bot.callback_prefixes import INV_DONE, INV_GO
    plex = rig.app.bot_data["plex"]
    plex.get_owned_server = AsyncMock(return_value=("m1", "Server"))
    plex.get_library_sections = AsyncMock(return_value=[
        SimpleNamespace(id=1, title="Movies", type="movie")])
    plex.invite_to_server = AsyncMock()
    await rig.say(DM_ID, "/invite")
    await rig.say(DM_ID, "friend@example.com")
    version = rig.user_data["inv_version"]
    await rig.tap(DM_ID, f"{INV_DONE}:{version}", rig.last_menu(DM_ID))
    await rig.say(GROUP_ID, "/weather")
    await rig.tap(DM_ID, f"{INV_GO}:{version}", rig.last_menu(DM_ID))
    plex.invite_to_server.assert_awaited_once()
    assert plex.invite_to_server.call_args.args[2] == "friend@example.com"


# --- every step that acts on a draft checks the draft's chat ------------------
#
# A draft whose owner chat is the group, read from a DM step whose buttons
# still carry the current version: only the owner check can refuse it.

def _issue_draft() -> dict:
    return {"issue_chat": GROUP_ID, "search_version": 3,
            "media": {"type": "movie", "tmdb_id": 348, "seerr_media_id": 20,
                      "title": "Alien", "year": "1979"},
            "season": None, "episode": None, "issue_type": 1,
            "description": "the group's words"}


def _rq_draft() -> dict:
    return {"rq_chat": GROUP_ID, "rq_search_version": 3,
            "rq_media": {"type": "movie", "tmdb_id": 348, "title": "Alien",
                         "year": "1979"},
            "rq_confirm_for": {"gen": 0, "version": 3, "target": None}}


def _inv_draft() -> dict:
    return {"inv_chat": GROUP_ID, "inv_version": 3,
            "inv_email": "friend@example.com", "inv_machine": "m1",
            "inv_selected": {1}, "inv_sections": []}


def _issue_steps():
    from bot import ticket_flow as tf
    return [
        (tf.ticket_title, dict(text="alien")),
        (tf.ticket_pick_type, dict(callback_data=f"{ISSUE_TYPE}:3:1")),
        (tf.ticket_description, dict(text="the dm's words")),
        (tf.ticket_offer_autofix, dict(callback_data=f"{TICKET_AUTOFIX_OFFER}:3:yes")),
        (tf.ticket_confirm_autofix, dict(callback_data=f"{TICKET_AUTOFIX_CONFIRM}:3:yes")),
    ]


@pytest.mark.parametrize("index", range(5))
async def test_each_issue_step_refuses_a_report_another_chat_took_over(index):
    from tests._handler_harness import make_update
    step, kw = _issue_steps()[index]
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=_issue_draft())
    ctx.bot_data["seerr"].create_issue = AsyncMock()
    upd = make_update(user_id=USER_ID, chat_id=DM_ID, **kw)
    state = await step(upd, ctx)
    from telegram.ext import ConversationHandler
    assert state == ConversationHandler.END
    shown = (upd.callback_query.edits if upd.callback_query
             else upd.effective_message.reply_calls)
    # The refusal is all this chat sees: no "Submitting" or "Checking" first.
    assert [e["text"] for e in shown] == [shown[-1]["text"]]
    assert shown[-1]["text"].startswith(ISSUE_REPLACED)
    ctx.bot_data["seerr"].create_issue.assert_not_awaited()
    ctx.bot_data["seerr"].search.assert_not_awaited()
    ctx.bot_data["radarr"].auto_fix.assert_not_awaited()
    assert ctx.user_data == _issue_draft()


async def test_the_issue_submit_refuses_a_report_another_chat_took_over():
    from bot.ticket_flow import _submit_ticket
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=_issue_draft())
    ctx.bot_data["seerr"].create_issue = AsyncMock()
    upd = make_update(callback_data="x", user_id=USER_ID, chat_id=DM_ID)
    await _submit_ticket(upd, ctx, autofix=True)
    ctx.bot_data["seerr"].create_issue.assert_not_awaited()
    ctx.bot_data["radarr"].auto_fix.assert_not_awaited()
    assert ctx.user_data == _issue_draft()


async def test_an_issue_step_in_the_owner_chat_still_acts():
    from bot.ticket_flow import ticket_description
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_issue_draft(), "issue_type": 4})
    ctx.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=5, url="https://seerr.example/5"))
    upd = make_update(text="the group's words", user_id=USER_ID, chat_id=GROUP_ID)
    await ticket_description(upd, ctx)
    ctx.bot_data["seerr"].create_issue.assert_awaited_once()


def _rq_steps():
    from bot import request_flow as rf
    return [
        (rf.request_title, dict(text="alien")),
        (rf._show_confirm, dict(callback_data=f"{RQ_MEDIA}:3:movie:348")),
        (rf.request_submit, dict(callback_data=f"{RQ_GO}:3")),
    ]


@pytest.mark.parametrize("index", range(3))
async def test_each_request_step_refuses_a_draft_another_chat_took_over(index):
    from telegram.ext import ConversationHandler
    from tests._handler_harness import make_update
    step, kw = _rq_steps()[index]
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=_rq_draft())
    upd = make_update(user_id=USER_ID, chat_id=DM_ID, **kw)
    assert await step(upd, ctx) == ConversationHandler.END
    shown = (upd.callback_query.edits if upd.callback_query
             else upd.effective_message.reply_calls)
    assert shown[-1]["text"].startswith(RQ_REPLACED)
    ctx.bot_data["seerr"].search.assert_not_awaited()
    ctx.bot_data["seerr"].create_request.assert_not_awaited()
    assert ctx.user_data == _rq_draft()


@pytest.mark.parametrize("step_name,kw", [
    ("invite_email", dict(text="other@example.com")),
    ("invite_send", dict(callback_data="invgo:3")),
])
async def test_each_invite_step_refuses_a_draft_another_chat_took_over(step_name, kw):
    from telegram.ext import ConversationHandler
    from bot import invite_flow
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=_inv_draft())
    upd = make_update(user_id=USER_ID, chat_id=DM_ID, **kw)
    assert await getattr(invite_flow, step_name)(upd, ctx) == ConversationHandler.END
    shown = (upd.callback_query.edits if upd.callback_query
             else upd.effective_message.reply_calls)
    assert shown[-1]["text"].startswith(
        "That invite was replaced by one you started in another chat.")
    ctx.bot_data["plex"].invite_to_server.assert_not_awaited()
    ctx.bot_data["plex"].get_owned_server.assert_not_awaited()
    assert ctx.user_data == _inv_draft()


async def test_a_typed_cancel_in_the_older_chat_leaves_the_newer_report():
    from bot.ticket_flow import ticket_cancel
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data=_issue_draft())
    upd = make_update(text="/cancel", user_id=USER_ID, chat_id=DM_ID)
    await ticket_cancel(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"].startswith("Cancelled")
    assert ctx.user_data == _issue_draft()


async def test_a_typed_cancel_in_the_older_chat_leaves_the_newer_request():
    from bot.request_flow import request_cancel
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data=_rq_draft())
    upd = make_update(text="/cancel", user_id=USER_ID, chat_id=DM_ID)
    await request_cancel(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"].startswith("Cancelled")
    assert ctx.user_data == _rq_draft()


async def test_a_timeout_in_the_older_chat_leaves_the_newer_report_and_request():
    from bot.request_flow import _request_timeout
    from bot.ticket_flow import _ticket_timeout
    from tests._handler_harness import make_update
    for handler, draft in ((_ticket_timeout, _issue_draft),
                           (_request_timeout, _rq_draft)):
        ctx = make_ctx(admin_id=USER_ID, user_data=draft())
        await handler(make_update(text="x", user_id=USER_ID, chat_id=DM_ID), ctx)
        assert ctx.user_data == draft()
        ctx.bot.send_message.assert_not_awaited()


async def test_a_finished_submit_in_the_older_chat_leaves_the_newer_draft():
    """A submit in the DM that finishes after a group flow took the draft
    over must not clear the group's draft."""
    from bot.request_flow import _clear_finished_rq_state
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data=_rq_draft())
    _clear_finished_rq_state(ctx, make_update(callback_data="x", user_id=USER_ID,
                                              chat_id=DM_ID))
    assert ctx.user_data == _rq_draft()
    _clear_finished_rq_state(ctx, make_update(callback_data="x", user_id=USER_ID,
                                              chat_id=GROUP_ID))
    assert "rq_media" not in ctx.user_data


def test_each_new_flow_entry_records_its_chat_as_the_drafts_owner():
    """Source-level map of the claims, checked by the flows above end to end."""
    from bot.shared import ISSUE_KEYS, RQ_KEYS
    from bot.invite_flow import _INV_KEYS
    from bot.app import _CONVERSATION_USER_DATA_KEYS
    assert "issue_chat" in ISSUE_KEYS and "rq_chat" in RQ_KEYS
    assert "inv_chat" in _INV_KEYS
    assert {"issue_chat", "rq_chat", "inv_chat"} <= set(_CONVERSATION_USER_DATA_KEYS)


# Each entry point that starts a new flow takes the draft for its chat, so a
# draft another chat's flow left behind no longer refuses this one.
def _entries():
    from bot import invite_flow as inv
    from bot import request_flow as rf
    from bot import ticket_flow as tf
    from bot.callback_prefixes import (ACCESS_APPROVE, ADMIN_INVITE,
                                       REQUESTS_BEHALF, REQUESTS_NEW,
                                       TICKETS_REPORT)
    return [
        (tf.ticket_start, dict(text="/issue"), "issue_chat"),
        (tf.ticket_hub_report, dict(callback_data=TICKETS_REPORT), "issue_chat"),
        (rf.request_start, dict(text="/request movie x"), "rq_chat"),
        (rf.request_hub_new, dict(callback_data=REQUESTS_NEW), "rq_chat"),
        (rf.request_hub_behalf, dict(callback_data=REQUESTS_BEHALF), "rq_chat"),
        (rf.request_from_ticket, dict(callback_data="rqfi:movie:5:i"), "rq_chat"),
        (inv.invite_start, dict(text="/invite"), "inv_chat"),
        (inv.invite_hub_start, dict(callback_data=ADMIN_INVITE), "inv_chat"),
        (inv.invite_access_start, dict(callback_data=f"{ACCESS_APPROVE}:4"),
         "inv_chat"),
    ]


@pytest.mark.parametrize("index", range(9))
async def test_each_new_flow_takes_the_draft_for_its_own_chat(index):
    from types import SimpleNamespace
    from tests._handler_harness import make_update
    entry, kw, owner_key = _entries()[index]
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={owner_key: GROUP_ID})
    ctx.args = ["movie", "x"]
    store = ctx.bot_data["store"]
    store.list_linked_users = AsyncMock(return_value=[(1000, 20, "Person 0")])
    store.get_access_ask = AsyncMock(return_value=SimpleNamespace(
        id=4, status="open", plex_email="friend@example.com", plex_username=""))
    plex = ctx.bot_data["plex"]
    plex.get_owned_server = AsyncMock(return_value=("m1", "Server"))
    plex.get_library_sections = AsyncMock(return_value=[
        SimpleNamespace(id=1, title="Movies", type="movie")])
    ctx.bot_data["flow_convs"] = []
    upd = make_update(user_id=USER_ID, chat_id=DM_ID, **kw)
    await entry(upd, ctx)
    assert ctx.user_data[owner_key] == DM_ID


async def test_a_group_command_ending_its_own_request_leaves_the_dm_draft(rq_rig):
    """The group's /request is live but the DM's newer /request owns the
    draft: ending the group's conversation must not clear the DM's draft."""
    rig = rq_rig
    await rig.say(GROUP_ID, "/request alien")
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    assert _live(rig, "request", GROUP_ID)
    await rig.say(GROUP_ID, "/status")
    assert not _live(rig, "request", GROUP_ID)
    assert rig.user_data["rq_media"]["tmdb_id"] == 100
    await rig.tap(DM_ID, f"{RQ_GO}:{version}", rig.last_menu(DM_ID))
    assert _requests(rig) == [100]


async def test_a_report_taken_over_mid_submit_keeps_the_newer_draft():
    """A group /issue started while the DM's report was being filed owns the
    draft by the time the DM submit finishes: the DM submit leaves it."""
    from bot.ticket_flow import _submit_ticket
    from tests._handler_harness import make_update
    draft = {**_issue_draft(), "issue_chat": DM_ID}
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=dict(draft))

    async def create_issue(**kw):
        ctx.user_data.update(issue_chat=GROUP_ID, description="newer")
        return CreatedIssue(id=5, url="https://seerr.example/5")

    ctx.bot_data["seerr"].create_issue = AsyncMock(side_effect=create_issue)
    upd = make_update(text="x", user_id=USER_ID, chat_id=DM_ID)
    await _submit_ticket(upd, ctx, autofix=False)
    assert ctx.user_data["issue_chat"] == GROUP_ID
    assert ctx.user_data["description"] == "newer"


# --- text-awaiting markers belong to the chat they were started in -----------


async def _reply_in_dm_then_close_with_comment_in_group(rig) -> None:
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_CLOSE_WITH_COMMENT}:2", 602)
    assert rig.user_data["tk_reply_id"] == 2


async def test_a_dm_text_does_not_land_on_the_ticket_closed_from_a_group(rig):
    await _reply_in_dm_then_close_with_comment_in_group(rig)
    await rig.say(DM_ID, "words for ticket one")
    rig.seerr.add_issue_comment.assert_not_awaited()
    rig.seerr.resolve_issue.assert_not_awaited()
    # The group's Close with comment is untouched and still works.
    await rig.say(GROUP_ID, "closing words")
    rig.seerr.add_issue_comment.assert_awaited_once()
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (2, "closing words")
    assert rig.seerr.resolve_issue.call_args.args[0] == 2


PROMPT_REPLACED = "That reply prompt was replaced by one you opened in another chat."


async def test_text_in_the_older_reply_chat_is_told_its_prompt_was_replaced(rig):
    await _reply_in_dm_then_close_with_comment_in_group(rig)
    await rig.say(DM_ID, "words for ticket one")
    assert rig.texts(DM_ID)[-1] == PROMPT_REPLACED
    assert not _live(rig, "ticket_reply", DM_ID)
    rig.seerr.add_issue_comment.assert_not_awaited()
    assert rig.user_data["tk_reply_id"] == 2


async def test_text_in_the_older_comment_chat_is_told_its_prompt_was_replaced(rig):
    await _comment_asked_in_dm_then_group(rig)
    await rig.say(DM_ID, "still broken in the dm")
    assert rig.texts(DM_ID)[-1] == PROMPT_REPLACED
    rig.seerr.add_issue_comment.assert_not_awaited()
    assert rig.user_data["awaiting_comment_for"] == 2


async def test_a_reply_with_its_markers_gone_stays_silent():
    """No marker at all (an error cleared them) is not a takeover: nothing
    is said about another chat."""
    from bot.resolve_flow import resolve_comment
    from bot.tickets import tk_reply_text
    from tests._handler_harness import make_update
    for handler in (tk_reply_text, resolve_comment):
        ctx = make_ctx(admin_id=USER_ID)
        upd = make_update(text="words", user_id=USER_ID, chat_id=DM_ID)
        await handler(upd, ctx)
        assert upd.effective_message.reply_calls == []


async def test_a_cancel_in_the_older_chat_leaves_the_newer_reply(rig):
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:2", 602)
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    await rig.say(GROUP_ID, "/cancel")
    assert rig.user_data["tk_reply_id"] == 1
    await rig.say(DM_ID, "words for ticket one")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (1, "words for ticket one")


async def test_a_reply_in_one_chat_still_posts(rig):
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:2", 602)
    await rig.say(GROUP_ID, "group words")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (2, "group words")
    assert "tk_reply_id" not in rig.user_data


async def _comment_asked_in_dm_then_group(rig) -> None:
    rig.recorded(DM_ID, 603)
    await rig.tap(DM_ID, f"{RESOLVE}:1:no", 603)
    rig.recorded(GROUP_ID, 604)
    await rig.tap(GROUP_ID, f"{RESOLVE}:2:no", 604)
    assert rig.user_data["awaiting_comment_for"] == 2


async def test_a_dm_text_does_not_land_on_the_resolve_comment_asked_in_a_group(rig):
    await _comment_asked_in_dm_then_group(rig)
    await rig.say(DM_ID, "still broken in the dm")
    rig.seerr.add_issue_comment.assert_not_awaited()
    await rig.say(GROUP_ID, "still broken")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (2, "still broken")


async def test_a_cancel_in_the_older_chat_leaves_the_newer_comment_prompt(rig):
    rig.recorded(GROUP_ID, 604)
    await rig.tap(GROUP_ID, f"{RESOLVE}:2:no", 604)
    rig.recorded(DM_ID, 603)
    await rig.tap(DM_ID, f"{RESOLVE}:1:no", 603)
    await rig.say(GROUP_ID, "/cancel")
    await rig.say(DM_ID, "still broken")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (1, "still broken")


async def test_a_reply_timeout_in_the_older_chat_leaves_the_newer_reply():
    from bot.tickets import tk_reply_timeout
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "tk_reply_id": 1, "tk_close_after": False, "tk_reply_chat": DM_ID})
    ctx.bot.send_message = AsyncMock()
    await tk_reply_timeout(make_update(text="x", user_id=USER_ID,
                                       chat_id=GROUP_ID), ctx)
    assert ctx.user_data["tk_reply_id"] == 1
    assert ctx.user_data["tk_reply_chat"] == DM_ID
    ctx.bot.send_message.assert_not_awaited()


async def test_a_reply_timeout_in_its_own_chat_still_clears_and_says_so():
    from bot.tickets import tk_reply_timeout
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "tk_reply_id": 1, "tk_close_after": False, "tk_reply_chat": DM_ID})
    ctx.bot.send_message = AsyncMock()
    await tk_reply_timeout(make_update(text="x", user_id=USER_ID,
                                       chat_id=DM_ID), ctx)
    assert "tk_reply_id" not in ctx.user_data
    assert "tk_reply_chat" not in ctx.user_data
    ctx.bot.send_message.assert_awaited_once()


async def test_a_comment_timeout_in_the_older_chat_leaves_the_newer_prompt():
    from bot.resolve_flow import _resolve_timeout
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "awaiting_comment_for": 1, "awaiting_comment_chat": DM_ID})
    ctx.bot.send_message = AsyncMock()
    await _resolve_timeout(make_update(text="x", user_id=USER_ID,
                                       chat_id=GROUP_ID), ctx)
    assert ctx.user_data["awaiting_comment_for"] == 1
    ctx.bot.send_message.assert_not_awaited()


async def test_a_comment_timeout_in_its_own_chat_still_clears_and_says_so():
    from bot.resolve_flow import _resolve_timeout
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "awaiting_comment_for": 1, "awaiting_comment_chat": DM_ID})
    ctx.bot.send_message = AsyncMock()
    await _resolve_timeout(make_update(text="x", user_id=USER_ID,
                                       chat_id=DM_ID), ctx)
    assert "awaiting_comment_for" not in ctx.user_data
    assert "awaiting_comment_chat" not in ctx.user_data
    ctx.bot.send_message.assert_awaited_once()


async def test_a_reply_moved_to_another_chat_mid_post_ends_this_chats_flow():
    """The post-comment check: a newer Reply started in another chat while
    the comment was posting keeps its markers, skips the close, and this
    chat's conversation ends (its key no longer has a reply to wait for)."""
    from bot.shared import AWAIT_TICKET_REPLY
    from bot.tickets import tk_reply_text
    from tests._handler_harness import make_update
    from telegram.ext import ConversationHandler
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "tk_reply_id": 2, "tk_close_after": True, "tk_reply_chat": GROUP_ID})

    async def post(*a, **kw):
        ctx.user_data.update(tk_reply_id=2, tk_close_after=False,
                             tk_reply_chat=DM_ID)

    ctx.bot_data["seerr"].add_issue_comment = AsyncMock(side_effect=post)
    state = await tk_reply_text(make_update(text="words", user_id=USER_ID,
                                            chat_id=GROUP_ID), ctx)
    assert state == ConversationHandler.END and state != AWAIT_TICKET_REPLY
    ctx.bot_data["seerr"].resolve_issue.assert_not_awaited()
    assert ctx.user_data["tk_reply_chat"] == DM_ID


async def test_a_cancel_in_the_older_chat_shows_no_back_button_of_the_newer_reply():
    from bot.tickets import tk_reply_cancel
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "tk_reply_id": 1, "tk_reply_chat": DM_ID, "tk_reply_from_list": True})
    upd = make_update(text="/cancel", user_id=USER_ID, chat_id=GROUP_ID)
    await tk_reply_cancel(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["reply_markup"] is None
    assert ctx.user_data["tk_reply_from_list"] is True


async def test_a_reply_timeout_with_no_update_still_clears():
    from bot.tickets import tk_reply_timeout
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "tk_reply_id": 1, "tk_reply_chat": DM_ID})
    await tk_reply_timeout(None, ctx)
    assert "tk_reply_id" not in ctx.user_data


def test_an_error_clears_the_marker_chats_too():
    from bot.app import _CONVERSATION_USER_DATA_KEYS
    assert {"tk_reply_chat", "awaiting_comment_chat"} <= set(
        _CONVERSATION_USER_DATA_KEYS)


# --- the movie Auto-fix confirm names its title -------------------------------


async def test_the_movie_auto_fix_confirm_names_the_title_it_deletes(rig):
    await rig.issue_to_description(DM_ID, "movie x", 1)
    await rig.say(DM_ID, "the picture is green")
    version = rig.user_data["search_version"]
    await rig.tap(DM_ID, f"{TICKET_AUTOFIX_OFFER}:{version}:yes",
                  rig.last_menu(DM_ID))
    confirm = rig.texts(DM_ID)[-1]
    assert confirm.startswith("Radarr: Movie X (2001)\n\n"), confirm
    assert "delete the current file" in confirm


# --- an error clears only what belongs to its own chat ------------------------


def _fail_once(monkeypatch, module, name: str) -> None:
    """The next call of module.name raises, as an unexpected bug would; the
    calls after it run as before."""
    real = getattr(module, name)
    calls = []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real(*a, **kw)

    monkeypatch.setattr(module, name, flaky)


ERROR_TEXT = "⚠️ Something went wrong on my end. Please run the command again."
PICK_CLEARED = "That Request for someone was ended"


async def test_a_request_for_someone_cut_off_by_an_error_refuses_its_title(
        rq_rig, monkeypatch):
    """The error clears the person picked; the flow must then refuse, not
    go on as the Admin's own Request."""
    from bot import request_flow
    from bot.callback_prefixes import REQUESTS_BEHALF, RQ_BEHALF_PICK
    rig = rq_rig
    rig.app.bot_data["store"].list_linked_users = AsyncMock(
        return_value=[(1000, 20, "Person 0"), (1001, 21, "Person 1")])
    rig.recorded(DM_ID, 620)
    await rig.tap(DM_ID, REQUESTS_BEHALF, 620)
    await rig.tap(DM_ID, f"{RQ_BEHALF_PICK}:21", 620)
    assert rig.user_data["rq_for"]["seerr_id"] == 21
    _fail_once(monkeypatch, request_flow, "send_typing")
    await rig.say(DM_ID, "movie x")
    assert rig.texts(DM_ID)[-1] == ERROR_TEXT
    assert "rq_for" not in rig.user_data
    await rig.say(DM_ID, "movie x")
    assert rig.texts(DM_ID)[-1].startswith(PICK_CLEARED)
    rig.seerr.search.assert_not_awaited()
    assert _requests(rig) == []


async def _error_in_the_group(rig, monkeypatch) -> None:
    """A /status in the group that hits a bug: a command that starts no flow,
    so only on_error can touch the DM's state."""
    _fail_once(monkeypatch, app_mod, "refuse_outside_admin_dm")
    await rig.say(GROUP_ID, "/status")
    assert rig.texts(GROUP_ID)[-1] == ERROR_TEXT


async def test_an_error_in_a_group_leaves_the_dm_report(rig, monkeypatch):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await _error_in_the_group(rig, monkeypatch)
    await rig.say(DM_ID, "the picture is green")
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 10


async def test_an_error_in_a_group_leaves_the_dm_request(rq_rig, monkeypatch):
    rig = rq_rig
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    confirm = rig.last_menu(DM_ID)
    await _error_in_the_group(rig, monkeypatch)
    await rig.tap(DM_ID, f"{RQ_GO}:{version}", confirm)
    assert _requests(rig) == [100]


async def test_an_error_in_a_group_leaves_the_dm_reply_prompt(rig, monkeypatch):
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    await _error_in_the_group(rig, monkeypatch)
    await rig.say(DM_ID, "words for ticket one")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (1, "words for ticket one")


async def test_an_error_in_a_group_leaves_a_dm_link_poll(rig, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from bot import link_flow
    from bot.callback_prefixes import LINK_CONSENT, LINK_PLATFORM
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    started, release = asyncio.Event(), asyncio.Event()
    plex = rig.app.bot_data["plex"]
    plex.request_pin = AsyncMock(return_value=SimpleNamespace(
        id=1, auth_url="https://plex.example/auth", code="abcd"))

    async def poll_pin(pin_id):
        started.set()
        await release.wait()
        return "tok"

    plex.poll_pin = poll_pin
    plex.get_user = AsyncMock(return_value=SimpleNamespace(uuid="u", username="un"))
    rig.seerr.login_with_plex = AsyncMock(return_value=(7, "U"))
    rig.app.bot_data["store"].link_with_plex = AsyncMock()
    await rig.say(DM_ID, "/link")
    await rig.tap(DM_ID, f"{LINK_CONSENT}:yes", rig.last_menu(DM_ID))
    task = asyncio.create_task(
        rig.tap(DM_ID, f"{LINK_PLATFORM}:desktop", rig.last_menu(DM_ID)))
    await asyncio.wait_for(started.wait(), 5)
    await _error_in_the_group(rig, monkeypatch)
    assert rig.user_data.get("link_active_loop")
    release.set()
    await asyncio.wait_for(task, 5)
    rig.app.bot_data["store"].link_with_plex.assert_awaited_once()


async def test_an_error_in_the_reports_own_chat_still_clears_it(rig, monkeypatch):
    from bot import ticket_flow
    await rig.issue_to_description(DM_ID, "movie x", 4)
    _fail_once(monkeypatch, ticket_flow, "send_typing")
    await rig.say(DM_ID, "the picture is green")
    assert rig.texts(DM_ID)[-1] == ERROR_TEXT
    assert "media" not in rig.user_data and "issue_chat" not in rig.user_data
    rig.seerr.create_issue.assert_not_awaited()


def test_every_key_an_error_clears_belongs_to_a_chat():
    """Each key in the error clean-up list is in exactly one owner group
    (hub_origin aside, which is cleared when every draft is the error
    chat's), so none is cleared for every chat by accident."""
    from bot.app import _CONVERSATION_USER_DATA_KEYS, _ERROR_KEY_OWNERS
    grouped = [k for _owner, keys in _ERROR_KEY_OWNERS for k in keys]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) | {"hub_origin"} == set(_CONVERSATION_USER_DATA_KEYS)
    assert "rq_for_picked" not in _CONVERSATION_USER_DATA_KEYS


async def test_an_error_in_a_group_keeps_the_dm_reports_back_button(rig, monkeypatch):
    from bot.callback_prefixes import TICKETS_REPORT
    from bot.tickets_hub import TICKETS_HUB
    rig.recorded(DM_ID, 621)
    await rig.tap(DM_ID, TICKETS_REPORT, 621)
    assert rig.user_data["hub_origin"] == TICKETS_HUB
    await _error_in_the_group(rig, monkeypatch)
    assert rig.user_data["hub_origin"] == TICKETS_HUB


# --- a replaced flow stays replaced after the newer flow ends -----------------


async def _end_group_issue(rig, how: str) -> None:
    if how == "filed":
        await rig.say(GROUP_ID, "no sound at all")
        rig.seerr.create_issue.assert_awaited_once()
    elif how == "cancelled":
        await rig.say(GROUP_ID, "/cancel")
    else:
        await rig.say(GROUP_ID, "/status")
    assert not _live(rig, "issue", GROUP_ID)
    assert "issue_chat" not in rig.user_data


@pytest.mark.parametrize("how", ["filed", "cancelled", "ended by a command"])
async def test_a_replaced_report_says_so_after_the_newer_report_ends(rig, how):
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.issue_to_description(GROUP_ID, "alien", 4)
    await _end_group_issue(rig, how)
    filed = rig.seerr.create_issue.await_count
    await rig.say(DM_ID, "the picture is green")
    assert rig.texts(DM_ID)[-1].startswith(ISSUE_REPLACED)
    assert not any("/link" in t for t in rig.texts(DM_ID))
    assert rig.seerr.create_issue.await_count == filed
    assert not _live(rig, "issue", DM_ID)


async def _end_group_request(rig, how: str, version: int) -> None:
    if how == "filed":
        await rig.tap(GROUP_ID, f"{RQ_GO}:{version}", rig.last_menu(GROUP_ID))
        assert _requests(rig) == [348]
    elif how == "cancelled":
        await rig.say(GROUP_ID, "/cancel")
    else:
        await rig.say(GROUP_ID, "/status")
    assert not _live(rig, "request", GROUP_ID)
    assert "rq_chat" not in rig.user_data


@pytest.mark.parametrize("how", ["filed", "cancelled", "ended by a command"])
async def test_a_replaced_request_says_so_after_the_newer_request_ends(rq_rig, how):
    rig = rq_rig
    dm_version = await _request_to_confirm(rig, DM_ID, "movie x")
    dm_confirm = rig.last_menu(DM_ID)
    group_version = await _request_to_confirm(rig, GROUP_ID, "alien")
    await _end_group_request(rig, how, group_version)
    await rig.tap(DM_ID, f"{RQ_GO}:{dm_version}", dm_confirm)
    assert rig.texts(DM_ID)[-1].startswith(RQ_REPLACED)
    assert 100 not in _requests(rig)


async def test_a_replaced_request_title_says_so_after_the_newer_request_ends(rq_rig):
    """The title step, which reads no draft at all: the DM's flow waits for a
    title while a group /request is filed."""
    rig = rq_rig
    rig.recorded(DM_ID, 622)
    from bot.callback_prefixes import REQUESTS_NEW
    await rig.tap(DM_ID, REQUESTS_NEW, 622)
    group_version = await _request_to_confirm(rig, GROUP_ID, "alien")
    await _end_group_request(rig, "filed", group_version)
    searches = rig.seerr.search.await_count
    await rig.say(DM_ID, "movie x")
    assert rig.texts(DM_ID)[-1].startswith(RQ_REPLACED)
    assert rig.seerr.search.await_count == searches


async def test_a_report_restarted_in_its_own_chat_is_not_replaced(rig):
    """A second /issue in the same chat takes its own draft: the chat's newer
    report files."""
    await rig.issue_to_description(DM_ID, "movie x", 4)
    await rig.issue_to_description(DM_ID, "alien", 4)
    await rig.say(DM_ID, "no sound at all")
    rig.seerr.create_issue.assert_awaited_once()
    assert rig.seerr.create_issue.call_args.kwargs["seerr_media_id"] == 20


async def test_an_error_in_the_reports_own_chat_is_not_called_a_replacement(rig, monkeypatch):
    from bot import ticket_flow
    await rig.issue_to_description(DM_ID, "movie x", 4)
    _fail_once(monkeypatch, ticket_flow, "send_typing")
    await rig.say(DM_ID, "the picture is green")
    await rig.say(DM_ID, "the picture is green")
    assert not any(t.startswith(ISSUE_REPLACED) for t in rig.texts(DM_ID))


# --- a replaced reply prompt stays replaced after the newer one ends ----------


async def _end_group_prompt(rig, how: str, words: str) -> None:
    if how == "posted":
        await rig.say(GROUP_ID, words)
        rig.seerr.add_issue_comment.assert_awaited_once()
    elif how == "cancelled":
        await rig.say(GROUP_ID, "/cancel")
    else:
        await rig.say(GROUP_ID, "/status")


@pytest.mark.parametrize("how", ["posted", "cancelled", "ended by a command"])
async def test_a_replaced_reply_prompt_says_so_after_the_newer_one_ends(rig, how):
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:2", 602)
    await _end_group_prompt(rig, how, "group words")
    posted = rig.seerr.add_issue_comment.await_count
    await rig.say(DM_ID, "words for ticket one")
    assert rig.texts(DM_ID)[-1] == PROMPT_REPLACED
    assert rig.seerr.add_issue_comment.await_count == posted
    assert not _live(rig, "ticket_reply", DM_ID)


@pytest.mark.parametrize("how", ["posted", "cancelled"])
async def test_a_replaced_comment_prompt_says_so_after_the_newer_one_ends(rig, how):
    await _comment_asked_in_dm_then_group(rig)
    await _end_group_prompt(rig, how, "still broken")
    posted = rig.seerr.add_issue_comment.await_count
    await rig.say(DM_ID, "still broken in the dm")
    assert rig.texts(DM_ID)[-1] == PROMPT_REPLACED
    assert rig.seerr.add_issue_comment.await_count == posted
    assert not _live(rig, "resolve", DM_ID)


async def test_a_reply_prompt_reopened_in_the_replaced_chat_posts(rig):
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:2", 602)
    rig.recorded(DM_ID, 603)
    await rig.tap(DM_ID, f"{TK_REPLY}:3", 603)
    await rig.say(DM_ID, "words for ticket three")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (3, "words for ticket three")


async def test_a_comment_prompt_reopened_in_the_replaced_chat_posts(rig):
    await _comment_asked_in_dm_then_group(rig)
    rig.recorded(DM_ID, 605)
    await rig.tap(DM_ID, f"{RESOLVE}:3:no", 605)
    await rig.say(DM_ID, "still broken in the dm")
    assert rig.seerr.add_issue_comment.call_args.args[:2] == (3, "still broken in the dm")


async def test_an_error_after_a_reply_prompt_was_reopened_is_not_called_a_replacement(
        rig, monkeypatch):
    """The reopened chat is no longer a replaced one: when an error then
    clears its own markers, its next text says nothing about another chat."""
    from bot import tickets
    rig.recorded(DM_ID, 601)
    await rig.tap(DM_ID, f"{TK_REPLY}:1", 601)
    rig.recorded(GROUP_ID, 602)
    await rig.tap(GROUP_ID, f"{TK_REPLY}:2", 602)
    rig.recorded(DM_ID, 603)
    await rig.tap(DM_ID, f"{TK_REPLY}:3", 603)
    _fail_once(monkeypatch, tickets, "token_for")
    await rig.say(DM_ID, "words")
    assert rig.texts(DM_ID)[-1] == ERROR_TEXT
    await rig.say(DM_ID, "words again")
    assert PROMPT_REPLACED not in rig.texts(DM_ID)


async def test_an_error_after_a_comment_prompt_was_reopened_is_not_called_a_replacement(
        rig, monkeypatch):
    from bot import resolve_flow
    await _comment_asked_in_dm_then_group(rig)
    rig.recorded(DM_ID, 605)
    await rig.tap(DM_ID, f"{RESOLVE}:3:no", 605)
    _fail_once(monkeypatch, resolve_flow, "token_for")
    await rig.say(DM_ID, "words")
    assert rig.texts(DM_ID)[-1] == ERROR_TEXT
    await rig.say(DM_ID, "words again")
    assert PROMPT_REPLACED not in rig.texts(DM_ID)


# --- the replaced refusal keeps a Hub flow's back button ---------------------

@pytest.mark.parametrize("kind", ["tap", "text"])
async def test_a_replaced_refusal_of_a_hub_flow_carries_the_back_button(
        kind, monkeypatch):
    from bot import hub as hubmod
    from bot import ticket_flow as tf
    from tests._handler_harness import make_update
    monkeypatch.setitem(hubmod.HUBS, "tickets", hubmod.Hub("Tickets", None))
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_issue_draft(), "hub_origin": "tickets"})
    if kind == "tap":
        upd = make_update(callback_data=f"{ISSUE_TYPE}:3:1",
                          user_id=USER_ID, chat_id=DM_ID)
        await tf.ticket_pick_type(upd, ctx)
        shown = upd.callback_query.edits[-1]
    else:
        upd = make_update(text="the dm's words", user_id=USER_ID, chat_id=DM_ID)
        await tf.ticket_description(upd, ctx)
        shown = upd.effective_message.reply_calls[-1]
    assert shown["text"].startswith(ISSUE_REPLACED)
    buttons = [b.callback_data for row in shown["reply_markup"].inline_keyboard
               for b in row]
    assert buttons == ["hubo:tickets"]


async def test_a_replaced_refusal_without_a_hub_origin_has_no_buttons():
    from bot import ticket_flow as tf
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data=_issue_draft())
    upd = make_update(text="the dm's words", user_id=USER_ID, chat_id=DM_ID)
    await tf.ticket_description(upd, ctx)
    assert upd.effective_message.reply_calls[-1].get("reply_markup") is None


# --- a /cancel in a replaced chat whose own submit still files ---------------

async def _hold_dm_submit(rig, monkeypatch=None, at: str = "create_issue"):
    """The DM report is sent and its submit waits inside `at` until the
    returned gate is set: Seerr's create_issue, or the submit's first await,
    which is the typing indicator for a typed description (issue type 4) and
    the tap's answer for the Auto-fix offer's Report to Admin (issue type 1).
    Returns (gate, task)."""
    gate, entered = asyncio.Event(), asyncio.Event()
    await rig.issue_to_description(DM_ID, "movie x", 1 if at == "answer" else 4)
    if at == "answer":
        await rig.say(DM_ID, "the picture is green")
        real_answer = ExtBot.answer_callback_query

        async def answer(self_, *a, **kw):
            if not entered.is_set():
                entered.set()
                await gate.wait()
            return await real_answer(self_, *a, **kw)
        monkeypatch.setattr(ExtBot, "answer_callback_query", answer)
        version = rig.user_data["search_version"]
        task = asyncio.create_task(rig.tap(
            DM_ID, f"{TICKET_AUTOFIX_OFFER}:{version}:no", rig.last_menu(DM_ID)))
        await asyncio.wait_for(entered.wait(), 5)
        return gate, task
    if at == "create_issue":
        async def create_issue(**kw):
            entered.set()
            await gate.wait()
            return CreatedIssue(id=5, url="https://seerr.example/5")
        rig.seerr.create_issue = AsyncMock(side_effect=create_issue)
    else:
        from bot import ticket_flow as tf

        async def send_typing(update, ctx):
            if update.effective_chat.id == DM_ID and not entered.is_set():
                entered.set()
                await gate.wait()
        monkeypatch.setattr(tf, "send_typing", send_typing)
    task = asyncio.create_task(rig.say(DM_ID, "the picture is green"))
    await asyncio.wait_for(entered.wait(), 5)
    return gate, task


async def _new_report_in_group(rig, how: str) -> None:
    if how == "typed":
        await rig.say(GROUP_ID, "/issue")
    else:
        rig.recorded(GROUP_ID, 620)
        await rig.tap(GROUP_ID, TICKETS_REPORT, 620)
    assert rig.user_data["issue_chat"] == GROUP_ID


@pytest.mark.parametrize("how", ["typed", "hub"])
async def test_a_cancel_in_a_replaced_chat_says_its_report_is_still_sending(rig, how):
    """The DM's report is being filed when a group /issue (or the Tickets
    Hub's Report a problem there) starts a new report. A /cancel in the DM
    must not say "Cancelled." while that report still lands."""
    gate, task = await _hold_dm_submit(rig)
    await _new_report_in_group(rig, how)
    await rig.say(DM_ID, "/cancel")
    assert rig.texts(DM_ID)[-1].startswith("Already sending")
    gate.set()
    await task
    rig.seerr.create_issue.assert_awaited_once()
    assert any(t.startswith("✅ Reported as ticket #5") for t in rig.texts(DM_ID))
    assert rig.user_data["issue_chat"] == GROUP_ID  # the newer draft is untouched
    assert "submitting_ticket" not in rig.user_data


@pytest.mark.parametrize("group", ["started", "at_description"])
@pytest.mark.parametrize("at", ["send_typing", "answer"])
async def test_a_report_being_filed_files_its_own_draft_after_a_group_issue(
        rig, monkeypatch, group, at):
    """The DM's report is held at its submit's first await while a group
    /issue starts (and maybe picks another title). The DM report is still
    filed, with its own title, type and words, and the group's draft stays."""
    gate, task = await _hold_dm_submit(rig, monkeypatch, at=at)
    if group == "started":
        await rig.say(GROUP_ID, "/issue")
    else:
        await rig.issue_to_description(GROUP_ID, "alien", 2)
    gate.set()
    await task
    rig.seerr.create_issue.assert_awaited_once()
    kw = rig.seerr.create_issue.call_args.kwargs
    assert (kw["seerr_media_id"], kw["issue_type"], kw["message"]) == (
        10, 1 if at == "answer" else 4, "the picture is green")
    assert any(t.startswith("✅ Reported as ticket #5") for t in rig.texts(DM_ID))
    assert rig.user_data["issue_chat"] == GROUP_ID
    if group == "at_description":
        assert rig.user_data["media"]["title"] == "Alien"
        assert rig.user_data["issue_type"] == 2


async def test_a_cancel_in_a_replaced_chat_says_its_request_is_still_sending():
    from bot.request_flow import request_cancel
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "rq_chat": GROUP_ID, "rq_chat_draft": 6,
        "rq_chat_claims": {DM_ID: 5, GROUP_ID: 6},
        "rq_submitting": ((DM_ID, 5), 0)})
    upd = make_update(text="/cancel", user_id=USER_ID, chat_id=DM_ID)
    await request_cancel(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"].startswith("Already sending")


async def test_a_cancel_in_a_replaced_chat_with_the_newer_chats_submit_running_cancels():
    from bot.ticket_flow import ticket_cancel
    from tests._handler_harness import make_update
    # The group's report is the one sending; the DM's own was only replaced.
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "issue_chat": GROUP_ID, "issue_chat_draft": 6,
        "issue_chat_claims": {DM_ID: 5, GROUP_ID: 6},
        "submitting_ticket": ((GROUP_ID, 6), 0)})
    upd = make_update(text="/cancel", user_id=USER_ID, chat_id=DM_ID)
    await ticket_cancel(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"].startswith("Cancelled")
    assert ctx.user_data["submitting_ticket"] == ((GROUP_ID, 6), 0)


# --- the submit marker is held before the tap is answered --------------------

def _answer_sees_marker(upd, ctx, key, seen):
    q = upd.callback_query
    real = q.answer

    async def answer(*a, **k):
        seen.append(key in ctx.user_data)
        return await real(*a, **k)
    q.answer = answer


@pytest.mark.parametrize("step,data", [
    ("ticket_offer_autofix", f"{TICKET_AUTOFIX_OFFER}:3:no"),
    ("ticket_confirm_autofix", f"{TICKET_AUTOFIX_CONFIRM}:3:yes"),
    ("ticket_confirm_autofix", f"{TICKET_AUTOFIX_CONFIRM}:3:no"),
])
async def test_a_report_confirm_holds_its_marker_before_it_answers(
        monkeypatch, step, data):
    from telegram.ext import ConversationHandler
    from bot import ticket_flow as tf
    from tests._handler_harness import make_update

    async def inner(*a, **k):
        return ConversationHandler.END
    monkeypatch.setattr(tf, "_submit_ticket_inner", inner)
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_issue_draft(), "issue_chat": DM_ID})
    upd = make_update(callback_data=data, user_id=USER_ID, chat_id=DM_ID)
    seen: list = []
    _answer_sees_marker(upd, ctx, "submitting_ticket", seen)
    await getattr(tf, step)(upd, ctx)
    assert seen == [True]


async def test_a_request_confirm_holds_its_marker_before_it_answers(monkeypatch):
    from telegram.ext import ConversationHandler
    from bot import request_flow as rf
    from tests._handler_harness import make_update

    async def inner(*a, **k):
        return ConversationHandler.END
    monkeypatch.setattr(rf, "_submit_request", inner)
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_rq_draft(), "rq_chat": DM_ID})
    upd = make_update(callback_data=f"{RQ_GO}:3", user_id=USER_ID, chat_id=DM_ID)
    seen: list = []
    _answer_sees_marker(upd, ctx, "rq_submitting", seen)
    await rf.request_submit(upd, ctx)
    assert seen == [True]


async def test_an_invite_confirm_holds_its_marker_before_it_answers(monkeypatch):
    from telegram.ext import ConversationHandler
    from bot import invite_flow
    from tests._handler_harness import make_update

    async def inner(*a, **k):
        return ConversationHandler.END
    monkeypatch.setattr(invite_flow, "_invite_send_inner", inner)
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID),
                   user_data={**_inv_draft(), "inv_chat": DM_ID})
    upd = make_update(callback_data="invgo:3", user_id=USER_ID, chat_id=DM_ID)
    seen: list = []
    _answer_sees_marker(upd, ctx, "inv_submitting", seen)
    await invite_flow.invite_send(upd, ctx)
    assert seen == [True]


def test_each_flows_owner_and_draft_id_keys_are_cleared_with_its_draft():
    from bot.invite_flow import _INV_KEYS
    from bot.shared import (_DRAFT_SUFFIX, ISSUE_KEYS, ISSUE_OWNER_KEY, RQ_KEYS,
                            RQ_OWNER_KEY)
    for owner, keys in ((ISSUE_OWNER_KEY, ISSUE_KEYS), (RQ_OWNER_KEY, RQ_KEYS),
                        ("inv_chat", _INV_KEYS)):
        assert owner in keys and owner + _DRAFT_SUFFIX in keys


def test_the_reply_marker_list_is_the_one_every_clear_uses():
    from bot.shared import REPLY_CHAT_KEY, REPLY_MARKER_KEYS
    assert "tk_reply_from_list" in REPLY_MARKER_KEYS
    assert REPLY_CHAT_KEY in REPLY_MARKER_KEYS
    assert set(REPLY_MARKER_KEYS) <= set(app_mod._CONVERSATION_USER_DATA_KEYS)
    assert (REPLY_CHAT_KEY, REPLY_MARKER_KEYS) in app_mod._ERROR_KEY_OWNERS

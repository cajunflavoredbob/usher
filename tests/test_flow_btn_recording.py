"""Regression tests: flow menus must be recorded with the global button gate.

Before v0.11.18, /issue and /link inline keyboards were never passed to
record_btn, so any user who already had recorded button messages (webhook
ticket DMs) had every flow tap rejected with the stale-menu toast and the
flow stalled on the first menu (field report, 2026-06-11).
"""
from __future__ import annotations

from types import SimpleNamespace

from telegram.constants import ChatType

from bot.issue_flow import issue_title
from bot.link_flow import cmd_link
from bot.shared import AWAIT_LINK_CONSENT, PICK_MEDIA, global_btn_gate, record_btn

from tests._handler_harness import make_ctx, make_update


def _seed_history(ctx, user_id: int, ids=(900, 901, 902)):
    """Fill the user's button history with other messages (the state a user
    is in after a few webhook ticket DMs) so the gate is actively enforcing."""
    for mid in ids:
        record_btn(ctx.application, user_id,
                   SimpleNamespace(message_id=mid, chat_id=100))


def _gate_update(user_id: int, message_id: int, chat_id: int = 100):
    """Minimal callback-query update for driving global_btn_gate directly."""
    q = SimpleNamespace(
        message=SimpleNamespace(message_id=message_id, chat_id=chat_id),
        from_user=SimpleNamespace(id=user_id),
        answers=[],
    )

    async def _answer(text="", show_alert=False):
        q.answers.append(text)

    async def _edit_reply_markup(reply_markup=None):
        pass

    q.answer = _answer
    q.edit_message_reply_markup = _edit_reply_markup
    return SimpleNamespace(callback_query=q)


async def test_issue_search_menu_recorded_and_admitted():
    ctx = make_ctx()
    ctx.bot_data["seerr"].search.return_value = [
        SimpleNamespace(media_type="tv", title="Widow's Bay", year=2026,
                        tmdb_id=1, seerr_media_id=5),
    ]
    _seed_history(ctx, 42)

    upd = make_update(text="Widow's Bay", user_id=42)
    state = await issue_title(upd, ctx)
    assert state == PICK_MEDIA

    # The menu must have been recorded as the user's newest button message...
    entries = ctx.application.bot_data["btn_msgs"][42]
    menu_id = entries[-1]["message_id"]
    assert menu_id not in (900, 901, 902)
    # ...so the gate admits a tap on it (pre-fix this raised
    # ApplicationHandlerStop with the stale-menu toast).
    await global_btn_gate(
        _gate_update(42, menu_id), SimpleNamespace(application=ctx.application)
    )


async def test_link_consent_prompt_recorded_and_admitted():
    ctx = make_ctx()
    _seed_history(ctx, 42)

    upd = make_update(text="/link", user_id=42)
    upd.effective_message.chat = SimpleNamespace(type=ChatType.PRIVATE)
    state = await cmd_link(upd, ctx)
    assert state == AWAIT_LINK_CONSENT

    menu_id = ctx.application.bot_data["btn_msgs"][42][-1]["message_id"]
    assert menu_id not in (900, 901, 902)
    await global_btn_gate(
        _gate_update(42, menu_id), SimpleNamespace(application=ctx.application)
    )

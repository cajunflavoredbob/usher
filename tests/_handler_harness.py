"""PTB handler test harness.

Builds fake Update + CallbackContext objects with the shape real handlers
expect. Doesn't attempt to mock the full PTB Application; that's heavy and
not what handler tests need. Just the surface area handlers actually touch.

Usage:
    from tests._handler_harness import make_update, make_ctx

    upd = make_update(callback_data="tkr:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    await some_handler(upd, ctx)
    assert upd.callback_query.edits[0]["text"].startswith("Ticket #42")
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

from store import Mapping


# --- Telegram object fakes --------------------------------------------------

def make_message(text: str = "", message_id: int = 100, chat_id: int = 100):
    """Fake telegram.Message. Records reply_text + edit_message_text calls.
    Like real PTB, reply_text returns the newly sent Message (a fresh fake
    with its own message_id) and edit_message_text returns the edited
    message itself, so handlers that record the result (button-gate
    bookkeeping) see realistic objects."""
    reply_calls: list[dict] = []
    edit_calls: list[dict] = []

    async def reply_text(text, **kwargs):
        reply_calls.append({"text": text, **kwargs})
        return make_message(message_id=message_id + len(reply_calls),
                            chat_id=chat_id)

    msg = SimpleNamespace(
        text=text,
        message_id=message_id,
        chat_id=chat_id,
        reply_text=reply_text,
        reply_calls=reply_calls,
        edit_calls=edit_calls,
    )

    async def edit_message_text(text, **kwargs):
        edit_calls.append({"text": text, **kwargs})
        return msg

    msg.edit_message_text = edit_message_text
    return msg


def make_callback_query(data: str, *, user_id: int, chat_id: int = 100,
                       message_id: int = 200):
    """Fake telegram.CallbackQuery. Records answer/edit_message_text/
    edit_message_reply_markup calls."""
    answers: list[tuple[str, bool]] = []
    edits: list[dict] = []
    markup_edits: list[Any] = []

    async def answer(text: str = "", show_alert: bool = False):
        answers.append((text, show_alert))

    msg = make_message(message_id=message_id, chat_id=chat_id)

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})
        # Real PTB returns the edited Message (same id as the source).
        return msg

    async def edit_message_reply_markup(reply_markup=None, **kwargs):
        markup_edits.append(reply_markup)
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=msg,
        answer=answer,
        edit_message_text=edit_message_text,
        edit_message_reply_markup=edit_message_reply_markup,
        answers=answers,
        edits=edits,
        markup_edits=markup_edits,
    )


def make_update(*, text: Optional[str] = None, callback_data: Optional[str] = None,
                user_id: int = 42, chat_id: int = 100, message_id: int = 100):
    """Fake telegram.Update.

    Either `text` (for a MessageHandler-style update) or `callback_data`
    (for a CallbackQueryHandler-style update); not both.
    """
    eff_user = SimpleNamespace(id=user_id)
    eff_chat = SimpleNamespace(id=chat_id)
    if callback_data is not None:
        q = make_callback_query(callback_data, user_id=user_id, chat_id=chat_id,
                                message_id=message_id)
        return SimpleNamespace(
            callback_query=q, effective_user=eff_user, effective_chat=eff_chat,
            effective_message=q.message, message=q.message,
        )
    msg = make_message(text=text or "", message_id=message_id, chat_id=chat_id)
    return SimpleNamespace(
        callback_query=None, effective_user=eff_user, effective_chat=eff_chat,
        effective_message=msg, message=msg,
    )


# --- Context fakes ----------------------------------------------------------

def make_mapping(*, telegram_id: int = 42, plex_token: str = "plex-abc",
                 plex_username: str = "kennyplex",
                 decrypt_failed: bool = False) -> Mapping:
    return Mapping(
        telegram_id=telegram_id,
        seerr_id=7,
        seerr_display="Kenny",
        plex_token=None if decrypt_failed else plex_token,
        plex_uuid="uuid-abc",
        plex_username=plex_username,
        plex_token_decrypt_failed=decrypt_failed,
    )


def make_ctx(*, admin_id: int = 999, user_data: Optional[dict] = None,
             bot_data_overrides: Optional[dict] = None,
             mapping: Optional[Mapping] = None):
    """Fake CallbackContext with sensible bot_data defaults.

    seerr/radarr/sonarr clients are SimpleNamespaces of AsyncMocks so tests
    can override return values via `ctx.bot_data["seerr"].get_issue.return_value = ...`
    or use `.side_effect = SomeException`.

    `mapping` (if provided) is what `store.get(telegram_id)` returns; default
    None means "user is unlinked."
    """
    seerr = SimpleNamespace(
        get_issue=AsyncMock(),
        get_media_title=AsyncMock(return_value=("Movie Title", "2026")),
        get_tv_seasons=AsyncMock(return_value=([], 12345)),
        list_issues=AsyncMock(return_value=[]),
        add_issue_comment=AsyncMock(),
        resolve_issue=AsyncMock(),
        search=AsyncMock(return_value=[]),
        public_url="http://seerr.example",
    )
    radarr = SimpleNamespace(
        auto_fix=AsyncMock(),
        mark_failed=AsyncMock(),
        movie_has_file=AsyncMock(return_value=False),
    )
    sonarr = SimpleNamespace(
        auto_fix_episode=AsyncMock(),
        mark_failed_episode=AsyncMock(),
        episode_has_file=AsyncMock(return_value=False),
        season_files_present=AsyncMock(return_value=(0, 0)),
    )
    store = SimpleNamespace(
        get=AsyncMock(return_value=mapping),
        add_pending_autofix=AsyncMock(return_value=1),
        log_autofix=AsyncMock(),
        find_by_plex_username=AsyncMock(return_value=None),
        count_autofix_24h=AsyncMock(return_value=0),
    )
    bot_data: dict = {
        "admin_id": admin_id,
        "store": store,
        "seerr": seerr,
        "radarr": radarr,
        "sonarr": sonarr,
        "http_port": 8765,
        "allowlist": {admin_id},
        "settings_store": SimpleNamespace(
            settings=SimpleNamespace(daily_autofix_limit=3),
        ),
    }
    if bot_data_overrides:
        bot_data.update(bot_data_overrides)

    bot = SimpleNamespace(send_message=AsyncMock())
    application = SimpleNamespace(bot_data=bot_data, bot=bot)
    return SimpleNamespace(
        bot_data=bot_data,
        user_data=user_data if user_data is not None else {},
        application=application,
        bot=bot,
    )

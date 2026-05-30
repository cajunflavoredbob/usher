"""Tests for bot.tickets.cmd_tickets: list rendering for admin vs user,
empty list, Seerr error."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.tickets import cmd_tickets
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_mapping, make_update


def _issue(id: int = 1, **kwargs) -> IssueListItem:
    defaults = dict(
        id=id,
        issue_type=1,
        status=1,
        created_at="2026-05-29T12:00:00Z",
        tmdb_id=12345,
        media_type="movie",
        problem_season=None,
        problem_episode=None,
        created_by="someone",
    )
    defaults.update(kwargs)
    return IssueListItem(**defaults)


# --- happy paths ---


async def test_admin_lists_all_issues():
    upd = make_update(text="/tickets", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].list_issues.return_value = [_issue(id=1), _issue(id=2)]
    await cmd_tickets(upd, ctx)
    # Admin sees "All open tickets" framing.
    text = upd.message.reply_calls[0]["text"]
    assert "All open tickets" in text
    assert "#1" in text and "#2" in text
    # Admin call sets as_plex_token=None.
    args, kwargs = ctx.bot_data["seerr"].list_issues.call_args
    assert kwargs["as_plex_token"] is None


async def test_user_lists_only_their_issues():
    mapping = make_mapping(telegram_id=42, plex_token="user-token")
    upd = make_update(text="/tickets", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=mapping)
    ctx.bot_data["seerr"].list_issues.return_value = [_issue(id=7)]
    await cmd_tickets(upd, ctx)
    text = upd.message.reply_calls[0]["text"]
    assert "Your open tickets" in text
    assert "#7" in text
    # User call passes their token so Seerr scopes the list.
    args, kwargs = ctx.bot_data["seerr"].list_issues.call_args
    assert kwargs["as_plex_token"] == "user-token"


# --- empty + auth + error paths ---


async def test_unlinked_non_admin_gets_link_prompt():
    upd = make_update(text="/tickets", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=None)  # not linked
    await cmd_tickets(upd, ctx)
    text = upd.message.reply_calls[0]["text"]
    assert "/link" in text
    # Seerr was NOT called.
    ctx.bot_data["seerr"].list_issues.assert_not_called()


async def test_empty_list_for_admin():
    upd = make_update(text="/tickets", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].list_issues.return_value = []
    await cmd_tickets(upd, ctx)
    assert "No open tickets across all users" in upd.message.reply_calls[0]["text"]


async def test_empty_list_for_user():
    mapping = make_mapping(telegram_id=42, plex_token="t")
    upd = make_update(text="/tickets", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=mapping)
    ctx.bot_data["seerr"].list_issues.return_value = []
    await cmd_tickets(upd, ctx)
    assert "No open tickets" in upd.message.reply_calls[0]["text"]
    assert "across all users" not in upd.message.reply_calls[0]["text"]


async def test_seerr_error_shows_friendly_message():
    upd = make_update(text="/tickets", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].list_issues.side_effect = RuntimeError("boom")
    await cmd_tickets(upd, ctx)
    text = upd.message.reply_calls[0]["text"]
    assert "Couldn't fetch tickets" in text
    # And the raw exception isn't echoed -- user_friendly_message wrap.
    assert "boom" not in text


# --- seerr-missing guard ---


async def test_seerr_not_configured_short_circuits():
    upd = make_update(text="/tickets", user_id=999)
    ctx = make_ctx(admin_id=999, bot_data_overrides={"seerr": None})
    await cmd_tickets(upd, ctx)
    # The require_seerr helper sends its own message; no list_issues call.
    assert upd.message.reply_calls
    assert "isn't configured" in upd.message.reply_calls[0]["text"]

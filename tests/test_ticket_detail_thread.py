"""Tests for the ticket detail view rendering the reply thread (v0.11.23).

Tapping a ticket previously showed only the original submission; the reply
thread parsed by get_issue is now rendered under a "Replies:" section,
oldest-first, capped at MAX_THREAD_COMMENTS.
"""
from __future__ import annotations

from bot.tickets import tk_open, MAX_THREAD_COMMENTS
from seerr import IssueComment, IssueListItem
from tests._handler_harness import make_ctx, make_update


def _issue(**kwargs) -> IssueListItem:
    defaults = dict(
        id=42,
        issue_type=1,
        status=1,
        created_at="2026-06-16T00:00:00Z",
        tmdb_id=12345,
        media_type="movie",
        problem_season=None,
        problem_episode=None,
        created_by="User2",
        description="Michael won't play",
    )
    defaults.update(kwargs)
    return IssueListItem(**defaults)


async def _open(ctx):
    upd = make_update(callback_data="tkopen:42", user_id=999)
    await tk_open(upd, ctx)
    return upd.callback_query.message.reply_calls[0]["text"]


async def test_replies_shown_oldest_first():
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(comments=[
        IssueComment(author="User1", message="looking into it", created_at=""),
        IssueComment(author="User2", message="any update?", created_at=""),
    ])
    text = await _open(ctx)
    assert "Description:" in text
    assert "Michael won&#x27;t play" in text or "Michael won't play" in text
    assert "Replies:" in text
    # Both replies present, in order.
    assert text.index("looking into it") < text.index("any update?")
    assert "User1" in text and "User2" in text


async def test_no_replies_section_when_thread_empty():
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(comments=[])
    text = await _open(ctx)
    assert "Replies" not in text


async def test_thread_capped_with_note():
    ctx = make_ctx(admin_id=999)
    many = [IssueComment(author=f"u{i}", message=f"msg{i}", created_at="")
            for i in range(MAX_THREAD_COMMENTS + 5)]
    ctx.bot_data["seerr"].get_issue.return_value = _issue(comments=many)
    text = await _open(ctx)
    assert f"last {MAX_THREAD_COMMENTS} of {MAX_THREAD_COMMENTS + 5}" in text
    # Oldest few dropped, newest kept.
    assert "msg0" not in text
    assert f"msg{MAX_THREAD_COMMENTS + 4}" in text


async def test_reply_author_and_message_escaped():
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(comments=[
        IssueComment(author="<b>x</b>", message="a & b <i>", created_at=""),
    ])
    text = await _open(ctx)
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "a &amp; b &lt;i&gt;" in text

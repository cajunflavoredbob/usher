"""A resolved Ticket shows its status, drops Fix and Close, and refuses Fix."""
from __future__ import annotations

from bot.tickets import resolve_fix_context, tk_back, tk_open
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_update


def _ticket(**kwargs) -> IssueListItem:
    defaults = dict(
        id=42, issue_type=1, status=1, created_at="2026-06-16T00:00:00Z",
        tmdb_id=12345, media_type="movie", problem_season=None,
        problem_episode=None, created_by="User2", description="No audio",
    )
    defaults.update(kwargs)
    return IssueListItem(**defaults)


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _open(status: int):
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _ticket(status=status)
    upd = make_update(callback_data="tkopen:42", user_id=999)
    await tk_open(upd, ctx)
    call = upd.callback_query.message.reply_calls[0]
    return call["text"], call["reply_markup"]


async def test_open_ticket_shows_open_status_and_all_admin_buttons():
    text, kb = await _open(1)
    assert "<b>Status:</b> Open" in text
    cbs = _callbacks(kb)
    assert len(cbs) == 3  # Reply, Fix, Close


async def test_resolved_ticket_shows_status_and_drops_fix_and_close():
    text, kb = await _open(2)
    assert "<b>Status:</b> Resolved" in text
    cbs = _callbacks(kb)
    assert len(cbs) == 1 and cbs[0].endswith(":42")
    open_text, open_kb = await _open(1)
    assert len(_callbacks(open_kb)) == 3
    assert cbs[0] == _callbacks(open_kb)[0]  # the Reply button stays


async def test_resolved_ticket_dm_back_keeps_history_row_without_fix():
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _ticket(status=2)
    upd = make_update(callback_data="tkbd:42", user_id=999)
    await tk_back(upd, ctx)
    kb = upd.callback_query.edits[0]["reply_markup"]
    cbs = _callbacks(kb)
    assert len(cbs) == 2  # Reply + History, no Fix or Close
    assert not any(c.startswith("tkf:") or c.startswith("tkc:") for c in cbs)


async def test_fix_context_refuses_resolved_ticket():
    ctx = make_ctx(admin_id=999)
    seerr = ctx.bot_data["seerr"]
    seerr.get_issue.return_value = _ticket(status=2)
    fix, err = await resolve_fix_context(seerr, 42, action_name="Fix")
    assert fix is None
    assert err is not None and "already resolved" in err
    seerr.get_media_title.assert_not_called()


async def test_fix_context_allows_open_ticket():
    ctx = make_ctx(admin_id=999)
    seerr = ctx.bot_data["seerr"]
    seerr.get_issue.return_value = _ticket(status=1)
    seerr.get_media_title.return_value = ("Foo", "2020")
    fix, err = await resolve_fix_context(seerr, 42, action_name="Fix")
    assert err is None and fix is not None

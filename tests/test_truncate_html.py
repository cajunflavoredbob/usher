"""truncate_message must leave Telegram-valid HTML: no cut inside a tag or
entity, and every tag left open by the cut is closed again. Also covers the
comment DM and new-Ticket DM, which pass their text through the same limit."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock

from bot.shared import truncate_message
from bot.webhook_handlers import handle_seerr_comment, handle_seerr_reported
from tests._handler_harness import make_ctx, make_link


def _balanced(text: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", text):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def test_short_text_unchanged():
    assert truncate_message("<i>hi</i>") == "<i>hi</i>"


def test_multiline_italic_cut_is_closed():
    body = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
    text = f"head\n<i>\"{body}\"</i>"
    out = truncate_message(text)
    assert len(out) <= 4000
    assert _balanced(out)
    assert out.endswith("(truncated)")


def test_long_single_line_never_cuts_inside_tag_or_entity():
    text = "<b>T</b> <i>" + "a&amp;b " * 1000 + "</i>"
    for limit in range(100, 140):
        out = truncate_message(text, limit)
        assert len(out) <= limit
        assert _balanced(out)
        assert not re.search(r"&\w{0,5}(?:\W|$)", out.replace("&amp;", ""))


def test_nested_tags_closed_in_reverse_order():
    out = truncate_message("<b><i>" + "z" * 500 + "</i></b>", 100)
    assert "</i></b>" in out
    assert _balanced(out)


def _app():
    app = make_ctx(admin_id=999).application
    app.bot_data["store"].get = AsyncMock(
        return_value=make_link(telegram_id=999, plex_username="adm"))
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=111, plex_username="user2"))
    return app


async def test_long_multiline_comment_dm_stays_valid_and_under_cap():
    app = _app()
    comment = "\n".join("c" * 80 for _ in range(60))
    await handle_seerr_comment(app, {
        "notification_type": "ISSUE_COMMENT",
        "issue": {"issue_id": 7, "reportedBy_username": "user2",
                  "issue_status": "OPEN"},
        "comment": {"commentedBy_username": "user2", "comment_message": comment},
        "media": {},
    })
    text = app.bot.send_message.call_args.kwargs["text"]
    assert len(text) <= 4096
    assert _balanced(text)


async def test_long_single_line_description_dm_stays_under_cap():
    app = _app()
    await handle_seerr_reported(app, {
        "notification_type": "ISSUE_CREATED",
        "message": "d" * 4090,
        "issue": {"issue_id": 8, "issue_type": "AUDIO",
                  "reportedBy_username": "user2"},
        "media": {"media_type": "movie", "tmdbId": 5},
    })
    text = app.bot.send_message.call_args.kwargs["text"]
    assert len(text) <= 4096
    assert _balanced(text)


def test_closers_longer_than_limit_do_not_hang():
    out = truncate_message("<b>" * 1400 + "x")
    assert len(out) <= 4000
    assert truncate_message("x" * 50, 10).endswith("(truncated)")


def test_cut_never_lands_inside_a_tag():
    text = "a" * 90 + '<a href="https://example.com/x">link</a>' + "b" * 200
    for limit in range(100, 140):
        out = truncate_message(text, limit)
        assert _balanced(out)
        assert "<a href" not in out or "</a>" in out


def test_plain_text_mode_keeps_literal_angle_brackets():
    text = "Sam <3 movies\n" + "y" * 300
    out = truncate_message(text, 200, html=False)
    assert out.startswith("Sam <3 movies")
    assert "</" not in out

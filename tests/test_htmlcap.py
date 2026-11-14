"""cap_html and clip: keep HTML messages under Telegram's limit."""
from __future__ import annotations

import re

import pytest

from bot.htmlcap import SAFE_LIMIT, TELEGRAM_LIMIT, cap_html, clip


def _well_formed(text: str) -> None:
    # No dangling "<" or "&" at the end, and every tag closed.
    assert text.rfind("<") <= text.rfind(">")
    # Every "&" starts a complete entity (the output ends with the tail, so
    # a split entity sits mid-text, not at the end).
    for m in re.finditer(r"&", text):
        assert re.match(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);", text[m.start():])
    opens = re.findall(r"<([a-z]+)[^>]*>", text)
    closes = re.findall(r"</([a-z]+)>", text)
    assert sorted(opens) == sorted(closes)


def test_short_text_is_unchanged():
    assert cap_html("<b>hi</b>") == "<b>hi</b>"


def test_long_text_is_cut_at_a_line_with_a_more_tail():
    text = "\n".join(f"  • <b>Radarr</b>: line {i} &amp; more" for i in range(500))
    out = cap_html(text)
    assert len(out) <= SAFE_LIMIT
    assert re.search(r"\n\(\+\d+ more lines\)$", out)
    kept = out.count("\n")
    assert f"(+{500 - kept} more lines)" in out
    _well_formed(out)


@pytest.mark.parametrize("pad", range(0, 40))
def test_a_cut_never_lands_inside_a_tag_or_entity(pad):
    # One huge line (no newline to cut at), tags and entities swept across
    # the cut point by varying the padding.
    unit = "<b>x</b>&amp;<a href=\"https://example.com/p?a=1&amp;b=2\">l</a>"
    text = "<b>" + "y" * pad + unit * 300 + "</b>"
    out = cap_html(text)
    assert len(out) <= SAFE_LIMIT
    _well_formed(out)


def test_unclosed_tags_are_closed_in_order():
    out = cap_html("<b><i>" + "z" * 5000)
    assert "</i></b>" in out
    _well_formed(out)


def test_result_fits_telegram_for_any_limit_default():
    assert len(cap_html("a" * 20000)) <= TELEGRAM_LIMIT


def test_clip_shortens_plain_text_with_an_ellipsis():
    assert clip("short", 10) == "short"
    out = clip("x" * 50, 10)
    assert len(out) == 10 and out.endswith("…")

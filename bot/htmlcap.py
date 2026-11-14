"""Keep an HTML message under Telegram's 4096-character limit.

Lives apart from bot.shared so bot.alarms can use it without pulling in the
Telegram imports."""
from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
# Room under the hard limit for closing tags and the "(+N more)" tail.
SAFE_LIMIT = 4000
LINE_LIMIT = 300

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_TAIL_RESERVE = 80


def clip(text: str, limit: int = LINE_LIMIT) -> str:
    """Shorten plain text (before it is HTML-escaped) to `limit` characters,
    ending in an ellipsis when something was cut."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:max(limit - 1, 0)].rstrip() + "…"


def _open_tags(fragment: str) -> list[str]:
    stack: list[str] = []
    for m in _TAG.finditer(fragment):
        name = m.group(2).lower()
        if m.group(1):
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        elif not m.group(0).endswith("/>"):
            stack.append(name)
    return stack


def cap_html(text: str, limit: int = SAFE_LIMIT) -> str:
    """Return `text` unchanged when it fits. Otherwise cut it at a line
    break (or, failing that, a safe character) so the result, closing tags
    and a "(+N more lines)" tail included, stays within `limit`. Never cuts
    inside a tag or an entity, and closes any tag left open."""
    if len(text) <= limit:
        return text
    budget = max(limit - _TAIL_RESERVE, 0)
    head = text[:budget]
    nl = head.rfind("\n")
    if nl >= budget // 2:
        head = head[:nl]
    else:
        # Do not end inside a tag or an entity.
        lt, gt = head.rfind("<"), head.rfind(">")
        if lt > gt:
            head = head[:lt]
        amp, semi = head.rfind("&"), head.rfind(";")
        if amp > semi and len(head) - amp <= 12:
            head = head[:amp]
    dropped = text[len(head):].strip("\n")
    more = dropped.count("\n") + 1 if dropped.strip() else 0
    closers = "".join(f"</{t}>" for t in reversed(_open_tags(head)))
    tail = f"\n(+{more} more lines)" if more else ""
    return head.rstrip() + closers + tail

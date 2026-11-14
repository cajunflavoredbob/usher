"""The "Request remaining seasons" button: shown on a show's detail card and
availability notice to a Linked user when the season availability lookup
finds at least one season a new Request would stick for. The tap handlers
live in bot.request_flow (they share its confirm and error wording)."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton

from bot.callback_prefixes import RQ_REMAINING

logger = logging.getLogger("usher")


def remaining_button(tmdb_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("➕ Request remaining seasons",
                                callback_data=f"{RQ_REMAINING}:{tmdb_id}")


async def requestable_seasons(seerr, tmdb_id: int) -> "list[int] | None":
    """Season numbers a new Request would stick for, sorted; None when the
    lookup failed (callers show no button rather than guess)."""
    try:
        seasons = await seerr.get_tv_season_availability(tmdb_id)
    except Exception:
        logger.debug("remaining-seasons lookup failed", exc_info=True)
        return None
    return sorted(s.season_number for s in seasons if s.requestable)

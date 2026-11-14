"""The Requests Hub (/request with no title): New request, My requests,
Trending and Subscriptions. See ADR 0009 and the Hub entry in GLOSSARY.md.

Holds only the Hub's render function and registration. Each button's handler
lives in the module that owns its flow, and each of those modules imports
REQUESTS_HUB from here, which registers the Hub as a side effect."""
from __future__ import annotations

from telegram import InlineKeyboardButton

from bot.callback_prefixes import (
    REQUESTS_FOLLOWS,
    REQUESTS_MINE,
    REQUESTS_NEW,
    REQUESTS_SUBSCRIPTIONS,
    REQUESTS_TRENDING,
    REQUESTS_WEEK,
)
from bot.hub import HubView, register_hub

REQUESTS_HUB = "requests"


async def render_requests_hub(update, ctx) -> HubView:
    return HubView(
        text=("🎬 <b>Requests</b>\nAsk for a movie or show, check on your "
              "requests, browse what's trending, see your "
              "Subscriptions and the shows you follow, or what's coming "
              "this week."),
        buttons=[
            InlineKeyboardButton("➕ New request", callback_data=REQUESTS_NEW),
            InlineKeyboardButton("📥 My requests", callback_data=REQUESTS_MINE),
            InlineKeyboardButton("🔥 Trending", callback_data=REQUESTS_TRENDING),
            InlineKeyboardButton("🔔 Subscriptions",
                                 callback_data=REQUESTS_SUBSCRIPTIONS),
            InlineKeyboardButton("📺 Follows", callback_data=REQUESTS_FOLLOWS),
            InlineKeyboardButton("🗓 Coming this week",
                                 callback_data=REQUESTS_WEEK),
        ],
    )


register_hub(REQUESTS_HUB, "Requests", render_requests_hub)

"""The Tickets Hub (/tickets): Report a problem and the ticket list ("My
tickets" for a User, "Open tickets" for the Admin). See ADR 0009 and the Hub
entry in GLOSSARY.md.

Holds only the Hub's render function, its registration and the detail card's
back-to-list button. The button handlers live in the modules that own their
flows (bot.issue_flow, bot.tickets), and each imports TICKETS_HUB from here,
which registers the Hub as a side effect."""
from __future__ import annotations

from telegram import InlineKeyboardButton

from bot.callback_prefixes import TICKETS_LIST, TICKETS_REPORT
from bot.hub import HUBS, HubView, register_hub

TICKETS_HUB = "tickets"


async def render_tickets_hub(update, ctx) -> HubView:
    is_admin = update.effective_user.id == ctx.bot_data.get("admin_id")
    whose = "open tickets" if is_admin else "your tickets"
    return HubView(
        text=("🎫 <b>Tickets</b>\nReport a problem with a movie or show, or "
              f"check on {whose}."),
        buttons=[
            InlineKeyboardButton("📝 Report a problem",
                                 callback_data=TICKETS_REPORT),
            InlineKeyboardButton(
                "📋 Open tickets" if is_admin else "📋 My tickets",
                callback_data=TICKETS_LIST),
        ],
    )


register_hub(TICKETS_HUB, "Tickets", render_tickets_hub)


def list_back_button() -> InlineKeyboardButton:
    """"⬅️ Tickets" on a detail card opened from the list: shows the list
    again (the list's own "⬅️ Tickets" goes back to the Hub)."""
    return InlineKeyboardButton(f"⬅️ {HUBS[TICKETS_HUB].label}",
                                callback_data=TICKETS_LIST)

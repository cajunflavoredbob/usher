"""The Admin Hub (/admin): Invite, Shares and Status, for the Admin in a DM
only. See ADR 0009 and the Hub, Share, Invite and Uninvite entries in
GLOSSARY.md.

Holds the Hub's render function, its registration, /admin itself and the
Admin-and-DM gate that the Admin commands and the Hub's Invite, Shares and
Status buttons run first (the Share confirm card's Remove and Keep check
Admin only). The button handlers live in the modules that own their flows (bot.invite_flow for
Invite and Shares, bot.app for Status), and each imports ADMIN_HUB from here,
which registers the Hub as a side effect."""
from __future__ import annotations

from telegram import InlineKeyboardButton

from bot.callback_prefixes import ADMIN_INVITE, ADMIN_SHARES, ADMIN_STATUS
from bot.hub import HubView, register_hub, send_hub

ADMIN_HUB = "admin"


async def render_admin_hub(update, ctx) -> HubView:
    return HubView(
        text=("🛠 <b>Admin</b>\nInvite someone to your Plex server, see and "
              "remove Shares, or check connections."),
        buttons=[
            InlineKeyboardButton("📨 Invite", callback_data=ADMIN_INVITE),
            InlineKeyboardButton("👥 Shares", callback_data=f"{ADMIN_SHARES}:0"),
            InlineKeyboardButton("📶 Status", callback_data=ADMIN_STATUS),
        ],
    )


register_hub(ADMIN_HUB, "Admin", render_admin_hub)


async def refuse_outside_admin_dm(update, ctx) -> bool:
    """Refuse anyone but the Admin ("Admin only.") and the Admin outside a
    private chat ("DM me /admin"). A command gets the refusal as a reply, a
    button tap as a toast. Returns True when it refused."""
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        text = "Admin only."
    elif update.effective_chat.type != "private":
        text = "DM me /admin"
    else:
        return False
    q = update.callback_query
    if q is not None:
        await q.answer(text, show_alert=True)
    else:
        await update.effective_message.reply_text(text)
    return True


async def cmd_admin(update, ctx) -> None:
    if await refuse_outside_admin_dm(update, ctx):
        return
    await send_hub(update, ctx, ADMIN_HUB)

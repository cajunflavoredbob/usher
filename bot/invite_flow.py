"""/invite and /uninvite: admin-only Plex server provisioning from the bot.

/invite shares the Plex server with an email or Plex username, with a
library checklist (all libraries pre-selected; the same multi-select idiom
as the request flow's season picker). Seerr picks the new user up on their
first Plex sign-in, so no Seerr-side call is needed here.

/uninvite revokes a share (or cancels a pending invite) after an explicit
confirmation.

Both reuse the admin's own /link Plex token: this surface only exists in
the bot, and the admin can't meaningfully operate it unlinked anyway. All
mutations require an explicit confirm tap; every listing shown is fetched
live from plex.tv."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from http_util import NotFoundAPIError, user_friendly_message
from plex import PlexClient
from store import UserStore

from bot.callback_prefixes import (
    INV_ALL,
    INV_CANCEL,
    INV_DONE,
    INV_GO,
    INV_LIB,
    UNINV_GO,
    UNINV_KEEP,
)
from bot.shared import (
    INV_CONFIRM,
    INV_EMAIL,
    INV_LIBS,
    record_btn,
    send_typing,
)
from const import INVITE_FLOW_TIMEOUT_S

logger = logging.getLogger("usher")

_STALE_INVITE_TOAST = "That invite menu is stale — /invite to start over."

# inv_version is deliberately NOT here: the stale-menu guard depends on the
# counter surviving every reset (same lesson as rq_search_version).
_INV_KEYS = ("inv_email", "inv_sections", "inv_selected", "inv_machine",
             "inv_server_name", "inv_submitting")


def _looks_like_email(s: str) -> bool:
    """Plex accepts an email or a Plex username; the loose shape check only
    guards obvious paste accidents."""
    return 3 <= len(s) <= 254 and " " not in s


def _current_version(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return ctx.user_data.get("inv_version") or 0


async def _admin_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      command: str = "invite") -> Optional[str]:
    """Admin + DM + linked. Returns the admin's Plex token or None (after
    messaging the user). `command` keeps the copy honest for /uninvite."""
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        await update.effective_message.reply_text("Admin only.")
        return None
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            f"DM me /{command} — email addresses don't belong in a group chat.")
        return None
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await update.effective_message.reply_text(
            "I need your Plex sign-in for this — run /link first. Sharing "
            "changes are made from YOUR Plex account.")
        return None
    return mapping.plex_token


async def _invite_expect_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Contact cards, photos, and voice notes can't be read here."""
    await update.effective_message.reply_text(
        "I can only read text here — type the email or username, or /cancel.")
    return INV_EMAIL


async def _invite_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    for key in _INV_KEYS:
        ctx.user_data.pop(key, None)
    try:
        if update is not None and update.effective_chat is not None:
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏱️ The /invite flow timed out. Start again with /invite.",
            )
    except Exception:
        logger.debug("invite-timeout notice failed (non-fatal)", exc_info=True)
    return ConversationHandler.END


def _invite_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("invite", invite_start)],
        states={
            INV_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, invite_email),
                MessageHandler(~filters.COMMAND, _invite_expect_text),
            ],
            INV_LIBS: [
                CallbackQueryHandler(invite_toggle_lib, pattern=fr"^{INV_LIB}:"),
                CallbackQueryHandler(invite_select_all, pattern=fr"^{INV_ALL}:"),
                CallbackQueryHandler(invite_libs_done, pattern=fr"^{INV_DONE}:"),
            ],
            INV_CONFIRM: [
                CallbackQueryHandler(invite_send, pattern=fr"^{INV_GO}:"),
            ],
            ConversationHandler.TIMEOUT: [TypeHandler(Update, _invite_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", invite_cancel),
            # Version-suffixed (invx:<v>); a stale card's Cancel toasts
            # instead of killing the active invite.
            CallbackQueryHandler(invite_cancel, pattern=fr"^{INV_CANCEL}:"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="invite",
        persistent=False,
        conversation_timeout=INVITE_FLOW_TIMEOUT_S,
    )


async def invite_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    token = await _admin_gate(update, ctx)
    if token is None:
        return ConversationHandler.END
    arg = " ".join(ctx.args or ()).strip()
    if arg:
        return await _accept_email(update, ctx, arg)
    await update.effective_message.reply_text(
        "Who am I inviting? Reply with their email (or Plex username).")
    return INV_EMAIL


async def invite_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _accept_email(update, ctx,
                               update.effective_message.text.strip())


async def _accept_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        email: str) -> int:
    if not _looks_like_email(email):
        await update.effective_message.reply_text(
            "That doesn't look like an email or username. Try again, or /cancel.")
        return INV_EMAIL
    ctx.user_data["inv_email"] = email
    await send_typing(update, ctx)
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        machine_id, server_name = await plex.get_owned_server(mapping.plex_token)
        sections = await plex.get_library_sections(mapping.plex_token, machine_id)
    except LookupError:
        await update.effective_message.reply_text(
            "Your linked Plex account doesn't own a server — sharing needs "
            "the server owner's account. /link with the owner account first.")
        _clear(ctx)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("plex server/section lookup failed")
        await update.effective_message.reply_text(
            f"Couldn't read your Plex server. {user_friendly_message(exc)}")
        _clear(ctx)
        return ConversationHandler.END
    if not sections:
        await update.effective_message.reply_text(
            "Your Plex server reports no libraries; nothing to share.")
        _clear(ctx)
        return ConversationHandler.END
    ctx.user_data["inv_machine"] = machine_id
    ctx.user_data["inv_server_name"] = server_name
    ctx.user_data["inv_sections"] = sections
    ctx.user_data["inv_selected"] = {s.id for s in sections}
    version = _current_version(ctx) + 1
    ctx.user_data["inv_version"] = version
    sent = await update.effective_message.reply_text(
        f"Inviting <b>{html.escape(email)}</b> to "
        f"<b>{html.escape(server_name)}</b>.\n\n"
        "Which libraries? All are selected — tap to exclude, then ▶️ Done.",
        reply_markup=_lib_keyboard(sections, ctx.user_data["inv_selected"],
                                   version),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return INV_LIBS


_TYPE_EMOJI = {"movie": "🎬", "show": "📺", "artist": "🎵", "photo": "🖼"}


def _lib_keyboard(sections: list, selected: set,
                  version: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for s in sections:
        mark = "☑ " if s.id in selected else ""
        emoji = _TYPE_EMOJI.get(s.type, "📁")
        row.append(InlineKeyboardButton(
            f"{mark}{emoji} {s.title}",
            callback_data=f"{INV_LIB}:{version}:{s.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📦 Select all", callback_data=f"{INV_ALL}:{version}"),
        InlineKeyboardButton("▶️ Done", callback_data=f"{INV_DONE}:{version}"),
    ])
    rows.append([InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"{INV_CANCEL}:{version}")])
    return InlineKeyboardMarkup(rows)


def _stale(ctx, version: int) -> bool:
    return version != _current_version(ctx)


async def invite_toggle_lib(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        _, version_s, sid_s = q.data.split(":")
        version, sid = int(version_s), int(sid_s)
    except (ValueError, IndexError):
        await q.answer()
        return INV_LIBS
    if _stale(ctx, version):
        await q.answer(_STALE_INVITE_TOAST,
                       show_alert=True)
        return INV_LIBS
    selected: set = ctx.user_data.get("inv_selected") or set()
    if sid in selected:
        selected.discard(sid)
    else:
        selected.add(sid)
    ctx.user_data["inv_selected"] = selected
    await q.answer()
    try:
        await q.edit_message_reply_markup(_lib_keyboard(
            ctx.user_data["inv_sections"], selected, version))
    except Exception:
        logger.debug("invite keyboard edit failed (non-fatal)", exc_info=True)
    return INV_LIBS


async def invite_select_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer()
        return INV_LIBS
    if _stale(ctx, version):
        await q.answer(_STALE_INVITE_TOAST,
                       show_alert=True)
        return INV_LIBS
    sections = ctx.user_data.get("inv_sections") or []
    ctx.user_data["inv_selected"] = {s.id for s in sections}
    await q.answer("All libraries selected")
    try:
        await q.edit_message_reply_markup(_lib_keyboard(
            sections, ctx.user_data["inv_selected"], version))
    except Exception:
        logger.debug("invite keyboard edit failed (non-fatal)", exc_info=True)
    return INV_LIBS


async def invite_libs_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer()
        return INV_LIBS
    if _stale(ctx, version):
        await q.answer(_STALE_INVITE_TOAST,
                       show_alert=True)
        return INV_LIBS
    selected: set = ctx.user_data.get("inv_selected") or set()
    if not selected:
        await q.answer("Pick at least one library.", show_alert=True)
        return INV_LIBS
    await q.answer()
    sections = ctx.user_data.get("inv_sections") or []
    titles = [s.title for s in sections if s.id in selected]
    email = ctx.user_data.get("inv_email", "?")
    sent = await q.edit_message_text(
        f"Invite <b>{html.escape(email)}</b> to "
        f"<b>{html.escape(ctx.user_data.get('inv_server_name', 'Plex'))}</b> "
        f"with {len(titles)} librar{'y' if len(titles) == 1 else 'ies'}:\n"
        f"{html.escape(', '.join(titles))}\n\n"
        "Plex will email them an invite from your account.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send invite",
                                 callback_data=f"{INV_GO}:{version}"),
            InlineKeyboardButton("🛑 Cancel",
                                 callback_data=f"{INV_CANCEL}:{version}"),
        ]]),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return INV_CONFIRM


async def invite_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer()
        return ConversationHandler.END
    if _stale(ctx, version):
        await q.answer(_STALE_INVITE_TOAST,
                       show_alert=True)
        return INV_CONFIRM
    await q.answer()
    if ctx.user_data.get("inv_submitting"):
        return ConversationHandler.END
    ctx.user_data["inv_submitting"] = True
    try:
        return await _invite_send_inner(update, ctx)
    finally:
        ctx.user_data.pop("inv_submitting", None)


async def _invite_send_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    email = ctx.user_data.get("inv_email")
    machine = ctx.user_data.get("inv_machine")
    selected = ctx.user_data.get("inv_selected") or set()
    if not (email and machine and selected):
        await update.callback_query.edit_message_text(
            "Lost the invite draft. /invite to start over.")
        _clear(ctx)
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await update.callback_query.edit_message_text(
            "Your /link is no longer usable. /link then /invite again.")
        _clear(ctx)
        return ConversationHandler.END
    q = update.callback_query
    try:
        await q.edit_message_text(
            f"Sending invite to <b>{html.escape(email)}</b>…",
            parse_mode="HTML")
    except Exception:
        logger.debug("invite working-state edit failed (non-fatal)",
                     exc_info=True)
    await send_typing(update, ctx)
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        await plex.invite_to_server(mapping.plex_token, machine, email,
                                    sorted(selected))
    except Exception as exc:
        logger.exception("plex invite failed")
        await q.edit_message_text(
            f"Couldn't send the invite. {user_friendly_message(exc)}")
        _clear(ctx)
        return ConversationHandler.END

    lib_word = "library" if len(selected) == 1 else "libraries"
    await q.edit_message_text(
        f"📨 Invite sent to <b>{html.escape(email)}</b> "
        f"({len(selected)} {lib_word}).\n"
        "They'll get an email from Plex; once they accept and sign in, "
        "Seerr picks them up and /link works for them.",
        parse_mode="HTML",
    )
    _clear(ctx)
    return ConversationHandler.END


def _clear(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _INV_KEYS:
        ctx.user_data.pop(key, None)


async def invite_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        try:
            version = int(q.data.split(":")[1])
        except (ValueError, IndexError):
            version = None
        if version is not None and version != _current_version(ctx):
            # Cancel on a superseded card must not end the ACTIVE invite.
            await q.answer(_STALE_INVITE_TOAST, show_alert=True)
            return None
        await q.answer("Cancelled")
        await q.edit_message_text("Cancelled. /invite to start over.")
    else:
        await update.effective_message.reply_text(
            "Cancelled. /invite to start over.")
    _clear(ctx)
    return ConversationHandler.END


# --- /uninvite (non-conversation: lookup -> confirm button -> delete) --------

async def cmd_uninvite(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    token = await _admin_gate(update, ctx, command="uninvite")
    if token is None:
        return
    query = " ".join(ctx.args or ()).strip().lower()
    if not query:
        await update.effective_message.reply_text(
            "Usage: /uninvite <email or username> — removes their access "
            "(or cancels a pending invite).")
        return
    await send_typing(update, ctx)
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        machine_id, _ = await plex.get_owned_server(token)
        shares = await plex.list_shares(token, machine_id)
    except LookupError:
        await update.effective_message.reply_text(
            "Your linked Plex account doesn't own a server — sharing needs "
            "the server owner's account.")
        return
    except Exception as exc:
        logger.exception("share listing failed")
        await update.effective_message.reply_text(
            f"Couldn't list shares. {user_friendly_message(exc)}")
        return
    matches = [s for s in shares
               if query in s.email.lower() or query in s.username.lower()]
    if not matches:
        await update.effective_message.reply_text(
            f'No share matches "{query}". Nothing changed.')
        return
    if len(matches) > 1:
        listing = "\n".join(f"• {html.escape(s.email or s.username)}"
                            for s in matches[:10])
        if len(matches) > 10:
            listing += f"\n…and {len(matches) - 10} more"
        await update.effective_message.reply_text(
            f"That matches {len(matches)} shares:\n{listing}\n\n"
            "Be more specific.", parse_mode="HTML")
        return
    share = matches[0]
    who = share.email or share.username
    if share.accepted:
        state = "accepted share"
        consequence = ("They lose the server immediately; Seerr and their "
                       "/link die with their Plex access.")
    else:
        state = "PENDING invite"
        consequence = "The invite will be cancelled before they accept it."
    flag = "a" if share.accepted else "p"
    sent = await update.effective_message.reply_text(
        f"Remove <b>{html.escape(who)}</b>'s access ({state})?\n{consequence}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Remove",
                                 callback_data=f"{UNINV_GO}:{share.id}:{flag}"),
            InlineKeyboardButton("🛑 Keep", callback_data=UNINV_KEEP),
        ]]),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)


async def uninvite_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        await q.answer("Admin only.", show_alert=True)
        return
    try:
        parts = q.data.split(":")
        share_id = int(parts[1])
        was_pending = len(parts) > 2 and parts[2] == "p"
    except (ValueError, IndexError):
        await q.answer()
        return
    await q.answer()
    # Double-tap guard: the second tap lands before the first edit strips
    # the keyboard; without this it would 404 and overwrite the success
    # message with a failure.
    if ctx.user_data.get("uninv_submitting"):
        return
    ctx.user_data["uninv_submitting"] = True
    try:
        await _uninvite_go_inner(update, ctx, share_id, was_pending)
    finally:
        ctx.user_data.pop("uninv_submitting", None)


async def _uninvite_go_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                             share_id: int, was_pending: bool) -> None:
    q = update.callback_query
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await q.edit_message_text("Your /link is no longer usable; /link first.")
        return
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        await plex.remove_share(mapping.plex_token, share_id)
    except NotFoundAPIError:
        # Already gone (removed elsewhere, or a racing duplicate tap):
        # the desired end state holds, so report it as done.
        await q.edit_message_text("✅ Already removed.")
        return
    except Exception as exc:
        logger.exception("share removal failed")
        await q.edit_message_text(
            f"Couldn't remove the share. {user_friendly_message(exc)}")
        return
    await q.edit_message_text("✅ Invite cancelled." if was_pending
                              else "✅ Access removed.")


async def uninvite_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The /uninvite confirm card's Keep button (its own prefix, so it can
    never collide with the invite conversation's cancel). Admin-gated: the
    handler edits whatever message the callback references."""
    q = update.callback_query
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer("Cancelled")
    try:
        await q.edit_message_text("Cancelled — nothing changed.")
    except Exception:
        logger.debug("uninvite cancel edit failed (non-fatal)", exc_info=True)

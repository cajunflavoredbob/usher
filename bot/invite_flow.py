"""/invite, /uninvite and the Admin Hub's Invite and Shares: admin-only
Plex server provisioning from the bot. The commands and the Hub's Invite,
Shares, Share view and Remove buttons answer only to the Admin in a DM; the
invite's later buttons follow that gated entry, and the confirm card's
Remove and Keep check Admin only.

/invite (or the Hub's Invite) shares the Plex server with an email or Plex
username, with a library checklist (all libraries pre-selected; the same
multi-select idiom as the request flow's season picker). Seerr picks the new
user up on their first Plex sign-in, so no Seerr-side call is needed here.
An invite started from the Hub ends with a back button to it.

The Hub's Shares lists every Share, 8 per page; tapping one shows its view,
whose Remove revokes it (or cancels a pending invite) after an explicit
confirmation. /uninvite <text> lands on the same view, or on the matching
Shares as buttons.

All of these reuse the admin's own /link Plex token: this surface only exists in
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
from plex import PlexClient, PlexShare
from store import UserStore

from bot.admin_hub import ADMIN_HUB, refuse_outside_admin_dm
from bot.callback_prefixes import (
    ADMIN_INVITE,
    ADMIN_SHARES,
    INV_ALL,
    INV_CANCEL,
    INV_DONE,
    INV_GO,
    INV_LIB,
    SHARE_MATCHES,
    SHARE_REMOVE,
    SHARE_VIEW,
    UNINV_GO,
    UNINV_KEEP,
)
from bot.hub import (back_button, end_flow, end_markup, hub_origin,
                     set_hub_origin)
from bot.shared import (
    INV_CONFIRM,
    INV_EMAIL,
    INV_LIBS,
    edit_or_send,
    force_end_flow_conversations,
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


_NOT_LINKED = ("I need your Plex sign-in for this — run /link first. Sharing "
               "changes are made from YOUR Plex account.")


def _admin_origin(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """ADMIN_HUB when the running invite was started from the Admin Hub. Any
    other Hub's mark counts as none."""
    return ADMIN_HUB if hub_origin(ctx) == ADMIN_HUB else None


async def _admin_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      hub: Optional[str] = None) -> Optional[str]:
    """Admin + DM + linked. Returns the admin's Plex token or None (after
    messaging the user). The link refusal carries `hub`'s back button."""
    if await refuse_outside_admin_dm(update, ctx):
        return None
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await end_flow(update, ctx, _NOT_LINKED, hub)
        return None
    return mapping.plex_token


async def _invite_expect_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Contact cards, photos, and voice notes can't be read here."""
    await update.effective_message.reply_text(
        "I can only read text here — type the email or username, or /cancel.")
    return INV_EMAIL


async def _invite_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    hub = _admin_origin(ctx)
    for key in _INV_KEYS:
        ctx.user_data.pop(key, None)
    try:
        if update is not None and update.effective_chat is not None:
            if hub is None:
                await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The /invite flow timed out. Start again with /invite.",
                )
            else:
                sent = await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The invite timed out.",
                    reply_markup=end_markup(hub, ADMIN_INVITE),
                )
                record_btn(ctx.application, update.effective_user.id, sent)
    except Exception:
        logger.debug("invite-timeout notice failed (non-fatal)", exc_info=True)
    return ConversationHandler.END


def _invite_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("invite", invite_start),
            CallbackQueryHandler(invite_hub_start, pattern=fr"^{ADMIN_INVITE}$"),
        ],
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
    set_hub_origin(ctx, None)
    token = await _admin_gate(update, ctx)
    if token is None:
        return ConversationHandler.END
    arg = " ".join(ctx.args or ()).strip()
    if arg:
        return await _accept_email(update, ctx, arg)
    await update.effective_message.reply_text(
        "Who am I inviting? Reply with their email (or Plex username).")
    return INV_EMAIL


async def invite_hub_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin Hub "Invite": the /invite email prompt, edited into the tapped
    Hub message. Same gate as typed /invite; its link refusal carries the
    back button."""
    if await refuse_outside_admin_dm(update, ctx):
        return ConversationHandler.END
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    set_hub_origin(ctx, ADMIN_HUB)
    if await _admin_gate(update, ctx, ADMIN_HUB) is None:
        return ConversationHandler.END
    await edit_or_send(
        q, "Who am I inviting? Reply with their email (or Plex username).")
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
        await end_flow(update, ctx,
                       "Your linked Plex account doesn't own a server — sharing needs "
                       "the server owner's account. /link with the owner account first.",
                       _admin_origin(ctx))
        _clear(ctx)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("plex server/section lookup failed")
        await end_flow(update, ctx,
                       f"Couldn't read your Plex server. {user_friendly_message(exc)}",
                       _admin_origin(ctx), ADMIN_INVITE)
        _clear(ctx)
        return ConversationHandler.END
    if not sections:
        await end_flow(update, ctx,
                       "Your Plex server reports no libraries; nothing to share.",
                       _admin_origin(ctx))
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
    hub = _admin_origin(ctx)
    if not (email and machine and selected):
        await end_flow(update, ctx, "Lost the invite draft.", hub, ADMIN_INVITE,
                       typed_text="Lost the invite draft. /invite to start over.")
        _clear(ctx)
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await end_flow(update, ctx, "Your /link is no longer usable; /link first.",
                       hub, typed_text=("Your /link is no longer usable. /link "
                                    "then /invite again."))
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
        await end_flow(update, ctx,
                       f"Couldn't send the invite. {user_friendly_message(exc)}",
                       hub, ADMIN_INVITE)
        _clear(ctx)
        return ConversationHandler.END

    lib_word = "library" if len(selected) == 1 else "libraries"
    await end_flow(update, ctx,
                   f"📨 Invite sent to <b>{html.escape(email)}</b> "
                   f"({len(selected)} {lib_word}).\n"
                   "They'll get an email from Plex; once they accept and sign in, "
                   "Seerr picks them up and /link works for them.",
                   hub, parse_mode="HTML")
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
    await end_flow(update, ctx, "Cancelled.", _admin_origin(ctx),
                   typed_text="Cancelled. /invite to start over.")
    _clear(ctx)
    return ConversationHandler.END


# --- Shares: the Admin Hub's list, one Share's view, and its removal ---------
#
# Not a conversation: every step fetches the listing live from plex.tv and
# carries what it needs in its callback. The one exception is the search text
# of a typed /uninvite, kept in user_data so its match pages can refilter.

SHARES_PER_PAGE = 8
_NO_SERVER = ("Your linked Plex account doesn't own a server — sharing needs "
              "the server owner's account.")
# user_data key holding the search text of the last typed /uninvite.
_SHARE_QUERY_KEY = "share_query"


def _who(share: PlexShare) -> str:
    return share.email or share.username or f"Share {share.id}"


def _share_label(share: PlexShare) -> str:
    who = _who(share)
    return who if share.accepted else f"⏳ {who} (pending)"


def _share_rows(shares) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(_share_label(s),
                                  callback_data=f"{SHARE_VIEW}:{s.id}")]
            for s in shares]


def _share_view(share: PlexShare) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"👤 <b>{html.escape(_who(share))}</b>"]
    if share.email and share.username:
        lines.append(f"Plex username: {html.escape(share.username)}")
    lines.append("Accepted." if share.accepted
                 else "Pending Invite, not accepted yet.")
    if share.libraries:
        lines.append(f"Libraries: {html.escape(', '.join(share.libraries))}")
    elif share.all_libraries:
        lines.append("Libraries: all")
    return "\n".join(lines), InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove",
                              callback_data=f"{SHARE_REMOVE}:{share.id}")],
        [InlineKeyboardButton("⬅️ Shares", callback_data=f"{ADMIN_SHARES}:0")],
    ])


def _confirm_card(share: PlexShare) -> tuple[str, InlineKeyboardMarkup]:
    who = _who(share)
    if share.accepted:
        state = "accepted share"
        consequence = ("They lose the server immediately; Seerr and their "
                       "/link die with their Plex access.")
    else:
        state = "PENDING invite"
        consequence = "The invite will be cancelled before they accept it."
    flag = "a" if share.accepted else "p"
    return (
        f"Remove <b>{html.escape(who)}</b>'s access ({state})?\n{consequence}",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Remove",
                                 callback_data=f"{UNINV_GO}:{share.id}:{flag}"),
            InlineKeyboardButton("🛑 Keep", callback_data=UNINV_KEEP),
        ]]),
    )


def _sorted_shares(shares) -> list[PlexShare]:
    return sorted(shares, key=lambda s: _who(s).lower())


def _share_page(shares, page: int, nav_prefix: str,
                header: str) -> tuple[str, InlineKeyboardMarkup]:
    """One page of Share buttons with Prev/Next (`nav_prefix:<page>`) and
    "⬅️ Admin"; `header` gets "Page N of M." when there is more than one."""
    pages = -(-len(shares) // SHARES_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    start = page * SHARES_PER_PAGE
    rows = _share_rows(shares[start:start + SHARES_PER_PAGE])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"{nav_prefix}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"{nav_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([back_button(ADMIN_HUB)])
    if pages > 1:
        header += f"\nPage {page + 1} of {pages}."
    return header, InlineKeyboardMarkup(rows)


def _matching(shares, query: str) -> list[PlexShare]:
    return _sorted_shares(
        s for s in shares
        if query in s.email.lower() or query in s.username.lower())


def _matches_header(count: int, query: str) -> str:
    return (f'{count} Shares match "{html.escape(query)}". '
            "Tap one to see it or remove it.")


def _page_of(data: str) -> int:
    try:
        return int(data.split(":")[1])
    except (ValueError, IndexError):
        return 0


async def _edit_recorded(update, ctx, text: str,
                         markup: InlineKeyboardMarkup) -> None:
    sent = await edit_or_send(update.callback_query, text,
                              reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)


async def _reply_recorded(update, ctx, text: str,
                          markup: InlineKeyboardMarkup) -> None:
    sent = await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)


async def _load_shares(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       retry: str) -> Optional[list[PlexShare]]:
    """The owned server's Shares, sorted by shown name, for a Shares button.
    On a refusal or failure, edits the tapped message into it (back button,
    plus Try again with `retry` on a failure) and returns None."""
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await end_flow(update, ctx, _NOT_LINKED, ADMIN_HUB)
        return None
    await send_typing(update, ctx)
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        machine_id, _ = await plex.get_owned_server(mapping.plex_token)
        shares = await plex.list_shares(mapping.plex_token, machine_id)
    except LookupError:
        await end_flow(update, ctx, _NO_SERVER, ADMIN_HUB)
        return None
    except Exception as exc:
        logger.exception("share listing failed")
        await end_flow(update, ctx,
                       f"Couldn't list Shares. {user_friendly_message(exc)}",
                       ADMIN_HUB, retry)
        return None
    return _sorted_shares(shares)


async def admin_shares(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin Hub "Shares" and its Prev/Next (adms:<page>): one page of Share
    buttons, edited into the tapped message."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    shares = await _load_shares(update, ctx, q.data)
    if shares is None:
        return
    if not shares:
        await end_flow(update, ctx,
                       "👥 <b>Shares</b>\nNo one has a Share on your server yet.",
                       ADMIN_HUB, parse_mode="HTML")
        return
    await _edit_recorded(update, ctx, *_share_page(
        shares, _page_of(q.data), ADMIN_SHARES,
        f"👥 <b>Shares</b> ({len(shares)})\nTap one to see it or remove it."))


async def share_matches(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Prev/Next of a typed /uninvite's matches (shq:<page>): refilters the
    live listing with the search text kept in user_data."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    query = ctx.user_data.get(_SHARE_QUERY_KEY)
    if not query:
        await end_flow(update, ctx, "That search expired. Type /uninvite again.",
                       ADMIN_HUB)
        return
    shares = await _load_shares(update, ctx, q.data)
    if shares is None:
        return
    matches = _matching(shares, query)
    if not matches:
        await end_flow(update, ctx,
                       f'No share matches "{html.escape(query)}" any more.',
                       ADMIN_HUB, parse_mode="HTML")
        return
    await _edit_recorded(update, ctx, *_share_page(
        matches, _page_of(q.data), SHARE_MATCHES,
        _matches_header(len(matches), query)))


async def _tapped_share(update: Update,
                        ctx: ContextTypes.DEFAULT_TYPE) -> Optional[PlexShare]:
    """Gate, answer and look up the Share a shv:/shrm:<id> tap names. Ends
    the step itself (and returns None) when refused, failed or gone."""
    if await refuse_outside_admin_dm(update, ctx):
        return None
    q = update.callback_query
    await q.answer()
    try:
        share_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return None
    shares = await _load_shares(update, ctx, q.data)
    if shares is None:
        return None
    for share in shares:
        if share.id == share_id:
            return share
    await end_flow(update, ctx,
                   "That Share is gone. It may have been removed elsewhere.",
                   ADMIN_HUB)
    return None


async def share_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A Share button (shv:<id>): who, pending or accepted, its Libraries,
    with Remove and "⬅️ Shares"."""
    share = await _tapped_share(update, ctx)
    if share is not None:
        await _edit_recorded(update, ctx, *_share_view(share))


async def share_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A Share view's Remove (shrm:<id>): the confirm card."""
    share = await _tapped_share(update, ctx)
    if share is not None:
        await _edit_recorded(update, ctx, *_confirm_card(share))


async def cmd_uninvite(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    token = await _admin_gate(update, ctx)
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
        await update.effective_message.reply_text(_NO_SERVER)
        return
    except Exception as exc:
        logger.exception("share listing failed")
        await update.effective_message.reply_text(
            f"Couldn't list shares. {user_friendly_message(exc)}")
        return
    matches = _matching(shares, query)
    if not matches:
        await update.effective_message.reply_text(
            f'No share matches "{query}". Nothing changed.')
        return
    if len(matches) == 1:
        await _reply_recorded(update, ctx, *_share_view(matches[0]))
        return
    ctx.user_data[_SHARE_QUERY_KEY] = query
    await _reply_recorded(update, ctx, *_share_page(
        matches, 0, SHARE_MATCHES, _matches_header(len(matches), query)))


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
    """Every end carries "⬅️ Admin"; a failure adds Try again, which re-sends
    the already confirmed removal."""
    q = update.callback_query
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(update.effective_user.id)
    if mapping is None or not mapping.plex_token:
        await end_flow(update, ctx, "Your /link is no longer usable; /link first.",
                       ADMIN_HUB)
        return
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        await plex.remove_share(mapping.plex_token, share_id)
    except NotFoundAPIError:
        # Already gone (removed elsewhere, or a racing duplicate tap):
        # the desired end state holds, so report it as done.
        await end_flow(update, ctx, "✅ Already removed.", ADMIN_HUB)
        return
    except Exception as exc:
        logger.exception("share removal failed")
        await end_flow(update, ctx,
                       f"Couldn't remove the share. {user_friendly_message(exc)}",
                       ADMIN_HUB, q.data)
        return
    await end_flow(update, ctx, "✅ Invite cancelled." if was_pending
                   else "✅ Access removed.", ADMIN_HUB)


async def uninvite_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The confirm card's Keep button (its own prefix, so it can never
    collide with the invite conversation's cancel). Admin-gated: the
    handler edits whatever message the callback references."""
    q = update.callback_query
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer("Cancelled")
    await end_flow(update, ctx, "Cancelled — nothing changed.", ADMIN_HUB)

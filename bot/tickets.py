"""Ticket management: /tickets, the tk_* callback family, _apply_fix, and the
reply ConversationHandler."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from fix_result import FixResult
from http_util import user_friendly_message
from radarr import RadarrClient
from seerr import SeerrClient
from sonarr import SonarrClient
from store import UserStore

from bot.shared import (
    AUTOFIX_ELIGIBLE_TYPES,
    AWAIT_TICKET_REPLY,
    ISSUE_TYPES,
    _edit_or_send,
    _format_age,
    _record_btn,
    _require_seerr,
    _ticket_detail_kb,
    _token_for,
)

logger = logging.getLogger("hermes")

async def cmd_tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    seerr = await _require_seerr(update, ctx)
    if seerr is None:
        return
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(user_id)

    # Eligibility check for non-admin users
    if not is_admin:
        if not mapping or not mapping.plex_token:
            await update.effective_message.reply_text(
                "DM me /link first so I know which Plex account is yours."
            )
            return

    # Fetch issues
    try:
        issues = await seerr.list_issues(
            filter="open",
            take=25,
            as_plex_token=None if is_admin else mapping.plex_token,
        )
    except Exception as exc:
        logger.exception("list_issues failed")
        await update.effective_message.reply_text(f"Couldn't fetch tickets. {user_friendly_message(exc)}")
        return

    if not issues:
        await update.effective_message.reply_text(
            ("No open tickets across all users. 🎉" if is_admin else "No open tickets! 🎉")
        )
        return

    # Resolve media titles in parallel
    title_tasks = [seerr.get_media_title(i.media_type, i.tmdb_id) for i in issues]
    title_results = await asyncio.gather(*title_tasks, return_exceptions=True)

    header = f"📋 {'All open tickets' if is_admin else 'Your open tickets'} ({len(issues)}):"
    lines = [header, ""]
    for issue, tr in zip(issues, title_results):
        emoji, _ = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        if isinstance(tr, Exception):
            media_label = f"TMDb {issue.tmdb_id}"
        else:
            title, year = tr
            media_label = title + (f" ({year})" if year else "")
        if issue.media_type == "tv" and issue.problem_season:
            if issue.problem_episode:
                media_label += f" S{int(issue.problem_season):02d}E{int(issue.problem_episode):02d}"
            else:
                media_label += f" S{int(issue.problem_season):02d}"
        age = _format_age(issue.created_at)
        line = f"#{issue.id} {emoji} {media_label} — {age}"
        if is_admin and issue.created_by:
            line += f" — {issue.created_by}"
        lines.append(line)
    lines.append("")
    lines.append("Tap a ticket number below to manage it.")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"

    # Inline keyboard: one button per ticket (#N), 4 per row
    button_rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for issue in issues:
        current.append(InlineKeyboardButton(f"#{issue.id}", callback_data=f"tkopen:{issue.id}"))
        if len(current) == 4:
            button_rows.append(current)
            current = []
    if current:
        button_rows.append(current)

    msg = await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(button_rows) if button_rows else None,
    )
    _record_btn(ctx.application, update.effective_user.id, msg)


def _ticket_detail_kb(issue_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    # Reply always goes straight to reply input for everyone (no submenu).
    # Only Close and Fix have submenus, since they have multiple action variants.
    row = [InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}")]
    if is_admin:
        row.append(InlineKeyboardButton("🔧 Fix", callback_data=f"tkf:{issue_id}"))
        row.append(InlineKeyboardButton("✅ Close", callback_data=f"tkc:{issue_id}"))
    return InlineKeyboardMarkup([row])


async def tk_reply_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped top-level [Reply]. Opens [Reply] [Close] sub-menu."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}"),
        InlineKeyboardButton("✅ Close", callback_data=f"tkcd:{issue_id}"),
    ]])
    await q.edit_message_text(f"Reply to ticket #{issue_id}?", reply_markup=kb)


async def tk_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tapped [#N] from /tickets list. Sends a NEW detail message with the
    ticket's context (type, media, S/E, reporter, age) so the admin/user knows
    what they're acting on without having to scroll back to the list."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await _token_for(ctx, tg_id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return

    text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    msg = await q.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    _record_btn(ctx.application, tg_id, msg)


async def _build_ticket_detail(
    ctx: ContextTypes.DEFAULT_TYPE,
    issue_id: int,
    is_admin: bool,
    token: Optional[str],
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the ticket detail message text + keyboard. Shared between tk_open
    (sending a new message) and tk_back (editing an existing one)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media_label = ""
    type_emoji = "📝"
    type_name = "Issue"
    reporter = ""
    age = ""
    description = ""
    try:
        issue = await seerr.get_issue(issue_id, as_plex_token=None if is_admin else token)
        type_emoji, type_name = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        reporter = issue.created_by or ""
        age = _format_age(issue.created_at) if issue.created_at else ""
        description = (issue.description or "").strip()
        if issue.media_type in ("movie", "tv") and issue.tmdb_id:
            try:
                title, year = await seerr.get_media_title(issue.media_type, issue.tmdb_id)
                m_emoji = "🎬" if issue.media_type == "movie" else "📺"
                media_label = f"{m_emoji} {title}" + (f" ({year})" if year else "")
                if issue.media_type == "tv" and issue.problem_season:
                    if issue.problem_episode:
                        media_label += f" — S{int(issue.problem_season):02d}E{int(issue.problem_episode):02d}"
                    else:
                        media_label += f" — S{int(issue.problem_season):02d}"
            except Exception:
                logger.exception("get_media_title failed for #%d", issue_id)
    except Exception:
        logger.exception("get_issue failed for #%d", issue_id)
    lines = [f"<b>Ticket #{issue_id}</b>"]
    if media_label:
        lines.append(html.escape(media_label))
    lines.append(f"<b>Issue:</b> {type_emoji} {type_name}")
    if reporter:
        lines.append(f"<b>Reported by:</b> {html.escape(reporter)}")
    if age:
        lines.append(f"<b>Age:</b> {age}")
    if description:
        lines.append("")
        lines.append("<b>Description:</b>")
        lines.append(f"<i>\"{html.escape(description)}\"</i>")
    return "\n".join(lines), _ticket_detail_kb(issue_id, is_admin)


async def tk_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel from a sub-menu -- edit the message back to the ticket detail view."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    tg_id = update.effective_user.id
    is_admin, token, _decrypt_failed = await _token_for(ctx, tg_id)
    text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("tk_back edit failed for #%d", issue_id)
        return
    # The same message_id is the active one; refresh sent_at so the 6h timer resets.
    _record_btn(ctx.application, tg_id, q.message)


async def tk_close_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Close] -- show the with/without comment options."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Comment", callback_data=f"tkcc:{issue_id}"),
            InlineKeyboardButton("✓ No comment", callback_data=f"tkcd:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Cancel", callback_data=f"tkback:{issue_id}")],
    ])
    await q.edit_message_text(f"Close ticket #{issue_id}?", reply_markup=kb)
    _record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Fix]. Opens the [Redownload] [Mark Failed] [Close] submenu."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Redownload", callback_data=f"tkfd:{issue_id}"),
            InlineKeyboardButton("🚫 Mark Failed", callback_data=f"tkfm:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Cancel", callback_data=f"tkback:{issue_id}")],
    ])
    await q.edit_message_text(f"🔧 Fix #{issue_id} — how?", reply_markup=kb)
    _record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix_redownload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete current file + trigger search."""
    await _apply_fix(update, ctx, strategy="redownload")


async def tk_fix_mark_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the most recent grab as failed → Radarr/Sonarr blocklists + re-searches."""
    await _apply_fix(update, ctx, strategy="mark_failed")


async def _apply_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, strategy: str) -> None:
    """Shared admin-fix path. strategy is 'redownload' or 'mark_failed'."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    store: UserStore = ctx.bot_data["store"]

    try:
        issue = await seerr.get_issue(issue_id)
    except Exception as exc:
        logger.exception("get_issue failed for #%d", issue_id)
        await _edit_or_send(q, f"Couldn't fetch ticket #{issue_id}. {user_friendly_message(exc)}")
        return

    media_type = issue.media_type
    tmdb_id = issue.tmdb_id
    season = issue.problem_season
    episode = issue.problem_episode
    action_name = "Redownload" if strategy == "redownload" else "Mark Failed"

    if media_type == "tv" and not episode:
        await _edit_or_send(q,
            f"{action_name} only works on individual episodes or movies — not whole "
            f"seasons or shows. For #{issue_id}, fix it in Sonarr directly."
        )
        return

    media: dict = {"type": media_type, "tmdb_id": tmdb_id}
    label_title = ""
    label_year = ""
    try:
        label_title, label_year = await seerr.get_media_title(media_type, tmdb_id)
    except Exception:
        logger.exception("get_media_title failed for #%d", issue_id)
    if media_type == "tv":
        try:
            _seasons, tvdb_id = await seerr.get_tv_seasons(tmdb_id)
            media["tvdb_id"] = tvdb_id
        except Exception:
            logger.exception("get_tv_seasons failed for #%d", issue_id)

    if strategy == "redownload":
        result = await _run_autofix(media, season, episode, radarr, sonarr)
    else:
        result = await _run_mark_failed(media, season, episode, radarr, sonarr)

    label = label_title + (f" ({label_year})" if label_year else "")
    if media_type == "tv" and season:
        label += (
            f" — S{int(season):02d}E{int(episode):02d}"
            if episode else f" — S{int(season):02d}"
        )

    if result.status == "failed":
        await _edit_or_send(q, f"⚠️ {action_name} for #{issue_id} didn't run: {result.message}")
        return

    # ok or partial. If a search was triggered, enqueue the completion poller.
    if result.should_poll:
        try:
            kwargs: dict = {
                "chat_id": q.message.chat_id,
                "user_id": update.effective_user.id,
                "media_type": media_type,
                "label": label or f"#{issue_id}",
                "issue_id": issue_id,
                "issue_url": "",
            }
            poll_info = result.poll_info or {}
            if media_type == "movie":
                kwargs["radarr_movie_id"] = poll_info.get("movie_id")
            else:
                kwargs["sonarr_series_id"] = poll_info.get("series_id")
                kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
                kwargs["sonarr_season"] = poll_info.get("season")
                kwargs["expected_episode_ids"] = poll_info.get("expected_episode_ids") or []
            await store.add_pending_autofix(**kwargs)
            await store.log_autofix(update.effective_user.id, media_type, tmdb_id,
                                    season=season, episode=episode)
        except Exception:
            logger.exception("failed to enqueue pending autofix for #%d", issue_id)
            prefix = "🔧" if result.ok else "⚠️"
            await _edit_or_send(q,
                f"{prefix} {action_name} for #{issue_id} ({result.message}), "
                "but couldn't enqueue completion notification."
            )
            return

    prefix = "🔧" if result.ok else "⚠️"
    tail = "\n\n🔔 I'll DM when the new file finishes downloading." if result.should_poll else ""
    await _edit_or_send(q,
        f"{prefix} {action_name} for #{issue_id}.\n{label}\n\n{result.message}{tail}"
    )
    return


async def tk_close_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Without comment]. Resolve immediately."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.resolve_issue(issue_id, as_plex_token=None)
    except Exception as exc:
        logger.exception("resolve_issue failed for #%d", issue_id)
        await _edit_or_send(q, f"Couldn't close #{issue_id}. {user_friendly_message(exc)}")
        return
    await _edit_or_send(q, f"✅ Closed ticket #{issue_id}.")


# --- Ticket reply conversation ----------------------------------------------

async def tk_reply_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkr:<id>` -- reply only (no close)."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    is_admin, token, decrypt_failed = await _token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can post comments as you.")
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = False
    await q.edit_message_text(
        f"Send the reply text for ticket #{issue_id} (or /cancel)."
    )
    return AWAIT_TICKET_REPLY


async def tk_close_with_comment_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkcc:<id>` -- post comment then close (admin only)."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = True
    await q.edit_message_text(
        f"Send the closing comment for ticket #{issue_id} (or /cancel)."
    )
    return AWAIT_TICKET_REPLY


async def tk_reply_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the reply text, post it, optionally close."""
    issue_id = ctx.user_data.get("tk_reply_id")
    close_after = bool(ctx.user_data.get("tk_close_after"))
    if not issue_id:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Empty message. Send a few words or /cancel.")
        return AWAIT_TICKET_REPLY
    is_admin, token, decrypt_failed = await _token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await update.effective_message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await update.effective_message.reply_text("Your /link is gone or incomplete. /link to re-link.")
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.add_issue_comment(issue_id, text, as_plex_token=token)
    except Exception as exc:
        logger.exception("add_issue_comment failed for #%d", issue_id)
        await update.effective_message.reply_text(f"Couldn't post comment on #{issue_id}. {user_friendly_message(exc)}")
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    if close_after:
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=None)
        except Exception as exc:
            logger.exception("resolve_issue failed for #%d", issue_id)
            await update.effective_message.reply_text(
                f"💬 Comment posted on #{issue_id}, but couldn't close. {user_friendly_message(exc)}"
            )
            ctx.user_data.pop("tk_reply_id", None)
            ctx.user_data.pop("tk_close_after", None)
            return ConversationHandler.END
        await update.effective_message.reply_text(f"💬 Replied and ✅ closed ticket #{issue_id}.")
    else:
        await update.effective_message.reply_text(f"💬 Replied to ticket #{issue_id}.")
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    return ConversationHandler.END


async def tk_reply_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def _ticket_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(tk_reply_start, pattern=r"^tkr:\d+$"),
            CallbackQueryHandler(tk_close_with_comment_start, pattern=r"^tkcc:\d+$"),
        ],
        states={
            AWAIT_TICKET_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tk_reply_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", tk_reply_cancel)],
        name="ticket_reply",
        persistent=False,
        allow_reentry=True,
    )

async def _run_autofix(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
) -> FixResult:
    """Run delete+search via Radarr/Sonarr. Returns FixResult; the caller
    inspects status (ok/partial/failed) and should_poll to decide whether
    to enqueue the autofix completion poller."""
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            return await radarr.auto_fix(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                # Whole-season / whole-show auto-fix is not supported -- too
                # destructive. Episode-only.
                return FixResult.failed(
                    "Auto-fix only works on individual episodes, not whole seasons."
                )
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            return await sonarr.auto_fix_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("auto_fix failed")
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")


async def _run_mark_failed(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
) -> FixResult:
    """Same shape as _run_autofix. Blocklists the most recent grab in
    addition to delete+search."""
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            return await radarr.mark_failed(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                return FixResult.failed("Mark Failed only works on individual episodes.")
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            return await sonarr.mark_failed_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("mark_failed failed")
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")



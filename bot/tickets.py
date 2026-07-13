"""Ticket management: /tickets, the tk_* callback family, _apply_fix, and the
reply ConversationHandler.

Public entry points:
  cmd_tickets          /tickets list command
  tk_open, tk_back, tk_close_menu, tk_close_direct, tk_fix,
    tk_fix_redownload, tk_fix_mark_failed                callback handlers
  tk_reply_start, tk_close_with_comment_start, tk_reply_text  reply convo
  _ticket_conversation()                                      conversation
  _run_arr_action(action="fix" | "mark_failed")               arr orchestrator
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Literal, Optional

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
from seerr import PlexTokenInvalidError, SeerrClient
from sonarr import SonarrClient
from store import UserStore

from bot.callback_prefixes import (
    TK_BACK,
    TK_CLOSE_DIRECT,
    TK_CLOSE_WITH_COMMENT,
    TK_FIX_MARK_FAILED,
    TK_FIX_REDOWNLOAD,
    TK_OPEN,
    TK_REPLY,
)
from const import TICKET_REPLY_TIMEOUT_S
from bot.shared import (
    DECRYPT_FAILED_MSG,
    AWAIT_TICKET_REPLY,
    RELINK_RESUME_EXECUTORS,
    ISSUE_TYPES,
    edit_or_send,
    format_age,
    format_media_label,
    record_btn,
    require_seerr,
    ticket_detail_kb,
    token_for,
    end_action,
    media_action_key,
    prompt_plex_relink,
    send_typing,
    try_begin_action,
    user_in_conversation,
)

logger = logging.getLogger("hermes")

# Cap the reply thread rendered in a ticket detail view so a long-running
# conversation can't push the message past Telegram's ~4096-char limit.
MAX_THREAD_COMMENTS = 20


async def _require_admin(
    q, ctx: ContextTypes.DEFAULT_TYPE, *, action_label: str,
) -> bool:
    """Gate an admin-only callback. Returns True if the caller is the admin;
    otherwise toasts 'Admin only.' and writes an `admin_callback_blocked`
    audit entry. Replaces silent no-op rejection so admins notice and
    non-admins know why nothing happened."""
    user = getattr(q, "from_user", None)
    user_id = getattr(user, "id", None)
    if user_id == ctx.bot_data.get("admin_id"):
        return True
    try:
        await q.answer("Admin only.", show_alert=False)
    except Exception:
        pass
    # Local import to avoid a top-level auth_util dep just for this entry.
    from auth_util import audit
    audit("admin_callback_blocked",
          user=str(user_id) if user_id is not None else "-",
          action=action_label)
    return False


def _truncate_message(text: str, limit: int = 4000) -> str:
    """Keep a message under Telegram's 4096-char cap (an
    oversized detail view failed the send silently). Cuts at the last
    newline before the limit so an HTML tag is never split mid-entity."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 20)
    if cut < limit // 2:
        cut = limit - 20
    return text[:cut] + "\n…(truncated)"


async def cmd_tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    seerr = await require_seerr(update, ctx)
    if seerr is None:
        return
    await send_typing(update, ctx)  # list + title fetches can take seconds
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
        issues, total = await seerr.list_issues(
            filter="open",
            take=25,
            as_plex_token=None if is_admin else mapping.plex_token,
        )
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
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

    # Honest truncation: the header used to claim "All open
    # tickets (25)" while issues 26+ were silently invisible.
    scope = "All open tickets" if is_admin else "Your open tickets"
    if total > len(issues):
        header = f"📋 {scope} (showing {len(issues)} of {total}; manage the rest in Seerr):"
    else:
        header = f"📋 {scope} ({len(issues)}):"
    lines = [header, ""]
    for issue, tr in zip(issues, title_results):
        emoji, _ = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        if isinstance(tr, Exception):
            media_label = f"TMDb {issue.tmdb_id}"
        else:
            title, year = tr
            media_label = format_media_label(
                title, year,
                season=issue.problem_season if issue.media_type == "tv" else None,
                episode=issue.problem_episode,
            )
        age = format_age(issue.created_at)
        line = f"#{issue.id} {emoji} {media_label} — {age}"
        if is_admin and issue.created_by:
            line += f" — {issue.created_by}"
        lines.append(line)
    lines.append("")
    lines.append("Tap a ticket number below to manage it.")
    text = _truncate_message("\n".join(lines))

    # Inline keyboard: one button per ticket (#N), 4 per row
    button_rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for issue in issues:
        current.append(InlineKeyboardButton(f"#{issue.id}", callback_data=f"{TK_OPEN}:{issue.id}"))
        if len(current) == 4:
            button_rows.append(current)
            current = []
    if current:
        button_rows.append(current)

    msg = await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(button_rows) if button_rows else None,
    )
    record_btn(ctx.application, update.effective_user.id, msg)


async def tk_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tapped [#N] from /tickets list. Sends a NEW detail message with the
    ticket's context (type, media, S/E, reporter, age) so the admin/user knows
    what they're acting on without having to scroll back to the list."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, tg_id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return

    try:
        text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
    msg = await q.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    record_btn(ctx.application, tg_id, msg)


async def _build_ticket_detail(
    ctx: ContextTypes.DEFAULT_TYPE,
    issue_id: int,
    is_admin: bool,
    token: Optional[str],
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Render the ticket detail message text + keyboard. Shared between tk_open
    (sending a new message) and tk_back (editing an existing one). Keyboard is
    None when the ticket couldn't be loaded (no actions over unknown state)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media_label = ""
    type_emoji = "📝"
    type_name = "Issue"
    reporter = ""
    age = ""
    description = ""
    comments: list = []
    try:
        issue = await seerr.get_issue(issue_id, as_plex_token=None if is_admin else token)
        type_emoji, type_name = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        reporter = issue.created_by or ""
        age = format_age(issue.created_at) if issue.created_at else ""
        description = (issue.description or "").strip()
        comments = issue.comments or []
        if issue.media_type in ("movie", "tv") and issue.tmdb_id:
            try:
                title, year = await seerr.get_media_title(issue.media_type, issue.tmdb_id)
                m_emoji = "🎬" if issue.media_type == "movie" else "📺"
                bare = format_media_label(
                    title, year,
                    season=issue.problem_season if issue.media_type == "tv" else None,
                    episode=issue.problem_episode,
                )
                media_label = f"{m_emoji} {bare}"
            except Exception:
                logger.exception("get_media_title failed for #%d", issue_id)
    except PlexTokenInvalidError:
        raise  # callers show the re-link prompt; a degraded view can't help
    except Exception:
        # Honest failure state: the old path rendered a
        # healthy-looking detail with live Fix/Close buttons over data it
        # never loaded.
        logger.exception("get_issue failed for #%d", issue_id)
        return (f"<b>Ticket #{issue_id}</b>\n"
                "Couldn't load the ticket details right now. "
                "Try again in a moment.", None)
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
    if comments:
        lines.append("")
        shown = comments[-MAX_THREAD_COMMENTS:]
        if len(comments) > MAX_THREAD_COMMENTS:
            lines.append(f"<b>Replies</b> (last {MAX_THREAD_COMMENTS} of {len(comments)}):")
        else:
            lines.append("<b>Replies:</b>")
        for c in shown:
            age_c = format_age(c.created_at) if c.created_at else ""
            head = f"<b>{html.escape(c.author or '?')}</b>"
            if age_c:
                head += f" · {age_c}"
            lines.append(f"{head}: <i>\"{html.escape(c.message)}\"</i>")
    return _truncate_message("\n".join(lines)), ticket_detail_kb(issue_id, is_admin)


async def tk_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Back from a sub-menu -- edit the message back to the ticket detail view."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, tg_id)
    # Same gate as tk_open: a non-admin without a usable token must not fall
    # through to _build_ticket_detail's admin-key fetch.
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return
    try:
        text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("tk_back edit failed for #%d", issue_id)
        return
    # The same message_id is the active one; refresh sent_at so the 6h timer resets.
    record_btn(ctx.application, tg_id, q.message)


async def tk_close_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Close] -- show the with/without comment options."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_close_menu"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Comment", callback_data=f"{TK_CLOSE_WITH_COMMENT}:{issue_id}"),
            InlineKeyboardButton("✓ No comment", callback_data=f"{TK_CLOSE_DIRECT}:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK}:{issue_id}")],
    ])
    await q.edit_message_text(f"Close ticket #{issue_id}?", reply_markup=kb)
    record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Fix]. Opens the [Redownload] [Mark Failed] [Back] submenu."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_fix"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Redownload", callback_data=f"{TK_FIX_REDOWNLOAD}:{issue_id}"),
            InlineKeyboardButton("🚫 Mark Failed", callback_data=f"{TK_FIX_MARK_FAILED}:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK}:{issue_id}")],
    ])
    await q.edit_message_text(f"🔧 Fix #{issue_id} — how?", reply_markup=kb)
    record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix_redownload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete current file + trigger search."""
    await _apply_fix(update, ctx, strategy="redownload")


async def tk_fix_mark_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the most recent grab as failed → Radarr/Sonarr blocklists + re-searches."""
    await _apply_fix(update, ctx, strategy="mark_failed")


@dataclass
class _FixContext:
    """Resolved context for an admin Fix / Mark Failed action.

    Carries everything _apply_fix needs to dispatch + render: the issue id
    (for messages + audit + poll-tracking row), the media dict the arr
    clients expect, the season/episode (for label + workflow), and the
    pre-built label for the success/failure DM."""
    issue_id: int
    media: dict
    season: Optional[int]
    episode: Optional[int]
    label: str


async def _resolve_fix_context(
    seerr: SeerrClient, issue_id: int, *, action_name: str,
) -> tuple[Optional[_FixContext], Optional[str]]:
    """Fetch the issue + media title + tvdb_id and return a _FixContext.
    On any unrecoverable failure returns (None, user_facing_error_string).
    Best-effort lookups (title, tvdb_id) don't fail the whole flow."""
    try:
        issue = await seerr.get_issue(issue_id)
    except Exception as exc:
        logger.exception("get_issue failed for #%d", issue_id)
        return None, f"Couldn't fetch ticket #{issue_id}. {user_friendly_message(exc)}"

    media_type = issue.media_type
    tmdb_id = issue.tmdb_id
    season = issue.problem_season
    episode = issue.problem_episode

    if media_type == "tv" and not episode:
        return None, (
            f"{action_name} only works on individual episodes or movies — not "
            f"whole seasons or shows. For #{issue_id}, fix it in Sonarr directly."
        )

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

    label = format_media_label(
        label_title, label_year,
        season=season if media_type == "tv" else None,
        episode=episode,
    )
    return _FixContext(issue_id=issue_id, media=media, season=season,
                       episode=episode, label=label), None


async def _enqueue_fix_completion(
    store: UserStore, *, fix: _FixContext, result: FixResult,
    chat_id: int, user_id: int, issue_url: str,
) -> None:
    """Build the pending_autofix row from result.poll_info. Raises on enqueue
    failure -- caller decides what to tell the user."""
    poll_info = result.poll_info or {}
    kwargs: dict = {
        "chat_id": chat_id,
        "user_id": user_id,
        "media_type": fix.media["type"],
        "label": fix.label or f"#{fix.issue_id}",
        "issue_id": fix.issue_id,
        "issue_url": issue_url,
    }
    if fix.media["type"] == "movie":
        kwargs["radarr_movie_id"] = poll_info.get("movie_id")
    else:
        kwargs["sonarr_series_id"] = poll_info.get("series_id")
        kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
        kwargs["sonarr_season"] = poll_info.get("season")
        kwargs["expected_episode_ids"] = poll_info.get("expected_episode_ids") or []
    await store.add_pending_autofix(**kwargs)


async def _apply_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, strategy: str) -> None:
    """Shared admin-fix path. strategy is 'redownload' or 'mark_failed'."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label=f"_apply_fix:{strategy}"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    store: UserStore = ctx.bot_data["store"]
    action_name = "Redownload" if strategy == "redownload" else "Mark Failed"

    # Working-state edit BEFORE any network call: strips the Fix
    # submenu so it can't be tapped again while the Seerr+Arr calls run.
    await edit_or_send(q, f"⏳ {action_name} for #{issue_id} - working...")

    fix, err = await _resolve_fix_context(seerr, issue_id, action_name=action_name)
    if err is not None:
        await edit_or_send(q, err)
        return
    assert fix is not None

    # Per-media serialization: a second trigger for the same
    # title - a racing double-tap that beat the edit above, or a user autofix
    # running concurrently - must not race delete/blocklist/search.
    media_key = media_action_key(fix.media)
    if not try_begin_action(ctx, media_key):
        await edit_or_send(q,
            f"⏳ Another fix for {fix.label or f'#{issue_id}'} is already "
            "running. Wait for its result before retrying."
        )
        return
    try:
        action: FixAction = "fix" if strategy == "redownload" else "mark_failed"
        result = await _run_arr_action(
            fix.media, fix.season, fix.episode, radarr, sonarr, action=action,
        )

        if result.status == "failed":
            await edit_or_send(q, f"⚠️ {action_name} for #{issue_id} didn't run: {result.message}")
            return

        # ok or partial: always log the autofix event (mirrors _submit_issue,
        # which logs even when no search ran); enqueue the completion poller
        # iff search ran.
        await store.log_autofix(update.effective_user.id, fix.media["type"],
                                fix.media["tmdb_id"],
                                season=fix.season, episode=fix.episode)
        if result.should_poll:
            try:
                await _enqueue_fix_completion(
                    store, fix=fix, result=result,
                    chat_id=q.message.chat_id,
                    user_id=update.effective_user.id,
                    issue_url=f"{seerr.public_url}/issues/{issue_id}",
                )
            except Exception:
                logger.exception("failed to enqueue pending autofix for #%d", issue_id)
                prefix = "🔧" if result.ok else "⚠️"
                await edit_or_send(q,
                    f"{prefix} {action_name} for #{issue_id} ({result.message}), "
                    "but couldn't enqueue completion notification."
                )
                return

        prefix = "🔧" if result.ok else "⚠️"
        tail = "\n\n🔔 I'll DM when the new file finishes downloading." if result.should_poll else ""
        await edit_or_send(q,
            f"{prefix} {action_name} for #{issue_id}.\n{fix.label}\n\n{result.message}{tail}"
        )
        return
    finally:
        end_action(ctx, media_key)


async def tk_close_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Without comment]. Resolve immediately."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_close_direct"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    # Double-tap guard: strip the submenu before the network
    # call, and drop a racing second tap that got in before the edit (it
    # would resolve an already-resolved issue and show a contradictory
    # "Closed" + "Couldn't close" pair on screen).
    close_key = f"close:{issue_id}"
    if not try_begin_action(ctx, close_key):
        return
    try:
        await edit_or_send(q, f"⏳ Closing #{issue_id}...")
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=None)
        except Exception as exc:
            logger.exception("resolve_issue failed for #%d", issue_id)
            await edit_or_send(q, f"Couldn't close #{issue_id}. {user_friendly_message(exc)}")
            return
        await edit_or_send(q, f"✅ Closed ticket #{issue_id}.")
    finally:
        end_action(ctx, close_key)


# --- Ticket reply conversation ----------------------------------------------

async def tk_reply_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkr:<id>` -- reply only (no close)."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return ConversationHandler.END
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can post comments as you.")
        return ConversationHandler.END
    # Same text-capture guard as the resolve comment prompt:
    # an active /issue conversation would swallow the reply as its next input.
    if user_in_conversation(ctx, update, "issue", "resolve"):
        await q.message.reply_text(
            "You're in the middle of another flow (like /issue). Finish or "
            "/cancel it first, then tap Reply again."
        )
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = False
    # Strip the inline buttons but keep the original issue-announcement text,
    # then prompt for the reply text in a separate message.
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Couldn't clear buttons on ticket #%d message", issue_id)
    await q.message.reply_text(
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
        logger.warning("Unparseable callback data: %r", q.data)
        return ConversationHandler.END
    if not await _require_admin(q, ctx, action_label="tk_close_with_comment_start"):
        return ConversationHandler.END
    # Same text-capture guard as tk_reply_start.
    if user_in_conversation(ctx, update, "issue", "resolve"):
        await q.message.reply_text(
            "You're in the middle of another flow (like /issue). Finish or "
            "/cancel it first, then tap the button again."
        )
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = True
    # Strip the inline buttons but keep the original issue-announcement text,
    # then prompt for the closing comment in a separate message.
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Couldn't clear buttons on ticket #%d message", issue_id)
    await q.message.reply_text(
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
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await update.effective_message.reply_text(
            DECRYPT_FAILED_MSG
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
    except PlexTokenInvalidError:
        # Carry the typed reply for the post-relink resume. close_after isn't
        # carried: close-with-comment is admin-only, and the admin (admin-key
        # attribution) can never hit this gate.
        await prompt_plex_relink(update, ctx, resume_kind="ticket_reply",
                                 resume_payload={"issue_id": issue_id,
                                                 "text": text})
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("add_issue_comment failed for #%d", issue_id)
        await update.effective_message.reply_text(f"Couldn't post comment on #{issue_id}. {user_friendly_message(exc)}")
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    # If the user started a new reply flow for a different issue during the
    # add_issue_comment await, our comment still landed on the right ticket
    # (we bound issue_id at entry) but we mustn't apply the close-after side
    # effect to whatever flow they're now on.
    if ctx.user_data.get("tk_reply_id") != issue_id:
        await update.effective_message.reply_text(
            f"💬 Reply posted on #{issue_id}. "
            "(You've started a new reply since then — that one's still active.)"
        )
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


async def _resume_ticket_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                               payload: dict) -> None:
    """Relink-resume executor: post the ticket reply the revoked token
    blocked, with the text the user had already typed."""
    issue_id = payload["issue_id"]
    text = payload["text"]
    is_admin, token, _ = await token_for(ctx, update.effective_user.id)
    if not is_admin and token is None:
        return  # can't happen right after a successful link; guard anyway
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.add_issue_comment(issue_id, text, as_plex_token=token)
    except Exception as exc:
        logger.exception("resumed add_issue_comment failed for #%d", issue_id)
        await update.effective_message.reply_text(
            f"Couldn't post comment on #{issue_id}. {user_friendly_message(exc)}")
        return
    await update.effective_message.reply_text(f"💬 Replied to ticket #{issue_id}.")


RELINK_RESUME_EXECUTORS["ticket_reply"] = _resume_ticket_reply


async def tk_reply_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def _tk_reply_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clear ticket-reply state so an abandoned
    conversation doesn't leak user_data for the life of the process."""
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    return ConversationHandler.END


def _ticket_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(tk_reply_start, pattern=fr"^{TK_REPLY}:\d+$"),
            CallbackQueryHandler(tk_close_with_comment_start, pattern=fr"^{TK_CLOSE_WITH_COMMENT}:\d+$"),
        ],
        states={
            AWAIT_TICKET_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tk_reply_text),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, _tk_reply_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", tk_reply_cancel)],
        name="ticket_reply",
        persistent=False,
        allow_reentry=True,
        conversation_timeout=TICKET_REPLY_TIMEOUT_S,  # idle clears tk_reply_id / tk_close_after
    )

FixAction = Literal["fix", "mark_failed"]


async def _run_arr_action(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
    *,
    action: FixAction,
) -> FixResult:
    """Run the configured Arr action against the media. `action="fix"` is the
    plain delete+search (Auto-fix); `action="mark_failed"` adds the blocklist
    step (Mark Failed). Returns FixResult — see fix_result.py for the
    ok/partial/failed status semantics and should_poll heuristic."""
    op_label = "Auto-fix" if action == "fix" else "Mark Failed"
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            if action == "fix":
                return await radarr.auto_fix(media["tmdb_id"])
            return await radarr.mark_failed(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                # Whole-season / whole-show variants are too destructive.
                return FixResult.failed(
                    f"{op_label} only works on individual episodes, not whole seasons."
                )
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            if action == "fix":
                return await sonarr.auto_fix_episode(tvdb_id, season, episode)
            return await sonarr.mark_failed_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("%s failed", op_label)
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")



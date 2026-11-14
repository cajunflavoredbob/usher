"""Automatic retry of failed Requests.

The MEDIA_FAILED webhook adds a row to the failed_retries table (see
`enqueue_failed_request`) when the switch is on. A job every 10 minutes reads
each due row's Request in Seerr first: any state but failed deletes the row
quietly (settled, retried by the Admin, or gone). A Request still failed gets
Seerr's own retry; a 409 from it also deletes the row. The schedule is 1 hour,
6 hours, then 24 hours for every try after. After the last try has been
made, the next check that still finds the Request failed tells the Requester
it needs the Admin and deletes the row.

A failure caused by one of our own retries sends MEDIA_FAILED again; the row
is refreshed, not restarted, so the schedule keeps going."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram.ext import ContextTypes

from bot.shared import format_media_title_line
from http_util import APIError, NotFoundAPIError
from seerr import REQUEST_STATUS_FAILED
from store import FailedRetry

logger = logging.getLogger("usher")

AUTO_RETRY_INTERVAL_S = 10 * 60
AUTO_RETRY_FIRST_DELAY_S = 180
# Seconds to wait before try 1, 2 and 3; every later try waits as long as the
# last entry.
RETRY_SCHEDULE_S = (3600, 6 * 3600, 24 * 3600)
MAX_TRIES_LIMIT = 5
_DB_TIME = "%Y-%m-%d %H:%M:%S"


def delay_before_try(tries_made: int) -> int:
    """Seconds to wait after `tries_made` retries before the next one."""
    return RETRY_SCHEDULE_S[min(tries_made, len(RETRY_SCHEDULE_S) - 1)]


def max_tries(cfg) -> int:
    try:
        n = int(getattr(cfg, "auto_retry_tries", 3))
    except (TypeError, ValueError):
        return 3
    return min(max(n, 1), MAX_TRIES_LIMIT)


def _db_time(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_DB_TIME)


def _parse_db_time(text: str) -> datetime:
    return datetime.strptime(text, _DB_TIME).replace(tzinfo=timezone.utc)


def wait_words(seconds: float) -> str:
    """"in about 1 hour" style wording for a wait."""
    if seconds < 55 * 60:
        minutes = max(1, round(seconds / 60))
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


async def enqueue_failed_request(
    app, request_id: Optional[int], tmdb_id: Optional[int],
    media_type: Optional[str], requester_chat_id: Optional[int],
    requester_seerr_id: Optional[int], now: Optional[datetime] = None,
) -> Optional[float]:
    """Add (or refresh) the failed Request's row when auto-retry is on.
    Returns the seconds until its next try, or None when nothing was queued
    (switch off, no request id, or the store failed)."""
    cfg = app.bot_data["settings_store"].settings
    if not getattr(cfg, "auto_retry_failed", False) or request_id is None:
        return None
    now = now or datetime.now(timezone.utc)
    store = app.bot_data["store"]
    try:
        next_try = await store.upsert_failed_retry(
            request_id, tmdb_id or 0, media_type or "", requester_chat_id,
            requester_seerr_id, _db_time(now + timedelta(
                seconds=delay_before_try(0))))
    except Exception:
        logger.exception("Couldn't queue failed Request #%s for retry", request_id)
        return None
    return max(0.0, (_parse_db_time(next_try) - now).total_seconds())


async def _tell_requester(app, row: FailedRetry, text: str) -> None:
    if not row.requester_chat_id:
        return
    try:
        await app.bot.send_message(chat_id=row.requester_chat_id, text=text,
                                   parse_mode="HTML",
                                   disable_web_page_preview=True)
    except Exception:
        logger.exception("Couldn't DM telegram_id=%s about Request #%d",
                         row.requester_chat_id, row.request_id)


async def _title_for(app, row: FailedRetry) -> str:
    if not row.tmdb_id or row.media_type not in ("movie", "tv"):
        return ""
    seerr = app.bot_data.get("seerr")
    try:
        return await format_media_title_line(
            seerr, {"media_type": row.media_type, "tmdbId": row.tmdb_id}) or ""
    except Exception:
        logger.debug("title lookup for Request #%d failed", row.request_id,
                     exc_info=True)
        return ""


async def _process(app, row: FailedRetry, tries_allowed: int,
                   now: datetime) -> None:
    seerr = app.bot_data["seerr"]
    store = app.bot_data["store"]
    try:
        current = await seerr.get_request(row.request_id)
    except NotFoundAPIError:
        await store.delete_failed_retry(row.request_id)
        logger.info("Auto-retry: Request #%d is gone; row removed", row.request_id)
        return
    if current.status != REQUEST_STATUS_FAILED:
        await store.delete_failed_retry(row.request_id)
        logger.info("Auto-retry: Request #%d is no longer failed; row removed",
                    row.request_id)
        return
    if row.tries >= tries_allowed:
        title = await _title_for(app, row)
        line = (f"⚠️ <b>{html.escape(title)}</b> still failed after "
                if title else "⚠️ Your request still failed after ")
        await _tell_requester(
            app, row,
            f"{line}{row.tries} automatic "
            f"{'retry' if row.tries == 1 else 'retries'}. It needs the "
            "Admin now.")
        await store.delete_failed_retry(row.request_id)
        logger.info("Auto-retry: Request #%d out of tries; row removed",
                    row.request_id)
        return
    try:
        await seerr.retry_request(row.request_id)
    except APIError as exc:
        if exc.status_code == 409:
            await store.delete_failed_retry(row.request_id)
            logger.info("Auto-retry: Request #%d answered 409; row removed",
                        row.request_id)
            return
        # Any other refusal counts as a try, so a broken Seerr is not hit
        # every pass; the next check still finds out whether it is failed.
        logger.warning("Auto-retry: retry of Request #%d refused: %s",
                       row.request_id, exc)
    tries = row.tries + 1
    await store.schedule_failed_retry(
        row.request_id, tries,
        _db_time(now + timedelta(seconds=delay_before_try(tries))))
    logger.info("Auto-retry: retried Request #%d (try %d of %d)",
                row.request_id, tries, tries_allowed)


async def run_auto_retry(app, now: Optional[datetime] = None) -> None:
    """One pass over the due rows. A row that fails to process is logged and
    left for the next pass; the others still run."""
    cfg = app.bot_data["settings_store"].settings
    if not getattr(cfg, "auto_retry_failed", False):
        return
    if app.bot_data.get("seerr") is None:
        return
    store = app.bot_data["store"]
    now = now or datetime.now(timezone.utc)
    for row in await store.list_due_failed_retries(_db_time(now)):
        try:
            await _process(app, row, max_tries(cfg), now)
        except Exception:
            logger.exception("Auto-retry of Request #%d failed (will try "
                             "again next pass)", row.request_id)


async def auto_retry_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_auto_retry(ctx.application)
    except Exception:
        logger.exception("Auto-retry job failed")

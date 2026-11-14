"""How a Reporter last played a Ticket's title, from Tautulli.

Admin-only: callers add these lines to the Admin's new-Ticket DM and the
Admin's Ticket card, never to a Reporter's view. Every failure (no Tautulli,
no Plex ratingKey, an unmapped Reporter, no matching play, a Tautulli error)
returns no lines, so the message simply omits them.
"""
from __future__ import annotations

import datetime
import asyncio
import html
import logging
from typing import Optional

from tautulli import find_user_by_plex_username, find_user_for_share

logger = logging.getLogger("usher." + __name__)

# History rows fetched when the match is made here (episodes), newest first.
_HISTORY_ROWS = 200

# Tautulli's transcode_decision values, as lowercase words. Now playing shows
# them as they are; the Ticket lines capitalize the first letter.
DECISIONS = {"direct play": "direct play", "copy": "direct stream",
             "transcode": "transcode"}


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pick_row(rows: list, media_type: str, season: Optional[int],
              episode: Optional[int]) -> Optional[dict]:
    """The newest history row for the Ticket's scope. A movie or a Ticket
    on the whole show takes the newest row; a season-only Ticket the newest
    row of that season; an episode Ticket only that season and episode."""
    # Seerr sends 0 for "not set": a whole-show Ticket is season 0.
    season = season or None
    episode = episode or None
    for r in rows:  # Tautulli returns newest first
        if media_type == "tv":
            if season is not None and _int(r.get("parent_media_index")) != season:
                continue
            if episode is not None and _int(r.get("media_index")) != episode:
                continue
        if r.get("row_id") is None:
            continue
        return r
    return None


def _format(row: dict, stream: dict) -> list[str]:
    def get(key):
        v = stream.get(key)
        return v if v not in (None, "") else row.get(key)

    decision = str(get("transcode_decision") or "").strip().lower()
    decision = DECISIONS.get(decision, decision).capitalize()
    parts = []
    player = str(get("player") or "").strip()
    platform = str(get("platform") or "").strip()
    if player:
        parts.append(f"<b>Player:</b> {html.escape(player)}")
    if platform:
        parts.append(f"<b>Platform:</b> {html.escape(platform)}")
    if decision:
        parts.append(f"<b>Playback:</b> {html.escape(decision)}")
    video = str(get("video_codec") or "").strip()
    audio = str(get("audio_codec") or "").strip()
    codecs = ", ".join(c for c in (
        f"video {video}" if video else "", f"audio {audio}" if audio else "") if c)
    if codecs:
        parts.append(f"<b>Codecs:</b> {html.escape(codecs)}")
    when = _int(row.get("date"))
    if when:
        try:
            parts.append("<b>Played:</b> " + datetime.datetime.fromtimestamp(
                when, datetime.timezone.utc).strftime("%Y-%m-%d"))
        except (OverflowError, OSError, ValueError):
            pass
    return parts


async def _playback_lines(bot_data, issue) -> list[str]:
    """HTML lines describing the Reporter's last play of the Ticket's
    title, or [] when anything is missing or fails. `issue` is a Seerr
    IssueListItem fetched with the Admin key."""
    client = bot_data.get("tautulli")
    rating_key = getattr(issue, "rating_key", None)
    if client is None or not rating_key:
        return []
    try:
        user = await find_user_by_plex_username(
            client, getattr(issue, "created_by_plex_username", None))
        user = user or await find_user_for_share(
            client, getattr(issue, "created_by_email", None),
            getattr(issue, "created_by_plex_username", None))
        if not user or user.get("user_id") is None:
            return []
        media_type = getattr(issue, "media_type", "")
        season = getattr(issue, "problem_season", None)
        episode = getattr(issue, "problem_episode", None)
        key = ("rating_key" if media_type == "movie"
               else "grandparent_rating_key")
        rows = await client.get_history(
            user_id=user["user_id"], order_column="date", order_dir="desc",
            length=1 if media_type == "movie" else _HISTORY_ROWS,
            **{key: rating_key})
        row = _pick_row(rows, media_type, season, episode)
        if row is None:
            return []
        stream = await client.get_stream_data(row["row_id"])
        return _format(row, stream if isinstance(stream, dict) else {})
    except Exception:
        logger.debug("Tautulli playback lookup failed", exc_info=True)
        return []


# Upper bound on the Tautulli lookups, so a slow Tautulli cannot hold back the
# Admin's new-Ticket DM or Ticket card.
PLAYBACK_TIMEOUT_S = 8.0


async def playback_lines(bot_data, issue) -> list[str]:
    """`_playback_lines` with a time limit; [] on timeout."""
    try:
        return await asyncio.wait_for(_playback_lines(bot_data, issue),
                                      PLAYBACK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.debug("Tautulli playback lookup timed out")
        return []

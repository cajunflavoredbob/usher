"""Named timeouts and limits used across multiple modules.

Single source of truth so tuning these doesn't require grepping a magic
number through the codebase.
"""
from __future__ import annotations

from typing import Final

# --- Plex link flow ---------------------------------------------------------
# Strong PIN has a 30-min lifetime at plex.tv; we poll for 28 min at 3s = 560
# iterations under the limit. Weak PIN: 15-min lifetime; we poll for 14 min.
PLEX_POLL_INTERVAL_S: Final = 3
PLEX_STRONG_PIN_MAX_ITERS: Final = 560   # 28 min, under the 30-min lifetime
PLEX_WEAK_PIN_MAX_ITERS: Final = 280     # 14 min, under the 15-min lifetime
PLEX_POLL_FAILURE_WARN_THRESHOLD: Final = 5  # consecutive failures before user DM
PLEX_POLL_MAX_BACKOFF_S: Final = 12.0

# --- Auto-fix poller --------------------------------------------------------
AUTOFIX_POLL_INTERVAL_S: Final = 60
# Morphing request cards: progress repaint cadence and how long a card is
# tracked before its final "still processing" edit.
REQUEST_WATCH_POLL_INTERVAL_S: Final = 45
REQUEST_WATCH_TIMEOUT_HOURS: Final = 24
AUTOFIX_POLL_FIRST_DELAY_S: Final = 30
AUTOFIX_TIMEOUT_HOURS: Final = 6

# --- UI keyboard limits -----------------------------------------------------
# Maximum buttons per row for the search-results keycap keyboard (1️⃣..9️⃣)
# before titles get illegible on iOS Telegram.
KB_BUTTONS_PER_ROW: Final = 3
# Short fixed-width buttons ("S3" / "E12" / "#41") tolerate wider rows.
SEASON_BUTTONS_PER_ROW: Final = 4
EPISODE_BUTTONS_PER_ROW: Final = 5
TICKET_BUTTONS_PER_ROW: Final = 4
# Labeled type buttons ("🎥 Video") wrap at 2 so labels never truncate.
TYPE_BUTTONS_PER_ROW: Final = 2
# Episode picker page size: 8 rows of 5. Long-running seasons (anime, soaps)
# can top 100 episodes, and Telegram rejects reply markup past 100 buttons,
# which used to kill the flow and leave a dead season keyboard.
EPISODE_PICKER_PAGE_SIZE: Final = 40
# Default search result count. Keycap-emoji buttons (1️⃣..9️⃣) are the
# hard ceiling; 5 is the practical UX default that keeps the search list
# compact while still surfacing useful alternates.
SEARCH_RESULT_LIMIT: Final = 5
REQUEST_LIST_TAKE: Final = 25
# /request search pagination: one Seerr fetch of up to 20 results (Seerr's
# server page size), rendered as screens of 5. Paging is a pure re-render
# from the stored batch -- no extra network calls.
REQUEST_SEARCH_FETCH_LIMIT: Final = 20
REQUEST_RESULTS_PER_PAGE: Final = 5
# Detail-card overview truncation (Telegram photo captions cap at 1024).
DETAIL_OVERVIEW_MAX_CHARS: Final = 300

# --- HTTP upload caps -------------------------------------------------------
ADMIN_UPLOAD_MAX_BYTES: Final = 32 * 1024 * 1024  # 32 MB for backup restores
# Per-member decompressed ceiling for restore ZIPs. The upload cap above only
# bounds COMPRESSED size; deflate can expand ~1000:1, so a crafted 32 MB zip
# could otherwise balloon into multi-GB allocations and OOM the container.
RESTORE_MEMBER_MAX_BYTES: Final = 512 * 1024 * 1024  # 512 MB

# --- Client lifecycle -------------------------------------------------------
# Grace before an evicted/retired httpx client is actually closed, so an
# in-flight request on a captured reference isn't killed. Must outlive a full
# retry chain (with_retry: ~4 x 15s + backoff). Shared by the Seerr user-client
# cache and bot/app.py's hot-reload close.
CLIENT_CLOSE_GRACE_S: Final = 90.0

# --- Account credentials ----------------------------------------------------
# Minimum admin-password length, enforced by the setup and change-password
# forms (client minlength + server check) so the number has one owner.
ADMIN_PASSWORD_MIN_CHARS: Final = 8
# Server-side floor for the backup passphrase; matches the admin-password
# minimum.
MIN_BACKUP_PASSPHRASE_CHARS: Final = ADMIN_PASSWORD_MIN_CHARS

# --- Conversation timeouts (seconds) ---------------------------------------
TICKET_REPLY_TIMEOUT_S: Final = 600    # 10 min
ISSUE_FLOW_TIMEOUT_S: Final = 600      # 10 min
REQUEST_FLOW_TIMEOUT_S: Final = 600    # 10 min
LINK_FLOW_TIMEOUT_S: Final = 1800      # 30 min (covers strong-PIN window)
RESOLVE_FLOW_TIMEOUT_S: Final = 600    # 10 min

# How long a relink-resume marker (the action interrupted by a revoked Plex
# token) stays valid. Matches LINK_FLOW_TIMEOUT_S so a resume can survive
# the full strong-PIN window but a stale draft can't fire hours later.
RELINK_RESUME_TTL_S: Final = 1800      # 30 min

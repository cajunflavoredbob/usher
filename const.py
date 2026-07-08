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
AUTOFIX_POLL_FIRST_DELAY_S: Final = 30
AUTOFIX_TIMEOUT_HOURS: Final = 6

# --- UI keyboard limits -----------------------------------------------------
# Maximum buttons per row before titles get illegible on iOS Telegram.
KB_BUTTONS_PER_ROW: Final = 3
# Default search result count. Keycap-emoji buttons (1️⃣..9️⃣) are the
# hard ceiling; 5 is the practical UX default that keeps the search list
# compact while still surfacing useful alternates.
SEARCH_RESULT_LIMIT: Final = 5

# --- HTTP upload caps -------------------------------------------------------
ADMIN_UPLOAD_MAX_BYTES: Final = 32 * 1024 * 1024  # 32 MB for backup restores
WEBHOOK_MAX_BYTES: Final = 128 * 1024             # 128 KB on the webhook handler

# --- Conversation timeouts (seconds) ---------------------------------------
TICKET_REPLY_TIMEOUT_S: Final = 600    # 10 min
ISSUE_FLOW_TIMEOUT_S: Final = 600      # 10 min
LINK_FLOW_TIMEOUT_S: Final = 1800      # 30 min (covers strong-PIN window)
RESOLVE_FLOW_TIMEOUT_S: Final = 600    # 10 min

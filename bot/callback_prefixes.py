"""Single source of truth for the callback_data prefixes used in inline
keyboards and registered as CallbackQueryHandler patterns.

Convention: each constant is the literal prefix string used before `:<id>`
(or stands alone for fixed-shape callbacks like LINK_HELP).
"""
from __future__ import annotations

from typing import Final

# --- Ticket management (/tickets, webhook DMs) -----------------------------
TK_OPEN: Final = "tkopen"             # [#N] from /tickets list -> detail view
TK_REPLY: Final = "tkr"               # [💬 Reply] (top-level + comment-DM)
TK_FIX: Final = "tkf"                 # [🔧 Fix] sub-menu opener (admin)
TK_CLOSE: Final = "tkc"               # [✅ Close] sub-menu opener (admin)
TK_BACK: Final = "tkback"             # [⬅️ Cancel] in sub-menus

# Sub-menu actions
TK_FIX_REDOWNLOAD: Final = "tkfd"     # delete + search
TK_FIX_MARK_FAILED: Final = "tkfm"    # blocklist + delete + search
TK_CLOSE_WITH_COMMENT: Final = "tkcc"
TK_CLOSE_DIRECT: Final = "tkcd"

# --- /link flow ------------------------------------------------------------
LINK_CONSENT: Final = "link_consent"  # link_consent:yes / link_consent:no
LINK_PLATFORM: Final = "tklplat"      # tklplat:desktop / tklplat:mobile
LINK_HELP: Final = "tklhelp"          # fixed-shape "Didn't work?" callback
RELINK: Final = "relink"              # fixed-shape "Unlink & sign in again"
                                      # (revoked-Plex-token recovery)

# --- /issue flow -----------------------------------------------------------
ISSUE_MEDIA: Final = "media"
ISSUE_SEASON: Final = "season"
ISSUE_EPISODE: Final = "ep"
ISSUE_TYPE: Final = "type"
ISSUE_AUTOFIX_OFFER: Final = "autofix"
ISSUE_AUTOFIX_CONFIRM: Final = "confirm"
ISSUE_CANCEL: Final = "cancel"
ISSUE_RESEARCH_PARENT: Final = "research_parent"

# --- Post-autofix resolve follow-up ----------------------------------------
RESOLVE: Final = "resolve"            # resolve:<issue_id>:yes|no|skip

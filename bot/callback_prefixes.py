"""Single source of truth for the callback_data prefixes used in inline
keyboards and registered as CallbackQueryHandler patterns.

Convention: each constant is the literal prefix string used before `:<id>`
(or stands alone for fixed-shape callbacks like LINK_HELP).
"""
from __future__ import annotations

from typing import Final

# --- Ticket management (/tickets, webhook DMs) -----------------------------
TK_OPEN: Final = "tkopen"             # [#N] from the ticket list, or [📜 History]
                                      # in a ticket DM -> detail view
TK_REPLY: Final = "tkr"               # [💬 Reply] (top-level + comment-DM)
TK_FIX: Final = "tkf"                 # [🔧 Fix] sub-menu opener (admin)
TK_CLOSE: Final = "tkc"               # [✅ Close] sub-menu opener (admin)
TK_BACK: Final = "tkback"             # [⬅️ Back] in sub-menus
TK_BACK_LIST: Final = "tkbl"          # [⬅️ Back] in sub-menus of a detail card
                                      # opened from the ticket list

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
ISSUE_EPISODE_PAGE: Final = "ep_page"
ISSUE_TYPE: Final = "type"
ISSUE_AUTOFIX_OFFER: Final = "autofix"
ISSUE_AUTOFIX_CONFIRM: Final = "confirm"
ISSUE_CANCEL: Final = "cancel"
ISSUE_RESEARCH_PARENT: Final = "research_parent"

# --- /request flow ----------------------------------------------------------
RQ_MEDIA: Final = "rqm"               # rqm:<version>:<media_type>:<tmdb_id>
RQ_SEASON: Final = "rqs"              # rqs:<version>:<n> toggle a season
RQ_SEASON_NA: Final = "rqna"          # rqna:<version>:<n> unrequestable season
RQ_SEASON_ALL: Final = "rqall"        # rqall:<version> select all requestable
RQ_SEASON_DONE: Final = "rqdone"      # rqdone:<version> proceed to confirm
RQ_GO: Final = "rqgo"                 # rqgo:<version> submit (standard)
RQ_GO_4K: Final = "rqgo4k"            # rqgo4k:<version> submit as 4K
RQ_PAGE: Final = "rqpg"               # rqpg:<version>:<screen> result paging
RQ_INFO: Final = "rqi"                # rqi:<version>:<media_type>:<tmdb_id>
                                      # detail card (poster + overview)
RQ_INFO_DISMISS: Final = "rqx"        # dismiss a detail card (global handler)
RQ_CANCEL: Final = "rqcancel"         # rqcancel:<version> (bare via /cancel)
RQ_FROM_ISSUE: Final = "rqfi"         # rqfi:<media_type>:<tmdb_id> jump from
                                      # the /issue not-in-library dead-end
RQ_LIST_CANCEL: Final = "rqlc"        # rqlc:<request_id> cancel from /requests

# --- /trending browse --------------------------------------------------------
DV_CATEGORY: Final = "dvc"            # dvc:<category> switch browse list
DV_PAGE: Final = "dvp"                # dvp:<version>:<screen> browse paging
DV_INFO: Final = "dvi"                # dvi:<version>:<media_type>:<tmdb_id>

# --- Availability subscriptions ----------------------------------------------
SUBSCRIPTION_ADD: Final = "subadd"    # subadd:<media_type>:<tmdb_id>
SUBSCRIPTION_DEL: Final = "subdel"    # subdel:<id> from /subscriptions

# --- Admin provisioning (/invite, /uninvite, the Admin Hub's Shares) ---------
INV_LIB: Final = "invl"               # invl:<version>:<section_id> toggle
INV_ALL: Final = "inva"               # inva:<version> select all libraries
INV_DONE: Final = "invd"              # invd:<version> proceed to confirm
INV_GO: Final = "invgo"               # invgo:<version> send the invite
INV_CANCEL: Final = "invx"            # invx:<version> cancel the invite flow
UNINV_GO: Final = "uninv"             # uninv:<share_id>:<a|p> confirmed removal
UNINV_KEEP: Final = "uninvk"          # dismiss the /uninvite confirm card
SHARE_VIEW: Final = "shv"             # shv:<share_id> one Share's view
SHARE_REMOVE: Final = "shrm"          # shrm:<share_id> Remove -> confirm card
SHARE_MATCHES: Final = "shq"          # shq:<page> Prev/Next of typed /uninvite
                                      # matches (search text in user_data)

# --- Post-autofix resolve follow-up ----------------------------------------
RESOLVE: Final = "resolve"            # resolve:<issue_id>:yes|no|skip

# --- Hubs (shared by every Hub; see bot.hub) --------------------------------
HUB_OPEN: Final = "hubo"              # hubo:<hub> "⬅️ <Hub>" back button:
                                      # re-render that Hub in place
HUB_RETRY: Final = "hubry"            # hubry:<step>[:<arg>] Try again for steps
                                      # re-run by hub_retry_*; other Try again
                                      # buttons reuse their entry callback
ACCOUNT_LINK: Final = "acclk"         # Account Hub "Link" button (enters the
                                      # link conversation)
ACCOUNT_UNLINK: Final = "accul"       # Account Hub "Unlink" button
REQUESTS_NEW: Final = "rqhn"          # Requests Hub "New request" (request
                                      # conversation entry point)
REQUESTS_MINE: Final = "rqhm"         # Requests Hub "My requests" (global handler)
REQUESTS_TRENDING: Final = "rqht"     # Requests Hub "Trending" (global handler)
REQUESTS_SUBSCRIPTIONS: Final = "rqhs"  # Requests Hub "Subscriptions" (global
                                      # handler)
TICKETS_REPORT: Final = "tkhr"        # Tickets Hub "Report a problem" (issue
                                      # conversation entry point)
TICKETS_LIST: Final = "tkhl"          # Tickets Hub list button and a detail
                                      # card's "⬅️ Tickets": show the list
ADMIN_INVITE: Final = "admi"          # Admin Hub "Invite" (invite conversation
                                      # entry point)
ADMIN_SHARES: Final = "adms"          # adms:<page> Admin Hub "Shares" (page 0),
                                      # Prev/Next and "⬅️ Shares"
ADMIN_STATUS: Final = "admt"          # Admin Hub "Status" (global handler)

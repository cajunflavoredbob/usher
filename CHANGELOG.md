# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.4] - 2026-05-28

### Security
- **CSRF tokens on every admin POST.** Double-submit cookie pattern: a `hermes_csrf` cookie (SameSite=Strict) is set on every form-rendering page; every POST validates the cookie matches a hidden `csrf_token` form field via `hmac.compare_digest`. Pre-auth flows (`/admin/setup`, `/admin/login`) and authenticated flows (`/admin/*`) both gated. Validation lives in `auth_util.validate_csrf`.
- **Login throttling.** 5 failed login attempts per IP within a 5-minute sliding window returns HTTP 429 with `Retry-After`; success resets the counter. `auth_util.LoginThrottle` is process-local in-memory.
- **First-run setup token.** On a fresh install (no admin account, no `/data/setup_token` file), `auth_util.load_or_create_setup_token` generates a random token, persists it to `/data/setup_token` (chmod 600), and logs it at WARNING level. `/admin/setup` rejects submissions without a matching token, so the first visitor on a LAN-exposed port can't claim the admin account silently. Token is cleared on successful setup. Existing installs (admin already configured) skip the token check entirely.
- **Audit log.** Dedicated `hermes.audit` logger records `login_success`, `login_fail`, `login_throttled`, `login_csrf_fail`, `setup_csrf_fail`, `setup_token_fail`, `setup_complete`, `password_changed`, `backup_download`, `restore_complete`, `admin_csrf_fail`, `logout` with `user`, `ip`, and event-specific fields. Searchable with `grep hermes.audit`.
- **`Secure` cookie flag when behind HTTPS.** Session and CSRF cookies set `Secure=True` when `request.scheme == "https"` or `X-Forwarded-Proto: https` is present. Plain-HTTP LAN installs keep working unchanged.
- **Backup ZIP can be wrapped with a passphrase.** New `backup_crypto.py`: PBKDF2(SHA-256, 600k iters) over the passphrase → 32-byte key → Fernet (AES-128-CBC + HMAC-SHA256). Output format: 12-byte magic `HERMES-BAK1\n` + 16-byte salt + Fernet token. File extension changes to `.hermes-backup`. Restore detects the format by magic prefix and prompts for the passphrase. The Account tab adds an optional "Passphrase" field to both backup and restore forms.
- **`restore_upload` validates uploads before overwriting.** Each member of the uploaded ZIP is sanity-checked: `settings.json` must parse and accept into the `Settings` dataclass; `encryption.key` must initialize a `Fernet(key)` without raising; `mappings.sqlite` is written to a tempfile and validated with `PRAGMA integrity_check` (must return `ok`). Current `settings.json`, `mappings.sqlite`, and `encryption.key` are copied to `/data/pre-restore-YYYYMMDD-HHMMSS/` before the new files land.

### Changed
- **Clean shutdown instead of `os._exit(0)`.** Settings-change-triggered restarts (`bot.py` setup-only mode + full-mode `_on_settings_changed`) and the post-restore exit in `webui.py` now send `SIGTERM` to self after a 2s delay via `_schedule_clean_exit`, letting PTB's `run_polling` and aiohttp's `runner.cleanup()` close httpx clients, DB connections, and the HTTP server gracefully. Falls back to `os._exit(0)` only if `SIGTERM` dispatch itself fails.
- **`/admin/backup` is now a POST.** Previous GET still worked but couldn't accept the optional passphrase. CSRF token required. Plain (no passphrase) downloads still serve a `.zip`.

### Added
- **`auth_util.py`** — `LoginThrottle`, CSRF helpers (`validate_csrf`, `attach_csrf_cookie`, `generate_csrf_token`, `csrf_for_request`), setup-token persistence (`load_or_create_setup_token`, `clear_setup_token`), `request_is_secure`, `client_ip`, `audit`.
- **`backup_crypto.py`** — `wrap`, `unwrap`, `is_wrapped`, format constants.
- **Tests** — `tests/test_auth_util.py` (throttle lifecycle, CSRF, setup token, secure-cookie detection, client IP — 19 cases), `tests/test_backup_crypto.py` (round-trip, wrong passphrase, malformed input, magic detection, salt randomness — 6 cases). Total 114 tests.

### Notes
- Phase 4 of the v0.11.x hardening roadmap. Closes audit findings #7 (CSRF + throttle + first-run guard), #8 (restore validation + backup snapshot), #10 (cookie Secure), #11 (backup passphrase), security #17 (clean shutdown), and adds a separate audit-log surface.
- Existing installs upgrading from <0.11.4 keep working: the setup-token guard only fires when `admin.is_set()` is False, and existing CSRF cookies are minted on the next GET. No data migration required.

## [0.11.3] - 2026-05-28

### Added
- **`http_util.py`** — shared `APIError` hierarchy (`TransientAPIError`, `PermanentAPIError`, `NotFoundAPIError`), error-body parser (extracts Seerr/Arr `{"message": "..."}` so user-facing text carries the real reason), `execute()` wrapper that handles retry + classify in one call, and `user_friendly_message(exc)` that formats exceptions for Telegram without leaking URLs/headers/stack traces.
- **`fix_result.py`** — `FixResult` dataclass with `status` (`ok`/`partial`/`failed`), `message`, `steps_done`, and `poll_info`. `.should_poll` is True iff a fresh search was triggered, so partial successes still enqueue the autofix poller.
- **Transient-failure retry across all API clients.** Seerr / Sonarr / Radarr / Plex calls now retry 429 + 502/503/504/408, plus `httpx.ConnectError`, `TimeoutException`, and `RemoteProtocolError`, up to 3 times with capped exponential backoff (0.5s → 5s cap, jittered). 4xx (except retryable) raises immediately as `PermanentAPIError`; 404 surfaces as `NotFoundAPIError`.
- **Autofix poller distinguishes media-gone from transient.** `NotFoundAPIError` on the `has_file` poll marks the pending row `failed` and DMs the user that the media was removed instead of polling for the full 6h timeout. `TransientAPIError` keeps polling next tick.
- **Plex pin-poll escalation.** Per-poll backoff goes 3s → 6s → 12s on consecutive failures; after 5 in a row the user gets a one-time "⚠️ Plex's API isn't responding right now" DM so they don't think the bot is silently broken.
- **Telegram BadRequest fallback** via new `_edit_or_send(q, text, **kwargs)` helper. When `edit_message_text` fails because the admin edited or deleted the source message, a new `send_message` lands in the same chat instead of silently swallowing the response.

### Changed
- **Mark Failed / Auto Fix return `FixResult`.** `sonarr.mark_failed_episode`, `sonarr.auto_fix_episode`, `sonarr.auto_fix_season`, `radarr.mark_failed`, `radarr.auto_fix`, plus `_run_autofix` / `_run_mark_failed` all use the structured result. `_apply_fix` consumes `result.should_poll` and `result.status` to deliver honest partial-success messages while still enqueueing the poller when search succeeded.
- **All user-facing exception strings sanitized.** ~14 sites in `bot.py` (cmd_tickets, tk_*, _finalize_link, _submit_issue, _check_connections, cmd_link*, etc.) now route via `user_friendly_message()` so users see "Seerr isn't responding right now; try again in a minute." instead of `Client error '503 Service Unavailable' for url 'http://192.168.x.x...'`. Logs still get the full exception via `logger.exception`.

### Notes
- Phase 3 of the v0.11.x hardening roadmap. Closes audit findings #2 (partial-success), #5 (poller media-gone), #9 (Plex poll escalation), #10 (retry), #11 (error-body parsing), #24 (Telegram BadRequest), and security #8 (exception leakage).
- New tests in `tests/test_http_util.py` (26 cases) cover the response classifier, retry-on-transient, no-retry-on-permanent + no-retry-on-404 paths, connection-error wrapping, and `user_friendly_message` for all branches. Existing 63 tests still pass; total 89.

## [0.11.2] - 2026-05-28

### Added
- **pytest harness.** New `tests/` directory with coverage of `Settings` round-trips and `SettingsStore` upgrade paths (`test_settings.py`); `UserStore` lifecycle including decrypt-failed semantics across key rotation and concurrent-write smoke (`test_store.py`); webhook auth + dispatch + dedupe + size cap + handler-exception containment (`test_webhook.py`); pure helpers `_format_age` and `_derive_parent_name` (`test_helpers.py`). 63 tests total, run in <1s locally.
- **Dev dependencies file** (`requirements-dev.txt`) — pytest + pytest-asyncio. Not pulled into the Docker image.
- **`pytest.ini`** sets `asyncio_mode = auto` so async tests don't need a per-function decorator.
- **CI test workflow** (`.github/workflows/test.yml`) runs on every push to main and on PRs.
- **Release workflow now gates on tests.** The build job depends on a test job; tag pushes won't ship an image if tests fail.

### Notes
- Phase 2 of the v0.11.x hardening roadmap. No source-code logic changes; this is purely a safety net for the heavier refactors in v0.11.5 (bot.py split) and v0.11.6 (concurrency hardening).
- The `_format_age` and `_derive_parent_name` tests import `bot.py` directly. `bot.py` has no side effects at module level beyond imports + class/function defs, so this is safe in CI without env vars set.

## [0.11.1] - 2026-05-28

### Changed
- **All `UserStore` methods are now async.** SQLite work moves into `asyncio.to_thread` so a slow DB call no longer blocks the event loop (other coroutines like the Plex `/link` poll, webhook handlers, and autofix poller can now interleave with DB activity). All ~25 call sites in `bot.py` updated with `await`.
- **`_token_for` is now async and returns a tri-state `(is_admin, token, decrypt_failed)`.** Callers can distinguish "not linked" from "linked but token won't decrypt" and surface the right message.

### Added
- **WAL journal mode and `busy_timeout=5000`.** The SQLite connection helper opens with a 5-second busy timeout, and the schema-init step enables WAL once per database file. Combined, these eliminate `database is locked` exceptions under realistic concurrent load (webhook flood + autofix poll + in-flight `/link` poll).
- **Locked-DB retry with backoff.** `UserStore._run_sync_with_retry` retries `OperationalError("...locked...")` up to 5 times with exponential backoff (50ms → 800ms; worst-case total ~1.5s) before re-raising. Lives off the event loop so the sleeps don't stall anything else.
- **`Mapping.plex_token_decrypt_failed: bool`.** Distinguishes a token row that exists but can't be decrypted (likely key rotation) from a row that has no token (legacy or never linked with Plex).
- **`UserStore.count_decrypt_failures()`.** Used at startup to count mappings whose tokens won't decrypt with the current key. If the count is non-zero, admin gets a one-time DM at startup with the count and a pointer to investigate.
- **User-visible "link broken" message.** When a callsite needs a user's Plex token and finds the row exists but decrypt failed, the user is told to `/unlink` and re-run `/link` instead of being silently treated as unlinked.

### Removed
- `TokenCrypto.safe_decrypt` — its tri-state logic moved into `UserStore._decrypt_field` so `Mapping` construction can populate `plex_token_decrypt_failed` cleanly.

### Notes
- Derived from audit findings #4 (concurrency: sync SQLite blocks event loop), #11 (error: no `OperationalError` retry), and #12 (security: silent decrypt-fail masks broken links). Phase 1 of the v0.11.x hardening roadmap.
- WAL mode persists across connections, so the one-time `PRAGMA journal_mode = WAL` at schema init is enough; existing prod databases will be migrated to WAL automatically the next time `_init_schema` runs.

## [0.11.0] - 2026-05-28

### Security
- **Webhook secret comparison is now constant-time.** `webhook.py` switched from `auth != secret` to `hmac.compare_digest(...)` to eliminate the timing side-channel that could let a LAN-resident attacker recover the secret byte by byte.
- **Webhook secret is now mandatory.** Previously empty was silently allowed, which made the receiver accept any POST. `SettingsStore` auto-generates a secret on first load if none is present (a warning is logged with a pointer to the `/admin` Webhook tab where the value can be read and pasted into Seerr's webhook config). The webhook handler refuses all POSTs when the secret is somehow unset (defense in depth). The webui rejects an empty secret on save.

### Added
- **Webhook body size cap (128KB).** The parent aiohttp app keeps its 32MB ceiling for admin backup restores; the webhook handler now enforces a 128KB ceiling via Content-Length check + read-time check, so unauthenticated clients can't force 32MB allocations per request.
- **Webhook deduplication.** A 60-second / 256-entry bounded SHA-256 body-hash cache drops duplicate deliveries (e.g., Seerr retries after a transient blip). The dedupe cache lives in the handler closure and evicts on TTL + size.
- **`hermes_public_url` scheme validation.** The webui's Telegram tab now rejects URLs that don't start with `http://` or `https://`. Empty is still acceptable (means "not configured"). New `settings.validate_public_url(url)` helper.

### Fixed
- **Webhook handlers no longer return 500 on internal exceptions.** Previously a transient Telegram 429 or DB lock inside `handle_seerr_*` returned 500, Seerr retried on backoff, and once throttling cleared the admin got duplicate DMs for the same event. Now: log the exception, return 200. We'd rather lose one notification than spam the admin with five.

### Notes
- Derived from the four-agent audit (security findings #1, #2, #14; error finding #1; security finding #13). Phase 0 of the v0.11.x hardening roadmap. See `~/hermes_briefing.md` for the full roadmap.

## [0.10.6] - 2026-05-28

### Fixed
- **"Mark Failed" now actually replaces the file.** Previous behavior only called Sonarr/Radarr's `/history/failed/{id}` endpoint, which blocklists the release but does NOT delete the on-disk file or trigger a new search unless the global "Redownload Failed" setting is on (it usually isn't). Symptom: instant "marked failed" confirmation, but the broken file stayed on disk and SABnzbd never saw a new job.

  `mark_failed_episode` (Sonarr) and `mark_failed` (Radarr) now perform a strict superset of `auto_fix` / `auto_fix_episode`:
  1. Blocklist the most recent grab via `/history/failed/{id}` (skipped cleanly if no grab history exists).
  2. Delete the on-disk file (if present).
  3. Trigger a fresh search.

  Success message updated to reflect the actual end-state. `poll_info` shape unchanged, so the existing post-fix polling in `_run_mark_failed` continues to work without modification.

### Notes
- Discovered via ticket #28 (`Mating Season S01E08`): file was a 1.1 GB zero-byte payload (EBML header all `0x00`) that Plex's `/transcode/universal/decision` endpoint rejected with HTTP 400. "Mark Failed" was the right user intent but didn't carry out the file replacement.

## [0.10.5] - 2026-05-28

### Added
- **Ticket detail now includes the original report text.** `IssueListItem` gains a `description` field; `SeerrClient.get_issue` populates it from the first entry in the issue's `comments` array (Seerr stores the original description there at creation time). The `/tickets` drilldown now appends:
  ```
  Description:
  "subs suck"
  ```
  under the metadata block.

### Changed
- Close sub-menu button labels trimmed: `[💬 With comment]` → `[💬 Comment]`, `[✓ Without comment]` → `[✓ No comment]`. Cancel unchanged.

## [0.10.4] - 2026-05-28

### Fixed
- **Reply was silently no-op'ing on the second attempt** after a previous reply conversation was abandoned without sending text or `/cancel`. `_ticket_conversation` was missing `allow_reentry=True`, so the entry-point callback was a no-op while a stale conversation was still considered "active." Added the flag — re-tapping Reply now re-enters cleanly.

### Added
- **Global "stale button" gate with 6-hour expiry.** A `TypeHandler` registered at group `-1` runs before any callback handler. It tracks the most recent button-bearing bot message per user (`bot_data["btn_msgs"][user_id]`) and dismisses callbacks from older messages or messages older than 6 hours with an explanatory toast + strips the buttons. Prevents stale buttons in chat history from misfiring. Six-hour TTL matches the auto-fix completion timeout.
- **`[⬅️ Cancel]` button on the Close and Fix sub-menus.** Tapping returns to the ticket detail view (edits the message back via the new `tk_back` handler) so admin can back out without committing to either action.
- Removed the `[✅ Close]` shortcut from the Fix sub-menu — Close belongs only under the dedicated Close path, per "Close should be the only one with close options."

### Changed
- **Admin top-level `[💬 Reply]` now goes straight to the reply input** (no `[Reply] [Close]` sub-menu). Only `[Close]` and `[🔧 Fix]` keep their sub-menus (since each has multiple action variants). Affects both the `/tickets` detail and the new-issue admin DM. The `tk_reply_menu` handler is retained for backward compatibility with any old buttons still floating in chat history, but no new buttons route to it.
- **Ticket detail message now shows full context.** Tapping `[#N]` from `/tickets` previously opened a bare `Ticket #N — choose an action` line. Now `tk_open` fetches the issue via `SeerrClient.get_issue()` and renders:
  ```
  Ticket #28
  📺 Mating Season (2026) — S01E08
  Issue: 🎥 Video
  Reported by: FooManChewy
  Age: 7h ago
  ```
  Same buttons attached. Refactored into a `_build_ticket_detail` helper shared with `tk_back`.

### Notes
- Button tracking applies to `/tickets`-flow messages (list, detail, sub-menus) and webhook DMs (new-issue admin, comment-reply). `/issue` and `/link` flows aren't yet tracked — those are bounded conversations that self-clean on completion or timeout, so staleness is less of a concern. Easy to extend later.

### Changed
- Hermes version now reported in the "Bot is online" status block as a regular bullet alongside Seerr/Radarr/Sonarr (e.g. `• Hermes: ✅ 0.10.3`) instead of being prefixed to the header line. Consistent style with the other services.

## [0.10.2] - 2026-05-27

### Changed
- **Search-result buttons switched from title-as-label to keycap emoji buttons** (1️⃣ 2️⃣ 3️⃣ …) laid out 3 per row max. For 5 results this lands as `[1 2 3]` on the top row and `[4 5 Cancel]` on the bottom row (Cancel piggybacks on the last partial row when there's space). Full titles are now in the message body where Telegram can word-wrap them, so no more truncation regardless of title length. The `\n`-in-label trick from v0.10.1 didn't render as line breaks on iOS/Android Telegram clients (only Desktop seemed to honor it), so this is a cleaner path. Removed the now-dead `_format_search_button` helper.

## [0.10.1] - 2026-05-27

### Added
- **Multi-line search-result buttons.** Long media titles no longer get hard-truncated at ~60 chars. A `\n` is inserted near the middle of long labels so Telegram renders the button on two lines, doubling visible width before any truncation kicks in (cap is now 90 chars).
- **Specials (S00) included in the TV season picker** as a "Specials" entry. Anime movies / OVAs / tie-in specials often live there; users were previously locked out of reporting issues on them. Discovered via a tester case (Demon Slayer: Infinity Castle = S00E16 of the parent show).
- **Parent-show re-search fallback.** When a search query contains a separator (`:`, ` - `, ` — `, ` | `) AND every Seerr result is out-of-library, the bot offers a `🔍 Search "..." instead` button using the part before the separator. Same button also shows on the "title isn't in your library" error message, so the user can pivot without restarting `/issue`. Refactored `issue_title` and the new `issue_research_parent` callback to share a `_show_search_results` helper.
- **Hermes version in the "Bot is online" startup DM.** Header is now `👋 Hermes vX.Y.Z is online.`
- **Admin also DM'd on resolved issues.** `handle_seerr_resolved` now sends a notification to the admin (in addition to the reporter) whenever a ticket is resolved, unless the admin IS the reporter. Format: `✅ Issue #N resolved\n<title>\nReported by: <user>`. Filled the gap left when we disabled Seerr's built-in Telegram notification agent in favor of Hermes's webhook.

### Fixed
- **New-issue admin DMs were silently dropped.** v0.10.0 looked for `notification_type == "ISSUE_REPORTED"`, but Seerr's webhook payload uses the enum name `ISSUE_CREATED` (the UI label "Issue Reported" is just a display string). The receiver now accepts both spellings. Also bumped the "unhandled notification_type" log from `DEBUG` to `INFO` so future payload-name mismatches surface immediately.

## [0.10.0] - 2026-05-26

### Added
- **Admin DM on new issue.** When a user files a ticket, the admin gets a rich DM with media context, issue type, reporter, status, description, and inline action buttons. Skipped when the admin is the reporter (they already got the `/issue` confirmation).
- **`ISSUE_REPORTED` webhook handler** in the receiver (alongside the existing `ISSUE_COMMENT` and `ISSUE_RESOLVED` dispatches). Requires "Issue Reported" enabled in Seerr → Notifications → Webhook.
- **`SonarrClient.mark_failed_episode` / `RadarrClient.mark_failed`** — call the `*/history/failed/{historyId}` endpoint on the most recent grab record. Radarr/Sonarr handle the blocklist + new-search side effects automatically.
- **`_run_mark_failed`** -- sibling of `_run_autofix`, routes movie vs episode to the right client method.

### Changed
- **Admin ticket detail now has 2-level menus** (matches the Close menu pattern that already existed):
  - Top-level: `[💬 Reply] [🔧 Fix] [✅ Close]`
  - **Reply sub-menu** (admin): `[💬 Reply]` (reply input) / `[✅ Close]` (close without comment, quick out)
  - **Fix sub-menu** (admin): `[🔄 Redownload]` (delete + search, the old auto-fix behavior) / `[🚫 Mark Failed]` (NEW — Radarr/Sonarr blocklist + re-search) / `[✅ Close]` (quick out)
  - **Close sub-menu** (admin, unchanged): `[💬 With comment]` / `[✓ Without comment]`
  - Users still get a single-tap `[💬 Reply]` that goes straight to input (no sub-menu).
- **Comment notification format** now uses an explicit `From:` line and `Comment:` label with blank-line spacing between sections.
- **Episode formatting is zero-padded everywhere** (`S01E02` instead of `S1E2`) -- /tickets list, /issue confirmation, comment DM, resolved DM, new-issue admin DM, fix-started message.

### Already in tree from v0.9.7 work (rolled up):
- Whole-season / whole-show auto-fix is refused (episode or movie only).
- `ISSUE_RESOLVED` webhook DMs the reporter when their ticket is closed.

### Notes
- Non-admin auto-fix from the `/issue` flow remains delete + search only; users do not get a `[Mark Failed]` option.
- `sonarr.auto_fix_season` is still defined but unreferenced.

## [0.9.6] - 2026-05-26

### Added
- **Admin `[🔧 Fix]` button on the ticket detail.** Admin ticket actions are now `[💬 Reply] [🔧 Fix] [✅ Close]`. Fix triggers the auto-fix delete-and-research flow on the media the ticket is about (movie via Radarr, TV episode/season via Sonarr), and enqueues a pending-autofix record so the admin gets a DM when the new file finishes downloading.
- `SeerrClient.get_issue(id)` for fetching a single ticket's details (needed to know what media + S/E to fix).

### Changed
- Auth code now renders as plain bold text (e.g. **ABCD**) instead of the emoji-sized squared letters. Added a blank line under the code plus a "Code expires in 15 minutes." line as filler so the code has breathing room above the inline button (Telegram strips trailing blank lines otherwise).
- Removed the now-unused `_emoji_code` helper.

## [0.9.5] - 2026-05-26

### Changed
- Auth code rendering uses Negative Squared Latin Capital Letters (🅐-🅩) for letters instead of Mathematical Bold (𝐀-𝐙). The squared letters render at full emoji size on Telegram and match the keycap digit emojis, so the whole code is visually consistent and actually large.

## [0.9.4] - 2026-05-26

### Fixed
- **Polling loop race when user tapped "Didn't work?" mid-poll.** The cancel-flag pattern in v0.9.3 had a set-true-then-reset-false sequence around the `await request_pin(...)` call. The old poll's sleep could outlast the flag's True state, miss it, then see the reset-to-False, and keep running. Two concurrent polls hit Plex's rate limit (429 Too Many Requests) and probably caused the spurious 400 some testers saw on `request_pin`. Replaced the boolean flag with a per-loop random `loop_id`; new loops claim the slot, old loops bail when their ID no longer matches. No reset, no race.
- **Plex `request_pin` now logs the response body on HTTP errors** so future "400 Bad Request" reports include Plex's actual error message in the log.

### Changed
- **Weak-PIN fallback message redesigned:**
  - Code rendered as big bold characters (Mathematical Bold for letters, keycap emojis for digits) — visible from across the room.
  - `disable_web_page_preview=True` so no Plex preview card.
  - Removed "plex.tv/link" from the message body to kill Telegram's auto-linking; the URL only appears in the `[📋 Copy plex.tv/link]` button label. Users tap the button, paste in a browser, then type the short code from memory.

## [0.9.3] - 2026-05-26

### Changed
- **Reworked `/link` into a guided multi-step flow with platform-aware paths and a fail-state fallback.** After consent, the bot asks the user whether they're on Desktop or iOS/Android.
  - **Desktop:** strong-PIN auth URL surfaced as a `[🌐 Open Plex authorization]` button. One tap, opens in their normal browser, sign in + Allow.
  - **iOS/Android:** strong-PIN auth URL exposed as a `[📋 Copy auth link]` button (uses Bot API 8.0's `CopyTextButton`). User pastes it into a real browser to avoid Telegram's in-app webview, which closes Plex's sign-in too early on iOS.
  - **`[❌ Having trouble?]` / `[❌ Didn't work?]`** button on either path drops to a weak-PIN fallback: a 4-character code in a prominent code block + `[📋 Copy plex.tv/link]` button. User types the code manually into plex.tv/link.
- The polling loop now respects a cancellation flag in `user_data`, so the "Didn't work?" callback can interrupt an in-progress strong-PIN poll and start fresh with a weak PIN without leaking the prior poll or its message state.
- Strong-PIN poll window is back to ~28 min (matches the strong PIN's 30-min lifetime); weak-PIN fallback stays at ~14 min.
- Improved Seerr-rejection message ("Plex authorized you, but Seerr rejected the sign-in") to point users at the actual fix (admin needs to invite them).
- Success and timeout messages trimmed for brevity.

### Dependency
- **Bumped `python-telegram-bot[ext]` from `21.6` to `22.7`** (latest stable v22) to unlock Bot API 8.0's `CopyTextButton`. Otherwise drop-in for our handler patterns.

## [0.9.2] - 2026-05-26

### Fixed
- **`plex.tv/link` fallback now actually works.** v0.9.1 displayed the PIN code from a `strong=true` Plex request, which returns a 25-char hash that `plex.tv/link` rejects. Switched to `strong=false` so we get the 4-char human-friendly code that `plex.tv/link` expects. The deeplink auth URL still works the same way.

### Changed
- PIN poll window shortened from ~28 minutes to ~14 minutes to match the shorter 15-minute lifetime of `strong=false` PINs.

## [0.9.1] - 2026-05-26

### Fixed
- **`/link` blocked all other commands while it was waiting on Plex.** `cmd_link_consent` runs the PIN polling loop (`await asyncio.sleep(3)` per iteration), and PTB's default `concurrent_updates=False` meant every other incoming update queued behind it. So if anyone was mid-`/link`, commands like `/tickets` from anyone (including the admin) sat in queue until the link flow finished or timed out. Set `concurrent_updates(True)` on the Application builder — updates now process in parallel. SQLite serializes its own access, httpx AsyncClient is concurrency-safe, and bot_data is read-mostly, so the change is safe across all existing code paths.

### Changed
- **Extended Plex PIN poll window from 5 minutes to ~28 minutes** to match the PIN's 30-minute lifetime. The previous 5-minute cap meant users who got distracted mid-flow lost their PIN even though Plex would still honor it.
- **Added `plex.tv/link` fallback to the `/link` message.** Some mobile setups (notably iPad/iOS with the Plex app installed) deep-link `app.plex.tv/auth` into the Plex app, which signs in but never shows the Allow Access consent screen. Users now also get the PIN code and the `plex.tv/link` manual entry URL as a guaranteed fallback path.
- **Admin web UI: `✓ Saved` marker fades out after ~4s.** The green confirmation next to a save button now animates to transparent (1s fade starting 3s after appearing) so the form looks clean again. Error markers still stay visible.

## [0.9.0] - 2026-05-26

### Added
- **In-Telegram ticket management.** `/tickets` now renders one `#N` button per open ticket. Tap a button → bot opens a detail message with the action buttons:
  - **Users** see `[💬 Reply]` for their own tickets.
  - **Admin** sees `[💬 Reply]` and `[✅ Close]`. Close opens a sub-menu: `[💬 With comment]` (post a closing comment, then resolve) / `[✓ Without comment]` (resolve immediately).
- **Reply from the webhook DM.** When the bot DMs you about a new comment on your ticket, the DM now includes a `[💬 Reply]` button (only if the ticket is still open per `issue.issue_status`). Tap to reply without going through `/tickets` first.
- User comments are posted via the per-user Plex token (correct Seerr attribution). Admin actions use the admin API key.

### Changed
- **Removed all user-facing Seerr URLs.** No more "View: http://..." link in the issue-creation reply, no more "View:" line in webhook DMs, no more "Manage in Seerr" footer in `/tickets`. The bot is now self-contained for users -- the only browser they need is the Plex sign-in popup during `/link`.
- **Removed legacy username-only link support.** Pre-v0.4.0 mappings (no `plex_token_enc`) are no longer accepted for any action. The `/issue` flow, `/tickets`, resolve follow-up, and ticket reply all require a Plex-OAuth-linked account. Affected users get the same "DM `/link`" prompt as anyone unlinked; their DB row stays around until they re-link (which UPSERTs it).
- Dropped the `[from Telegram: name ↔ seerr_user]` body-prefix attribution hack from issue creation and resolve-follow-up comments. With Plex OAuth required, attribution is correct natively in Seerr's UI.

### Migration
- Anyone still on a legacy username-only mapping will see "DM me /link first..." when they try to use the bot. They just need to run `/link` once.

## [0.8.4] - 2026-05-26

### Fixed
- **Hot reload was closing the new Seerr/Radarr/Sonarr clients instead of the old ones.** `_build_clients_from_settings` scheduled a close-task that read `bot_data["seerr"]` lazily; by the time the task ran, the new client was already in `bot_data`, so the new client got closed. Result: after a settings save, the next `/link` (or any per-user Seerr call) failed with `RuntimeError: Cannot send a request, as the client has been closed`. Now captures references to the old clients before swapping, then closes those specifically.

### Changed
- Save confirmation wording made consistent across all tabs. Default is now `Saved.` (was `Settings saved.` on Seerr/Auto-fix/Webhook tabs, while Telegram already said `Saved.`).

## [0.8.3] - 2026-05-25

### Changed
- Save confirmation now renders inline next to the relevant Save button (small green `✓ Saved`) instead of as a top-of-page banner. Errors render the same way in red. The page no longer shifts when a save lands.

## [0.8.2] - 2026-05-25

### Added
- **Hermes Admin UI URL** setting on the Telegram tab. When set, the bot's startup DM links you back to the actual host (LAN IP, reverse-proxy URL, etc.) instead of the generic `<host>` placeholder. Tolerates URLs entered with or without a trailing `/admin`.
- **Per-user daily auto-fix limit** is now configurable on the Auto-fix tab (was hardcoded to 3). Validates positive integer. Admin still bypasses the limit.
- Auto-fix tab now has a short explanation of what auto-fix does, so the field set isn't context-free.
- Webui shows the Hermes version (`v0.8.2`) in the top-right alongside a Log out button. Single source of truth in `_version.py`.

### Changed
- Removed the nav bar with "Settings" / "Log out" links. Top-right corner of the page is now: version + log out. Tab list immediately under.

## [0.8.1] - 2026-05-25

### Changed
- **Tabbed admin UI.** Settings page reorganized into five tabs: Telegram, Seerr, Auto-fix (Radarr/Sonarr/allowlist), Webhook, Account. CSS-only, no JS dependency.
- Each tab posts to its own endpoint (`/admin/telegram`, `/admin/seerr`, `/admin/autofix`, `/admin/webhook`) so saving one section can't accidentally clobber another. Re-renders the page with the same tab active so the workflow stays put.
- Webhook tab now shows the live webhook URL pulled from the request's `Host` header (works behind reverse proxies that pass `X-Forwarded-Proto`/`Host`) -- copy it directly into Seerr's webhook configuration.
- Account tab consolidates the password-change form, backup download, and restore upload.

## [0.8.0] - 2026-05-25

### Changed
- **Zero-env-var install.** `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, and Seerr settings all moved into the web UI. New installs start the container with no environment variables and complete setup in the browser at `http://<host>:8765/admin`.
- First-run setup page now collects: admin username + password, Telegram bot token, Admin Telegram User ID, Seerr URL, Seerr API key. Submission writes everything to `/data/settings.json` and restarts the container so the bot comes online.
- Settings page exposes Telegram bot token + Admin Telegram User ID. Changes to either trigger a 2-second container exit so Docker restarts the process with the new identity.
- Bot now runs in two modes: **setup-only** (just the aiohttp web UI, no Telegram polling) when settings are incomplete, and **full** (PTB + web UI + webhook) when configured.
- Unraid template stripped down to: App Data path, port mapping, optional `HERMES_ENCRYPTION_KEY` (advanced). No more required env vars to fill in / re-enter every time the template is edited.
- `docker-compose.yml` mirrors the same: no env vars required by default.

### Migration
- Existing v0.7.0 installs upgrade transparently: on first v0.8.0 startup, `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_ID` env vars (still present from the v0.7.0 template) get migrated into `settings.json`. After that, you can blank those env vars in Unraid -- they'll be ignored.

## [0.7.0] - 2026-05-25

### Added
- **Slim admin web UI** at `/admin` on the same port as the webhook receiver (default `8765`). Features:
  - First-run flow at `/admin/setup` to create the admin username and password (pbkdf2_sha256, 600k iterations)
  - Settings page for Seerr / Radarr / Sonarr URLs and API keys, Seerr public URL, auto-fix allowlist, and webhook secret. **Hot reload** -- saving rebuilds the Seerr/Radarr/Sonarr clients in place, no container restart needed
  - Backup download: ZIP containing `settings.json`, `mappings.sqlite`, and `encryption.key`
  - Backup restore: upload that ZIP, atomic swap, container exits to pick up the new state
  - Change-password form
- `settings.py` module with `SettingsStore` (JSON-backed, env-var seed on first run), pbkdf2 password helpers, session-secret loader
- `webui.py` module with aiohttp routes, inline HTML, signed-cookie sessions (HMAC-SHA256, 7-day TTL)
- Stdlib-only auth (no new deps: pbkdf2 from hashlib, HMAC from hmac, sessions from json + base64)

### Changed
- Most settings moved from env vars to `/data/settings.json`. Env vars are still read once on first run as initial seeds, then ignored. **Existing installs upgrade transparently** -- current env vars get baked into `settings.json` on the next start.
- `SEERR_URL` and `SEERR_API_KEY` no longer required env vars; Hermes will start with no Seerr configured and prompt users in Telegram + admin UI to fill them in
- Unified the webhook and webui under a single aiohttp `web.Application` and single HTTP server. One port covers everything.
- Webhook secret is now read from settings on every request (rotates without a restart)

### Notes
- The Unraid template now exposes the web UI via Unraid's "WebUI" button (top-right of the container card)
- A backup includes the encryption key. Treat backup ZIPs as secret -- anyone who has the file can decrypt the stored Plex tokens.

## [0.6.0] - 2026-05-25

### Added
- **Comment-reply notifications via Seerr webhook.** When someone replies to an issue inside Seerr's UI, Hermes DMs the reporter on Telegram with the comment text and a link to the issue. Configure in Seerr: Settings → Notifications → Webhook, point at `http://<host>:8765/webhook/seerr`, enable the **Issue Comment** event.
- New aiohttp webhook server inside Hermes, listening on `/webhook/seerr` (port `8765` by default). Also exposes `/healthz` for liveness checks.
- New env vars: `WEBHOOK_PORT` (default `8765`), `WEBHOOK_BIND` (default `0.0.0.0`), `HERMES_WEBHOOK_SECRET` (optional; if set, requires matching Authorization header on incoming webhooks).
- `UserStore.find_by_plex_username` for mapping Seerr's `reportedBy_username` back to a linked Telegram user.

### Notes
- Hermes silently drops webhook events for users it doesn't have a Telegram mapping for. Users on legacy username-only links still work for this -- as long as `plex_username` is on the record, comment notifications will route.
- Hermes skips notifying the reporter when the commenter IS the reporter (no echo-back).
- Added `aiohttp==3.10.10` dependency.

## [0.5.2] - 2026-05-25

### Added
- `SEERR_PUBLIC_URL` env var. Optional reverse-proxy/public URL of Seerr used only in user-facing links (the `View:` URL after submitting an issue, and the `Manage in Seerr:` footer in `/tickets`). API calls always use `SEERR_URL`. If unset, falls back to `SEERR_URL` so existing setups keep working.

## [0.5.1] - 2026-05-26

### Fixed
- 401 Unauthorized on per-user issue submission. Cause: the cookie jar transfer between the admin client (used for `/auth/plex`) and the freshly created per-user client was unreliable. Now auth and subsequent calls happen on the same client so the session cookie persists naturally.

## [0.5.0] - 2026-05-26

### Added
- `/tickets` command. Users see their own open issues (sorted newest-first, up to 25). Admin sees all open issues with the reporter's name. Each entry shows ticket number, issue type, media title + year (with S/E for TV), and age. Footer links to Seerr's issues page for full management.
- `SeerrClient.list_issues(filter, take, as_plex_token)` and `get_media_title(media_type, tmdb_id)`.

### Notes
- Users on legacy username-only links can't use `/tickets` (no way to fetch their issues without per-user auth). Bot tells them to `/link` to enable it.

## [0.4.1] - 2026-05-26

### Changed
- `/link` consent text trimmed down. No more wall of security text — just a one-liner about what it does and a mention of `/unlink` to revoke.
- `/unlink` reply now explicitly tells the user the Plex token is removed from Hermes's storage, and points to Plex's authorized-devices page if they want full revocation.

## [0.4.0] - 2026-05-26

### Added
- **Plex OAuth (PIN flow) for per-user Seerr attribution.** `/link` now walks the user through Plex's PIN authorization, stores the resulting Plex token (encrypted at rest via Fernet), and uses it to authenticate to Seerr per user. Issues, comments, and resolves are now correctly attributed to the actual reporting user in Seerr's UI -- not the admin.
- Consent prompt before linking, with explicit disclosure that the Plex token is stored and grants Plex account access.
- New `HERMES_ENCRYPTION_KEY` env var. If unset, Hermes generates a Fernet key on first run and persists to `/data/encryption.key`.
- New `plex.py` module (Plex OAuth + user info).

### Changed
- `/link` no longer takes a username argument. Run `/link` with no args and follow the OAuth prompt.
- `SeerrClient.create_issue` / `add_issue_comment` / `resolve_issue` accept an optional `as_plex_token` to authenticate as that user.
- Legacy username-only mappings still work but fall back to admin attribution. Users should re-link with the new flow to get true per-user attribution.

### Security
- Plex tokens stored in SQLite are now encrypted with Fernet (AES-128 CBC + HMAC-SHA256).
- New `cryptography==43.0.1` dependency.

## [0.3.0] - 2026-05-26

### Added
- Follow-up conversation after auto-fix completes. Bot DMs the reporter "Did this resolve the problem?" with [✅ Yes, close it] / [💬 No, add a comment] buttons. Yes calls Seerr's resolve endpoint and closes the issue. No prompts for a comment which gets posted to the Seerr issue. No second auto-fix is offered on the "No" branch -- the admin handles it from there.
- Same follow-up on timeout, with [💬 Add a comment] / [🙅 No, leave it] options.
- `SeerrClient.add_issue_comment` and `SeerrClient.resolve_issue`.

### Changed
- Admin (`ADMIN_TELEGRAM_ID`) now bypasses the daily auto-fix rate limit. Non-admin users still capped at 3/day.

## [0.2.0] - 2026-05-26

### Added
- Completion notifications for auto-fix. When an auto-fix triggers a re-download, Hermes now polls Radarr/Sonarr every 60s and DMs (or replies in the originating chat) when the new file is imported. Sends a timeout notification after 6h if nothing arrives.
- Pending auto-fixes persist in SQLite (`pending_autofixes` table) and survive container restarts.

### Changed
- `RadarrClient.auto_fix` and `SonarrClient.auto_fix_episode`/`auto_fix_season` now return a third element with the IDs needed for polling.

## [0.1.1] - 2026-05-25

### Fixed
- Issues were being attached to the wrong media because the bot was passing TMDb IDs as Seerr's `mediaId`. Seerr's issue API uses its internal `media.id` (auto-increment), not TMDb. Fixed by reading `mediaInfo.id` from search results.
- Now bails out gracefully when a picked media isn't yet in Seerr's library (no `mediaInfo`), with a message telling the user to request it via Seerr first.

## [0.1.0] - 2026-05-25

### Added
- Telegram bot with `/start`, `/help`, `/link`, `/unlink`, `/issue`, `/status`
- Conversation flow for issue reporting (title search → media pick → season/episode picker for TV → issue type → description)
- Seerr API client (search, find_user, get_tv_seasons, create_issue)
- SQLite-backed user mapping store
- Reporter identity preserved by prefixing issue message bodies (Seerr's API doesn't honor userId attribution)
- Optional auto-fix via Radarr (movies) and Sonarr (TV episodes or whole seasons)
- Auto-fix allowlist (`ALLOWED_AUTOFIX_TELEGRAM_IDS`) with default-to-admin behavior
- Daily per-user auto-fix rate limit (3/day)
- Admin welcome wizard on startup; admin-only `/status` diagnostics
- Dockerfile + docker-compose.yml
- Unraid Community Applications template

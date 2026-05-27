# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

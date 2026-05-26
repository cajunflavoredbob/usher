# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

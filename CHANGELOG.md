# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

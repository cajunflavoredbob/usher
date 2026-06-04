# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.13] - 2026-06-03

### Added
- **Connection-test buttons on the Telegram, Seerr, Auto-fix, and Webhook tabs.** Each sits to the left of Save. Clicking POSTs the form's values to a new JSON endpoint (`/admin/test/<which>`), then overlays the button: green **PASS ✓** or red **FAIL ✗**, with a one-line detail (bot username / service version / error) beside it. The overlay fades out after 5 seconds and the button returns to neutral.
  - Telegram: validates the typed token against `getMe`.
  - Seerr: pings with the typed URL + API key.
  - Auto-fix: pings whichever of Radarr/Sonarr have a URL filled; PASS only if every configured client succeeds (per-service detail).
  - Webhook: self-POSTs a synthetic `TEST_NOTIFICATION` to the live webhook URL using the **saved** secret, confirming the receiver is reachable and the secret round-trips (so the flow is Generate → Save → Test).
- **Webhook Secret Show / Generate / Copy controls.** The secret is mandatory and auto-generated (it's the value you copy into Seerr's `Authorization Header`), but it was rendered as an unreadable masked field. **Show** toggles reveal/mask, **Generate** rolls a fresh `token_urlsafe`-shaped value and reveals it, **Copy** puts it on the clipboard (with an `execCommand` fallback).
- `tests/test_admin_connection_tests.py` — 15 cases exercising the four endpoints end to end (forged session + CSRF, outbound clients/httpx mocked): pass/fail/missing-input per service, auth-required redirect, and CSRF rejection.

### Fixed
- **`autocomplete="off"` on all config-secret fields** (Telegram token, Seerr/Radarr/Sonarr API keys, Webhook secret). Stops the browser password manager from silently overwriting these `type="password"` fields with the saved admin login password — which could have clobbered the working secret on the next Save.

## [0.11.12] - 2026-05-30

### Added
- **`tests/_handler_harness.py`** — minimal PTB handler test harness. Factory helpers for `Update` + `CallbackQuery` + `CallbackContext`-shaped objects with sensible `bot_data` defaults (admin_id, SimpleNamespace AsyncMock seerr/radarr/sonarr clients, store stub with AsyncMock methods, settings_store stub). Recording wrappers on `reply_text` / `edit_message_text` / `answer` / `edit_message_reply_markup` so tests can assert on what each handler emitted. Reusable foundation for future handler tests.
- **`tests/test_cmd_tickets.py`** — 7 cases. Admin lists all (with "All open tickets" header + `as_plex_token=None`); user lists own (with "Your open tickets" header + scoped token); unlinked non-admin gets the /link prompt; empty list per-role messaging; raw exceptions wrapped to user-friendly strings; `seerr=None` short-circuits via the require-seerr helper.
- **`tests/test_apply_fix.py`** — 7 cases. Non-admin blocked + "Admin only." toast + `admin_callback_blocked` audit log; movie redownload happy path enqueues pending autofix + logs autofix event + edits success message; movie mark_failed happy path; `get_issue` failure surfaces user-friendly message and does NOT echo raw exception; whole-season TV rejected with "only works on individual episodes"; partial-success (delete failed but search ran) still enqueues poller; failed result skips enqueue.
- **`tests/test_issue_pick_media.py`** — 5 cases. Version-tag match advances to `PICK_TYPE`; **version-tag mismatch shows "Search context changed" and ends the conversation (the v0.11.10 CONC #10 regression test);** missing search_results entirely treated as mismatch; malformed callback_data (too few parts) shows "Couldn't parse selection"; non-int version in callback_data same.
- **`tests/test_tk_reply_text.py`** — 7 cases. Admin reply posts with `as_plex_token=None`; user reply posts with their Plex token; decrypt_failed mapping shows the broken-link message instead of silent "not linked"; **post-await mismatch suppresses close-after side effect (the v0.11.6 CONC #9 regression test);** close_after happy path calls both `add_issue_comment` + `resolve_issue`; empty whitespace-only message re-prompts and stays in state; missing `tk_reply_id` ends quietly.
- **189 tests total** (was 163; +26). Handler coverage starts here and can grow with each new handler change.

### Notes
- Advances the v1.0 gate's "non-trivial test coverage" criterion from "data + helper layers only" to "data + helper layers + the four highest-traffic and most-recently-patched handlers."
- The harness is intentionally minimal -- it models the surface area handlers actually touch, not the full PTB Application. If future handlers exercise PTB internals the harness doesn't yet cover, extend it as needed.
- No source-code logic changes.

## [0.11.11] - 2026-05-30

### Security
- **PBKDF2 auto-upgrade on successful login** (audit SEC #16). After a successful `verify_password`, the stored hash's iteration count is parsed from the `pbkdf2_sha256$<iters>$<salt>$<hash>` format and compared against the current `PBKDF2_ITERATIONS`. If lower, the password is rehashed and persisted, and an `event=password_rehashed user=... ip=... from_iters=N to_iters=M` audit line is written. The rehash is wrapped in its own try/except so a write failure doesn't roll back the (already-successful) login.

### Removed
- **`_run_autofix` and `_run_mark_failed` back-compat shims** in `bot/tickets.py`. Confirmed orphan since v0.11.7 migrated `bot/issue_flow.py` to call `_run_arr_action` directly. Module docstring updated to drop them from the public-entry-points list.
- **Unused-imports sweep across `bot/*` and root modules.** `bot/app.py` (`typing.Optional`), `bot/shared.py` (`html`), `bot/link_flow.py` (`_record_btn`), `bot/resolve_flow.py` (`typing.Optional`, `InlineKeyboardButton`, `InlineKeyboardMarkup`, `_token_for`), `bot/tickets.py` (`AUTOFIX_ELIGIBLE_TYPES`), `webui.py` (`CSRF_COOKIE`). Pyflakes-clean now.

### Added
- `tests/test_login_pbkdf2_auto_upgrade.py` — 3 cases: a hash already at the current iter count is NOT rehashed (no audit entry); a stale-iter hash IS rehashed + audit-logged with `from_iters`/`to_iters` fields, and the new hash still verifies the same password; a malformed stored hash fails the login with 401 (not 500) and doesn't trigger the rehash path.
- 163 tests total (was 160).

### Notes
- After this release: **the only outstanding audit item is SEC #9 (session rotation / jti) — explicitly deferred to v0.12 per the briefing.** ERR #18 (Plex API logging hygiene) is verified already-closed by v0.11.3's `execute()` wrapper, which uniformly parses error bodies into `APIError.user_message` and logs them at the call site via `logger.exception`. The pre-v0.11.3 explicit-body block in `plex.request_pin` was already removed during the v0.11.3 migration.
- v1.0 gate per the briefing: closed audit ✓ + one week clean operational use (in progress since 2026-05-30) + non-trivial test coverage. Next planned investment: handler-level test harness (v0.11.12 scope).

## [0.11.10] - 2026-05-30

### Fixed
- **Autofix poller no longer double-notifies on overlapping ticks** (audit CONC #8). New module-level `_inflight: set[int]` in `bot/autofix_poll.py` tracks fix IDs currently being processed; the next tick skips any ID still in flight. Fixes the "Sonarr is slow → tick stretches past 60s → next tick fires `_notify_complete` again before `mark_autofix_status('complete')` lands" failure mode.
- **`/issue` search results carry a version tag** (audit CONC #10). Every fresh `_show_search_results` bumps `ctx.user_data["search_version"]` and embeds it in each result's `callback_data` (now `media:<version>:<media_type>:<tmdb_id>`). `issue_pick_media` verifies the version matches before resolving — rapid `/issue → A → /issue → B` reentry no longer causes the in-flight pick to pull the wrong item from the freshly-replaced `search_results` dict. The mismatching tap gets a clean "Search context changed (you started a new /issue search since this keyboard appeared). /issue to pick again." message.

### Changed
- **`SeerrClient._as_user` caches authenticated user clients per Plex token** (audit CONC #11). LRU + TTL (`_USER_CLIENT_MAX=32`, `_USER_CLIENT_TTL_S=300`). Under a webhook comment flood, repeated `add_issue_comment` / `resolve_issue` / `list_issues` / `create_issue` / `get_issue` calls as the same Plex user no longer pay the TCP-handshake + `/auth/plex` cost on every invocation. The cache owns each client's lifecycle; the per-call `try: ... finally: await client.aclose()` blocks in five caller sites have been removed. `SeerrClient.close()` drains the cache (closes every cached client) on shutdown.

### Added
- **`tests/test_autofix_poll_inflight.py`** — 2 cases. Overlapping ticks on the same fix.id dedupe (only the first invocation calls `_notify_complete`); sequential ticks both run cleanly.
- **`tests/test_seerr_user_client_cache.py`** — 6 cases. Same token returns the same client instance; different tokens get different clients; TTL expiry closes the stale client and mints a fresh one; LRU eviction at the cap closes the dropped client; touching an entry promotes it (saves it from the next eviction); `close()` drains every cached client.
- 160 tests total (was 152).

### Notes
- Closes the last three v0.11.x audit items: CONC #8 (poller dedup), CONC #10 (search-results versioning), CONC #11 (user-client cache). All explicitly deferred-from-v0.11.x audit items are now closed; only the v0.12-deferred SEC #9 (session rotation), SEC #16 (PBKDF2 auto-upgrade), and ERR #18 (Plex API logging hygiene — already substantially closed by v0.11.3) remain.
- Callback_data format change: old `media:movie:42` is now `media:1:movie:42`. Any keyboard rendered by a pre-0.11.10 process whose pick lands after the upgrade will fall through to "Couldn't parse selection. /issue to start over." That's the correct behavior — those keyboards' search_results dicts don't survive the restart either.

## [0.11.9] - 2026-05-30

### Changed
- **`_apply_fix` decomposed** (audit M4) into `_resolve_fix_context` + `_enqueue_fix_completion` + a thin policy core. New `_FixContext` dataclass carries the resolved issue + media + label between the helpers.
- **`format_media_label(title, year, *, season, episode)` shared helper** (audit M5) replaces four sites that each rendered this independently: `cmd_tickets`, `_build_ticket_detail`, `_apply_fix`, `_submit_issue`. The display format is now uniform: `Title (Year) — S01E08`. (`/tickets` list previously rendered `Title (Year) S01E08` without the em-dash — minor cosmetic shift.)
- **`PendingAutofix.is_complete(radarr, sonarr) -> tuple[bool, str]` method** (audit M7). The poller's inline movie-vs-episode-vs-season branching moves into the data type. Returns `(done, extra)` where `extra` is the `(present/total episodes)` suffix used by `_notify_complete`.
- **Non-admin `/start` cleaned up.** Greeting + commands only — no connection diagnostics, no inline `/link` directive. Admin path keeps diagnostics.
- **Startup admin DM now includes the version string** (`"👋 Bot is online (v0.11.9)."`).
- **`_post_init` startup-DM failures classified** (audit ERR #15): distinct WARN per cause — "bot was blocked", "chat not found" (with the "must be a numeric ID from @userinfobot" hint), "user is deactivated" — instead of a catch-all "likely never started a conversation."

### Fixed
- **`cmd_link_didnt_work` clears `link_active_loop` explicitly on timeout and success** (audit ERR #19) so the conversation frees up immediately instead of waiting for the 30-min `_link_conversation` timeout.
- **`format_age` logs WARN once per unparseable timestamp prefix** (audit ERR #20) instead of silently returning `"?"`. New module-level `_FORMAT_AGE_WARNED` set; first hit for each 20-char prefix surfaces, repeats are quiet.
- **Webhook 401 logging sampled per IP** (audit ERR #8). First rejection from each IP logs at WARN; further rejections from the same IP within 5 minutes drop to DEBUG. Botnet probing no longer floods the operational log.
- **Admin-only callback gate factored into `_require_admin(q, ctx, *, action_label)`** (audit SEC #15). `tk_close_menu`, `tk_close_direct`, `tk_close_with_comment_start`, `tk_fix`, `_apply_fix` migrated. Non-admin taps now get an "Admin only." toast and an `admin_callback_blocked` audit log entry (was: silent no-op).
- **`on_error` clears known conversation `user_data` keys** (audit CONC #12). New `_CONVERSATION_USER_DATA_KEYS` tuple covers `tk_reply_id`, `tk_close_after`, `link_active_loop`, `media`, `search_results`, `seasons`, `season`, `episode`, `issue_type`, `description`, `autofix`, `research_parent`. A mid-conversation crash no longer leaks half-state into the next conversation.

### Added
- **`tests/test_format_media_label.py`** — 9 cases covering title-only, with year, season, episode, year-less + seasoned, empty title, two-digit zero-padding, season-zero behavior, and episode-falsy handling.
- **`tests/test_pending_autofix_is_complete.py`** — 10 cases covering movie complete/pending/no-radarr/no-id, single-episode complete/pending, whole-season partial/complete/zero-expected, and error propagation.
- **`tests/test_require_admin.py`** — 4 cases for admin pass-through (no toast), non-admin (toast + audit log + False return), missing admin_id (defensive block), anonymous user (no `from_user`).
- **`tests/test_helpers.py`** — added `test_format_age_warns_once_per_prefix` regression.
- 152 tests total (was 126).

### Notes
- Final audit-closure release in the v0.11.x line. **All Critical + High + Medium audit findings are now closed.**
- Explicitly deferred to v0.12 (not blocking v1.0): SEC #9 (session rotation / jti), SEC #16 (PBKDF2 auto-upgrade), ERR #18 (Plex API logging hygiene -- substantially closed by v0.11.3's `execute()` body-in-APIError), CONC #8 (per-fix in-flight tracking), CONC #10 (search_results version tag), CONC #11 (`_as_user` client cache).
- v1.0 gate per the briefing: closed audit ✓ + one week clean operational use (in progress) + non-trivial test coverage (handler-level tests are the natural next investment).

## [0.11.8] - 2026-05-30

### Fixed
- **`load_or_create_session_secret` no longer strips bytes from the loaded secret.** The previous `p.read_bytes().strip()` call removed ASCII whitespace bytes (`\n`, `\t`, ` `, etc.) from both ends of the 32-byte random secret. Since `secrets.token_bytes(32)` returns uniformly random bytes, ~7% of secrets had a whitespace byte at one end; on those installs the runtime session key was 30–31 bytes (silently truncated) and varied between the first and second load. CI on v0.11.7 caught the case where a freshly-generated secret happened to start with `\n` and the round-trip mismatch failed `test_session_secret_persists`. New `test_session_secret_preserves_whitespace_bytes` deterministic regression added.

### Notes
- **Back-compat hazard for the ~7% of installs whose existing session secret has a leading or trailing whitespace byte on disk.** The pre-v0.11.8 binary read that secret minus those bytes; the v0.11.8 binary will read the full 32 bytes. The HMAC key changes → previously-issued admin session cookies become invalid → admin sees one auto-logout on the upgrade. Re-login is the only step.
- 126 tests pass (was 125; added the regression).

## [0.11.7] - 2026-05-29

### Fixed
- **`_submit_issue` was calling `_run_autofix` with a stale tuple-unpacking shape.** Carried over from before v0.11.3 made the orchestrators return `FixResult`; would have `ValueError`'d on first user-side auto-fix-during-/issue. Rewritten to consume the structured result + use `result.should_poll` to decide whether to enqueue the completion poller.

### Added
- **`const.py`** — named timeouts and limits used across multiple modules: Plex poll cadence + window + escalation threshold + backoff cap, autofix poll interval / first-delay / 6h timeout, keyboard layout caps (3/row), search result count, HTTP upload caps (32MB admin restore vs 128KB webhook), and the three ConversationHandler timeouts. Single source of truth for tuning.
- **`bot/callback_prefixes.py`** — named constants for every `callback_data` prefix (`TK_OPEN`, `TK_REPLY`, `TK_FIX_REDOWNLOAD`, `TK_FIX_MARK_FAILED`, `TK_CLOSE`, `TK_CLOSE_WITH_COMMENT`, `TK_CLOSE_DIRECT`, `TK_BACK`, `LINK_CONSENT`, `LINK_PLATFORM`, `LINK_HELP`, `ISSUE_MEDIA`, `ISSUE_SEASON`, `ISSUE_EPISODE`, `ISSUE_TYPE`, `ISSUE_AUTOFIX_OFFER`, `ISSUE_AUTOFIX_CONFIRM`, `ISSUE_CANCEL`, `ISSUE_RESEARCH_PARENT`, `RESOLVE`). All inline keyboards and `CallbackQueryHandler` patterns updated to use them.

### Changed
- **`_run_autofix` and `_run_mark_failed` collapse into `_run_arr_action`** (`bot/tickets.py`) keyed by `action: Literal["fix", "mark_failed"]`. The two outer names remain as one-line back-compat shims; `_apply_fix` and `_submit_issue` are migrated to call `_run_arr_action` directly.
- **`bot/app.py`, `bot/link_flow.py`, `bot/issue_flow.py`, `bot/autofix_poll.py`, `bot/tickets.py`, `bot/resolve_flow.py`, `bot/webhook_handlers.py`, `bot/shared.py`** all import named constants instead of carrying magic strings + magic numbers (Plex poll iters, autofix poll interval, conversation timeouts, keyboard buttons-per-row, search result count, upload caps).
- **Module-level docstrings on `bot/*.py`** refined for the v0.11.5 split's responsibility lines.

### Removed
- **`tk_reply_menu` callback handler + the `tkrmenu:` pattern registration.** Confirmed orphan (no keyboard emits `tkrmenu:` since v0.10.4 made admin Reply go direct).

### Updated
- `Dockerfile` — `const.py` added to the top-level `COPY` list.

### Notes
- Phase 7 (final) of the v0.11.x hardening roadmap. Closes audit findings M3 (callback prefix sprawl), M6 (magic numbers), L3 (`_run_autofix`/`_run_mark_failed` duplication), and the silent v0.11.3 regression in `_submit_issue`.
- All 125 tests still pass. No new tests added — this phase is pure tidiness + one quietly-broken-since-v0.11.3 user path put right.
- After this release: **all Critical + High audit findings are closed**. Per the v1.0 gate in `~/hermes_briefing.md` (Critical + High closed + one week of clean operational use + non-trivial test coverage), the remaining wait is bake time. 125 tests cover the data + helper layers; handler-level tests would be the natural next investment before tagging v1.0.

## [0.11.6] - 2026-05-28

### Fixed
- **Allowlist read-then-await race** (audit concurrency #1). `bot/issue_flow.py issue_description` now snapshots `bot_data["allowlist"]` into a `frozenset` at handler entry, so a mid-handler settings reload can't shift the eligibility check to a stale set during the subsequent `count_autofix_24h` await.
- **`btn_msgs` gate is now race-tolerant** (audit concurrency #4 + #13). The gate tracks the last 3 button-bearing messages per user (was: only the latest); rapid-fire webhook DMs no longer make each-other's buttons look stale. Read of the per-user list happens once at gate entry — no read-then-await window. Eviction is FIFO at `BTN_HISTORY_MAX=3`.
- **Autofix poller re-fetches arr clients per iteration** (audit concurrency #3). `bot/autofix_poll.py poll_pending_autofixes` reads `radarr`/`sonarr` from `bot_data` inside the per-fix loop, so a settings reload during a 10-fix tick picks up the new clients on the very next iteration. The earlier "capture-once-per-tick" pattern could close-then-touch a captured client mid-loop.
- **`_close_old` tasks are tracked and drained on shutdown** (audit concurrency #5). `bot/app.py _build_clients_from_settings` parks a strong reference to each settings-reload close task in `bot_data["_pending_closes"]`; `_post_shutdown` awaits them via `asyncio.gather` before closing current clients. Eliminates "Task was destroyed while it is pending!" warnings on hot reload + shutdown.
- **`_post_shutdown` now closes Seerr/Radarr/Sonarr/Plex clients explicitly.** Previous shutdown leaked httpx connection pools.
- **`tk_reply_text` post-await mismatch check** (audit concurrency #9). After `seerr.add_issue_comment` returns, the handler verifies `ctx.user_data["tk_reply_id"]` still matches the `issue_id` bound at entry. If the user kicked off a new reply flow during the await, the comment still landed on the right ticket (the binding is local) but the close-after side effect is suppressed, with an honest "reply posted; you've started a new one since then" message.

### Added
- **`conversation_timeout` on all three ConversationHandlers.** `_ticket_conversation` and `_issue_conversation` get 600s (10 min); `_link_conversation` gets 1800s (30 min, covering the 28-min strong-PIN window). Each defines a `ConversationHandler.TIMEOUT` state handler that clears the relevant `user_data` keys (`tk_reply_id`/`tk_close_after`, `link_active_loop`, or the issue flow's `media`/`search_results`/`seasons`/`season`/`episode`/`issue_type`/`description`/`autofix`). Abandoned conversations no longer leak state for the life of the process.
- **`tests/test_btn_gate.py`** — 11 cases covering `record_btn` history capping + independence per user, and `global_btn_gate` permissive bootstrap, latest-message admission, any-of-recent-N admission, evicted-message blocking, expired-TTL blocking, cross-user isolation, and non-callback-query pass-through. Existing 114 tests still pass; total 125.

### Notes
- Phase 6 of the v0.11.x hardening roadmap. Closes audit concurrency findings #1, #3, #4, #5, #9, #13.
- Also picked up several stale-imports + missing-imports inside the split modules that pyflakes flagged (`secrets` in `link_flow`, `asyncio` in `tickets`, `UserStore` in `resolve_flow`, `_require_seerr` in `link_flow` and `issue_flow`, `CreatedIssue` in `issue_flow`, and a duplicate local `_ticket_detail_kb` that shadowed the shared one). None affected the import chain (function bodies aren't evaluated at module load) but they would have NameError'd the first time those handlers ran. All fixed.

## [0.11.5] - 2026-05-28

### Changed
- **`bot.py` split into a `bot/` package** along domain lines. ~2560 lines now spread across nine modules: `bot/app.py` (wiring, `main()`, startup/shutdown, `_check_connections`, `cmd_start`/`cmd_status`, error handler, setup-only path), `bot/shared.py` (constants, conversation states, cross-module helpers, `format_media_title_line`), `bot/webhook_handlers.py`, `bot/tickets.py`, `bot/issue_flow.py`, `bot/link_flow.py`, `bot/autofix_poll.py`, `bot/resolve_flow.py`, plus `bot/__init__.py` and `bot/__main__.py`. Entry point is now `python -m bot`. No behavior changes — pure refactor.
- **Per-client three-step workflow helpers.** `sonarr._run_episode_workflow(*, series, match, blocklist: bool)` and `radarr._run_movie_workflow(*, movie, blocklist: bool)` collapse the near-identical orchestration that previously lived inside `auto_fix*` / `mark_failed*`. Both pairs of public methods drop from ~60 lines each to a ~5-line prelude + delegation. Same `FixResult` semantics as v0.11.3.
- **`_check_connections` uses `ping()` instead of reaching into `_client`.** Adds `ping()` to `SeerrClient`, `RadarrClient`, `SonarrClient` (each calls the upstream version endpoint via the standard `execute()` wrapper, so retries + classified errors come along for free). Closes audit finding H3 (private-attr leakage).

### Added
- **`format_media_title_line(seerr, media, *, problem_season=None, problem_episode=None)`** in `bot/shared.py`. Builds `"🎬 Movie Title (Year)"` or `"📺 Show Title (Year) — S01E02"` from a Seerr webhook payload's media block. Used by all three `handle_seerr_*` handlers; deletes ~40 lines of drifting title-construction code.
- **`format_se_suffix(problem_season, problem_episode)`** helper used by the new title formatter and the `reported` flow's season/episode suffix.

### Removed
- `SeerrClient.find_user` + the `SeerrUser` dataclass — dead code, never called from the bot.
- `bot.py` — replaced by the `bot/` package. `python bot.py` → `python -m bot`.

### Updated
- `Dockerfile` — `COPY bot/ ./bot/` plus the unchanged top-level modules; `CMD ["python", "-m", "bot"]`.
- `tests/test_helpers.py` — imports update to `from bot.shared import format_age` and `from bot.issue_flow import _derive_parent_name`. All 114 tests still pass.

### Notes
- Phase 5 of the v0.11.x hardening roadmap. Closes audit findings H1 (bot.py god module), H2 (radarr/sonarr duplication), H3 (private-client access in `_check_connections`), M1 (handle_seerr_* title duplication), and M2 (dead code).
- `bot/shared.py` exposes both the new clean names (`format_age`, `token_for`, `record_btn`, etc.) and underscore-prefixed aliases (`_format_age`, `_token_for`, `_record_btn`, etc.) so the extracted handler modules keep their original symbol references without renaming. New call sites should prefer the unprefixed names.
- Highest single-release risk in the roadmap. End-to-end import chain verified (`from bot.app import main` + every domain module's public symbols); the test suite (114 cases) passes. Manual smoke after deploy: `/start`, `/link`, `/tickets`, `/issue`, webhook DM, auto-fix Redownload, Mark Failed.

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

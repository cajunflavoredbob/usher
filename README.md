# Hermes

A Telegram bot that lets your users report media issues to [Seerr](https://seerr.dev/) and optionally trigger Radarr/Sonarr to auto-fix the problem by deleting the file and re-downloading. Users sign in with Plex, so every issue is filed in Seerr as the actual reporter. Configuration is done through a built-in web admin UI.

## Features

- **`/issue` flow**: pick media → (for TV) pick season + episode → pick issue type (Video / Audio / Subtitles / Other) → describe the problem. The issue is created in Seerr **as the linked user**, not your admin account.
- **Plex sign-in linking**: `/link` runs a Plex OAuth flow (no usernames or passwords typed into Telegram). The user's Plex token is encrypted at rest and used to authenticate to Seerr on their behalf.
- **`/tickets`**: users list and follow up on their own open issues; the admin sees everyone's. Replies post back to Seerr as comments, and issues can be resolved from Telegram.
- **Webhook-driven updates**: Seerr posts to Hermes at `/webhook/seerr`, so new comments on a user's issue are delivered to them in Telegram and the resolve flow stays in sync.
- **Optional auto-fix** for Video / Audio / Subtitles: Hermes tells Radarr (movies) or Sonarr (TV) to delete the current file and trigger a new search, then polls for completion and notifies the user. Gated by:
  - **Allowlist** of Telegram user IDs (defaults to admin only)
  - **Configurable daily limit** per user (default 3 per 24h)
  - **Explicit confirmation** before any destructive action
- **Admin web UI** at `/admin`: first-run setup wizard, settings tabs (Telegram / Seerr / Auto-fix / Webhook / Account), connection **Test** buttons, encrypted backups, and restore.
- Works in DMs or any Telegram group the bot is added to (per-user conversation state).
- Admin gets `/status` for connection diagnostics and a startup health DM.

## How it works

Hermes runs two things in one container:

1. A **Telegram bot** (python-telegram-bot, long-polling — no inbound Telegram URL needed).
2. An **HTTP server** (aiohttp) on port **8765** that serves the `/admin` web UI and receives Seerr webhooks at `/webhook/seerr`.

All configuration lives in `/data/settings.json`, managed through the web UI. On first run — before an admin account and the required fields exist — Hermes starts in **setup-only mode** (just the web UI), then restarts into full mode once setup is complete.

## Install (Unraid Community Applications)

1. Search Community Apps for **Hermes** and install.
2. Set **App Data** (e.g. `/mnt/user/appdata/hermes`) and the **Web UI / Webhook Port** (default `8765`). Start the container.
3. Grab the first-run setup token from the logs:
   ```sh
   docker logs hermes | grep "setup token"
   ```
4. Open `http://<your-server>:8765/admin`, enter the setup token, and complete the wizard:
   - **Telegram bot token** (from `@BotFather`)
   - **Admin Telegram user ID** (DM `@userinfobot`)
   - **Seerr URL** + **API key** (Seerr → Settings → General)
   - Optionally Radarr/Sonarr URLs + API keys for auto-fix

   The container restarts into full mode when setup finishes.
5. In `@BotFather` → `/setprivacy` → select your bot → **Disable** (lets the bot see `/issue` in group chats).
6. Configure the Seerr webhook (see [Webhook setup](#webhook-setup)).

## Install (docker compose)

```sh
git clone https://github.com/cajunflavoredbob/hermes.git
cd hermes
docker compose up -d
docker compose logs | grep "setup token"
# open http://localhost:8765/admin, enter the token, finish setup
```

You can pre-seed first-run values via environment variables (see [Configuration](#configuration)), but the web UI is the source of truth after that.

## Linking users

Each user DMs the bot once and signs in with Plex — there are no Seerr or Plex usernames to type:

```
/link
```

The bot walks them through a Plex authorization (desktop opens the auth page; mobile copies an auth link to paste into a browser, with a `plex.tv/link` code fallback). Once approved, Hermes signs the user into Seerr with their Plex token and stores the (encrypted) mapping in `/data/mappings.sqlite`. After that, `/issue` and `/tickets` work in DM or any group the bot is in.

The user's Plex account must be shared into your Seerr instance. If it isn't, the bot says so and asks them to have the admin invite them. `/unlink` removes the stored token at any time.

## Example conversation

```
user: /issue
bot:  What movie or show is the issue with?
user: solo
bot:  Pick which one:
      [🎬 Solo: A Star Wars Story (2018)]
      [🎬 Solo (1996)]
      [Cancel]
user: (taps Solo: A Star Wars Story)
bot:  Selected: Solo: A Star Wars Story (2018)
      What kind of issue?
      [🎥 Video] [🔊 Audio] [📝 Subtitles] [❓ Other]
user: (taps Audio)
bot:  Type: 🔊 Audio
      Now briefly describe what's wrong:
user: Audio goes out of sync after about 30 minutes
bot:  Try to auto-fix? This will delete the file and trigger a new search.
      (Auto-fixes remaining today: 3)
      [✅ Try auto-fix] [📨 Just report]
user: (taps Try auto-fix)
bot:  ⚠️ This will delete the current file from disk and trigger a new download. Confirm?
      [⚠️ Yes, delete & re-search] [No, just report]
user: (taps Yes)
bot:  ✅ Reported as issue #14
        🔊 Audio — Solo: A Star Wars Story (2018)
      🔧 Auto-fix: Deleted file (if any) and triggered re-search for 'Solo: A Star Wars Story'.

      View: http://seerr.example.com/issues/14
```

## Webhook setup

Hermes receives events from Seerr so issue comments and resolutions reach the user in Telegram.

1. In `/admin` → **Webhook** tab, copy the webhook URL and the secret (use **Show**/**Copy**; **Generate** rolls a new one). A secret is mandatory and auto-generated on first run — Hermes rejects unauthenticated webhook POSTs.
2. In Seerr → Settings → Notifications → **Webhook**: set the URL, paste the secret into the **Authorization Header** field, and enable the **Issue Comment** event.
3. Use the Webhook tab's **Test** button to confirm the round-trip.

Seerr reaches Hermes on its LAN address and port — no public URL is required, but Seerr must be able to reach the Hermes port.

## Configuration

Configuration is managed in the `/admin` web UI and persisted to `/data/settings.json`. Environment variables only **seed the first run** (and migrate legacy installs); after that the web UI wins.

| Setting (env seed) | Required? | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from `@BotFather` |
| `ADMIN_TELEGRAM_ID` | yes | Your numeric Telegram user ID (DM `@userinfobot`) |
| `SEERR_URL` | yes | Base URL of your Seerr instance |
| `SEERR_API_KEY` | yes | Seerr → Settings → General → API Key |
| `SEERR_PUBLIC_URL` | no | Public/reverse-proxy URL used in links sent to users (falls back to `SEERR_URL`) |
| `HERMES_PUBLIC_URL` | no | URL the startup DM uses to point the admin back to `/admin` |
| `RADARR_URL` / `RADARR_API_KEY` | no | Enables movie auto-fix when both set |
| `SONARR_URL` / `SONARR_API_KEY` | no | Enables TV auto-fix when both set |
| `ALLOWED_AUTOFIX_TELEGRAM_IDS` | no | Comma-separated Telegram IDs allowed to auto-fix. Defaults to admin only. |
| `HERMES_WEBHOOK_SECRET` | no | Auto-generated on first run if unset; copy it into Seerr's webhook Authorization header |
| `HERMES_ENCRYPTION_KEY` | no | Fernet key for encrypting stored Plex tokens. Auto-generated to `/data/encryption.key` if unset |

Runtime/path overrides: `DATA_DIR` (default `/data`), `STORE_PATH` (mappings DB), `WEBHOOK_PORT` (default `8765`), `WEBHOOK_BIND` (default `0.0.0.0`).

## How user attribution works

Because users sign in with Plex, Hermes authenticates to Seerr **as the user** (via their Plex token) when creating issues, posting comments, and listing tickets. Issues are therefore attributed to the real reporter in Seerr's UI — no admin-account workaround or message prefixing. The encrypted Plex token never leaves your stack.

## Auto-fix details

- Offered only for issue types **Video / Audio / Subtitles** (where a re-download might help).
- Offered only to Telegram IDs in the allowlist (defaults to just the admin).
- **Per-user daily limit** (default 3 per 24h), configurable in the Auto-fix tab. The admin bypasses it.
- Explicit confirmation required before deletion.
- For TV: per-episode auto-fix **or** "whole season" (deletes all episode files in that season + a season-wide search).
- Hermes polls the *arr after triggering and DMs the user when the new file lands (or on timeout).
- If the media isn't managed by Radarr/Sonarr, Hermes reports "not in Radarr/Sonarr" without touching the issue.
- All auto-fix events are recorded in the `autofix_events` SQLite table.

## Files

- `bot/` — the Telegram application package (entry point `python -m bot`): app wiring, issue/link/resolve flows, ticket management, webhook handlers, auto-fix poller.
- `webui.py` — aiohttp admin web UI (`/admin`): setup, settings tabs, connection tests, backup/restore.
- `webhook.py` — Seerr webhook receiver (`/webhook/seerr`).
- `seerr.py`, `radarr.py`, `sonarr.py`, `plex.py` — API clients.
- `store.py` — SQLite store (encrypted Plex tokens, user mappings, auto-fix audit).
- `settings.py`, `const.py` — settings model/persistence and named constants.
- `http_util.py`, `fix_result.py` — API error contracts + retry, auto-fix result types.
- `auth_util.py`, `backup_crypto.py` — admin auth/CSRF/throttle, passphrase-wrapped backups.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `unraid-template.xml`
- `.github/workflows/` — tests gate releases; images publish to GHCR and Docker Hub on tag.

## Privacy

- The Telegram side uses long-polling — no public URL required.
- Seerr reaches Hermes over your LAN for webhooks; nothing needs to be exposed publicly.
- Plex tokens are encrypted at rest (Fernet) in the local SQLite DB.
- Hermes talks only to the Telegram API, your Seerr, your *arr stack, and Plex's auth API — nothing else.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Can't reach `/admin` | Check the container is up and port `8765` is mapped/reachable; first run prints a setup token in the logs |
| Bot doesn't respond in groups | `@BotFather` → `/setprivacy` → select bot → **Disable** |
| `/link` fails after Plex approval | The user's Plex account isn't shared in Seerr — invite them in Seerr first |
| Webhook events not arriving | Secret mismatch (copy it from the Webhook tab into Seerr's Authorization header), or Seerr can't reach the Hermes port; use the Webhook **Test** button |
| Auto-fix not offered | Telegram ID not in the allowlist, issue type is "Other", or today's limit is hit |
| Auto-fix says "not in Radarr/Sonarr" | The media isn't managed by *arr — add it there first |
| `/status` shows ❌ for a service | Check the URL is reachable from inside the container (use an IP, not `localhost`) |

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. Keep changes small and focused. Update [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

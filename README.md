# Hermes

A Telegram bot that lets your users report media issues to [Seerr](https://seerr.dev/) and optionally trigger Radarr/Sonarr to auto-fix the problem by deleting the file and re-downloading.

## Features

- `/issue` conversation walks the user through: pick media → (for TV) pick season + episode → pick issue type (Video / Audio / Subtitles / Other) → describe the problem
- Issues are POSTed to Seerr with reporter identity preserved in the message body
- Optional **auto-fix** for Video / Audio / Subtitles issues: the bot tells Radarr (movies) or Sonarr (TV) to delete the current file and trigger a new search
- Auto-fix gated by:
  - **Allowlist** of Telegram user IDs (defaults to admin only)
  - **Daily limit** of 3 per user per 24 hours
  - **Explicit confirmation** prompt before any destructive action
- Works in DMs or in any Telegram group the bot is added to (per-user conversation state)
- Admin gets a `/status` command and a welcome wizard summarizing connection health

## Install (Unraid Community Applications)

1. Search Community Apps for **Hermes** and install
2. Fill in the required fields in the template:
   - **Telegram bot token** (from `@BotFather`)
   - **Seerr URL** (e.g. `http://192.168.1.10:5056`)
   - **Seerr API key** (Seerr → Settings → General)
   - **Admin Telegram user ID** (DM `@userinfobot` on Telegram)
3. Optionally fill in:
   - Radarr URL + API key (enables movie auto-fix)
   - Sonarr URL + API key (enables TV auto-fix)
   - Allowlist of additional Telegram user IDs for auto-fix
4. Start the container
5. Configure your bot in Telegram → `@BotFather` → `/setprivacy` → select your bot → **Disable** (lets the bot see `/issue` commands in group chats)
6. DM your bot `/start` to see the welcome wizard

## Install (docker compose)

```sh
git clone https://github.com/cajunflavoredbob/hermes.git
cd hermes
cp .env.example .env
# edit .env with your tokens
docker compose up -d
```

## Linking users

Each user DMs the bot once:

```
/link <their seerr or plex username>
```

The bot verifies the username exists in Seerr and stores the mapping in `./data/mappings.sqlite`. After that, `/issue` works in DM or any group the bot is in.

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

## Configuration reference

| Variable | Required? | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from `@BotFather` |
| `SEERR_URL` | yes | Base URL of your Seerr instance (e.g. `http://192.168.1.10:5056`) |
| `SEERR_API_KEY` | yes | Seerr → Settings → General → API Key |
| `ADMIN_TELEGRAM_ID` | yes | Your numeric Telegram user ID (DM `@userinfobot`) |
| `RADARR_URL` | no | Radarr base URL — enables movie auto-fix when set with API key |
| `RADARR_API_KEY` | no | Radarr → Settings → General → API Key |
| `SONARR_URL` | no | Sonarr base URL — enables TV auto-fix when set with API key |
| `SONARR_API_KEY` | no | Sonarr → Settings → General → API Key |
| `ALLOWED_AUTOFIX_TELEGRAM_IDS` | no | Comma-separated Telegram user IDs allowed to trigger auto-fix. Defaults to admin only. |

Internal:
| Variable | Default | Description |
|---|---|---|
| `STORE_PATH` | `/data/mappings.sqlite` | SQLite file for user mappings and auto-fix audit log |

## How user attribution works

Seerr's API does **not** allow setting the `userId` of a created issue — issues are always attributed to the user whose API key was used (typically your admin user). To preserve the actual reporter's identity, the bot prefixes every issue's message body with:

```
[from Telegram: <telegram name> ↔ <linked seerr display name>]

<the user's actual description>
```

That way you can always see in Seerr's UI who reported what.

## Auto-fix details

- Only offered for issue types **Video / Audio / Subtitles** (where a re-download might help)
- Only offered to Telegram user IDs in `ALLOWED_AUTOFIX_TELEGRAM_IDS` (defaults to just the admin)
- Hard limit: **3 per user per 24 hours**
- Explicit confirmation required before deletion
- For TV: per-episode auto-fix OR "whole season" (deletes ALL episode files in that season + season-wide search)
- If the media isn't being managed by Radarr/Sonarr, the bot reports "not in Radarr/Sonarr" without affecting the issue
- All auto-fix events are recorded in the `autofix_events` SQLite table

## Files

- `bot.py` — main, conversation handlers
- `seerr.py` — Seerr API client
- `radarr.py` — Radarr API client
- `sonarr.py` — Sonarr API client
- `store.py` — SQLite store (user mappings + auto-fix audit)
- `Dockerfile`, `docker-compose.yml`, `.env.example`
- `unraid-template.xml` — Unraid Community Applications template
- `.github/workflows/release.yml` — builds and publishes the container image to GHCR and Docker Hub on tag

## Privacy

- The bot uses Telegram long-polling. No public URL required.
- All data stays between your Telegram, your Seerr, and your *arr stack.
- User mappings live in the local SQLite file (`./data/mappings.sqlite`).
- The bot does not send anything to any third party other than the Telegram API and the URLs you configure.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot doesn't respond in groups | `@BotFather` → `/setprivacy` → select bot → **Disable** |
| `/link` says "no user matched" | Use Plex username, or the display name as it appears in Seerr |
| Auto-fix not offered | Either your Telegram ID isn't in the allowlist, issue type is "Other", or you've hit today's limit |
| Auto-fix says "not in Radarr/Sonarr" | The media isn't being managed by *arr. Add it there first. |
| TV auto-fix says "couldn't find TVDb ID" | Seerr's TV details endpoint didn't return a TVDb mapping for that show — rare |
| `/status` shows ❌ for Seerr | Check `SEERR_URL` is reachable from inside the container (bridge networking; use IP not "localhost") |

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. Keep changes small and focused. Update [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

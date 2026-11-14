# Usher

A Telegram bot and web admin panel that sit in front of a Plex server and its [Seerr](https://seerr.dev/) request manager. Users sign in with Plex, which gives each one a Link. They then request movies and shows, browse what is trending, get told when a title becomes available, and report problems as Tickets. Requests and Tickets are made in Seerr as the actual user. The bot can also fix a bad file by telling Radarr or Sonarr to delete it and search again, and the Admin can invite people to the Plex server. Configuration is done through the built-in web admin panel.

## Features

- **Link**: `/account` opens the Account Hub, where a User links or unlinks their Plex sign-in. It is a Plex OAuth flow, so no usernames or passwords are typed into Telegram. The User's Plex token is encrypted at rest and used to authenticate to Seerr on their behalf.
- **Requests**: a Linked user asks for a movie or show with `/request`, and the Request is made in Seerr as that user. A Request that needs approval gets a Progress card that says 'Waiting for approval' until it is approved or declined, then edits itself in place as the title is grabbed, downloaded and becomes available. `/requests` lists a User's own Requests and cancels pending ones. The Admin sees everyone's.
- **Trending**: `/trending` browses what is trending, with `movies`, `tv` and `upcoming` as optional arguments. Tapping a title's number starts a Request for it.
- **Subscriptions**: the bell button on a title's detail card asks to be told when it becomes available. A Subscription is one-shot: the first availability notice uses it up. Availability notices (the Requester's DM and Subscription notices) carry a ▶️ Watch now button that opens the title in Plex once Seerr has its link. `/subscriptions` lists and removes a User's own.
- **Tickets**: `/tickets` opens the Tickets Hub, with buttons to report a problem and to list open Tickets: a User sees their own, the Admin sees everyone's. A Ticket records the title, an Issue type (Video, Audio, Subtitles or Other) and, for TV, its Scope: one episode or one whole season. Replies post back to Seerr as comments, and Tickets can be closed from Telegram.
- **Fixes**: the Admin can start a Fix from a Ticket. **Redownload** deletes the file and searches again. **Mark failed** blocklists the current release when one is found, deletes the file, then searches again. Progress shows on a Progress card, and the chat where the Fix was started is told when the new file lands, or when it times out.
- **Auto-fix**: for Video / Audio / Subtitles Tickets, the Reporter can start a Redownload themselves on one episode or movie, gated by the Admin's allowlist and daily limit (see [Auto-fix details](#auto-fix-details)).
- **Shares**: the Admin can `/invite` someone to the Plex server by email or Plex username, choosing the Libraries to share, and `/uninvite` them. The Admin Hub also has a Share list, where each Share can be viewed and removed. With Tautulli linked, its 💤 Inactive button lists the Shares that haven't watched anything for a set number of days (60 by default, changed in the panel's Tautulli tab) or never, and each opens the usual Share view with Remove. An Invite is a Share the recipient has not accepted yet, and `/uninvite` removes either kind.
- **Access asks**: when someone's Plex sign-in is rejected by Seerr during Link, the failure message offers 🙋 Ask for access. They see what the Admin will get (Telegram name, Plex username and email) and tap Send. The Admin gets a DM with ✅ Approve and 🚫 Ignore, which also says whether that email already has a Share. Approve opens the Invite flow at the Library picker with the email filled in (cancelling it leaves the ask open); once the Invite is sent, the person gets a message to accept it in Plex and link again. Ignore marks the ask ignored and tells the person nothing. Limits: one open ask per person, no new ask for 7 days after an Ignore, at most 10 open asks, and asks older than 30 days are deleted.
- **Broadcast**: the Admin Hub's 📣 Broadcast asks for a message, shows a preview with Send and Cancel, then sends it as plain text (so `*` or `<` arrive as typed) under a "📣 From the Admin" header to every Linked user except the Admin, at about 25 messages a second. The preview becomes a report of how many got it and how many couldn't be reached (a user who blocked the bot counts as not reached). Each recipient is recorded as it is sent, so a restart carries on without repeats. The message is limited to 3500 characters.
- **SABnzbd priority**: with SABnzbd connected, the Admin can have downloads that Requests and auto-fixes trigger bumped up its queue, at High priority or Force. The bump applies once per download. The Admin Hub also gets a ⏯ Downloads button there: it shows whether SABnzbd is paused, its speed, limit and what's left, with Pause, Pause 30 minutes, Resume and speed limit buttons (25%, 50%, 75%, No limit). A percent limit needs a maximum speed set in SABnzbd; the bot says so when it is missing.
- **Service alarms**: the Admin gets a DM when Radarr or Sonarr reports a new warning or error, or a disk has less free space than the limits set in the panel's Telegram tab (default under 10% or under 50 GB, whichever is hit first), checked every 5 minutes. Each problem sends one DM, and one more when it clears. Switch health and disk alarms on or off in the same tab. The Admin also gets a DM when the Plex server goes offline (after two failed checks in a row, using plex.tv and, if linked, Tautulli) and when it is back, with its own switch. /status lists Radarr and Sonarr health, free space per disk and whether Plex is online.
- **Notification toggles**: in the web admin panel's Telegram tab, the Admin can turn off five classes of bot DM for everyone: Request updates to Requesters, new-Request notices to the Admin, the failed-download alarm to the Admin, Ticket DMs, and availability notices for Subscriptions.
- **Webhook-driven updates**: Seerr posts to Usher at `/webhook/seerr`, so new comments on a Ticket, resolutions and Request status changes are delivered in Telegram and the close flow stays in sync.
- **Admin web panel** at `/admin`: first-run setup wizard, settings tabs for Telegram, Seerr, Auto-fix, SABnzbd, Tautulli, Webhook and Account, connection **Test** buttons, encrypted backups, and restore.
- Works in DMs or any Telegram group the bot is added to with per-user conversation state. The Admin Hub is DM-only.
- The Admin also gets a startup health DM.

## Commands

The bot's command menu lists the Hubs plus `/help`. A Hub is a command that opens one message of buttons for an area of the bot, and flows started from a Hub normally end with a way back to it. The menu is registered at every startup, so it always matches the code.

| Menu entry | Shown to | What it holds |
|---|---|---|
| `/request` | everyone | **Requests Hub**: New request, My requests, Trending, Subscriptions. `/request <title>` skips the Hub and starts a Request for that title. |
| `/tickets` | everyone | **Tickets Hub**: Report a problem, and My tickets (Open tickets for the Admin). |
| `/account` | everyone | **Account Hub**: Link, or Unlink once linked. |
| `/help` | everyone | The command list. For the Admin it also shows connection status. |
| `/admin` | the Admin only (extra menu entry in the Admin's chat) | **Admin Hub**: Invite, Shares (the Share list), Status, Broadcast, and Downloads when SABnzbd is connected. DM only. |

Every command that is not in the menu still works when typed, including the ones that take an argument:

- Everyone: `/link`, `/unlink`, `/issue` (report a problem), `/requests`, `/trending [movies|tv|upcoming]`, `/subscriptions`.
- Admin only, and only in a DM with the bot: `/invite [email or Plex username]`, `/uninvite <email or username>`, `/status` (connection status). Anyone else gets "Admin only."

## How it works

Usher runs two things in one container:

1. A **Telegram bot** (python-telegram-bot, long-polling, so no inbound Telegram URL is needed).
2. An **HTTP server** (aiohttp) on port **8765** that serves the `/admin` web admin panel and receives Seerr webhooks at `/webhook/seerr`.

All configuration lives in `/data/settings.json`, managed through the web admin panel. On first run, before a Panel account and the required fields exist, Usher starts in **setup-only mode** (just the web admin panel), then restarts into full mode once setup is complete.

## Install (Docker)

Every release publishes a multi-arch (amd64 + arm64) image to Docker Hub and GHCR.

```sh
docker run -d --name usher \
  -p 8765:8765 \
  -v ./data:/data \
  --restart unless-stopped \
  cajunflavoredbob/usher:latest
```

Then do [First-run setup](#first-run-setup).

## Install (Unraid)

Usher is not in Community Applications; the repo ships `unraid-template.xml` instead.

1. Docker tab, **Template Repositories**, add `https://github.com/cajunflavoredbob/usher` and save.
2. **Add Container**, pick **usher** from the template list.
3. Set **App Data** (e.g. `/mnt/user/appdata/usher`) and the **Web UI / Webhook Port** (default `8765`). Start the container.
4. Do [First-run setup](#first-run-setup).

## Install (docker compose, from source)

```sh
git clone https://github.com/cajunflavoredbob/usher.git
cd usher
docker compose up -d
```

`docker-compose.yml` pulls the published image by default; uncomment `build: .` to build from source instead. You can pre-seed first-run values via environment variables (see [Configuration](#configuration)), but the web admin panel is the source of truth after that.

## First-run setup

1. Open `http://<your-server>:8765/admin`. The first visit creates a one-time setup token and writes it to the logs (and to `/data/setup_token`):
   ```sh
   docker logs usher | grep "setup token"
   ```
2. Enter the setup token and complete the wizard:
   - **Panel account**: the username and password you will sign in to `/admin` with
   - **Telegram bot token** (from `@BotFather`)
   - **Admin Telegram user ID** (DM `@userinfobot`)
   - **Seerr URL** + **API key** (Seerr → Settings → General)

   The container restarts into full mode when setup finishes. Radarr, Sonarr and the other services are set up afterwards on their tabs in `/admin`.
3. In `@BotFather` → `/setprivacy` → select your bot → **Disable** (lets the bot see `/issue` in group chats).
4. Configure the Seerr webhook (see [Webhook setup](#webhook-setup)).

## Linking users

Each user DMs the bot once and signs in with Plex, so there are no Seerr or Plex usernames to type:

```
/link
```

The bot walks them through a Plex authorization (desktop opens the auth page; mobile copies an auth link to paste into a browser, with a `plex.tv/link` code fallback). Once approved, Usher signs the user into Seerr with their Plex token and stores the encrypted Link in `/data/mappings.sqlite`. After that, `/request`, `/issue` and `/tickets` work in DM or any group the bot is in.

The user's Plex account must be shared into your Seerr instance. If it isn't, the bot says so and offers an Ask for access button that sends the Admin an Access ask. Unlinking from the Account Hub (or `/unlink`) removes the stored token at any time.

## Example conversation

```
user: /issue
bot:  What movie or show is the issue with? (Reply with the title.)
user: solo
bot:  Pick which one:

      1. 🎬 Solo: A Star Wars Story (2018)
      2. 🎬 Solo (1996)
      [1️⃣] [2️⃣]
      [🛑 Cancel]
user: (taps 1️⃣)
bot:  Selected: Solo: A Star Wars Story (2018)

      What kind of issue?
      [🎥 Video] [🔊 Audio]
      [📝 Subtitles] [❓ Other]
      [🛑 Cancel]
user: (taps Audio)
bot:  Type: 🔊 Audio

      Now briefly describe what's wrong:
user: Audio goes out of sync after about 30 minutes
bot:  Try to auto-fix? This will delete the file and trigger a new search.
      (Auto-fixes remaining today: 3)
      [✅ Try auto-fix] [📨 Report to Admin]
user: (taps Try auto-fix)
bot:  ⚠️ This will delete the current file from disk and trigger a new download. Confirm?
      [⚠️ Yes, delete & re-search] [No, report to Admin]
user: (taps Yes)
bot:  ✅ Reported as ticket #14
        🔊 Audio - Solo: A Star Wars Story (2018)
      🔧 Auto-fix: Deleted file (if any) and triggered re-search for 'Solo: A Star Wars Story'.
      🔔 I'll DM you when the new file finishes downloading (or after 6h timeout).

      Use /tickets to manage it.
```

If a Fix (Auto-fix, Redownload or Mark Failed, from anyone) already ran on the same movie or episode in the last 30 days, the offer instead warns that another Auto-fix will likely grab the same file, says which Fix ran, how long ago and whether it came back as a repeat, and lists **Report to Admin** first and **Try auto-fix anyway** second.

## Webhook setup

Usher receives events from Seerr so Ticket comments and resolutions, and Request updates, reach the user in Telegram.

1. In `/admin` → **Webhook** tab, copy the webhook URL. A secret is mandatory and Usher rejects unauthenticated webhook POSTs. The panel never shows a saved secret again, so click **Generate**, **Copy** the new secret, then **Save**.
2. In Seerr → Settings → Notifications → **Webhook**: set the URL, paste the secret into the **Authorization Header** field, and enable the **Issue Reported**, **Issue Comment**, and **Issue Resolved** events, plus the Request events: Request Pending Approval, Request Approved, Request Automatically Approved, Request Available, Request Declined and Request Processing Failed (Usher handles all of them; leaving one off drops that notification type, and /status lists the missing ones).
3. Use the Webhook tab's **Test** button to confirm Usher's receiver accepts the saved secret, then use Seerr's own **Test** button on its webhook page to confirm Seerr can reach Usher with it.

Seerr reaches Usher on its LAN address and port. No public URL is required, but Seerr must be able to reach the Usher port.

When Seerr's webhook carries the request id, the Admin's DM about a pending Request has **Approve** and **Decline** buttons, and the DM about a failed Request has **Retry**. Each tap first checks the Request in Seerr, so a Request already handled there is left alone and the DM says what it is now. Both DMs also get an **Open in Seerr** link when a Seerr public URL is configured (`SEERR_PUBLIC_URL`; this link does not fall back to `SEERR_URL`).

## Configuration

Configuration is managed in the `/admin` web admin panel and persisted to `/data/settings.json`. Environment variables only **seed the first run** (and migrate legacy installs); after that the web admin panel wins.

| Setting (env seed) | Required? | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from `@BotFather` |
| `ADMIN_TELEGRAM_ID` | yes | Your numeric Telegram user ID (DM `@userinfobot`) |
| `SEERR_URL` | yes | Base URL of your Seerr instance |
| `SEERR_API_KEY` | yes | Seerr → Settings → General → API Key |
| `SEERR_PUBLIC_URL` | no | Public/reverse-proxy URL used in links sent to users (falls back to `SEERR_URL`) |
| `USHER_PUBLIC_URL` | no | URL the startup DM uses to point the admin back to `/admin` |
| `RADARR_URL` / `RADARR_API_KEY` | no | Enables movie auto-fix when both set |
| `SONARR_URL` / `SONARR_API_KEY` | no | Enables TV auto-fix when both set |
| `ALLOWED_AUTOFIX_TELEGRAM_IDS` | no | Comma-separated Telegram IDs allowed to auto-fix. Defaults to admin only. |
| `USHER_WEBHOOK_SECRET` | no | Auto-generated on first run if unset. To put a known value in Seerr, set it here, or generate one on the panel's Webhook tab |
| `USHER_ENCRYPTION_KEY` | no | Fernet key for encrypting stored Plex tokens. Unlike the settings above, it is read at every start and never saved: once you set it, keep it set, or every Link stops decrypting. Unset, a key is generated once and kept in `/data/encryption.key` |

Runtime/path overrides: `DATA_DIR` (default `/data`), `STORE_PATH` (mappings DB), `WEBHOOK_PORT` (default `8765`), `WEBHOOK_BIND` (default `0.0.0.0`), `TRUSTED_PROXIES` (comma-separated IPs/CIDRs of reverse proxies allowed to set `X-Forwarded-For`/`X-Forwarded-Proto`; unset by default, so those headers are ignored).

## How user attribution works

Because users sign in with Plex, Usher authenticates to Seerr **as the user** (via their Plex token) when making Requests, creating Tickets, posting comments, and listing Tickets. Requests and Tickets are therefore attributed to the real Requester or Reporter in Seerr's UI, with no admin-account workaround or message prefixing. The encrypted Plex token never leaves your stack.

## Auto-fix details

- Offered only for Issue types **Video / Audio / Subtitles** (where a re-download might help).
- Offered only to Telegram IDs in the allowlist (defaults to just the admin).
- **Per-user daily limit** (default 3 per 24h), configurable in the Auto-fix tab. The Admin bypasses it.
- Explicit confirmation required before deletion.
- For TV: **per-episode only**. A whole-season Ticket (the bot offers one episode or a whole season) or a whole-show Ticket (made in Seerr's own UI) is reported to Seerr but never auto-fixed (the season-wide delete workflow was removed in 0.12.0).
- Usher polls the *arr after triggering and DMs the user when the new file lands (or on timeout).
- If the media isn't managed by Radarr/Sonarr, Usher reports "not in Radarr/Sonarr" without touching the Ticket.
- All auto-fix events are recorded in the `autofix_events` SQLite table.
- **Auto-fix blocklists on a repeat** (Auto-fix tab, off by default). When a Reporter's Auto-fix brings back what looks like the same release, Usher runs Mark Failed once on its own: it blocklists the release it can identify, deletes the file and searches again. It tells the Reporter a different release is being tried and DMs the Admin what it blocklisted. It runs once per Auto-fix, so the retry's own repeat does nothing more, and an Admin's Fix never triggers it. Turn it on knowing the risks:
  - A good release can be blocklisted when the download client was at fault.
  - The only available release can be removed.
  - The blocklist grows.

## Files

- `bot/`: the Telegram application package (entry point `python -m bot`): app wiring, Hubs, Request/Ticket/Link flows, Ticket management, webhook handlers, auto-fix poller.
- `webui.py`: aiohttp web admin panel (`/admin`): setup, settings tabs, connection tests, backup/restore.
- `webhook.py`: Seerr webhook receiver (`/webhook/seerr`).
- `seerr.py`, `radarr.py`, `sonarr.py`, `sabnzbd.py`, `tautulli.py`, `plex.py`: API clients.
- `store.py`: SQLite store (encrypted Plex tokens, Links, Subscriptions, auto-fix audit).
- `settings.py`, `const.py`: settings model/persistence and named constants.
- `http_util.py`, `fix_result.py`: API error contracts and retry, auto-fix result types.
- `auth_util.py`, `backup_crypto.py`: admin auth/CSRF/throttle, passphrase-wrapped backups.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `unraid-template.xml`
- `.github/workflows/`: tests gate releases; images publish to GHCR and Docker Hub on tag.

## Privacy

- The Telegram side uses long-polling, so no public URL is required.
- Seerr reaches Usher over your LAN for webhooks; nothing needs to be exposed publicly.
- Plex tokens are encrypted at rest (Fernet) in the local SQLite DB.
- Usher talks only to the Telegram API, your Seerr, your *arr stack, SABnzbd and Tautulli if you connect them, and Plex's auth API. Nothing else.

## Securing the admin UI

Usher has no built-in TLS. On the default bind (`0.0.0.0`, plain HTTP) the
admin password and session cookies are readable by anyone on the same network
segment, and Usher logs a startup warning saying so. Recommended setups:

- **Reverse proxy with TLS** (Nginx Proxy Manager, Caddy, Traefik): terminate
  HTTPS at the proxy and set `TRUSTED_PROXIES` to the proxy's IP or CIDR.
  Usher then trusts `X-Forwarded-For`/`X-Forwarded-Proto` from that proxy, so
  login throttling keys on the real client IP and cookies get the `Secure`
  flag. Without `TRUSTED_PROXIES`, those headers are ignored (they're
  client-forgeable) and every request behind the proxy shares one throttle
  bucket: fail-safe, but set the variable for correct behavior.
- **Loopback-only**: set `WEBHOOK_BIND=127.0.0.1` and reach the admin UI via
  an SSH tunnel; point Seerr's webhook at the same loopback address.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Can't reach `/admin` | Check the container is up and port `8765` is mapped/reachable; the first visit to `/admin` prints a setup token in the logs and writes it to `/data/setup_token` |
| Bot doesn't respond in groups | `@BotFather` → `/setprivacy` → select bot → **Disable** |
| `/link` fails after Plex approval | The user's Plex account isn't shared in Seerr: invite them in Seerr first |
| Webhook events not arriving | Secret mismatch (generate a new one on the Webhook tab, copy it, Save, and paste it into Seerr's Authorization header), or Seerr can't reach the Usher port; use Seerr's webhook **Test** button |
| Auto-fix not offered | Telegram ID not in the allowlist, Issue type is "Other", or today's limit is hit |
| Auto-fix says "not in Radarr/Sonarr" | The media isn't managed by *arr: add it there first |
| `/status` shows ❌ for a service | Check the URL is reachable from inside the container (use an IP, not `localhost`) |

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. Keep changes small and focused. Update [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

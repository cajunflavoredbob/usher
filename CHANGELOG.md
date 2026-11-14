# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Automatic follows**: when one daily pass follows several shows for a user at once (as on the first pass after they Link or switch automatic follows on), they get one summary listing the shows, with a numbered 🔕 Unfollow button for each of the first 25, instead of one message per show. A pass that follows a single show still sends the single message.
- **Menu order**: the command menu and the /start and /help list now run /admin (the Admin only), /request, /tickets, /account, then /help. /admin used to come last and /account first.

## [0.20.0] - 2026-11-14

### Added
- **Plex watchlist**: a detail card has a ➕ Plex watchlist button for anyone with a Link, the Admin included. It finds the title on Plex's side by its TMDb id (a Plex Discover title search, keeping only an item whose ids include that TMDb id, so a remake with the same title is never picked) and adds it to your own Plex watchlist with your own Plex sign-in. When Plex has no match it says so and suggests adding it in Plex, and adds nothing. A Plex sign-in that is no longer valid gets the usual sign-in-again prompt.
- **Share expiry**: the Admin can give a Share an expiry date. The Invite's confirm card has a Never / 7 days / 30 days / 90 days row (Never is the default), and a Share's view has a ⏳ Set expiry button with the same choices and a Clear button, so an existing Share or a pending Invite can get one later. The Shares list marks each Share that has one with "⏳" and the date, and the Share view shows it. Dates are kept in the database, so they survive a restart and travel in a backup. Three days before the date the Admin gets a DM with Extend 30 days, Let it end and Remove now, and a Linked holder gets one telling them to ask the Admin for more time. On or after the date the bot removes the Share by itself (the same removal as /uninvite) and tells the Admin; a Share already gone is forgotten quietly. A removal that fails is retried every hour, and the Admin is told at most once a day, with a Retry button.
- **More like this**: a detail card has a 🔁 More like this button. It lists Seerr's recommendations for that title, or its similar titles when there are no recommendations, as a list like Trending (paging, detail cards and the request buttons). When both are empty it says nothing similar turned up. Linked users and the Admin only.
- **Browse by genre**: the Trending list has a 🎭 Genres button. Pick Movies or Shows, then a genre from Seerr's list, and the genre's titles open as a list like the other Trending lists, with paging, detail cards and the request buttons. Linked users and the Admin only, like the rest of Trending.
- **Request for someone**: the Admin's Requests Hub has a 👥 Request for someone button. Pick a Linked user from the list (8 to a page), then make the Request the usual way. Seerr saves it as that user and checks their own permissions and quota, so they get the normal Requester DMs. The confirm names them and shows their quota, and a quota or permission refusal names them too. Other users never see the button.
- **Request remaining seasons**: a show's detail card and its "available" notice gain a ➕ Request remaining seasons button for Linked users, shown only when at least one season can still be requested. The confirm lists those seasons (and your quota), and Send asks Seerr for every season not already available or requested, as you. When nothing is left it says "Nothing left to request", and a quota or permission refusal gets the same message as a normal Request.
- **Auto-retry of failed Requests**: when Seerr reports a Request failed, Usher retries it in Seerr by itself after 1 hour, then 6 hours, then every 24 hours, up to 3 tries by default. Before each try it checks the Request, and a Request that is no longer failed (or already retried by the Admin) is dropped without a message. The Requester's "failed" DM says when the next try is, and after the last try they get one message that it needs the Admin. The Admin's failed alarm says auto-retry is on and keeps its Retry button. The web admin panel's Telegram tab has a switch (on by default) and the number of tries (1 to 5). Waiting retries survive a restart.
- **Auto-fix repeat detection**: when a replacement file comes back as the same release that was just removed, the Progress card and the completion DM say it is a repeat. A Reporter's repeat gives back that day's Auto-fix use when the daily limit is on (an Admin's own Fixes are never given back). Fix history is kept 30 days instead of 2.
- **Ask the Admin to try a different release**: after a repeat, the Reporter gets an "Ask the Admin to try a different release" button. One tap DMs the Admin the Ticket and the release that came back, with Mark Failed first; a second tap says "Already sent to the Admin", even after a restart. The Admin's own repeat DM carries Mark Failed directly.
- **Blocklist on a repeat**: a new Auto-fix switch in the web admin panel, off by default, makes Usher run Mark Failed by itself once when a Reporter's Auto-fix comes back as a repeat. The Reporter is told a different release is being tried and the Admin is told what was blocklisted. Admin Fixes never trigger it. If it can't run (title busy, Ticket resolved, Mark Failed failed), the Admin gets a DM saying why.
- **Pointless retry warning**: when a Fix already ran on the same movie or episode in the last 30 days, the Auto-fix offer warns that another will likely grab the same file, says which Fix ran and how long ago, and offers Report to Admin first and Try auto-fix anyway second. The plain report button is now called Report to Admin.
- **Coming this week**: the Requests Hub has a 🗓 Coming this week button listing the episodes airing and movies releasing in the next 7 days (from Sonarr and Radarr), each under its day and 10 to a page. Linked users get an "Only what I follow" button that keeps just the shows they follow. A service that is not set up is left out; with neither, the message says so.
- **Automatic follows**: with Tautulli linked, a daily job Follows shows a Linked user has watched 2 or more episodes of in the last 30 days (matched by Plex username). The first automatic Follow of a show sends one message with Unfollow; a Follow you removed is never added back, and the switch under Account, 🔔 Notices now turns it off. The message follows the Follow pings switch in the web admin panel.
- **Tautulli feeds new content**: with Tautulli linked, the Follow pings and the daily digest use Tautulli's recently added list instead of Radarr and Sonarr imports, so titles added by hand count too. Items Tautulli can't match to a TMDb id are skipped. Unlinking Tautulli goes back to Radarr and Sonarr without repeating anything already seen.
- **Tautulli new content fixes**: a season or show that Plex lists as one row now records every episode in it, up to 250 new items are handled per check so a big batch finishes over a few checks, and an item Plex has not matched to TMDb yet is tried again for up to 6 hours instead of being dropped. Linking Tautulli starts from where Radarr and Sonarr last read, and a failed check no longer moves the other source's place.
- **Daily digest**: Linked users can opt in (Account, then 🔔 Notices) to a daily "New on the server" message listing what was added the day before: movies first, then shows with how many new episodes, each with a ▶️ Watch now button or a button that opens its detail card. A day with nothing new sends nothing. It is off for everyone until the Admin turns on "Send the daily Digest" and picks the hour (the server's local time) in the web admin panel's Telegram tab, where Digest DMs also has its own switch. Notices also holds a switch for automatic follows (on by default).
- **Follow a show**: Linked users can tap Follow on a show's detail card or on its "available" notice and get a ping for each new episode, with a ▶️ Watch now button when the title is in Plex. Episodes that land together arrive as one ping, and each episode is only ever pinged once. Follows are listed with Unfollow under Follows in the Requests Hub. The Admin can switch Follow pings off in the web admin panel (Telegram tab, Notifications). New episodes are found by checking Radarr and Sonarr imports every 15 minutes.
- **Watch now**: the "available" DM to the Requester and the Subscription notice gain a ▶️ Watch now button that opens the title in Plex. It appears once Seerr has a Plex link for the title; without one the notice is sent as before.
- **Tautulli**: link Tautulli in the web admin panel (URL, API key, Test, Save, Remove the saved key) and see its version or error in /status.
- **Playback details on Tickets**: with Tautulli linked, the Admin's new-Ticket DM and Ticket card show how the Reporter last played that title (player, platform, direct play or transcode, video and audio codecs, date). Left out when there is no matching play; never shown to the Reporter.
- **Popular here this week**: with Tautulli linked, the Trending list gains a category of the server's most watched movies and shows over 7 days, as titles only (no names, no play counts). Titles Tautulli can match to TMDb open detail cards and Requests as usual.
- **My stats**: with Tautulli linked, /mystats (and a My stats button in the Trending list) shows your plays and watch time for the last 7 and 30 days and your most watched titles.
- **Now playing**: with Tautulli linked, the Admin's Status lists who is playing now, with a Stop button on each stream (confirm first, with or without a message to the viewer).
- **Inactive Shares**: with Tautulli linked, the Admin Hub's Shares list gains a 💤 Inactive button listing Shares that haven't watched for 60 days or more (changeable in the panel's Tautulli tab), or never, each opening the usual Share view with Remove.
- **Service alarms**: the Admin gets a DM when Radarr or Sonarr reports a new warning or error (with its wiki link), and when a disk drops below a free-space limit (default under 10%; an optional GB limit counts too, whichever is hit first, 0 leaves it off, set in the panel's Telegram tab). One DM per problem, one more when it clears. Notices are left out, and a disk both apps report counts once. Each alarm has a panel switch, and /status gains Health and Free space lines.
- **Plex offline alarm**: the Admin gets a DM when the Plex server goes offline (plex.tv says so, or Tautulli if linked) after two checks in a row, and one more when it is back. It needs the Admin's Plex link; without it the check is skipped and /status says so. Panel switch in the Telegram tab, and /status shows Plex online or offline.
- **Download controls**: with SABnzbd connected, the Admin Hub gains a ⏯ Downloads button showing whether SABnzbd is paused, its speed, limit and what's left, with Pause, Pause 30 minutes, Resume and speed limit buttons (25%, 50%, 75%, No limit). A percent limit tells you to set SABnzbd's maximum speed first when it has none.
- **Ask for access**: when Seerr rejects someone's Plex sign-in while they link, the failure message offers 🙋 Ask for access. After they confirm what the Admin will see (their Telegram name and Plex username and email), the Admin gets a DM with Approve and Ignore, saying whether that email already has a Share. Approve opens the Invite flow at the Library picker with the email filled in, and sending the Invite tells the person to accept it in Plex and link again. Ignore drops the ask without telling them. One open ask per person, none for 7 days after an Ignore, at most 10 open at once, and asks older than 30 days are pruned.
- **Broadcast**: the Admin Hub gains 📣 Broadcast. The Admin types a message, sees a preview with Send and Cancel, and it goes out as plain text under a "📣 From the Admin" header to every Linked user except the Admin, at about 25 messages a second. The preview is edited into a report of how many got it and how many couldn't be reached (for example, they blocked the bot). A restart mid-send carries on without messaging anyone twice, and finished broadcasts older than 30 days are deleted.

### Fixed
- **Group chat menus belong to one person**: in a group, a button on a menu the bot sent to someone else now does nothing and shows "This menu isn't yours". It used to run as the person who tapped and show their own lists in the group. Menus in direct messages work as before.
- **Report a Ticket buttons are versioned**: every button in the Report a Ticket flow (Cancel, search again, Season, Episode, Issue type and both Auto-fix questions) now belongs to one report, so tapping an old menu after starting a new /issue says the context changed instead of cancelling or moving the newer report along.
- **Small bot fixes**: a Broadcast preview's Send and Cancel now say "That preview is old" and do nothing when tapped on a draft you have since replaced, instead of sending the newest draft. A Ticket on a whole show (season 0 from Seerr) now gets its playback lines. The Plex alarm and /status say the linked Plex account owns no server instead of "unexpected error". "1 day" reads singular in the Inactive Shares list, the access-ask refusal and the quota line. The Popular button on an old Trending list after Tautulli is unlinked shows a short notice instead of nothing. The Auto-fix completion DM drops "Yes, close it" while the bot's own retry is running.
- The Seerr Ticket no longer says the file was removed when an Auto-fix was skipped or failed; a comment now says what happened (a fresh download started, skipped for the limit or a busy title, ran without a search, or failed), and a failed Auto-fix still gets the Reporter a reply.
- A bot token Telegram rejects, or one with spaces, non-ASCII characters or an absurd length, no longer stops Usher: the Admin page stays up with a note on the Telegram tab, and such a token is refused at save and in setup.
- A mistyped service URL (for example a non-numeric port) is refused at save, in setup and by Test; one already saved turns off only that service instead of stopping Usher.
- A Digest hour of 0 saves again on the Telegram tab, an unreadable login or setup form is a 400 instead of a server error, and the New Plex Sign-In check stays quiet with an unusable Seerr URL.
- Trending (the /trending command, its Requests Hub button, category, page and detail buttons) is now for Linked users and the Admin only; anyone else is told to /link first. Popular here this week and the My stats button follow the same rule, and a forged tap on Popular is refused.
- The admin login lockout now holds against a burst of parallel attempts, and the login and first-run setup pages refuse oversized or compressed form bodies.
- Restore runs one at a time, so a double-clicked Restore can no longer overwrite the pre-restore snapshot.
- The Webhook tab's Test works when `WEBHOOK_BIND` is set to one specific address.
- A crafted non-ASCII cookie or Authorization header is refused instead of causing a server error.
- The Admin's ❌ on a stale /requests list no longer deletes a Request that was approved in the meantime.
- Approved, declined and failed events change only the requester's Progress card, not other users' cards for the same title, and a Request still waiting for approval keeps its card past 24 hours.
- Subscribe and Ask the Admin no longer show an error when Seerr or the Arr apps are slow, and a long /subscriptions list always sends.
- Non-admins tapping an Admin-only Ticket button now see "Admin only."; double taps no longer send duplicate messages; commands addressed to another bot in a group no longer end Usher's flows.
- The Account Hub says when a Link can no longer be decrypted, instead of "Linked as".
- When two Telegram accounts share one Seerr account, the newest Link gets the DMs.
- An old "Ask the Admin" claim no longer blocks a new Ticket that reuses its id.
- Overlapping settings saves no longer fail with a server error.
- Setting `DATA_DIR` now moves the database too in the published image.

### Changed
- The CSRF cookie is HttpOnly.
- Fix results name the service problem ("Radarr isn't responding") instead of an exception class.
- The relink button reads "Sign in to Plex again", and Request confirmations promise a DM only when Request updates to requesters are on.
- The Ask the Admin DM warns that Mark Failed deletes the current file.
- README and /help now list Now playing, Popular here this week, /mystats, the Reporter's playback details, Ask the Admin, Follows, the Digest, Coming this week, Downloads, Broadcast and access asks. The privacy line names every plex.tv use, and the Docker note says `WEBHOOK_BIND=127.0.0.1` needs host networking or a proxy on the same host.
- README, Unraid template and `.env.example`: corrected first-run and webhook-secret steps, the full list of Seerr events to enable, and what `USHER_ENCRYPTION_KEY` does. The Unraid template now restarts the container after Usher's own restarts.

## [0.19.1] - 2026-11-12

### Fixed
- A command sent while a slow step of a flow is still running no longer lets that
  step bring the flow back to life afterwards, so your next message is no longer
  swallowed by the flow you left. A Ticket Reply started in that window no longer
  leaves a stale reply target behind, and a Plex sign-in started in that window
  no longer starts polling for a flow you already left.

## [0.19.0] - 2026-11-12

### Added
- The Auto-fix and SABnzbd tabs show a "Remove the saved key" box next to a
  stored Radarr, Sonarr or SABnzbd API key; ticking it and saving clears the key.

### Fixed
- Cancelling a TV Request from /requests now closes its Progress card with
  "Request cancelled." when it is the only waiting card made for the same
  seasons. Cards made before this update are left as they were.
- The abandoned, complete and timed-out messages for a Fix an Admin ran now
  name that Fix (Redownload or Mark Failed) instead of calling it Auto-fix.
- The bot token is replaced with a placeholder in log output once it is loaded from
  settings. If Telegram rejects the token at startup, the log now says so in one
  line instead of printing an error message and traceback.
- A Test with the API key field left blank no longer sends the saved key to a
  URL other than the saved one. Seerr, Radarr, Sonarr and SABnzbd Test now ask
  you to type the key again for the new URL.
- Mark Failed now blocklists the release that produced the file on disk (the
  grab matching the newest import) instead of the newest grab, which could be a
  later download still in flight. When that release can't be identified,
  nothing is blocklisted and the final reply says so.
- "Your open tickets" no longer lists other people's Tickets for a user whose Seerr
  account can manage or view all Issues; the list is now limited to the user's
  own.
- "Your requests" no longer lists other people's Requests (with Cancel buttons
  on their pending ones) for a user whose Seerr account can manage or view all
  Requests; the list is now limited to the user's own.
- The logged-in Settings page no longer shows the bot token, Seerr, Radarr,
  Sonarr or SABnzbd API keys or the webhook secret. A stored secret shows an
  empty field with "Already set"; saving with the field blank keeps the stored
  value, and the Telegram, Seerr, Auto-fix and SABnzbd Test buttons use it.

## [0.18.1] - 2026-11-12

### Changed
- The daily-limit notice when creating a Ticket and the Auto-fix intro in the
  Admin panel now say Ticket instead of issue.

### Fixed
- Backup and restore now use the encryption key file Usher actually runs with,
  so a custom ENCRYPTION_KEY_PATH is included in the backup and restored in
  place instead of a stray copy in the data folder.
- The Fix timeout message no longer tells a Reporter to check Sonarr or Radarr,
  which only the Admin can open.
- When a Request or Ticket action expires before you finish re-linking, the
  message now points you to the command that restarts that action (/request,
  /issue or /tickets) instead of always naming /issue and /tickets.
- Shutting down Usher now closes the SABnzbd connection too.
- After a Plex sign-in finishes or times out, the "Didn't work?" button no
  longer treats the old sign-in as still in progress.
- Finishing or cancelling /issue no longer resets the counters that retire old
  Request and Invite confirm buttons, so an older confirm card cannot submit a
  newer Request or send a newer Invite.
- When creating a Ticket, a comment or a Request times out after Usher sent it,
  the message now says it may have gone through and to check before trying
  again, instead of "try again in a minute", so you look before filing a
  duplicate.
- Container logs no longer print the bot token or the SABnzbd API key in
  request URLs, and finishing first-run setup now restarts Usher into
  full mode instead of hanging.
- The first-run setup page no longer shows a bot token, Seerr API key or Admin
  Telegram id that were set from the environment. It says "Already set" and
  keeps the stored value when you leave the field blank.
- Progress cards now update from Seerr webhooks (Approved, Available,
  Declined), Subscriptions fire, and Open in Seerr opens the title page, even
  when Seerr sends the TMDb id as text, which its default template does.
- The Notify-me button on a title card now works instead of saying the menu is
  from an older message. Approve, Decline and Retry on the Admin's Request messages keep
  working after 6 hours or newer messages. A button from a different chat is no
  longer mistaken for a live one.
- Linking again after a revoked Plex sign-in no longer unlinks you first, so a
  plex.tv outage cannot leave you unlinked. The Plex sign-in link and code are
  only shown in a private chat, and the "Didn't work?" button only acts while a
  sign-in is in progress.
- A Fix whose file delete timed out after Radarr or Sonarr had already removed
  the file no longer stops with "couldn't delete file". Usher checks the file is
  gone and still triggers the new search.
- Replying to or resolving a Ticket from a button no longer loses your comment
  when a /request or /invite prompt is open. Usher now asks you to finish or
  /cancel that first.
- The Webhook tab no longer shows a Seerr URL ending in /admin when the Usher
  Admin UI URL was saved with /admin on the end. That URL never reached the
  webhook, so no Seerr event was processed. The same URL cleanup now backs the
  startup message, and the bad-URL error names the "Usher Admin UI URL" field.
- A long Ticket description or comment with line breaks no longer makes
  Telegram reject the message. Cut-off text now closes its formatting, so the
  Ticket detail, History and Back open again, and a very long comment or new
  Ticket still reaches the Admin's DM.
- A resolved Ticket now shows Status: Resolved and offers only Reply, and a
  Fix tapped from an older message on a resolved Ticket is refused instead of
  deleting the current file again.
- Cancelling a movie Request from /requests now closes its Progress card
  with "Request cancelled." instead of leaving it on "Waiting for approval".
- Typing a command in the middle of /issue, /request, /invite or /link now
  tells you the unfinished flow was stopped, instead of dropping it silently.
- Tapping a number in /trending now runs the same checks as a search: a movie
  already in Plex or a blocklisted title says so and keeps the list, and a
  movie in Plex offers the 4K copy when you may request it.
- Ticket messages now reach the right person even when their Seerr Display Name
  differs from their Plex name. A Display Name can no longer hide a Ticket
  message or send it to someone else, and the Admin is not messaged about their
  own comments.
- Restoring a backup now restarts Usher promptly. Once a restart is scheduled,
  the Admin panel refuses saves ("Usher is restarting. Refresh in a moment.") so
  they cannot overwrite the restored settings, and an unfinished /link sign-in
  no longer holds the restart up.
- A Fix on one episode no longer shows another episode's download percent, or
  spends its one-time SABnzbd speed boost on that other episode's download.

## [0.18.0] - 2026-11-11

Hubs: a short command menu with one button message per area.

### Added
- **Hubs.** The command menu now lists only /account, /request, /tickets,
  /help, and /admin for the Admin. Each opens one message of buttons for
  its area. Flows started from a Hub end with a back button to it, plus
  Try again when a step fails. Every command that left the menu still
  works when typed, including ones that take an argument.
- **Account Hub (/account)** shows the Link state with a Link or Unlink
  button.
- **Requests Hub (bare /request)** with New request, My requests, Trending
  and Subscriptions. `/request <title>` still searches straight away.
- **Tickets Hub (/tickets)** with Report a problem and the ticket list (My
  tickets for a User, Open tickets for the Admin).
- **Admin Hub (/admin, DM only)** with Invite, Shares and Status. The Share
  list pages with Prev/Next. Anyone else gets "Admin only."; the Admin in a
  group gets "DM me /admin", and /invite, /uninvite and /status now answer
  the same way.
- **Approve, Decline and Retry on Admin Request DMs.** A pending Request DM
  gets Approve and Decline, a failed one gets Retry, and both get an Open in
  Seerr link when a Seerr public URL is set. A tap checks the Request's
  status in Seerr first, so a Request handled elsewhere is left alone. When
  the Admin is the requester, their own failed-Request DM carries Retry and
  Open in Seerr too.
- **/invite explains Plex "already sharing".** The reply names /uninvite and
  the Plex web app instead of a generic failure.

### Changed
- **A submitted Request keeps its result on the flow message**, and the
  Progress card arrives as a new message.
- **Admin Fix progress goes on a new Progress card.** The Ticket card shows
  the Ticket again with its buttons, and the card header names the Fix
  (Redownload or Mark Failed) instead of always saying Auto-fix.
- **Typed /uninvite matches page 8 at a time** with Prev/Next, like the
  Share list, instead of asking for a longer name.
- **Copy says Ticket and Subscription.** The panel toggles read "Ticket
  DMs" and "Subscription DMs" (settings keys unchanged), the bot's
  Subscription list and empty state say Subscriptions, and backup notes
  say "the database" instead of "mappings". User-facing em dashes are gone.
- **Log lines and job names say "Progress card"** where they used to say
  request watch. Best-effort Telegram and file calls that fail now log the
  error at debug level instead of passing silently.
- **Web UI address placeholders** use documentation-range addresses.
- **aiohttp 3.14.4.**
- The database gains two add-only columns on `pending_autofixes`
  (`reply_markup`, `action_name`), created on first start. No data changes.

### Fixed
- **The Progress card keeps its keyboard** on auto-fix progress edits, so
  "⬅️ Tickets" no longer disappears on the first update.
- **A new-Ticket DM keeps its History button** after a Fix starts and you
  go Back.
- **Double taps on Reply or Close with comment** no longer start the flow
  twice.

## [0.17.3] - 2026-10-02

Dependency refresh.

### Changed
- **aiohttp 3.14.3 and cryptography 50.0.2.** Existing backups and stored
  Plex tokens carry over unchanged.

## [0.17.2] - 2026-08-21

Honest install docs.

### Fixed
- **README no longer claims a Community Applications listing.** Usher was
  never submitted to CA; the Unraid section now walks through adding the
  repo as a template repository instead.

### Changed
- **The published image is the documented install path.** A `docker run`
  block using `cajunflavoredbob/usher:latest` leads the install section,
  `docker-compose.yml` pulls that image by default (building from source
  is the commented-out alternative), and first-run setup is factored into
  one section shared by every install path.

## [0.17.1] - 2026-08-17

Terser admin panel.

### Changed
- **Admin panel copy rewritten in a terse, direct voice.** Notes are one or
  two short sentences; parentheticals are gone; the webhook section points
  at Seerr's settings instead of enumerating every event name (the bot's
  startup connection check still names anything missing).

### Removed
- **The progress-cards toggle.** Card edits are silent, so they never
  belonged in the notification controls; cards are simply always on. A
  saved `tg_progress_cards` key in settings.json is ignored.

## [0.17.0] - 2026-08-17

Admin provisioning and notification controls.

### Added
- **`/invite`** (admin, DM-only): share the Plex server with an email or
  Plex username straight from the bot: a per-invite library checklist
  (all libraries pre-selected, tap to exclude, e.g. the 4K sections), an
  explicit confirmation showing exactly what will be shared, then the
  Plex invite is sent from the admin's own linked account. Seerr picks
  the new user up on their first Plex sign-in.
- **`/uninvite`** (admin, DM-only): revoke a share or cancel a pending
  invite, with a live lookup, disambiguation when several shares match,
  and an explicit confirm step.
- **Notification controls** in the admin panel (Telegram tab): per-class
  toggles for requester request updates, admin new-request notices, the
  failed-download alarm, issue DMs, availability-watch notifications, and
  the morphing progress cards. All default on. Silencing a class stops
  its DMs (events that fire while it's off are not replayed later); the
  cards toggle is paint-only, so queue boosting and download tracking
  continue underneath. The requester's failure DM drops its "admin has
  been notified" claim when the admin alarm is off.

### Changed
- Progress pollers tick every 10s (was 20s) for snappier card updates.
  The arr RefreshMonitoredDownloads nudge stays throttled to one per 15s
  per arr, and Telegram edits still fire only when the line changes.

## [0.16.2] - 2026-08-17

Live progress that tracks reality, and a search fix for punctuated titles.

### Fixed
- **Searches containing commas (or any reserved URL character) work.**
  Seerr rejects raw reserved characters in the query string, so a verbatim
  title like "Goodbye, Lara" failed outright; queries are now fully
  percent-encoded. The no-matches message also nudges toward spelling and
  fewer words, since TMDB search is strict.
- **Progress cards no longer lag minutes behind the download.** Three
  causes fixed together: pollers tick every 20s instead of 45-60s; each
  tick asks Radarr/Sonarr to sync with the download client
  (RefreshMonitoredDownloads) instead of waiting out their own schedule;
  and when SABnzbd is configured the card reads the job's LIVE state
  directly from it: real percent and time left while downloading, then
  "📦 Post-processing (Extracting)…" and "📥 Importing…" instead of the
  old frozen 95% during unpack and import. Multi-file grabs aggregate
  across their downloads, a failed download shows an honest "waiting for
  another release" holding line instead of stranding, and a card the
  webhook just finalized can no longer be overwritten by a stale progress
  edit.

## [0.16.1] - 2026-08-16

### Fixed
- **Admin-triggered auto-fixes now morph their progress card.** 0.16.0
  only attached the morphing card on the /issue flow's own confirmations;
  fixes started from the /tickets Fix menu enqueued the poller without a
  card message, so nothing ever showed replacement progress. The Fix
  outcome message is now the card, same as /issue.
- Sonarr queue progress falls back to the series' queue records when a
  grab carries no per-episode ids (season packs), instead of showing
  "Searching" while the download runs.


## [0.16.0] - 2026-08-16

Discovery, availability watches, and live download progress.

### Added
- **`/trending`**: browse Trending, Popular Movies, Popular Shows, and
  Upcoming as text-forward numbered lists with one-tap category switching.
  Every result is immediately requestable (the tap drops straight into the
  /request flow) and carries the same 🖼 poster/detail cards as search.
  Lists are TMDB-driven only: nothing surfaces other users' requests or
  activity.
- **Availability watches**: detail cards on titles that aren't available
  yet grow a 🔔 "Notify me when available" button; `/subscriptions` lists
  your watches with one-tap unwatch. Watches are strictly private
  (nobody can see anyone else's, or that anyone else has one) and
  one-shot: consumed on the first availability event, so re-grabs and
  auto-fix replacements can't re-fire them.
- **Morphing request cards**: the /request confirmation message now edits
  in place through the download's life: waiting for approval → approved
  and grabbing → live percent and time-remaining from Radarr/Sonarr →
  ✅ available (or declined/failed), driven by a background poller plus
  the Seerr webhook events. Auto-fix confirmations morph the same way
  while the replacement downloads; the interactive completion/timeout DMs
  are unchanged.
- **SABnzbd integration**: a new admin tab (URL, API key, connection
  Test) with an **auto-bump priority** option: Off / High / Force. When
  enabled, any download this bot caused to be grabbed (requests and
  auto-fixes) gets its SABnzbd queue priority raised automatically, once
  per download.
- The bot now **registers its Telegram command menu at startup** (with an
  admin-scoped variant including /status), so new commands appear in the
  menu without touching BotFather (0.15.0's /request shipped invisible
  because the menu was last set by hand long ago).
- Search/browse detail buttons are labeled 🖼 instead of ℹ️, which
  rendered confusably close to the numbered pick buttons.


## [0.15.0] - 2026-08-16

Media requesting. Users can now ask for movies and shows through the bot,
not just report problems with them.

### Added
- **`/request`**: search, pick, and request media, submitted to Seerr as
  the linked user so Seerr's own permissions, quotas, and auto-approve
  rules apply (the bot adds no gate of its own). `/request <title>` skips
  the prompt. Movies confirm in one step with a quota preview ("4 of 10
  requests left in the current 7-day window"); TV gets a multi-select
  season keyboard that marks already-available or already-requested
  seasons and only offers what a request would actually cover. Outcomes
  are stated honestly: approved-and-grabbing vs waiting-for-approval vs
  quota exceeded vs already requested vs nothing-left-to-request.
- **`/requests`**: your requests with live status (pending / grabbing /
  declined / failed / available) and one-tap cancel for still-pending
  ones; the admin sees everyone's, with requester names.
- **Request lifecycle DMs** via the Seerr webhook: requesters hear when
  their request is approved, declined, available, or failed; the admin
  gets new-request and auto-approved notices plus failure alerts. Enable
  the request events on Seerr's webhook (the Webhook tab's help text lists
  them). Search results in `/request` also annotate availability up front
  so nobody burns a tap on something already in Plex.
- The `/issue` flow's "that title isn't in Seerr's library yet" dead end
  now offers **Request it instead**, jumping straight into the request
  flow with the title carried over.
- **Search pagination**: one Seerr fetch of up to 20 results rendered as
  pages of five with Page N navigation (paging is a pure re-render, no
  extra queries).
- **Detail cards**: every search result has an ℹ️ button that posts a
  dismissable card with the poster (when one exists), rating, type,
  availability, and a synopsis, so users unsure by name alone can check before picking.
  The pick list itself stays text-only.
- **4K requests**: users whose Seerr permissions allow 4K get a ✨ 4K
  option on the confirm screen, and a title that's already available in
  standard quality passes through to a 4K-only confirm instead of
  dead-ending (unless the 4K copy is also covered). Known limitation: the
  TV season picker marks availability on the standard track, so
  re-requesting a standard-available season in 4K still needs the Seerr
  UI.

Robustness built in from the start: stale menus from an earlier /request
toast instead of acting on the newer flow, detail cards can't crowd out
the live pick list, request webhooks resolve the requester by Seerr user
id (display names are user-editable), the season picker mirrors Seerr's
per-tier drop rules exactly, and an admin who never ran /link still gets
request lifecycle DMs, including download failures.

- **/status now checks the Seerr webhook agent**: it reads Seerr's webhook
  notification settings and flags a disabled agent or missing event types
  (upgraded installs predate the request events, and a miss silently
  drops every issue/request DM). The startup DM carries the same line.
- Cancel taps on /requests acknowledge instantly and report outcomes on
  the re-rendered list (a slow Seerr could previously outlive the
  callback's validity and turn a successful cancel into an error DM);
  /request shows typing/working states at its slow hops like /issue does;
  TV confirmations report the seasons Seerr actually granted when it
  skipped already-covered ones.

## [0.14.2] - 2026-08-16

### Fixed
- The top-bar Log out button sits on the same line as the version and theme
  toggle instead of riding lower (it inherited the generic button's top
  margin).

## [0.14.1] - 2026-08-16

### Fixed
- **The admin UI renders cleanly on phones.** Below 640px the settings tabs
  spread across a single full-width row (no wrap, attachment to the panel
  preserved), page and card padding tighten, and text inputs bump to 16px so
  iOS Safari no longer zooms the page when a field is focused.

## [0.14.0] - 2026-08-16

The project is now **Usher** (formerly Hermes). Upgrades are in-place: old
settings keys, `HERMES_*` environment variables, and passphrase-wrapped
backups from earlier versions are still read.

### Changed
- **Renamed to Usher** across the bot, web UI, images, docs, and Unraid
  template. Published images move to `cajunflavoredbob/usher` (Docker Hub)
  and `ghcr.io/cajunflavoredbob/usher`.
  - `settings.json` upgrades transparently: `hermes_public_url` is read as a
    fallback and written back as `usher_public_url`.
  - `USHER_ENCRYPTION_KEY`, `USHER_PUBLIC_URL`, and `USHER_WEBHOOK_SECRET`
    replace the `HERMES_*` names; the old names still work.
  - Wrapped backups now use a `USHER-BAK1` header and a `.usher-backup`
    extension; restoring `HERMES-BAK1` / `.hermes-backup` files keeps
    working.
  - Session and CSRF cookies are renamed, so everyone is signed out of the
    admin UI once after upgrading.
  - New Plex links are authorized as "Usher"; devices linked earlier keep
    showing "Hermes" in Plex's authorized-device list until relinked.
- **The admin UI is now themed with Dracula Pro variants**: Van Helsing
  (near-black, blue accent) by default, with a light/dark toggle in the top
  bar that switches to Alucard (light, purple accent). The choice persists
  in the browser and applies before first paint.

### Fixed
- The active settings tab now meets the panel squarely instead of floating
  over the panel's rounded corner, and the Seerr sign-in warning banner
  moved inside the Seerr panel so the attachment holds when it appears.

## [0.13.0] - 2026-08-16

Hardening across security, backend reliability, UX, and maintainability. No
breaking changes; existing settings and backups load unchanged.

### Security
- **CSRF tokens are now HMAC-signed and bound to the session.** The bare
  double-submit cookie could be planted by anyone able to set a cookie on
  the origin. Tokens now carry an HMAC over the session cookie's signature,
  so a token minted for one session (or none) can't be replayed against
  another. Cookies rotate transparently on login and password change.
- **Login no longer blocks the event loop or lets a username-rotating flood
  bypass throttling.** Password verification runs off-thread, a per-IP
  aggregate cap is checked before any PBKDF2 work, and throttle eviction
  prefers non-locked buckets so a flood can't evict (and reset) a locked
  bucket.
- **Backup restore enforces an upload size cap and a per-member
  decompression cap.** Multipart uploads bypass aiohttp's `client_max_size`,
  so the streamed read is now bounded (413 past 32MB) and each zip member's
  declared decompressed size is checked before it is read, closing a
  zip-bomb OOM vector.
- **Restore writes secrets with 0600 permissions** instead of leaving
  `settings.json` and the mappings DB world-readable on the host volume, and
  the settings temp file is created 0600 up front rather than chmod-ed after
  the rename.
- **Session logout is now a CSRF-protected POST with server-side
  revocation**, so a captured cookie dies at logout instead of surviving its
  full TTL.
- **Audit log entries sanitize control characters** so an attacker-supplied
  username or forwarded-for hop can't forge log lines, and only IP-parseable
  forwarded-for hops are trusted.
- **The first-run setup token fails closed** when its file is unreadable
  (503) instead of skipping the admin-creation check, and an empty token
  file regenerates rather than disabling the check.
- **Backup passphrases have a minimum length**, and `/admin` responses carry
  `Cache-Control: no-store` so intermediary caches can't retain credentials.

### Reliability
- **Sonarr fixes match on season AND episode and reject a null-season
  payload**, so a malformed issue can no longer delete an episode file from
  the wrong season.
- **HTTP calls carry a total per-attempt deadline** on top of the per-phase
  timeouts, and Radarr/Sonarr/Plex 2xx bodies are shape-checked, so a
  drip-fed or garbage-but-200 response can't hang or crash a handler.
- **The Seerr per-user client cache uses per-token creation locks**, so one
  slow sign-in no longer stalls every other user's cache hits; retired
  clients are reliably closed on shutdown and a longer close grace covers a
  full retry chain.
- **Settings and backup work runs off the event loop** (settings saves,
  backup VACUUM/zip, passphrase KDF), and restore serializes against live DB
  writes so a write can't land between the DB swap and WAL cleanup.
- **Autofix bookkeeping tables are pruned** on startup and daily instead of
  growing for the life of the install.
- **Hot-reloaded clients close after a grace period** instead of killing
  in-flight requests, and shutdown cancels those timers instead of hanging
  up to 90 seconds.
- **The HTTP server starts before the connection probes**, so `/healthz` and
  the webhook receiver are reachable immediately on boot.

### UX
- **Conversation timeouts now fire and notify** even when the flow was
  abandoned at a button step (the message-only timeout handler never ran for
  callback-driven flows), so /issue, ticket reply, and resolve-comment
  prompts explain the timeout instead of silently swallowing later text.
- **Non-text replies get an actionable prompt** (a screenshot sent as a
  description no longer vanishes), and an unhandled error surfaces a
  last-resort "please run the command again" message instead of a silent
  dead end.
- **The episode picker paginates**, so long-running seasons no longer exceed
  Telegram's keyboard limit and strand a dead menu; a season with no episode
  list falls through to whole-season selection.
- **Follow-up and admin surfaces are consistent:** the admin Redownload/Mark
  Failed menu states that it deletes the current file, decrypt-failed users
  get the same remedy everywhere, specials render as `S00Exx`, the resolve
  flow guards against a double-tapped close, and connection-status text
  renders correctly (it was showing literal Markdown asterisks).
- **Non-admins get an actionable message** when Seerr isn't configured
  instead of a placeholder admin URL.

### Maintainability
- Consolidated duplicated literals behind named constants (client-close
  grace, keyboard widths, password/passphrase minimums) and a shared
  `json_or_raise` helper; refreshed README, the Unraid template, and stale
  comments/docstrings to match current behavior.

## [0.12.2] - 2026-07-18

### Fixed
- **Follow-up DMs no longer claim "All seasons" on a single-episode
  ticket.** Seerr sends the affected season/episode only in the
  issue-created webhook's `extra` array; comment and resolved payloads
  carry neither it nor problemSeason/problemEpisode. Those handlers fed
  the absent value to `format_scope_label`, whose absent -> "All seasons"
  rule is correct for the created event but wrong for follow-ups, so a
  comment on a Season 6 Episode 9 ticket announced "All seasons".
  Follow-ups now resolve the ticket's real scope through the API
  (new `followup_scope_label`) and render no scope line when it cannot
  be determined - a missing line beats a wrong one. Season 0 still
  renders "All seasons": zero means whole-series, None means not sent,
  and that distinction is the bug.

## [0.12.1] - 2026-07-13

### Fixed
- **The 0.12.0 image crash-looped on startup** (`ModuleNotFoundError:
  fsutil`): the Dockerfile copied a hand-maintained list of root modules,
  and the two modules added in 0.12.0 (`fsutil.py`, `procutil.py`) were
  never added to it. The COPY is now a glob (`*.py`), and the release
  workflow gained an image-level import smoke test that runs BEFORE any
  push - pytest runs from the repo checkout, so it structurally cannot
  catch packaging failures like this one.

## [0.12.0] - 2026-07-12

Broad hardening pass over v0.11.27 across security, backend reliability, UX
consistency, and code hygiene.

### Removed / internal hygiene
- Dead whole-season auto-fix chain removed (`auto_fix_season`,
  `SeasonSearch` trigger, `season_files_present`, the season branch of
  `PendingAutofix.is_complete`) - no caller ever produced a season-shaped
  poll; DB columns kept for a future real implementation.
- The 6-hour auto-fix timeout has one source of truth: the store default
  and both user-facing texts now derive from `AUTOFIX_TIMEOUT_HOURS`.
- `schedule_clean_exit` deduplicated into a new root `procutil` module
  (was a verbatim copy in webui and bot.shared); the Seerr client's
  five pasted client-selection branches collapsed into `_client_for()`;
  the six drifting "can't be decrypted" messages collapsed into one
  shared constant.
- The twelve-entry `_foo = foo` alias block in `bot/shared.py` is gone;
  every caller uses the public names.
- Stale comments/copy fixed: webui route-table docstring, frozen
  `X-Plex-Version` (now the real app version), Back/Fix submenu docstrings.
- Dead code removed: unused `WEBHOOK_MAX_BYTES` const duplicate,
  `login_with_plex`'s discarded cookies return, `TvSeason.name` and the
  Specials naming logic, `PlexUser.id`/`email` (a gratuitous PII fetch
  nothing read), the never-assigned `"autofix"` user_data key.
- Consistency: Seerr/Radarr/Sonarr URLs get the same save-time validation
  as the public URL; all root modules log under the `usher.*` namespace
  so filtering catches the API clients; unparseable callback data is
  logged instead of silently dropped; hand-edited string ids in the
  autofix allowlist are coerced to int on load (they previously never
  matched).

### Added
- **Dismissible "New Plex Sign-In" warning on the Seerr tab.** When Seerr's
  `newPlexLogin` setting is enabled, any Plex account with access to the
  Plex server becomes a Seerr user by signing in through the bot's /link.
  The admin panel now checks the setting (async after page load; silent
  when Seerr is down, unconfigured, or too old to have the key) and shows a
  dismissible banner explaining the exposure. A dismissal persists until a
  check observes the setting OFF, so the warning only ever returns on an
  off -> on transition - never on restarts or upgrades.

### Changed
- One object, one name: all user-facing copy now says **ticket** ("Reported
  as ticket #N", "Ticket #N closed", "New ticket #N", comment/timeout DMs);
  "issue" remains only as the problem description ("what's the issue?").
- Back is not Cancel: ticket submenu buttons now read "⬅️ Back" (they
  return to the detail view); real cancels are consistently "🛑 Cancel".
- The /issue flow checks the full link state at ENTRY (missing, token-less,
  and decrypt-failed each get their correct message) instead of dying at
  submit with a misleading "lost conversation state" that discarded the
  typed description. The stale "/link <username>" copy is gone (/link
  takes no username).
- A failed ticket-detail load now says so, with no action buttons - it
  previously rendered a healthy-looking view with live Fix/Close buttons
  over data it never loaded. Detail views are also length-capped like the
  list (newline-safe truncation under Telegram's 4096 limit).
- Typing indicators on the three slow reads (ticket list, title search,
  submit) so the bot no longer looks frozen.

### Fixed
- **The webhook self-test can no longer exfiltrate the secret.** It built its target URL from the attacker-influenceable Host
  header and sent the real webhook secret in Authorization - blind SSRF
  plus secret leak. It now posts to `http://127.0.0.1:<port>/webhook/seerr`
  unconditionally; the receiver lives in the same process.
- **Password changes now kill every outstanding session.**
  Session cookies were stateless 7-day HMAC tokens, so a stolen cookie
  survived a password rotation. Cookies now embed a `password_version`
  that bumps on every change; old sessions die instantly, the changing
  admin's own session is re-issued in place, and pre-0.12.0 cookies count
  as version 0 (still valid until the first change - no forced logout on
  upgrade).
- **Unencrypted backups require explicit acknowledgement.**
  A blank-passphrase backup is a plain ZIP holding encryption.key, the DB
  it decrypts (all Plex tokens), and settings.json (admin hash, API keys,
  bot token). The form now spells that out, and the server refuses the
  download unless a passphrase is set or the "I understand the risk"
  checkbox is ticked.
- **Both unauthenticated tracking maps are bounded.** The
  login throttle no longer mints a bucket for every key it merely checks,
  drops empty buckets, and caps tracked keys at 4096 with expired-then-
  oldest eviction; the webhook's rejection-log sampler sweeps expired
  entries and caps at 1024 sources. Combined with socket-peer
  keying, a spray of forged headers can no longer grow either map at all.
- **A garbage 2xx from Seerr no longer masquerades as a retryable failure.** `execute()` only guarantees the status code; every Seerr
  response body is now parsed through a shape guard that raises
  `AmbiguousResponseError` on empty/HTML/misshapen bodies. Critically,
  `create_issue` - where the issue is already created by the time the body
  is read - now says "check Seerr before retrying to avoid a duplicate"
  instead of inviting a retry, and a missing `id` can no longer produce a
  `.../issues/None` URL plus a NOT NULL crash on the poller insert.
- **`/tickets` is honest about truncation.** `list_issues` now
  returns Seerr's full matching count from `pageInfo`, and the header reads
  "showing 25 of N; manage the rest in Seerr" instead of claiming "All open
  tickets (25)" while issues 26+ were invisible.
- **Sonarr Mark Failed pages history until the grab is found.**
  A churn-heavy episode could push the grabbed event past the newest 20
  records, silently skipping the blocklist and re-grabbing the exact
  release Mark Failed was meant to bury. Now pages up to 250 records deep.
- **The per-user Seerr client cache is race-free.** Concurrent
  cache misses for one token both authenticated and the loser's client
  leaked; get-or-create now runs under an async lock. Evicted/expired
  clients are retired and closed after a 60s grace period instead of
  immediately, so a request another coroutine has mid-flight on the old
  client isn't killed with a confusing transport error.
- **Raw titles can no longer kill a flow mid-edit.** The issue
  flow's season/type screens and the link-success message used
  `parse_mode="Markdown"` with unescaped Seerr titles / Plex display names;
  a title like `M*A*S*H` or `[REC]` made Telegram reject the send and wiped
  the flow - worst case the link-success message, where the link was already
  stored but the user saw nothing and retried. All three now use the
  HTML + `html.escape` pattern the rest of the bot already uses.
- **Destructive admin actions are double-tap-proof.** With
  `concurrent_updates(True)`, a second tap on Redownload / Mark Failed /
  Close / the auto-fix confirm ran in parallel: double delete+blocklist+
  search, duplicate pending rows and DMs, duplicate Seerr issues. Fix
  actions now edit to a working state (stripping the buttons) before any
  network call, the issue submit has a per-user in-flight guard, and direct
  close drops a racing second tap.
- **Fixes on the same title are serialized.** A new per-media
  in-flight set (`type:tmdb_id`) covers both the admin fix path and user
  auto-fix, so two triggers for the same movie/episode can't interleave
  delete/blocklist/search (where the second blocklist could mark the first
  fix's fresh grab as failed). The second trigger gets "another fix for
  this title is already running" instead.
- **Expired keyboards now say so.** Mid-flow keyboards outlive
  their 10-minute conversation by hours in chat history; a tap used to hang
  the Telegram spinner with no feedback. A terminal catch-all handler
  (registered last) answers any unclaimed callback with an "expired" toast
  and strips the dead keyboard.
- **Text-awaiting flows can no longer capture each other's input.** Tapping "add a comment" (resolve DM) or "Reply" / close-with-
  comment mid-/issue put two conversations in a text-awaiting state, and
  the issue flow silently swallowed the comment as its description. Those
  entry points now check a conversation registry and ask the user to finish
  or /cancel the active flow first (in a new message, so the button stays
  usable). The relink-resume issue executor got the same guard: a new
  /issue started mid-relink owns the draft, so the interrupted report is
  not auto-submitted over it.
- **One corrupt DB row can no longer kill the autofix poller.**
  `list_pending_autofixes` parsed every row's `expected_episode_ids` JSON in
  one comprehension, so a single garbage cell raised on every 60s tick and
  permanently stopped all completion/timeout DMs. Bad rows are now skipped
  and logged individually; the batch continues.
- **A corrupt `timeout_at` no longer creates an immortal zombie fix.** A parse failure on the timestamp - the only time bound a fix has -
  left the row pending and re-polled forever. It is now treated as timed
  out and exits the poll set through the normal marked path.
- **Backups now capture uncheckpointed WAL data; restores are atomic.** The store runs in WAL mode, but backup zipped the bare
  `mappings.sqlite`, silently missing recent commits (links, pending fixes)
  and risking a torn mid-checkpoint copy. Backup now snapshots via `VACUUM
  INTO`. Restore wrote over live files non-atomically; it now writes each
  file via temp+fsync+rename and removes stale `-wal`/`-shm` sidecars so
  SQLite can't replay the old database's log into the restored file.
- **First-boot key/secret writes are crash-safe.** A torn
  `encryption.key` write crash-looped the container on every boot
  (`Fernet(key)` raises SystemExit) until the file was deleted by hand.
  New `fsutil.atomic_write_bytes/text` (temp -> fsync -> rename -> parent
  fsync, 0600 before rename) now backs `encryption.key`, the session
  secret, the Plex client id, and the setup token.

### Added
- **Schema version stamp + full-table migrations.** Migrations
  previously reconciled only `user_mapping`; `pending_autofixes` and
  `autofix_events` relied on `CREATE TABLE IF NOT EXISTS`, which never
  updates an existing old-shape table - any column drift meant a permanent
  "no such column" poller kill. `_migrate_schema` now reconciles all three
  tables against their expected column sets and stamps `PRAGMA
  user_version` (schema version 1) so future migrations can branch on it.
- **Arr lookups can no longer select the wrong media for deletion.** `get_movie_by_tmdb` / `get_series_by_tvdb` took `items[0]` of the
  filtered lookup without verifying the ID matched. Radarr/Sonarr silently
  ignore unknown query params (the v0.11.25 `/history` bug proved it), so an
  ignored filter would have fed an arbitrary title into the
  blocklist+delete+re-search workflow. Both lookups now scan the response
  for the requested tmdbId/tvdbId and return None on no match - fail-safe in
  front of an irreversible delete.
- **Login throttle can no longer be bypassed with X-Forwarded-For.** `client_ip()` trusted the client-forgeable XFF header, so
  rotating it on each `/admin/login` attempt landed in a fresh throttle
  bucket forever. The socket peer is now the key unless it belongs to a
  configured trusted proxy (`TRUSTED_PROXIES` env, comma-separated CIDRs),
  and then the XFF chain is walked right-to-left past trusted hops so a
  client-prepended spoof can't win. `X-Forwarded-Proto` (Secure-cookie
  detection) gets the same trust gate, and the webhook's unauthenticated-
  request log sampling now keys on the same hardened value.

### Added
- **Plaintext-admin startup warning.** When the HTTP server
  binds a non-loopback address with no trusted proxy configured, startup
  logs a warning that the admin password and cookies are LAN-visible. New
  README section "Securing the admin UI" documents the reverse-proxy and
  loopback+SSH-tunnel setups; `TRUSTED_PROXIES` added to the compose example
  and Unraid template (advanced).

## [0.11.27] - 2026-07-12

### Added
- **Interrupted actions resume automatically after a re-link.** When a
  revoked Plex token gates an action mid-flow (submitting an issue, closing
  a ticket, adding a comment, replying to a ticket), the bot now stashes
  what the user was doing and picks it back up as soon as the sign-in
  completes: "▶️ Picking up where you left off..." followed by the original
  action running with the fresh token. Typed comment text is preserved, and
  an interrupted issue submit keeps its whole draft (media, type,
  description) - nothing gets re-asked. Markers expire after 30 minutes
  (matching the link flow's own window), are consumed before executing so a
  failure can't double-fire, never contain tokens, and a voluntary /unlink
  abandons them - only the recovery-button path resumes.

## [0.11.26] - 2026-07-12

### Fixed
- **Revoked Plex tokens now get a guided re-link instead of a false "Seerr
  isn't responding".** Seerr reports a dead Plex token on `/auth/plex` as a
  500 "Unable to authenticate." - which the retry classifier read as
  transient, so every user-token action (submit issue, close, comment,
  /tickets) answered "try again in a minute" for an error no retry can fix.
  `_as_user` now converts that (and 401/403) into `PlexTokenInvalidError`,
  and every surface catches it with a one-tap recovery: "Unlink & sign in
  again" clears the dead session and drops the user straight into the
  platform-choice step of the existing /link flow (consent is implied by
  the tap). Genuine Seerr outages keep the transient classification.

### Changed
- **Resolve-flow and timeout copy no longer refers to "the admin" in the
  third person when the admin is the one in the flow.** The comment prompt,
  comment acknowledgement, and auto-fix-timeout DM now use admin-appropriate
  wording ("I'll add it to the issue" / "Want to add a note to the issue?")
  when the recipient is the admin.

## [0.11.25] - 2026-07-12

### Fixed
- **Mark Failed now blocklists the right movie's grab.** The Radarr client
  filtered history with `movieId=` on the paginated `/history` endpoint - a
  param Radarr silently ignores (its filter is `movieIds`, plural) - so it
  actually fetched the newest 20 events across the whole library and marked
  the most recent grab it found there as failed, whichever movie it belonged
  to. Meanwhile the target movie's release was never blocklisted, so the
  triggered re-search re-grabbed the exact same file. Now uses the per-movie
  `/history/movie?movieId=` endpoint, which also finds grabs older than the
  newest 20 events. Sonarr was unaffected (`episodeId` is a real filter
  there).

## [0.11.24] - 2026-06-17

Three issue-notification improvements.

### Fixed
- **TV notifications now show the affected season/episode (or "All seasons").** The new-issue, comment, and resolved DMs read `issue.problemSeason`/`problemEpisode` - but those are *REST API* field names; the Seerr *webhook* delivers the affected season/episode in the top-level `extra` array (`Affected Season` / `Affected Episode`). No handler read `extra`, so the scope silently dropped and a TV issue looked identical whether it was one episode or the whole series. A new `extract_affected_se` parses the `extra` array (falling back to `problemSeason`/`problemEpisode` for custom webhook templates), and `format_scope_label` renders a human-readable line under the title: `Season 1, Episode 5` / `Season 1` / `All seasons` (Seerr's season 0 / absent = whole series). Movies show no scope line. The raw `extra` shape is logged once per process so the field names can be verified against a live payload.

### Added
- **📜 History button on new-issue and comment notifications.** A comment DM showed a single comment in isolation; if a reply arrived later, buried in chat history, there was no way to see the conversation. Both the new-issue and comment messages now carry a History button that opens the full ticket detail (with the reply thread added in v0.11.23). The comment DM shows History always and Reply only while the ticket is open.

### Changed
- **Clearer auto-fix description.** The `(Auto-fix triggered by reporter.)` line appended to an auto-fix issue now explains what auto-fix does: that the current file was removed and a fresh download started automatically, and that it can take a while to finish and reappear in Plex.

## [0.11.23] - 2026-06-16

The ticket detail view showed only the original submission - no replies.

### Fixed
- **Tapping a ticket now shows the full reply thread.** `get_issue` parsed the issue's `comments` array but kept only `comments[0]` (the original report, used as the description) and discarded every later comment, so the detail view had no thread to render. `get_issue` now splits the array: `comments[0]` stays the description, and the rest is parsed into a new `IssueListItem.comments` list (author, message, timestamp). The detail view renders these under a "Replies:" section, oldest-first, each line showing the commenter and relative age. The thread is capped at the 20 most recent comments (with a "last 20 of N" note) so a long conversation can't push the message past Telegram's ~4096-char limit. No new Seerr request - same `get_issue` call, previously-discarded data.

## [0.11.22] - 2026-06-16

Two issue-conversation bugs: a reporter's follow-up comment never reached the admin, and tapping Reply destroyed the issue announcement it was attached to.

### Fixed
- **A reporter's follow-up comment now notifies the admin.** `handle_seerr_comment` only ever DMed the reporter and bailed out entirely when the commenter *was* the reporter (`commenter == reporter -> skip`), so when a user replied to the admin's reply on their own ticket, nobody was notified - the admin never saw the follow-up. The handler now routes to the *other* party in the conversation: the reporter is notified when someone else comments (unchanged), and the admin is notified when the reporter (or a third party) comments. Whoever wrote the comment is never notified about their own comment, and when the admin is also the reporter they're DMed once, not twice. The Reply button still appears only while the ticket is OPEN.
- **Tapping Reply no longer erases the issue announcement.** `tk_reply_start` and `tk_close_with_comment_start` called `edit_message_text`, overwriting the entire message (issue title, type, reporter, description) with the "Send the reply text..." prompt. They now strip only the inline buttons (`edit_message_reply_markup(None)`, the same call the button gate uses) and send the prompt as a separate message, so the announcement stays on screen for context while you type.

## [0.11.21] - 2026-06-13

HTTP idempotency and retry-correctness cluster. The retry layer treated every request as safe to retry, so a flaky network could silently duplicate side effects.

### Fixed
- **Non-idempotent requests (POST/PATCH) are no longer blindly retried.** `execute()` wrapped every method in `with_retry`, so a `create_issue` / `add_issue_comment` / `resolve_issue` POST that hit a timeout or 5xx after the server had already processed it got retried - posting a duplicate ticket or comment. Retry safety is now derived from the HTTP method (`IDEMPOTENT_METHODS`): GET/HEAD/PUT/DELETE retry on any transient failure as before; POST/PATCH retry **only** when the request provably never reached the server (a pre-send connect error) or was rejected before processing (429). A post-send timeout or a 5xx on a non-idempotent call now raises instead of risking a duplicate. Callers can override with `execute(..., idempotent=True/False)`.
- **`with_retry` now catches `httpx.ReadError` / `WriteError`.** Connection resets mid-response previously slipped past the retry/wrapping layer and surfaced as a raw httpx error with no friendly message; they're now handled like other transport failures (retried for idempotent calls, surfaced cleanly for non-idempotent ones).
- **A 3xx response is treated as an error, not success.** The clients don't follow redirects, so `classify_response`'s `< 400` check passed a 3xx through as if it carried a usable body; an unexpected redirect (e.g. an http->https bounce from a misconfigured base URL) now raises a permanent error naming the status.
- **500 is retryable; 501 is not.** `RETRYABLE_STATUSES` omitted 500 despite the comment claiming the standard 5xx transients. 500/502/503/504 (and 408/429) now retry for idempotent calls; 501 Not Implemented stays permanent.

## [0.11.20] - 2026-06-13

Settings-durability cluster: an unsafe shutdown of the host could silently wipe the admin config.

### Fixed
- **`settings.json` writes are now crash-safe.** `_write` previously wrote a temp file and atomically renamed it, but never `fsync`'d the data before the rename or the parent directory after. On an unsafe shutdown (the host losing power mid-write) the rename could reach disk before the contents, leaving a truncated `settings.json`. The temp file's contents are now flushed and fsync'd before the atomic `os.replace`, and the parent directory is fsync'd after (best-effort) so the rename itself is durable.
- **A corrupt `settings.json` is preserved, not silently overwritten.** `_load_or_seed` caught any parse error and immediately reseeded from env defaults, destroying the admin password hash, webhook secret, and autofix allowlist with no backup - so a truncated file from the bug above turned into a full config wipe and admin lockout on the next boot. The unreadable file is now moved aside to a numbered `settings.json.corrupt.N` sidecar (numbered so a bad file reappearing boot-after-boot never clobbers an earlier rescue copy) and logged at ERROR before reseeding. A genuinely missing file is still treated as a normal fresh install and seeds silently.

## [0.11.19] - 2026-06-12

Post-autofix resolve chain fixes: it was broken end to end.

### Fixed
- **The poller's "Did this resolve the problem?" buttons now work.** The auto-fix completion and timeout DMs were the one remaining keyboard sender that never called `record_btn` (the v0.11.18 bug class), so the button gate rejected every tap with the stale-menu toast for exactly the users who get autofix DMs - the resolve follow-up flow was unreachable in normal operation. Both sends are now recorded for the receiving user.
- **Autofix DMs no longer vanish on titles with Markdown metacharacters.** The poller built its DMs with legacy `parse_mode="Markdown"` and raw media titles; a title with an unbalanced `*`/`_`/`[` (e.g. M\*A\*S\*H) made Telegram reject the whole message, the error was swallowed, and the fix was marked complete anyway - the user was never notified. The poller now uses HTML with `html.escape`, matching the webhook handlers.
- **Status is marked before notifying.** Previously a failed `mark_autofix_status` write after a successful completion/timeout DM left the row pending, re-sending the same DM every 60s tick until the write succeeded. The poller now writes the status first and prefers losing one notification over spamming (same policy as the webhook dedupe).
- **Unlinked users can no longer act with the admin API key.** `resolve_start`/`resolve_comment` (the Yes/No buttons on autofix DMs) and `tk_back` fell through to `as_plex_token=None` - the bare admin-key client - when the tapping user had no usable Plex token, closing/commenting/fetching with admin attribution. All three now apply the same gate as the rest of tickets.py: decrypt-failed users are told to re-link, unlinked users to /link first. The identity is re-checked at comment submit time too, in case the link vanished mid-conversation.
- **The resolve conversation now times out (10 min) like every other flow.** It was the only ConversationHandler without `conversation_timeout`, so an abandoned "add a comment" prompt swallowed the user's next plain-text DM - days later - as a comment on the stale issue. Timeout handler clears `awaiting_comment_for`; the key is also covered by `on_error`'s half-state cleanup now.
- **Admin Fix DMs no longer render a dangling "Original issue:" label.** The admin /tickets fix path enqueued pending autofixes with `issue_url=""` (the user /issue path sets the real URL); it now builds the URL from Seerr's public URL, and the poller omits the line entirely for legacy empty-URL rows. The autofix analytics event is also logged for ok/partial results whose search step failed, matching the user path.

## [0.11.18] - 2026-06-11

### Fixed
- **/issue and /link menus no longer die with the "Use the most recent message" toast.** The global button gate (v0.11.6) only admits callbacks from a user's last 3 *recorded* button messages, but only ticket and webhook messages were ever recorded - the `/issue` and `/link` flows never registered their inline keyboards. The gate is fail-open while a user's history is empty, so this stayed hidden until a user received their first webhook ticket DM; from then on every tap on a flow menu (even a freshly sent one) was rejected as stale and the flow stalled. Every button-bearing send/edit in `bot/issue_flow.py` and `bot/link_flow.py` now calls `record_btn`.
- `record_btn` now dedupes: re-recording an already-tracked message (a flow step editing its menu in place) refreshes its timestamp and moves it to the newest history slot instead of appending a duplicate, so a multi-step flow can't evict itself out of its own 3-slot history. It also ignores non-Message returns (the bare `True` an inline-message edit yields) instead of crashing.

## [0.11.17] - 2026-06-08

### Fixed
- **A new command now abandons any in-progress flow ("most recent command wins").** Previously a half-finished `/issue` left its conversation armed in the title/description text state; if the user then started a ticket reply and typed it, the parked `/issue` (registered first) intercepted the text and ran a Seerr search on it, so the user saw `No matches for "<their reply>"` and the reply never reached the ticket. New group=-2 `reset_stale_flows` gate ends every in-progress conversation (issue / ticket / link / resolve) the moment a top-level command arrives, before any following text is processed, so nothing the user types gets swallowed. `/cancel` is exempt so each conversation's own cancel fallback still fires. Mirrors the existing button gate's "newest wins" behavior.

## [0.11.16] - 2026-06-05

### Changed
- **Admin UI copy + layout polish** (no behavior changes):
  - Auto-fix tab: "Allow all linked users" → **"Allow all users"**; allowlist note reworded to `… "Allow all" lets every signed-in user auto-fix.`; daily-limit note reworded to `… "Unlimited" removes the cap for all users.`; tab heading "Auto-fix (Radarr / Sonarr)" → **"Auto-fix"**.
  - A divider now separates each tab's **Test / Save** action row from the fields above it (Telegram, Seerr, Auto-fix, Webhook). On the Webhook tab this also separates Test/Save from the Show / Generate / Copy secret controls.
  - Account tab: dropped "(PBKDF2 + AES-GCM)" from the backup note; restore note shortened to "Overwrites settings, mappings DB, and encryption key after validating. A backup is created before restoring." (the pre-restore snapshot + container restart behavior is unchanged).

## [0.11.15] - 2026-06-05

### Added
- **Auto-fix tab: "Allow all linked users" toggle.** A checkbox above the allowlist input. When checked, every linked user may auto-fix (the admin is always allowed); when unchecked, the existing allowlist applies. New setting `autofix_allow_all` (default `false`); enforcement in `bot/issue_flow.py` is now `allow_all OR tg_id in allowlist`.
- **Auto-fix tab: "Unlimited" toggle for the per-user daily limit.** A checkbox above the limit input. When checked, the per-user daily cap is not enforced and the offer message drops the "remaining today" line. New setting `daily_autofix_unlimited` (default `false`).
- Both toggles render their paired input **readonly + dimmed** while checked (not `disabled`), so the underlying allowlist / number still posts and is **retained** in `settings.json`; un-checking later restores exactly what was entered. Live toggle via a small `bindLock` helper in the page script.

## [0.11.14] - 2026-06-03

### Fixed
- **A test fixture with a hardcoded timeout date.** Once that date passed, the test failed in CI and blocked the v0.11.13 image from publishing, so Docker Hub stayed at v0.11.12. The fixtures now use timestamps relative to the current time, and the overlap test waits on an explicit event instead of a sleep. No source-code changes; this release carries all of v0.11.13's features to Docker Hub.

## [0.11.13] - 2026-06-03

### Added
- **Connection-test buttons on the Telegram, Seerr, Auto-fix, and Webhook tabs.** Each sits to the left of Save. Clicking POSTs the form's values to a new JSON endpoint (`/admin/test/<which>`), then overlays the button: green **PASS ✓** or red **FAIL ✗**, with a one-line detail (bot username / service version / error) beside it. The overlay fades out after 5 seconds and the button returns to neutral.
  - Telegram: validates the typed token against `getMe`.
  - Seerr: pings with the typed URL + API key.
  - Auto-fix: pings whichever of Radarr/Sonarr have a URL filled; PASS only if every configured client succeeds (per-service detail).
  - Webhook: self-POSTs a synthetic `TEST_NOTIFICATION` to the live webhook URL using the **saved** secret, confirming the receiver is reachable and the secret round-trips (so the flow is Generate → Save → Test).
- **Webhook Secret Show / Generate / Copy controls.** The secret is mandatory and auto-generated (it's the value you copy into Seerr's `Authorization Header`), but it was rendered as an unreadable masked field. **Show** toggles reveal/mask, **Generate** rolls a fresh `token_urlsafe`-shaped value and reveals it, **Copy** puts it on the clipboard (with an `execCommand` fallback).

### Fixed
- **`autocomplete="off"` on all config-secret fields** (Telegram token, Seerr/Radarr/Sonarr API keys, Webhook secret). Stops the browser password manager from silently overwriting these `type="password"` fields with the saved admin login password, which could have clobbered the working secret on the next Save.

## [0.11.12] - 2026-05-30

### Notes
- Internal test tooling only; no source-code logic changes.

## [0.11.11] - 2026-05-30

### Security
- **PBKDF2 auto-upgrade on successful login**. After a successful `verify_password`, the stored hash's iteration count is parsed from the `pbkdf2_sha256$<iters>$<salt>$<hash>` format and compared against the current `PBKDF2_ITERATIONS`. If lower, the password is rehashed and persisted, and an `event=password_rehashed user=... ip=... from_iters=N to_iters=M` audit line is written. The rehash is wrapped in its own try/except so a write failure doesn't roll back the (already-successful) login.

### Removed
- **`_run_autofix` and `_run_mark_failed` back-compat shims** in `bot/tickets.py`. Confirmed orphan since v0.11.7 migrated `bot/issue_flow.py` to call `_run_arr_action` directly. Module docstring updated to drop them from the public-entry-points list.
- **Unused-imports sweep across `bot/*` and root modules.** `bot/app.py` (`typing.Optional`), `bot/shared.py` (`html`), `bot/link_flow.py` (`_record_btn`), `bot/resolve_flow.py` (`typing.Optional`, `InlineKeyboardButton`, `InlineKeyboardMarkup`, `_token_for`), `bot/tickets.py` (`AUTOFIX_ELIGIBLE_TYPES`), `webui.py` (`CSRF_COOKIE`). Pyflakes-clean now.

## [0.11.10] - 2026-05-30

### Fixed
- **Autofix poller no longer double-notifies on overlapping ticks**. New module-level `_inflight: set[int]` in `bot/autofix_poll.py` tracks fix IDs currently being processed; the next tick skips any ID still in flight. Fixes the "Sonarr is slow → tick stretches past 60s → next tick fires `_notify_complete` again before `mark_autofix_status('complete')` lands" failure mode.
- **`/issue` search results carry a version tag**. Every fresh `_show_search_results` bumps `ctx.user_data["search_version"]` and embeds it in each result's `callback_data` (now `media:<version>:<media_type>:<tmdb_id>`). `issue_pick_media` verifies the version matches before resolving, so rapid `/issue → A → /issue → B` reentry no longer causes the in-flight pick to pull the wrong item from the freshly-replaced `search_results` dict. The mismatching tap gets a clean "Search context changed (you started a new /issue search since this keyboard appeared). /issue to pick again." message.

### Changed
- **`SeerrClient._as_user` caches authenticated user clients per Plex token**. LRU + TTL (`_USER_CLIENT_MAX=32`, `_USER_CLIENT_TTL_S=300`). Under a webhook comment flood, repeated `add_issue_comment` / `resolve_issue` / `list_issues` / `create_issue` / `get_issue` calls as the same Plex user no longer pay the TCP-handshake + `/auth/plex` cost on every invocation. The cache owns each client's lifecycle; the per-call `try: ... finally: await client.aclose()` blocks in five caller sites have been removed. `SeerrClient.close()` drains the cache (closes every cached client) on shutdown.

### Notes
- Callback_data format change: old `media:movie:42` is now `media:1:movie:42`. Any keyboard rendered by a pre-0.11.10 process whose pick lands after the upgrade will fall through to "Couldn't parse selection. /issue to start over." That's the correct behavior, since those keyboards' search_results dicts don't survive the restart either.

## [0.11.9] - 2026-05-30

### Changed
- **`_apply_fix` decomposed** into `_resolve_fix_context` + `_enqueue_fix_completion` + a thin policy core. New `_FixContext` dataclass carries the resolved issue + media + label between the helpers.
- **`format_media_label(title, year, *, season, episode)` shared helper** replaces four sites that each rendered this independently: `cmd_tickets`, `_build_ticket_detail`, `_apply_fix`, `_submit_issue`. The display format is now uniform: `Title (Year) - S01E08`. (`/tickets` list previously rendered `Title (Year) S01E08` without the dash, a minor cosmetic shift.)
- **`PendingAutofix.is_complete(radarr, sonarr) -> tuple[bool, str]` method**. The poller's inline movie-vs-episode-vs-season branching moves into the data type. Returns `(done, extra)` where `extra` is the `(present/total episodes)` suffix used by `_notify_complete`.
- **Non-admin `/start` cleaned up.** Greeting + commands only, no connection diagnostics, no inline `/link` directive. Admin path keeps diagnostics.
- **Startup admin DM now includes the version string** (`"👋 Bot is online (v0.11.9)."`).
- **`_post_init` startup-DM failures classified**: distinct WARN per cause: "bot was blocked", "chat not found" (with the "must be a numeric ID from @userinfobot" hint), "user is deactivated", instead of a catch-all "likely never started a conversation."

### Fixed
- **`cmd_link_didnt_work` clears `link_active_loop` explicitly on timeout and success** so the conversation frees up immediately instead of waiting for the 30-min `_link_conversation` timeout.
- **`format_age` logs WARN once per unparseable timestamp prefix** instead of silently returning `"?"`. New module-level `_FORMAT_AGE_WARNED` set; first hit for each 20-char prefix surfaces, repeats are quiet.
- **Webhook 401 logging sampled per IP**. First rejection from each IP logs at WARN; further rejections from the same IP within 5 minutes drop to DEBUG. Botnet probing no longer floods the operational log.
- **Admin-only callback gate factored into `_require_admin(q, ctx, *, action_label)`**. `tk_close_menu`, `tk_close_direct`, `tk_close_with_comment_start`, `tk_fix`, `_apply_fix` migrated. Non-admin taps now get an "Admin only." toast and an `admin_callback_blocked` audit log entry (was: silent no-op).
- **`on_error` clears known conversation `user_data` keys**. New `_CONVERSATION_USER_DATA_KEYS` tuple covers `tk_reply_id`, `tk_close_after`, `link_active_loop`, `media`, `search_results`, `seasons`, `season`, `episode`, `issue_type`, `description`, `autofix`, `research_parent`. A mid-conversation crash no longer leaks half-state into the next conversation.

## [0.11.8] - 2026-05-30

### Fixed
- **`load_or_create_session_secret` no longer strips bytes from the loaded secret.** The previous `p.read_bytes().strip()` call removed ASCII whitespace bytes (`\n`, `\t`, ` `, etc.) from both ends of the 32-byte random secret. Since `secrets.token_bytes(32)` returns uniformly random bytes, ~7% of secrets had a whitespace byte at one end; on those installs the runtime session key was 30 or 31 bytes (silently truncated) and varied between the first and second load. CI on v0.11.7 caught the case where a freshly-generated secret happened to start with `\n` and the round-trip mismatch failed `test_session_secret_persists`.
### Notes
- **Back-compat hazard for the ~7% of installs whose existing session secret has a leading or trailing whitespace byte on disk.** The pre-v0.11.8 binary read that secret minus those bytes; the v0.11.8 binary will read the full 32 bytes. The HMAC key changes → previously-issued admin session cookies become invalid → admin sees one auto-logout on the upgrade. Re-login is the only step.

## [0.11.7] - 2026-05-29

### Fixed
- **`_submit_issue` was calling `_run_autofix` with a stale tuple-unpacking shape.** Carried over from before v0.11.3 made the orchestrators return `FixResult`; would have `ValueError`'d on first user-side auto-fix-during-/issue. Rewritten to consume the structured result + use `result.should_poll` to decide whether to enqueue the completion poller.

### Added
- **`const.py`**: named timeouts and limits used across multiple modules: Plex poll cadence + window + escalation threshold + backoff cap, autofix poll interval / first-delay / 6h timeout, keyboard layout caps (3/row), search result count, HTTP upload caps (32MB admin restore vs 128KB webhook), and the three ConversationHandler timeouts. Single source of truth for tuning.
- **`bot/callback_prefixes.py`**: named constants for every `callback_data` prefix (`TK_OPEN`, `TK_REPLY`, `TK_FIX_REDOWNLOAD`, `TK_FIX_MARK_FAILED`, `TK_CLOSE`, `TK_CLOSE_WITH_COMMENT`, `TK_CLOSE_DIRECT`, `TK_BACK`, `LINK_CONSENT`, `LINK_PLATFORM`, `LINK_HELP`, `ISSUE_MEDIA`, `ISSUE_SEASON`, `ISSUE_EPISODE`, `ISSUE_TYPE`, `ISSUE_AUTOFIX_OFFER`, `ISSUE_AUTOFIX_CONFIRM`, `ISSUE_CANCEL`, `ISSUE_RESEARCH_PARENT`, `RESOLVE`). All inline keyboards and `CallbackQueryHandler` patterns updated to use them.

### Changed
- **`_run_autofix` and `_run_mark_failed` collapse into `_run_arr_action`** (`bot/tickets.py`) keyed by `action: Literal["fix", "mark_failed"]`. The two outer names remain as one-line back-compat shims; `_apply_fix` and `_submit_issue` are migrated to call `_run_arr_action` directly.
- **`bot/app.py`, `bot/link_flow.py`, `bot/issue_flow.py`, `bot/autofix_poll.py`, `bot/tickets.py`, `bot/resolve_flow.py`, `bot/webhook_handlers.py`, `bot/shared.py`** all import named constants instead of carrying magic strings + magic numbers (Plex poll iters, autofix poll interval, conversation timeouts, keyboard buttons-per-row, search result count, upload caps).
- **Module-level docstrings on `bot/*.py`** refined for the v0.11.5 split's responsibility lines.

### Removed
- **`tk_reply_menu` callback handler + the `tkrmenu:` pattern registration.** Confirmed orphan (no keyboard emits `tkrmenu:` since v0.10.4 made admin Reply go direct).

### Updated
- `Dockerfile`: `const.py` added to the top-level `COPY` list.

### Notes
- Mostly internal tidying, plus one user path that had been quietly broken since v0.11.3 put right.

## [0.11.6] - 2026-05-28

### Fixed
- **Allowlist read-then-await race**. `bot/issue_flow.py issue_description` now snapshots `bot_data["allowlist"]` into a `frozenset` at handler entry, so a mid-handler settings reload can't shift the eligibility check to a stale set during the subsequent `count_autofix_24h` await.
- **`btn_msgs` gate is now race-tolerant**. The gate tracks the last 3 button-bearing messages per user (was: only the latest); rapid-fire webhook DMs no longer make each-other's buttons look stale. Read of the per-user list happens once at gate entry, so there is no read-then-await window. Eviction is FIFO at `BTN_HISTORY_MAX=3`.
- **Autofix poller re-fetches arr clients per iteration**. `bot/autofix_poll.py poll_pending_autofixes` reads `radarr`/`sonarr` from `bot_data` inside the per-fix loop, so a settings reload during a 10-fix tick picks up the new clients on the very next iteration. The earlier "capture-once-per-tick" pattern could close-then-touch a captured client mid-loop.
- **`_close_old` tasks are tracked and drained on shutdown**. `bot/app.py _build_clients_from_settings` parks a strong reference to each settings-reload close task in `bot_data["_pending_closes"]`; `_post_shutdown` awaits them via `asyncio.gather` before closing current clients. Eliminates "Task was destroyed while it is pending!" warnings on hot reload + shutdown.
- **`_post_shutdown` now closes Seerr/Radarr/Sonarr/Plex clients explicitly.** Previous shutdown leaked httpx connection pools.
- **`tk_reply_text` post-await mismatch check**. After `seerr.add_issue_comment` returns, the handler verifies `ctx.user_data["tk_reply_id"]` still matches the `issue_id` bound at entry. If the user kicked off a new reply flow during the await, the comment still landed on the right ticket (the binding is local) but the close-after side effect is suppressed, with an honest "reply posted; you've started a new one since then" message.

### Added
- **`conversation_timeout` on all three ConversationHandlers.** `_ticket_conversation` and `_issue_conversation` get 600s (10 min); `_link_conversation` gets 1800s (30 min, covering the 28-min strong-PIN window). Each defines a `ConversationHandler.TIMEOUT` state handler that clears the relevant `user_data` keys (`tk_reply_id`/`tk_close_after`, `link_active_loop`, or the issue flow's `media`/`search_results`/`seasons`/`season`/`episode`/`issue_type`/`description`/`autofix`). Abandoned conversations no longer leak state for the life of the process.

### Notes
- Also picked up several stale-imports + missing-imports inside the split modules that pyflakes flagged (`secrets` in `link_flow`, `asyncio` in `tickets`, `UserStore` in `resolve_flow`, `_require_seerr` in `link_flow` and `issue_flow`, `CreatedIssue` in `issue_flow`, and a duplicate local `_ticket_detail_kb` that shadowed the shared one). None affected the import chain (function bodies aren't evaluated at module load) but they would have NameError'd the first time those handlers ran. All fixed.

## [0.11.5] - 2026-05-28

### Changed
- **`bot.py` split into a `bot/` package** along domain lines. ~2560 lines now spread across nine modules: `bot/app.py` (wiring, `main()`, startup/shutdown, `_check_connections`, `cmd_start`/`cmd_status`, error handler, setup-only path), `bot/shared.py` (constants, conversation states, cross-module helpers, `format_media_title_line`), `bot/webhook_handlers.py`, `bot/tickets.py`, `bot/issue_flow.py`, `bot/link_flow.py`, `bot/autofix_poll.py`, `bot/resolve_flow.py`, plus `bot/__init__.py` and `bot/__main__.py`. Entry point is now `python -m bot`. No behavior changes; pure refactor.
- **Per-client three-step workflow helpers.** `sonarr._run_episode_workflow(*, series, match, blocklist: bool)` and `radarr._run_movie_workflow(*, movie, blocklist: bool)` collapse the near-identical orchestration that previously lived inside `auto_fix*` / `mark_failed*`. Both pairs of public methods drop from ~60 lines each to a ~5-line prelude + delegation. Same `FixResult` semantics as v0.11.3.
- **`_check_connections` uses `ping()` instead of reaching into `_client`.** Adds `ping()` to `SeerrClient`, `RadarrClient`, `SonarrClient` (each calls the upstream version endpoint via the standard `execute()` wrapper, so retries + classified errors come along for free).

### Added
- **`format_media_title_line(seerr, media, *, problem_season=None, problem_episode=None)`** in `bot/shared.py`. Builds `"🎬 Movie Title (Year)"` or `"📺 Show Title (Year) - S01E02"` from a Seerr webhook payload's media block. Used by all three `handle_seerr_*` handlers; deletes ~40 lines of drifting title-construction code.
- **`format_se_suffix(problem_season, problem_episode)`** helper used by the new title formatter and the `reported` flow's season/episode suffix.

### Removed
- `SeerrClient.find_user` + the `SeerrUser` dataclass: dead code, never called from the bot.
- `bot.py`: replaced by the `bot/` package. `python bot.py` → `python -m bot`.

### Updated
- `Dockerfile`: `COPY bot/ ./bot/` plus the unchanged top-level modules; `CMD ["python", "-m", "bot"]`.

### Notes
- `bot/shared.py` exposes both the new clean names (`format_age`, `token_for`, `record_btn`, etc.) and underscore-prefixed aliases (`_format_age`, `_token_for`, `_record_btn`, etc.) so the extracted handler modules keep their original symbol references without renaming. New call sites should prefer the unprefixed names.
- Large refactor with no intended behavior change.

## [0.11.4] - 2026-05-28

### Security
- **CSRF tokens on every admin POST.** Double-submit cookie pattern: a `usher_csrf` cookie (SameSite=Strict) is set on every form-rendering page; every POST validates the cookie matches a hidden `csrf_token` form field via `hmac.compare_digest`. Pre-auth flows (`/admin/setup`, `/admin/login`) and authenticated flows (`/admin/*`) both gated. Validation lives in `auth_util.validate_csrf`.
- **Login throttling.** 5 failed login attempts per IP within a 5-minute sliding window returns HTTP 429 with `Retry-After`; success resets the counter. `auth_util.LoginThrottle` is process-local in-memory.
- **First-run setup token.** On a fresh install (no admin account, no `/data/setup_token` file), `auth_util.load_or_create_setup_token` generates a random token, persists it to `/data/setup_token` (chmod 600), and logs it at WARNING level. `/admin/setup` rejects submissions without a matching token, so the first visitor on a LAN-exposed port can't claim the admin account silently. Token is cleared on successful setup. Existing installs (admin already configured) skip the token check entirely.
- **Audit log.** Dedicated `usher.audit` logger records `login_success`, `login_fail`, `login_throttled`, `login_csrf_fail`, `setup_csrf_fail`, `setup_token_fail`, `setup_complete`, `password_changed`, `backup_download`, `restore_complete`, `admin_csrf_fail`, `logout` with `user`, `ip`, and event-specific fields. Searchable with `grep usher.audit`.
- **`Secure` cookie flag when behind HTTPS.** Session and CSRF cookies set `Secure=True` when `request.scheme == "https"` or `X-Forwarded-Proto: https` is present. Plain-HTTP LAN installs keep working unchanged.
- **Backup ZIP can be wrapped with a passphrase.** New `backup_crypto.py`: PBKDF2(SHA-256, 600k iters) over the passphrase → 32-byte key → Fernet (AES-128-CBC + HMAC-SHA256). Output format: 11-byte magic `USHER-BAK1\n` + 16-byte salt + Fernet token. File extension changes to `.usher-backup`. Restore detects the format by magic prefix and prompts for the passphrase. The Account tab adds an optional "Passphrase" field to both backup and restore forms.
- **`restore_upload` validates uploads before overwriting.** Each member of the uploaded ZIP is sanity-checked: `settings.json` must parse and accept into the `Settings` dataclass; `encryption.key` must initialize a `Fernet(key)` without raising; `mappings.sqlite` is written to a tempfile and validated with `PRAGMA integrity_check` (must return `ok`). Current `settings.json`, `mappings.sqlite`, and `encryption.key` are copied to `/data/pre-restore-YYYYMMDD-HHMMSS/` before the new files land.

### Changed
- **Clean shutdown instead of `os._exit(0)`.** Settings-change-triggered restarts (`bot.py` setup-only mode + full-mode `_on_settings_changed`) and the post-restore exit in `webui.py` now send `SIGTERM` to self after a 2s delay via `_schedule_clean_exit`, letting PTB's `run_polling` and aiohttp's `runner.cleanup()` close httpx clients, DB connections, and the HTTP server gracefully. Falls back to `os._exit(0)` only if `SIGTERM` dispatch itself fails.
- **`/admin/backup` is now a POST.** Previous GET still worked but couldn't accept the optional passphrase. CSRF token required. Plain (no passphrase) downloads still serve a `.zip`.

### Added
- **`auth_util.py`**: `LoginThrottle`, CSRF helpers (`validate_csrf`, `attach_csrf_cookie`, `generate_csrf_token`, `csrf_for_request`), setup-token persistence (`load_or_create_setup_token`, `clear_setup_token`), `request_is_secure`, `client_ip`, `audit`.
- **`backup_crypto.py`**: `wrap`, `unwrap`, `is_wrapped`, format constants.

### Notes
- Existing installs upgrading from <0.11.4 keep working: the setup-token guard only fires when `admin.is_set()` is False, and existing CSRF cookies are minted on the next GET. No data migration required.

## [0.11.3] - 2026-05-28

### Added
- **`http_util.py`**: shared `APIError` hierarchy (`TransientAPIError`, `PermanentAPIError`, `NotFoundAPIError`), error-body parser (extracts Seerr/Arr `{"message": "..."}` so user-facing text carries the real reason), `execute()` wrapper that handles retry + classify in one call, and `user_friendly_message(exc)` that formats exceptions for Telegram without leaking URLs/headers/stack traces.
- **`fix_result.py`**: `FixResult` dataclass with `status` (`ok`/`partial`/`failed`), `message`, `steps_done`, and `poll_info`. `.should_poll` is True iff a fresh search was triggered, so partial successes still enqueue the autofix poller.
- **Transient-failure retry across all API clients.** Seerr / Sonarr / Radarr / Plex calls now retry 429 + 502/503/504/408, plus `httpx.ConnectError`, `TimeoutException`, and `RemoteProtocolError`, up to 3 times with capped exponential backoff (0.5s → 5s cap, jittered). 4xx (except retryable) raises immediately as `PermanentAPIError`; 404 surfaces as `NotFoundAPIError`.
- **Autofix poller distinguishes media-gone from transient.** `NotFoundAPIError` on the `has_file` poll marks the pending row `failed` and DMs the user that the media was removed instead of polling for the full 6h timeout. `TransientAPIError` keeps polling next tick.
- **Plex pin-poll escalation.** Per-poll backoff goes 3s → 6s → 12s on consecutive failures; after 5 in a row the user gets a one-time "⚠️ Plex's API isn't responding right now" DM so they don't think the bot is silently broken.
- **Telegram BadRequest fallback** via new `_edit_or_send(q, text, **kwargs)` helper. When `edit_message_text` fails because the admin edited or deleted the source message, a new `send_message` lands in the same chat instead of silently swallowing the response.

### Changed
- **Mark Failed / Auto Fix return `FixResult`.** `sonarr.mark_failed_episode`, `sonarr.auto_fix_episode`, `sonarr.auto_fix_season`, `radarr.mark_failed`, `radarr.auto_fix`, plus `_run_autofix` / `_run_mark_failed` all use the structured result. `_apply_fix` consumes `result.should_poll` and `result.status` to deliver honest partial-success messages while still enqueueing the poller when search succeeded.
- **All user-facing exception strings sanitized.** ~14 sites in `bot.py` (cmd_tickets, tk_*, _finalize_link, _submit_issue, _check_connections, cmd_link*, etc.) now route via `user_friendly_message()` so users see "Seerr isn't responding right now; try again in a minute." instead of `Client error '503 Service Unavailable' for url 'http://192.168.x.x...'`. Logs still get the full exception via `logger.exception`.

## [0.11.2] - 2026-05-28

### Added
- **pytest harness.** New `tests/` directory with coverage of `Settings` round-trips and `SettingsStore` upgrade paths (`test_settings.py`); `UserStore` lifecycle including decrypt-failed semantics across key rotation and concurrent-write smoke (`test_store.py`); webhook auth + dispatch + dedupe + size cap + handler-exception containment (`test_webhook.py`); pure helpers `_format_age` and `_derive_parent_name` (`test_helpers.py`).
- **Dev dependencies file** (`requirements-dev.txt`): pytest + pytest-asyncio. Not pulled into the Docker image.
- **`pytest.ini`** sets `asyncio_mode = auto` so async tests don't need a per-function decorator.
- **CI test workflow** (`.github/workflows/test.yml`) runs on every push to main and on PRs.
- **Release workflow now gates on tests.** The build job depends on a test job; tag pushes won't ship an image if tests fail.

### Notes
- No source-code logic changes; this is purely a CI safety net ahead of the heavier refactors in v0.11.5 (bot.py split) and v0.11.6 (concurrency hardening).
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
- `TokenCrypto.safe_decrypt`: its tri-state logic moved into `UserStore._decrypt_field` so `Mapping` construction can populate `plex_token_decrypt_failed` cleanly.

### Notes
- WAL mode persists across connections, so the one-time `PRAGMA journal_mode = WAL` at schema init is enough; existing databases will be migrated to WAL automatically the next time `_init_schema` runs.

## [0.11.0] - 2026-05-28

### Security
- **Webhook secret comparison is now constant-time.** `webhook.py` switched from `auth != secret` to `hmac.compare_digest(...)` to eliminate the timing side-channel that could let a LAN-resident attacker recover the secret byte by byte.
- **Webhook secret is now mandatory.** Previously empty was silently allowed, which made the receiver accept any POST. `SettingsStore` auto-generates a secret on first load if none is present (a warning is logged with a pointer to the `/admin` Webhook tab where the value can be read and pasted into Seerr's webhook config). The webhook handler refuses all POSTs when the secret is somehow unset (defense in depth). The webui rejects an empty secret on save.

### Added
- **Webhook body size cap (128KB).** The parent aiohttp app keeps its 32MB ceiling for admin backup restores; the webhook handler now enforces a 128KB ceiling via Content-Length check + read-time check, so unauthenticated clients can't force 32MB allocations per request.
- **Webhook deduplication.** A 60-second / 256-entry bounded SHA-256 body-hash cache drops duplicate deliveries (e.g., Seerr retries after a transient blip). The dedupe cache lives in the handler closure and evicts on TTL + size.
- **`usher_public_url` scheme validation.** The webui's Telegram tab now rejects URLs that don't start with `http://` or `https://`. Empty is still acceptable (means "not configured"). New `settings.validate_public_url(url)` helper.

### Fixed
- **Webhook handlers no longer return 500 on internal exceptions.** Previously a transient Telegram 429 or DB lock inside `handle_seerr_*` returned 500, Seerr retried on backoff, and once throttling cleared the admin got duplicate DMs for the same event. Now: log the exception, return 200. We'd rather lose one notification than spam the admin with five.

## [0.10.6] - 2026-05-28

### Fixed
- **"Mark Failed" now actually replaces the file.** Previous behavior only called Sonarr/Radarr's `/history/failed/{id}` endpoint, which blocklists the release but does NOT delete the on-disk file or trigger a new search unless the global "Redownload Failed" setting is on (it usually isn't). Symptom: instant "marked failed" confirmation, but the broken file stayed on disk and SABnzbd never saw a new job.

  `mark_failed_episode` (Sonarr) and `mark_failed` (Radarr) now perform a strict superset of `auto_fix` / `auto_fix_episode`:
  1. Blocklist the most recent grab via `/history/failed/{id}` (skipped cleanly if no grab history exists).
  2. Delete the on-disk file (if present).
  3. Trigger a fresh search.

  Success message updated to reflect the actual end-state. `poll_info` shape unchanged, so the existing post-fix polling in `_run_mark_failed` continues to work without modification.

### Notes
- Cause found on a TV episode: the file was a 1.1 GB zero-byte payload (EBML header all `0x00`) that Plex's `/transcode/universal/decision` endpoint rejected with HTTP 400. "Mark Failed" was the right user intent but didn't carry out the file replacement.

## [0.10.5] - 2026-05-28

### Added
- **Ticket detail now includes the original report text.** `IssueListItem` gains a `description` field; `SeerrClient.get_issue` populates it from the first entry in the issue's `comments` array (Seerr stores the original description there at creation time). The `/tickets` drilldown now appends:
  ```
  Description:
  "subtitles are out of sync"
  ```
  under the metadata block.

### Changed
- Close sub-menu button labels trimmed: `[💬 With comment]` → `[💬 Comment]`, `[✓ Without comment]` → `[✓ No comment]`. Cancel unchanged.

## [0.10.4] - 2026-05-28

### Fixed
- **Reply was silently no-op'ing on the second attempt** after a previous reply conversation was abandoned without sending text or `/cancel`. `_ticket_conversation` was missing `allow_reentry=True`, so the entry-point callback was a no-op while a stale conversation was still considered "active." Added the flag, so re-tapping Reply now re-enters cleanly.

### Added
- **Global "stale button" gate with 6-hour expiry.** A `TypeHandler` registered at group `-1` runs before any callback handler. It tracks the most recent button-bearing bot message per user (`bot_data["btn_msgs"][user_id]`) and dismisses callbacks from older messages or messages older than 6 hours with an explanatory toast + strips the buttons. Prevents stale buttons in chat history from misfiring. Six-hour TTL matches the auto-fix completion timeout.
- **`[⬅️ Cancel]` button on the Close and Fix sub-menus.** Tapping returns to the ticket detail view (edits the message back via the new `tk_back` handler) so admin can back out without committing to either action.
- Removed the `[✅ Close]` shortcut from the Fix sub-menu; Close belongs only under the dedicated Close path.

### Changed
- **Admin top-level `[💬 Reply]` now goes straight to the reply input** (no `[Reply] [Close]` sub-menu). Only `[Close]` and `[🔧 Fix]` keep their sub-menus (since each has multiple action variants). Affects both the `/tickets` detail and the new-issue admin DM. The `tk_reply_menu` handler is retained for backward compatibility with any old buttons still floating in chat history, but no new buttons route to it.
- **Ticket detail message now shows full context.** Tapping `[#N]` from `/tickets` previously opened a bare `Ticket #N: choose an action` line. Now `tk_open` fetches the issue via `SeerrClient.get_issue()` and renders:
  ```
  Ticket #12
  📺 Example Show (2026) - S01E08
  Issue: 🎥 Video
  Reported by: alice
  Age: 7h ago
  ```
  Same buttons attached. Refactored into a `_build_ticket_detail` helper shared with `tk_back`.

### Notes
- Button tracking applies to `/tickets`-flow messages (list, detail, sub-menus) and webhook DMs (new-issue admin, comment-reply). `/issue` and `/link` flows aren't yet tracked; those are bounded conversations that self-clean on completion or timeout, so staleness is less of a concern. Easy to extend later.

### Changed
- Usher version now reported in the "Bot is online" status block as a regular bullet alongside Seerr/Radarr/Sonarr (e.g. `• Usher: ✅ 0.10.3`) instead of being prefixed to the header line. Consistent style with the other services.

## [0.10.2] - 2026-05-27

### Changed
- **Search-result buttons switched from title-as-label to keycap emoji buttons** (1️⃣ 2️⃣ 3️⃣ …) laid out 3 per row max. For 5 results this lands as `[1 2 3]` on the top row and `[4 5 Cancel]` on the bottom row (Cancel piggybacks on the last partial row when there's space). Full titles are now in the message body where Telegram can word-wrap them, so no more truncation regardless of title length. The `\n`-in-label trick from v0.10.1 didn't render as line breaks on iOS/Android Telegram clients (only Desktop seemed to honor it), so this is a cleaner path. Removed the now-dead `_format_search_button` helper.

## [0.10.1] - 2026-05-27

### Added
- **Multi-line search-result buttons.** Long media titles no longer get hard-truncated at ~60 chars. A `\n` is inserted near the middle of long labels so Telegram renders the button on two lines, doubling visible width before any truncation kicks in (cap is now 90 chars).
- **Specials (S00) included in the TV season picker** as a "Specials" entry. Anime movies / OVAs / tie-in specials often live there; users were previously locked out of reporting issues on them.
- **Parent-show re-search fallback.** When a search query contains a separator (`:`, ` - `, an em dash, ` | `) AND every Seerr result is out-of-library, the bot offers a `🔍 Search "..." instead` button using the part before the separator. Same button also shows on the "title isn't in your library" error message, so the user can pivot without restarting `/issue`. Refactored `issue_title` and the new `issue_research_parent` callback to share a `_show_search_results` helper.
- **Usher version in the "Bot is online" startup DM.** Header is now `👋 Usher vX.Y.Z is online.`
- **Admin also DM'd on resolved issues.** `handle_seerr_resolved` now sends a notification to the admin (in addition to the reporter) whenever a ticket is resolved, unless the admin IS the reporter. Format: `✅ Issue #N resolved\n<title>\nReported by: <user>`. Filled the gap left when we disabled Seerr's built-in Telegram notification integration in favor of Usher's webhook.

### Fixed
- **New-issue admin DMs were silently dropped.** v0.10.0 looked for `notification_type == "ISSUE_REPORTED"`, but Seerr's webhook payload uses the enum name `ISSUE_CREATED` (the UI label "Issue Reported" is just a display string). The receiver now accepts both spellings. Also bumped the "unhandled notification_type" log from `DEBUG` to `INFO` so future payload-name mismatches surface immediately.

## [0.10.0] - 2026-05-26

### Added
- **Admin DM on new issue.** When a user files a ticket, the admin gets a rich DM with media context, issue type, reporter, status, description, and inline action buttons. Skipped when the admin is the reporter (they already got the `/issue` confirmation).
- **`ISSUE_REPORTED` webhook handler** in the receiver (alongside the existing `ISSUE_COMMENT` and `ISSUE_RESOLVED` dispatches). Requires "Issue Reported" enabled in Seerr → Notifications → Webhook.
- **`SonarrClient.mark_failed_episode` / `RadarrClient.mark_failed`**: call the `*/history/failed/{historyId}` endpoint on the most recent grab record. Radarr/Sonarr handle the blocklist + new-search side effects automatically.
- **`_run_mark_failed`** -- sibling of `_run_autofix`, routes movie vs episode to the right client method.

### Changed
- **Admin ticket detail now has 2-level menus** (matches the Close menu pattern that already existed):
  - Top-level: `[💬 Reply] [🔧 Fix] [✅ Close]`
  - **Reply sub-menu** (admin): `[💬 Reply]` (reply input) / `[✅ Close]` (close without comment, quick out)
  - **Fix sub-menu** (admin): `[🔄 Redownload]` (delete + search, the old auto-fix behavior) / `[🚫 Mark Failed]` (new: Radarr/Sonarr blocklist + re-search) / `[✅ Close]` (quick out)
  - **Close sub-menu** (admin, unchanged): `[💬 With comment]` / `[✓ Without comment]`
  - Users still get a single-tap `[💬 Reply]` that goes straight to input (no sub-menu).
- **Comment notification format** now uses an explicit `From:` line and `Comment:` label with blank-line spacing between sections.
- **Episode formatting is zero-padded everywhere** (`S01E02` instead of `S1E2`) -- /tickets list, /issue confirmation, comment DM, resolved DM, new-issue admin DM, fix-started message.

### Also included
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
  - Code rendered as big bold characters (Mathematical Bold for letters, keycap emojis for digits), visible from across the room.
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
- **`/link` blocked all other commands while it was waiting on Plex.** `cmd_link_consent` runs the PIN polling loop (`await asyncio.sleep(3)` per iteration), and PTB's default `concurrent_updates=False` meant every other incoming update queued behind it. So if anyone was mid-`/link`, commands like `/tickets` from anyone (including the admin) sat in queue until the link flow finished or timed out. Set `concurrent_updates(True)` on the Application builder, so updates now process in parallel. SQLite serializes its own access, httpx AsyncClient is concurrency-safe, and bot_data is read-mostly, so the change is safe across all existing code paths.

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
- **Usher Admin UI URL** setting on the Telegram tab. When set, the bot's startup DM links you back to the actual host (LAN IP, reverse-proxy URL, etc.) instead of the generic `<host>` placeholder. Tolerates URLs entered with or without a trailing `/admin`.
- **Per-user daily auto-fix limit** is now configurable on the Auto-fix tab (was hardcoded to 3). Validates positive integer. Admin still bypasses the limit.
- Auto-fix tab now has a short explanation of what auto-fix does, so the field set isn't context-free.
- Webui shows the Usher version (`v0.8.2`) in the top-right alongside a Log out button. Single source of truth in `_version.py`.

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
- Unraid template stripped down to: App Data path, port mapping, optional `USHER_ENCRYPTION_KEY` (advanced). No more required env vars to fill in / re-enter every time the template is edited.
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
- `SEERR_URL` and `SEERR_API_KEY` no longer required env vars; Usher will start with no Seerr configured and prompt users in Telegram + admin UI to fill them in
- Unified the webhook and webui under a single aiohttp `web.Application` and single HTTP server. One port covers everything.
- Webhook secret is now read from settings on every request (rotates without a restart)

### Notes
- The Unraid template now exposes the web UI via Unraid's "WebUI" button (top-right of the container card)
- A backup includes the encryption key. Treat backup ZIPs as secret -- anyone who has the file can decrypt the stored Plex tokens.

## [0.6.0] - 2026-05-25

### Added
- **Comment-reply notifications via Seerr webhook.** When someone replies to an issue inside Seerr's UI, Usher DMs the reporter on Telegram with the comment text and a link to the issue. Configure in Seerr: Settings → Notifications → Webhook, point at `http://<host>:8765/webhook/seerr`, enable the **Issue Comment** event.
- New aiohttp webhook server inside Usher, listening on `/webhook/seerr` (port `8765` by default). Also exposes `/healthz` for liveness checks.
- New env vars: `WEBHOOK_PORT` (default `8765`), `WEBHOOK_BIND` (default `0.0.0.0`), `USHER_WEBHOOK_SECRET` (optional; if set, requires matching Authorization header on incoming webhooks).
- `UserStore.find_by_plex_username` for mapping Seerr's `reportedBy_username` back to a linked Telegram user.

### Notes
- Usher silently drops webhook events for users it doesn't have a Telegram mapping for. Users on legacy username-only links still work for this -- as long as `plex_username` is on the record, comment notifications will route.
- Usher skips notifying the reporter when the commenter IS the reporter (no echo-back).
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
- `/link` consent text trimmed down. No more wall of security text, just a one-liner about what it does and a mention of `/unlink` to revoke.
- `/unlink` reply now explicitly tells the user the Plex token is removed from Usher's storage, and points to Plex's authorized-devices page if they want full revocation.

## [0.4.0] - 2026-05-26

### Added
- **Plex OAuth (PIN flow) for per-user Seerr attribution.** `/link` now walks the user through Plex's PIN authorization, stores the resulting Plex token (encrypted at rest via Fernet), and uses it to authenticate to Seerr per user. Issues, comments, and resolves are now correctly attributed to the actual reporting user in Seerr's UI -- not the admin.
- Consent prompt before linking, with explicit disclosure that the Plex token is stored and grants Plex account access.
- New `USHER_ENCRYPTION_KEY` env var. If unset, Usher generates a Fernet key on first run and persists to `/data/encryption.key`.
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
- Completion notifications for auto-fix. When an auto-fix triggers a re-download, Usher now polls Radarr/Sonarr every 60s and DMs (or replies in the originating chat) when the new file is imported. Sends a timeout notification after 6h if nothing arrives.
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

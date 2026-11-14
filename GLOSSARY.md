# Usher

A Telegram bot and web admin panel that sit in front of a Plex server and its Seerr request manager, so users can report problems, request titles, and get notified without leaving Telegram.

## People

**Admin**:
The one Telegram account that runs the server. Gets admin DMs, bypasses limits, and can use admin-only commands.
_Avoid_: owner, operator

**Panel account**:
The username and password that sign in to the web admin panel. Separate from the Admin's Telegram identity even when the same person holds both.
_Avoid_: admin account, admin login

**User**:
A Telegram person talking to the bot.
_Avoid_: member, subscriber, chat

**Linked user**:
A User with a Link, so the bot can act for them in Seerr.

**Reporter**:
The User who opened a Ticket.

**Requester**:
The User who made a Request.

**Link**:
The tie between a User's Telegram account and their Plex sign-in, which lets the bot act as them in Seerr.
_Avoid_: mapping, binding

**Relink**:
Signing in again after a Link's Plex sign-in stops working.

## Plex access

**Library**:
A section of the Plex server, the unit a Share grants.
_Avoid_: section, Seerr library (say "known to Seerr" instead)

**Share**:
A person's access to chosen Libraries on the Plex server. Only the Admin creates or removes one.
_Avoid_: provisioning, friend

**Invite**:
A Share the recipient has not accepted yet.

**Uninvite**:
Removing a Share, pending or accepted.
_Avoid_: unshare

## Tickets

**Ticket**:
A User's report of a problem with a title, tracked until it is closed.
_Avoid_: issue (for the report itself)

**Issue type**:
The kind of problem a Ticket describes: Video, Audio, Subtitles, or Other.

**Scope**:
The part of a show a Ticket covers: one episode, one season, or all seasons. The bot itself offers one episode or a whole season (Specials go to Seerr as season 0, which Seerr treats as all seasons) and has no all-seasons button; a whole-show Ticket is made in Seerr's own UI.

**Fix**:
Replacing a bad file named in a Ticket.

**Redownload**:
A Fix that deletes the file and searches for it again.

**Mark failed**:
A Fix that blocklists the current release (when one is found), deletes the file, then searches again.

**Auto-fix**:
A Redownload the Reporter starts themselves on one episode or movie, allowed by the Admin's allowlist and daily limit.
_Avoid_: self-fix, re-search

**Repeat**:
An Auto-fix or Redownload whose replacement looks like the same release as the file it replaced. The bot compares release name, or else group, quality and size; it is a hint, not proof.

**Ask the Admin**:
A Reporter's one-tap request, after a Repeat or a comment, that the Admin try a different release. The bot sends the Admin a DM with Mark Failed first, once per Ticket.

**Auto-fix blocklists on a repeat**:
An Admin setting, off by default, that makes the bot run Mark Failed once by itself when a Reporter's Auto-fix is a Repeat. The retry is marked so its own Repeat never triggers it again.

## Requests and notifications

**Request**:
A Linked user's ask for a movie or show, made in Seerr as that user.

**Subscription**:
A one-shot ask to be told when a title becomes available.
_Avoid_: watch, availability watch

## Chat

**Hub**:
A command in the bot's menu that opens one message of buttons for an area of the bot, such as requests, Tickets, the Link, or the Admin's tools. Every flow started from a Hub ends with a way back to it.
_Avoid_: menu (the command list itself), panel (the web admin panel)

**Progress card**:
A bot message that edits itself in place as a Request or fix moves along.
_Avoid_: morphing card, request watch

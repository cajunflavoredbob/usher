"""Service alarms: DMs to the Admin when something needs attention, and the
matching lines on /status.

The plumbing is generic so more alarms can reuse it: a check builds
{key: text} for the problems present right now, and `reconcile` compares
that with the alarm_state table, DMs once for each new problem and once for
each that cleared. This module's checks are Radarr and Sonarr health
(warning and error only) and a disk below the percent or GB threshold
(whichever is hit first), listed once per path even when both apps report it.

The Plex server check is the third kind: plex.tv's resources listing for
the Admin's owned server, plus Tautulli's `server_status` when linked, and
either one saying offline counts. It alarms only after two checks in a row
say offline; the strike count is held in `bot_data["plex_strikes"]`, so a
restart forgets it (the alarm then comes one check later).

A problem whose check could not run (an app is down) is left alone, so an
outage never reads as "cleared"."""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from http_util import user_friendly_message
from settings import DEFAULT_LOW_DISK_GB, DEFAULT_LOW_DISK_PERCENT

logger = logging.getLogger("usher")

ALARM_INTERVAL_S = 5 * 60
ALARM_FIRST_DELAY_S = 60
HEALTH_LEVELS = ("warning", "error")
KIND_HEALTH = "health"
KIND_DISK = "disk"
KIND_PLEX = "plex"
PLEX_KEY = "plex:offline"
PLEX_OFFLINE_STRIKES = 2
PLEX_OFFLINE_TEXT = "<b>Plex</b> server is offline"
GB = 1024 ** 3
_APPS = (("radarr", "Radarr"), ("sonarr", "Sonarr"))
_SLASHES = "/" + chr(92)


# --- Reusable plumbing -------------------------------------------------------

async def _dm(ctx, text: str) -> bool:
    try:
        await ctx.bot.send_message(chat_id=ctx.bot_data["admin_id"], text=text,
                                   parse_mode="HTML",
                                   disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Alarm DM to the Admin failed (will retry next check)")
        return False


async def reconcile(ctx, current: dict[str, str],
                    checked: Callable[[str], bool]) -> None:
    """Compare `current` problems (key -> HTML text) with alarm_state.
    A stored alarm whose key `checked` accepts and that is absent from
    `current` has cleared: DM once, delete. A problem not yet told is DMed
    once. A DM that fails leaves its row so the next check retries."""
    store = ctx.bot_data["store"]
    rows = {k: (t, n) for k, t, n in await store.list_alarms()}
    for key, (text, was_told) in rows.items():
        if checked(key) and key not in current:
            # A problem the Admin was never told about clears silently.
            if not was_told or await _dm(ctx, f"✅ Cleared: {text}"):
                await store.delete_alarm(key)
    for key, text in current.items():
        if key not in rows:
            await store.add_alarm(key, text)
            notified = False
        else:
            notified = rows[key][1]
        if not notified and await _dm(ctx, f"⚠️ {text}"):
            await store.mark_alarm_notified(key)


async def drop_kind(ctx, kind: str) -> None:
    """Forget a kind's alarms without a DM (its switch was turned off), so
    turning it back on alarms afresh."""
    store = ctx.bot_data["store"]
    for key, _t, _n in await store.list_alarms():
        if key.startswith(kind + ":"):
            await store.delete_alarm(key)


# --- Radarr and Sonarr checks ------------------------------------------------

@dataclass
class ArrChecks:
    """health[service] / disks[service] hold the app's raw list, or an
    Exception when that app could not be asked. Only configured apps appear."""
    health: dict[str, object] = field(default_factory=dict)
    disks: dict[str, object] = field(default_factory=dict)


async def _safe(client, method: str):
    try:
        return await getattr(client, method)()
    except Exception as exc:
        logger.warning("Alarm check failed: %s", exc)
        return exc


async def gather_checks(bot_data) -> ArrChecks:
    out = ArrChecks()
    jobs = []
    for key, label in _APPS:
        client = bot_data.get(key)
        if client is None:
            continue
        jobs.append((label, "health", _safe(client, "get_health")))
        jobs.append((label, "disks", _safe(client, "get_disk_space")))
    results = await asyncio.gather(*(j[2] for j in jobs))
    for (label, what, _c), res in zip(jobs, results):
        getattr(out, what)[label] = res
    return out


def _wiki(item: dict) -> str:
    w = item.get("wikiUrl")
    if isinstance(w, dict):   # newer Arr builds send an object
        w = w.get("fullUri") or ""
    return str(w or "").strip()


def health_problems(service: str, items) -> dict[str, str]:
    """key -> HTML text for the warning and error items. Notice and ok are
    left out."""
    out: dict[str, str] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        level = str(it.get("type") or "").lower()
        if level not in HEALTH_LEVELS:
            continue
        source = str(it.get("source") or "").strip()
        message = str(it.get("message") or "").strip()
        text = (f"<b>{html.escape(service)}</b> {level} "
                f"({html.escape(source or 'unknown source')}): "
                f"{html.escape(message)}")
        wiki = _wiki(it)
        if wiki:
            text += f"\n{html.escape(wiki)}"
        out[f"{KIND_HEALTH}:{service}:{source}:{message}"] = text
    return out


@dataclass
class Disk:
    path: str
    label: str
    free: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 * self.free / self.total if self.total > 0 else 100.0

    def is_low(self, pct: int, gb: int) -> bool:
        if self.total <= 0:
            return False
        return self.percent < pct or (gb > 0 and self.free / GB < gb)

    def describe(self) -> str:
        name = html.escape(self.path)
        return (f"{name}: {self.percent:.0f}% free "
                f"({self.free / GB:.1f} GB of {self.total / GB:.1f} GB)")


def _norm(path: str) -> str:
    return path.rstrip(_SLASHES) or path


def merged_disks(per_service) -> list[Disk]:
    """One Disk per path across the apps' reports (the lower free space wins
    when two disagree). Apps that failed (an Exception) are skipped."""
    by_path: dict[str, Disk] = {}
    for items in per_service.values():
        if isinstance(items, BaseException):
            continue
        for it in items or []:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "").strip()
            if not path:
                continue
            try:
                d = Disk(path=path, label=str(it.get("label") or ""),
                         free=int(it.get("freeSpace") or 0),
                         total=int(it.get("totalSpace") or 0))
            except (TypeError, ValueError):
                continue
            old = by_path.get(_norm(path))
            if old is None or d.free < old.free:
                by_path[_norm(path)] = d
    return sorted(by_path.values(), key=lambda d: d.path)


def disk_problems(disks: list[Disk], pct: int, gb: int) -> dict[str, str]:
    return {f"{KIND_DISK}:{_norm(d.path)}": f"Low disk space on {d.describe()}"
            for d in disks if d.is_low(pct, gb)}


# --- Plex server -------------------------------------------------------------

async def plex_state(ctx) -> tuple[Optional[bool], str]:
    """(online, note). online is True, False, or None when it could not be
    told; note says why in plain words ("" when online is known). Offline
    wins: either plex.tv or Tautulli saying so is enough. None, with a
    "skipped" note, when the Admin has no usable Plex token."""
    bd = ctx.bot_data
    plex = bd.get("plex")
    link = await bd["store"].get(bd["admin_id"])
    token = getattr(link, "plex_token", None) if link else None
    if plex is None or not token:
        return None, "skipped, the Admin has no Plex token (link Plex with /link)"
    answers: list[bool] = []
    errors: list[str] = []
    try:
        seen = await plex.get_owned_server_online(token)
        if seen is not None:
            answers.append(seen)
    except LookupError:
        errors.append("the linked Plex account owns no server "
                      "(check which Plex account the Admin linked with /link)")
    except Exception as exc:
        logger.warning("Plex presence check failed: %s", exc)
        errors.append(user_friendly_message(exc))
    tautulli = bd.get("tautulli")
    if tautulli is not None:
        try:
            conn = await tautulli.get_server_status()
            if conn is not None:
                answers.append(conn)
        except Exception as exc:
            logger.warning("Tautulli server_status check failed: %s", exc)
            errors.append(user_friendly_message(exc))
    if False in answers:
        return False, ""
    if True in answers:
        return True, ""
    if errors:
        return None, "couldn't ask. " + "; ".join(errors)
    return None, "couldn't tell, plex.tv gave no presence"


async def _check_plex(ctx) -> None:
    online, _note = await plex_state(ctx)
    bd = ctx.bot_data
    if online is None:
        # Could not ask (or skipped): neither a strike nor a recovery.
        return
    if online:
        bd["plex_strikes"] = 0
        await reconcile(ctx, {}, lambda k: k == PLEX_KEY)
        return
    strikes = bd.get("plex_strikes", 0) + 1
    bd["plex_strikes"] = strikes
    if strikes >= PLEX_OFFLINE_STRIKES:
        await reconcile(ctx, {PLEX_KEY: PLEX_OFFLINE_TEXT},
                        lambda k: k == PLEX_KEY)


async def check_alarms(ctx) -> None:
    """The 5-minute job."""
    cfg = ctx.bot_data["settings_store"].settings
    want_health = bool(getattr(cfg, "alarm_health", True))
    want_disk = bool(getattr(cfg, "alarm_disk", True))
    want_plex = bool(getattr(cfg, "alarm_plex", True))
    try:
        if not want_health:
            await drop_kind(ctx, KIND_HEALTH)
        if not want_disk:
            await drop_kind(ctx, KIND_DISK)
        if not want_plex:
            await drop_kind(ctx, KIND_PLEX)
            ctx.bot_data.pop("plex_strikes", None)
        else:
            try:
                await _check_plex(ctx)
            except Exception:
                logger.exception("Plex alarm check failed")
        if not (want_health or want_disk):
            return
        checks = await gather_checks(ctx.bot_data)
        if want_health:
            current: dict[str, str] = {}
            ok_services: set[str] = set()
            for svc, items in checks.health.items():
                if isinstance(items, BaseException):
                    continue
                ok_services.add(svc)
                current.update(health_problems(svc, items))
            await reconcile(
                ctx, current,
                lambda k: k.startswith(KIND_HEALTH + ":")
                and k.split(":", 2)[1] in ok_services)
        if want_disk:
            pct = int(getattr(cfg, "low_disk_percent", DEFAULT_LOW_DISK_PERCENT))
            gb = int(getattr(cfg, "low_disk_gb", DEFAULT_LOW_DISK_GB))
            disk_now = disk_problems(merged_disks(checks.disks), pct, gb)
            # A path only one app reports must not read as cleared while
            # that app can't be asked.
            all_ok = not any(isinstance(v, BaseException)
                             for v in checks.disks.values())
            await reconcile(
                ctx, disk_now,
                lambda k: k.startswith(KIND_DISK + ":") and all_ok)
    except Exception:
        logger.exception("Alarm check failed")


# --- /status -----------------------------------------------------------------

async def status_section(ctx) -> str:
    """HTML starting with a blank line: Radarr and Sonarr health, then free
    space per disk, then the Plex server line. "" when neither app and no
    Plex client is configured."""
    plex_line = await _plex_status_line(ctx)
    if not any(ctx.bot_data.get(k) for k, _ in _APPS):
        return "\n\n" + plex_line if plex_line else ""
    checks = await gather_checks(ctx.bot_data)
    lines = ["\n\n<b>Health:</b>"]
    for svc, items in checks.health.items():
        if isinstance(items, BaseException):
            lines.append(f"  • <b>{svc}</b>: couldn't ask. "
                         f"{html.escape(user_friendly_message(items))}")
            continue
        probs = [i for i in items or [] if isinstance(i, dict)
                 and str(i.get("type") or "").lower() in HEALTH_LEVELS]
        if not probs:
            lines.append(f"  • <b>{svc}</b>: no problems")
        for i in probs:
            lines.append(f"  • <b>{svc}</b> {str(i.get('type')).lower()}: "
                         f"{html.escape(str(i.get('message') or ''))}")
    lines.append("\n<b>Free space:</b>")
    failed = [s for s, v in checks.disks.items() if isinstance(v, BaseException)]
    disks = merged_disks(checks.disks)
    cfg = ctx.bot_data["settings_store"].settings
    pct = int(getattr(cfg, "low_disk_percent", DEFAULT_LOW_DISK_PERCENT))
    gb = int(getattr(cfg, "low_disk_gb", DEFAULT_LOW_DISK_GB))
    for d in disks:
        mark = "⚠️ " if d.is_low(pct, gb) else ""
        lines.append(f"  • {mark}{d.describe()}")
    if not disks and not failed:
        lines.append("  • no disks reported")
    for s in failed:
        lines.append(f"  • <b>{s}</b>: couldn't ask. "
                     f"{html.escape(user_friendly_message(checks.disks[s]))}")
    if plex_line:
        lines.append("\n" + plex_line)
    return "\n".join(lines)


async def _plex_status_line(ctx) -> str:
    if ctx.bot_data.get("plex") is None:
        return ""
    online, note = await plex_state(ctx)
    if online is None:
        return f"<b>Plex server:</b> {html.escape(note)}"
    return f"<b>Plex server:</b> {'online' if online else 'offline'}"

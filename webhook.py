"""HTTP routes that receive Seerr webhook events.

Seerr -> Settings -> Notifications -> Webhook delivers JSON POSTs to
/webhook/seerr. The default Overseerr/Jellyseerr JSON template is what
this handler expects; no customization needed by the user.

The webhook secret is read from app state on every request so that
runtime updates via the webui take effect without a restart.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("hermes.webhook")

CommentHandler = Callable[[dict], Awaitable[None]]
ResolvedHandler = Callable[[dict], Awaitable[None]]
ReportedHandler = Callable[[dict], Awaitable[None]]
SecretProvider = Callable[[], str]

# Bounded dedupe cache: SHA-256 of the request body -> insert timestamp.
# Seerr's default retry window is short; 60s covers all realistic retries.
DEDUPE_TTL_S = 60
DEDUPE_MAX = 256

# Seerr's JSON payloads are well under 8KB. The parent aiohttp app has
# client_max_size=32MB for admin backup restores; we enforce a tighter
# limit here so unauthenticated clients can't allocate 32MB per request.
MAX_BODY_BYTES = 128 * 1024


def attach_webhook(
    app: web.Application,
    *,
    on_comment: CommentHandler,
    on_resolved: ResolvedHandler,
    on_reported: ReportedHandler,
    secret_provider: SecretProvider,
) -> None:
    """Register POST /webhook/seerr and GET /healthz on the given app.

    secret_provider() is called on every request so the secret can be
    rotated without restarting the server. An empty/missing secret causes
    all POSTs to be rejected (defense in depth; SettingsStore auto-
    generates a secret at startup so this should not happen in practice).
    """
    recent: "OrderedDict[str, float]" = OrderedDict()

    def _seen_recently(body_hash: str) -> bool:
        now = time.monotonic()
        # Evict expired entries opportunistically.
        while recent:
            oldest_key = next(iter(recent))
            if now - recent[oldest_key] > DEDUPE_TTL_S:
                recent.popitem(last=False)
            else:
                break
        if body_hash in recent:
            recent.move_to_end(body_hash)
            return True
        recent[body_hash] = now
        while len(recent) > DEDUPE_MAX:
            recent.popitem(last=False)
        return False

    async def handle(request: web.Request) -> web.Response:
        secret = secret_provider() or ""
        if not secret:
            logger.warning("Webhook rejected: webhook_secret is unset")
            return web.Response(status=503, text="webhook secret unset")

        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode("utf-8"), secret.encode("utf-8")):
            logger.warning("Webhook rejected: bad/missing Authorization header")
            return web.Response(status=401, text="unauthorized")

        try:
            content_length = int(request.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length > MAX_BODY_BYTES:
            logger.warning("Webhook rejected: Content-Length %d > %d", content_length, MAX_BODY_BYTES)
            return web.Response(status=413, text="payload too large")

        try:
            body = await request.read()
        except Exception:
            logger.warning("Webhook rejected: failed to read body")
            return web.Response(status=400, text="bad body")
        if len(body) > MAX_BODY_BYTES:
            logger.warning("Webhook rejected: body %d bytes > %d after read", len(body), MAX_BODY_BYTES)
            return web.Response(status=413, text="payload too large")

        body_hash = hashlib.sha256(body).hexdigest()
        if _seen_recently(body_hash):
            logger.info("Webhook deduped: identical body within %ds window", DEDUPE_TTL_S)
            return web.Response(status=200, text="duplicate")

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            logger.warning("Webhook rejected: invalid JSON")
            return web.Response(status=400, text="bad json")

        nt = (payload.get("notification_type") or "").upper()
        if nt == "TEST_NOTIFICATION":
            logger.info("Webhook test notification received from Seerr")
            return web.Response(status=200, text="ok")

        # Handler-internal exceptions are logged and answered with 200.
        # Seerr retries 5xx on backoff, which produces duplicate admin DMs
        # once the underlying issue (e.g., Telegram 429) clears -- we'd
        # rather lose one notification than spam the admin with five.
        if nt == "ISSUE_COMMENT":
            try:
                await on_comment(payload)
            except Exception:
                logger.exception("on_comment handler failed (returning 200 to suppress Seerr retry)")
            return web.Response(status=200, text="ok")
        if nt == "ISSUE_RESOLVED":
            try:
                await on_resolved(payload)
            except Exception:
                logger.exception("on_resolved handler failed (returning 200 to suppress Seerr retry)")
            return web.Response(status=200, text="ok")
        # Seerr's enum names the event ISSUE_CREATED in the payload (despite
        # the UI labeling it "Issue Reported"). Accept both spellings just in
        # case a fork or future version uses ISSUE_REPORTED.
        if nt in ("ISSUE_CREATED", "ISSUE_REPORTED"):
            try:
                await on_reported(payload)
            except Exception:
                logger.exception("on_reported handler failed (returning 200 to suppress Seerr retry)")
            return web.Response(status=200, text="ok")
        logger.info("Webhook received notification_type=%s (unhandled)", nt)
        return web.Response(status=200, text="ok")

    async def health(_request: web.Request) -> web.Response:
        return web.Response(status=200, text="ok")

    app.router.add_post("/webhook/seerr", handle)
    app.router.add_get("/healthz", health)


async def start_http_server(
    app: web.Application,
    *,
    host: str,
    port: int,
) -> web.AppRunner:
    """Start the shared aiohttp server. Caller must `await runner.cleanup()`."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("HTTP server listening on %s:%d", host, port)
    return runner

"""HTTP routes that receive Seerr webhook events.

Seerr -> Settings -> Notifications -> Webhook delivers JSON POSTs to
/webhook/seerr. The default Overseerr/Jellyseerr JSON template is what
this handler expects; no customization needed by the user.

The webhook secret is read from app state on every request so that
runtime updates via the webui take effect without a restart.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("hermes.webhook")

CommentHandler = Callable[[dict], Awaitable[None]]
ResolvedHandler = Callable[[dict], Awaitable[None]]
ReportedHandler = Callable[[dict], Awaitable[None]]
SecretProvider = Callable[[], str]


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
    rotated without restarting the server.
    """

    async def handle(request: web.Request) -> web.Response:
        secret = secret_provider() or ""
        if secret:
            auth = request.headers.get("Authorization", "")
            if auth != secret:
                logger.warning("Webhook rejected: bad/missing Authorization header")
                return web.Response(status=401, text="unauthorized")

        try:
            payload = await request.json()
        except Exception:
            logger.warning("Webhook rejected: invalid JSON")
            return web.Response(status=400, text="bad json")

        nt = (payload.get("notification_type") or "").upper()
        if nt == "TEST_NOTIFICATION":
            logger.info("Webhook test notification received from Seerr")
            return web.Response(status=200, text="ok")
        if nt == "ISSUE_COMMENT":
            try:
                await on_comment(payload)
            except Exception:
                logger.exception("on_comment handler failed")
                return web.Response(status=500, text="handler failed")
            return web.Response(status=200, text="ok")
        if nt == "ISSUE_RESOLVED":
            try:
                await on_resolved(payload)
            except Exception:
                logger.exception("on_resolved handler failed")
                return web.Response(status=500, text="handler failed")
            return web.Response(status=200, text="ok")
        # Seerr's enum names the event ISSUE_CREATED in the payload (despite
        # the UI labeling it "Issue Reported"). Accept both spellings just in
        # case a fork or future version uses ISSUE_REPORTED.
        if nt in ("ISSUE_CREATED", "ISSUE_REPORTED"):
            try:
                await on_reported(payload)
            except Exception:
                logger.exception("on_reported handler failed")
                return web.Response(status=500, text="handler failed")
            return web.Response(status=200, text="ok")
        # Surface unknown events at INFO so we have visibility into payload variants.
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

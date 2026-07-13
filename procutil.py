"""Process-lifecycle helpers shared by the bot and the web UI (they were
verbatim copies before 0.12.0; the audit)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger("hermes")


def schedule_clean_exit(delay_s: float = 2.0) -> None:
    """Send SIGTERM to self after `delay_s` so PTB's run_polling and
    aiohttp's runner unwind cleanly (closing httpx clients, DB
    connections, the HTTP server). Falls back to os._exit only if
    the SIGTERM dispatch itself fails."""
    loop = asyncio.get_running_loop()

    def _kill() -> None:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            logger.exception("SIGTERM dispatch failed; falling back to os._exit")
            os._exit(0)

    loop.call_later(delay_s, _kill)

import asyncio
import logging
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import bot.app as app_mod


def test_httpx_and_httpcore_not_logged_at_info():
    # httpx logs full request URLs at INFO, which carry the bot token and the SABnzbd API key.
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level >= logging.WARNING


def test_setup_only_stops_on_sigterm(tmp_path):
    handlers = {}
    runner = MagicMock()
    runner.cleanup = AsyncMock()

    async def run():
        loop = asyncio.get_running_loop()

        def fake_add(sig, cb, *args):
            handlers[sig] = cb

        with patch.object(app_mod, "attach_webui"), \
             patch.object(app_mod, "start_http_server", AsyncMock(return_value=runner)), \
             patch.object(loop, "add_signal_handler", fake_add, create=True):
            task = asyncio.create_task(app_mod._run_setup_only(
                MagicMock(), b"s", tmp_path, tmp_path / "settings.json",
                str(tmp_path / "db"), 8765, "127.0.0.1"))
            await asyncio.sleep(0.05)
            assert not task.done()
            assert signal.SIGTERM in handlers and signal.SIGINT in handlers
            handlers[signal.SIGTERM]()
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
    runner.cleanup.assert_awaited_once()

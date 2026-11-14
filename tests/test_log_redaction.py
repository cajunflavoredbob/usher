import asyncio
import io
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import InvalidToken

import bot.app as app_mod

TOKEN = "123456789:AAFakeTokenValueForTestsOnly_abc"


@pytest.fixture
def stream():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(app_mod.TokenRedactor())
    log = logging.getLogger("usher.redaction_test")
    log.addHandler(handler)
    log.propagate = False
    app_mod.register_log_secret(TOKEN)
    yield log, buf
    log.removeHandler(handler)
    app_mod.unregister_log_secret(TOKEN)


def test_token_in_message_is_replaced(stream):
    log, buf = stream
    log.error("The token `%s` was rejected by the server.", TOKEN)
    assert TOKEN not in buf.getvalue()
    assert "rejected by the server" in buf.getvalue()


def test_token_in_traceback_is_replaced(stream):
    log, buf = stream
    try:
        raise InvalidToken(f"The token `{TOKEN}` was rejected by the server.")
    except InvalidToken:
        log.exception("startup failed")
    assert TOKEN not in buf.getvalue()
    assert "InvalidToken" in buf.getvalue()


def test_token_in_url_style_args_is_replaced(stream):
    log, buf = stream
    log.warning("GET https://api.telegram.org/bot%s/getMe", TOKEN)
    assert TOKEN not in buf.getvalue()


def test_every_root_handler_carries_the_filter_after_import():
    # pytest adds its own root handlers, so check in a fresh interpreter.
    code = ("import logging, bot.app as a; h = logging.getLogger().handlers; "
            "assert h and all(any(isinstance(f, a.TokenRedactor) for f in x.filters) for x in h)")
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "bot")])}
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_mismatched_log_args_do_not_raise_from_the_filter(stream):
    log, buf = stream
    log.error("bad %s %s", TOKEN)  # too few args
    assert TOKEN not in buf.getvalue()


def test_invalid_token_at_startup_keeps_the_admin_page_up_and_does_not_exit(caplog, tmp_path):
    store = MagicMock()
    store.settings.telegram_bot_token = TOKEN
    app = MagicMock()
    app.run_polling.side_effect = InvalidToken(f"The token `{TOKEN}` was rejected by the server.")
    app.bot_data = {"settings_store": store, "session_secret": b"s", "data_dir": tmp_path,
                    "settings_path": tmp_path / "settings.json", "db_path": str(tmp_path / "db"),
                    "http_port": 8765, "http_bind": "127.0.0.1"}
    runner = MagicMock()
    runner.cleanup = AsyncMock()
    seen = {}

    def fake_attach(web_app, **kw):
        seen["cb"] = kw["on_settings_changed"]

    async def fake_start(web_app, host, port):
        seen["started"] = (host, port)
        # panel is up: change the token as the Admin page would, then stop
        store.settings.telegram_bot_token = "new-token"
        await seen["cb"]()
        return runner

    async def fake_wait(self):
        return None

    with caplog.at_level(logging.ERROR, logger="usher"), \
         patch.object(app_mod, "attach_webui", fake_attach), \
         patch.object(app_mod, "start_http_server", fake_start), \
         patch.object(app_mod, "schedule_clean_exit") as exit_later, \
         patch.object(app_mod.asyncio.Event, "wait", fake_wait):
        app_mod._run_polling(app)  # returns: no SystemExit
    assert seen["started"] == ("127.0.0.1", 8765)
    exit_later.assert_called_once_with(2.0)  # saving a new token restarts
    assert "Telegram rejected the bot token" in caplog.text
    assert TOKEN not in caplog.text


def test_rejected_token_stays_up_when_other_settings_are_saved(tmp_path):
    store = MagicMock()
    store.settings.telegram_bot_token = TOKEN
    runner = MagicMock()
    runner.cleanup = AsyncMock()
    seen = {}

    async def fake_start(web_app, host, port):
        await seen["cb"]()  # token unchanged
        return runner

    async def fake_wait(self):
        return None

    with patch.object(app_mod, "attach_webui", lambda w, **kw: seen.update(cb=kw["on_settings_changed"])), \
         patch.object(app_mod, "start_http_server", fake_start), \
         patch.object(app_mod, "schedule_clean_exit") as exit_later, \
         patch.object(app_mod.asyncio.Event, "wait", fake_wait):
        asyncio.run(app_mod._run_setup_only(
            store, b"s", tmp_path, tmp_path / "s.json", str(tmp_path / "db"),
            8765, "127.0.0.1", token_rejected=True))
    exit_later.assert_not_called()


def test_admin_page_notes_a_rejected_token():
    import webui
    from settings import Settings
    try:
        webui.set_token_rejected(True)
        assert "Telegram rejected the bot token" in webui._settings_page(Settings())
        webui.set_token_rejected(False)
        assert "Telegram rejected the bot token" not in webui._settings_page(Settings())
    finally:
        webui.set_token_rejected(False)

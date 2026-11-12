import io
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

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


def test_invalid_token_at_startup_exits_1_with_one_clear_log_line(caplog):
    app = MagicMock()
    app.run_polling.side_effect = InvalidToken(f"The token `{TOKEN}` was rejected by the server.")
    with caplog.at_level(logging.ERROR, logger="usher"), pytest.raises(SystemExit) as ei:
        app_mod._run_polling(app)
    assert ei.value.code == 1
    assert "Telegram rejected the bot token" in caplog.text
    assert TOKEN not in caplog.text

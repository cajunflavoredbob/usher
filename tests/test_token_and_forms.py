"""Values that could never work are refused before they can stop Usher:
bot tokens that cannot form a URL, plus the panel's form-read and render
edge cases."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from telegram.error import NetworkError

from bot import app as app_mod
from settings import Settings, validate_bot_token
import webui


@pytest.mark.parametrize("token", ["123:abc", "111:STORED-bot-token", "bad"])
def test_ordinary_tokens_pass(token):
    assert validate_bot_token(token) is None


@pytest.mark.parametrize("token", ["123:ab\tc", "123:ab c", "123:é",
                                   "1:" + "a" * 300])
def test_tokens_that_cannot_form_a_url_are_refused(token):
    assert validate_bot_token(token) is not None


def test_unbuildable_token_url_falls_back_to_the_panel():
    app = MagicMock()
    app.run_polling.side_effect = NetworkError(
        "Unknown error in HTTP implementation: InvalidURL('bad')")
    with patch.object(app_mod, "_run_setup_only") as setup_only, \
         patch.object(app_mod.asyncio, "run") as run:
        app_mod._run_polling(app)
    run.assert_called_once()
    assert setup_only.call_args.kwargs.get("token_rejected") is True


def test_a_plain_network_error_still_raises():
    app = MagicMock()
    app.run_polling.side_effect = NetworkError("connection refused")
    with pytest.raises(NetworkError):
        app_mod._run_polling(app)


def test_digest_hour_zero_renders_as_zero():
    page = webui._settings_page(Settings(digest_hour=0),
                                webhook_url="http://x/webhook/seerr",
                                csrf_token="t")
    assert 'name="digest_hour" value="0"' in page


async def test_any_form_read_error_is_a_400():
    async def bad_post():
        raise RuntimeError("unknown Content-Transfer-Encoding")
    req = SimpleNamespace(content_length=10, headers={}, post=bad_post)
    form, rejection = await webui._read_unauth_form(req)
    assert form is None and rejection.status == 400

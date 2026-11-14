"""The bot's welcome-screen text and profile photo are published at every
startup, like the command menu, so they always match the code. Failures are
logged, never raised."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _app(tmp_path, **bot_overrides):
    bot = SimpleNamespace(
        set_my_description=AsyncMock(return_value=True),
        set_my_short_description=AsyncMock(return_value=True),
        set_my_profile_photo=AsyncMock(return_value=True),
    )
    for k, v in bot_overrides.items():
        setattr(bot, k, v)
    return SimpleNamespace(bot=bot, bot_data={"data_dir": tmp_path})


@pytest.fixture
def avatar(tmp_path, monkeypatch):
    import bot.app as app_mod
    p = tmp_path / "avatar.png"
    p.write_bytes(b"\x89PNG first")
    monkeypatch.setattr(app_mod, "_AVATAR_PATH", p)
    return p


def test_texts_fit_telegram_limits():
    from bot.app import _BOT_DESCRIPTION, _BOT_SHORT_DESCRIPTION
    assert len(_BOT_DESCRIPTION) <= 512
    assert len(_BOT_SHORT_DESCRIPTION) <= 120
    assert _BOT_DESCRIPTION.startswith("Hi, I'm your Usher")


async def test_sets_description_and_short_description(tmp_path, avatar):
    from bot.app import _BOT_DESCRIPTION, _BOT_SHORT_DESCRIPTION, _register_profile
    app = _app(tmp_path)
    await _register_profile(app)
    app.bot.set_my_description.assert_awaited_once_with(description=_BOT_DESCRIPTION)
    app.bot.set_my_short_description.assert_awaited_once_with(
        short_description=_BOT_SHORT_DESCRIPTION)


async def test_text_failure_is_logged_not_raised(tmp_path, avatar, caplog):
    from bot.app import _register_profile
    app = _app(tmp_path, set_my_description=AsyncMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="usher"):
        await _register_profile(app)
    assert "set_my_description" in caplog.text
    # The photo step still runs after a text failure.
    app.bot.set_my_profile_photo.assert_awaited_once()


async def test_profile_photo_uploaded_once_until_it_changes(tmp_path, avatar):
    from bot.app import _register_profile
    from telegram import InputProfilePhotoStatic
    app = _app(tmp_path)

    await _register_profile(app)
    app.bot.set_my_profile_photo.assert_awaited_once()
    sent = app.bot.set_my_profile_photo.await_args.kwargs["photo"]
    assert isinstance(sent, InputProfilePhotoStatic)

    await _register_profile(app)  # same file: no second upload
    assert app.bot.set_my_profile_photo.await_count == 1

    avatar.write_bytes(b"\x89PNG second")
    await _register_profile(app)
    assert app.bot.set_my_profile_photo.await_count == 2


async def test_profile_photo_failure_is_logged_and_retried(tmp_path, avatar, caplog):
    from bot.app import _register_profile
    app = _app(tmp_path, set_my_profile_photo=AsyncMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="usher"):
        await _register_profile(app)
    assert "set_my_profile_photo" in caplog.text
    # Nothing recorded, so the next startup tries again.
    app.bot.set_my_profile_photo = AsyncMock(return_value=True)
    await _register_profile(app)
    app.bot.set_my_profile_photo.assert_awaited_once()


async def test_garbled_marker_means_upload_not_crash(tmp_path, avatar):
    from bot.app import _AVATAR_MARKER, _register_profile
    (tmp_path / _AVATAR_MARKER).write_bytes(b"\xff\xfe\x00junk")
    app = _app(tmp_path)
    await _register_profile(app)
    app.bot.set_my_profile_photo.assert_awaited_once()


async def test_missing_avatar_skips_photo(tmp_path, monkeypatch):
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_AVATAR_PATH", tmp_path / "nope.png")
    app = _app(tmp_path)
    await app_mod._register_profile(app)
    app.bot.set_my_profile_photo.assert_not_awaited()
    app.bot.set_my_description.assert_awaited_once()


def test_avatar_shipped_in_image():
    from pathlib import Path
    import bot.app as app_mod
    assert app_mod._AVATAR_PATH.is_file()
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    assert "assets/avatar.png" in dockerfile


async def test_post_init_registers_profile():
    import inspect
    import bot.app as app_mod
    assert "_register_profile(app)" in inspect.getsource(app_mod._post_init)

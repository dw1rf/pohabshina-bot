import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import discord
import pytest
from PIL import Image

from bot_client import MovieBot
from cogs.quick_help import QuickHelpButtons
from config import load_settings
from utils.bot_profile import LAUNCHER_URL, sync_profile


def test_launcher_presence_and_help_link():
    async def scenario():
        async with MovieBot(load_settings()) as bot:
            assert bot.activity.type == discord.ActivityType.playing
            assert bot.activity.name == "BlockEra Launcher · blockera.space"
            links = [item for item in QuickHelpButtons(None).children if item.url]
            assert [(button.label, button.url) for button in links] == [("BlockEra Launcher", "https://blockera.space")]

    asyncio.run(scenario())


def test_profile_sync_retries_and_keeps_existing_description(tmp_path):
    async def scenario():
        user = SimpleNamespace(id=42, edit=AsyncMock(side_effect=[OSError("temporary failure"), None]))
        app = SimpleNamespace(description="Наш сервер", edit=AsyncMock())
        path = tmp_path / "profile.db"
        async with aiosqlite.connect(path) as db:
            with pytest.raises(OSError):
                await sync_profile(db, user, app)
            await sync_profile(db, user, app)
            assert user.edit.await_count == 2
            images = user.edit.await_args.kwargs
            for key, size in [("avatar", (512, 512)), ("banner", (1200, 480))]:
                with Image.open(BytesIO(images[key])) as image:
                    assert image.format == "PNG" and image.size == size
                    image.verify()
            app.edit.assert_awaited_once_with(description=f"Наш сервер\nBlockEra Launcher · {LAUNCHER_URL}")
        async with aiosqlite.connect(path) as db:
            app.description = app.edit.await_args.kwargs["description"]
            await sync_profile(db, user, app)
            assert user.edit.await_count == 2 and app.edit.await_count == 1
            second_user = SimpleNamespace(id=43, edit=AsyncMock())
            app.description = "а" * 400
            await sync_profile(db, second_user, app)
            second_user.edit.assert_awaited_once()
            assert app.edit.await_count == 1

    asyncio.run(scenario())

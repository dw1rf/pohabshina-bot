import asyncio
from unittest.mock import AsyncMock

import bot as entrypoint
from bot_client import MovieBot
from config import load_settings


def test_entrypoint_starts_with_discord_token(monkeypatch):
    settings = load_settings()
    settings.discord_token = "fixture"
    monkeypatch.setattr(entrypoint, "load_settings", lambda: settings)
    start = AsyncMock()
    monkeypatch.setattr(MovieBot, "start", start)
    asyncio.run(entrypoint.main())
    start.assert_awaited_once_with("fixture")


def test_startup_loads_core_cogs(tmp_path, monkeypatch):
    async def scenario():
        settings = load_settings()
        settings.db_path = str(tmp_path / "bot.db")
        bot = MovieBot(settings)
        async with bot:
            bot.tree.sync = AsyncMock()
            monkeypatch.chdir(tmp_path)
            await bot.setup_hook()
            assert bot.get_cog("ModerationCog") is not None
            assert bot.get_cog("MusicCog") is not None
            bot.tree.sync.assert_awaited_once()

    asyncio.run(scenario())

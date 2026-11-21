import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot as entrypoint
from bot_client import MovieBot
from cogs.movies import MoviesCog
from config import load_settings
from services.watchmode_service import WatchmodeService


def test_entrypoint_accepts_discord_token_without_watchmode_key(monkeypatch):
    settings = load_settings()
    settings.discord_token = "fixture"
    settings.watchmode_api_key = ""
    monkeypatch.setattr(entrypoint, "load_settings", lambda: settings)
    start = AsyncMock()
    monkeypatch.setattr(MovieBot, "start", start)
    asyncio.run(entrypoint.main())
    start.assert_awaited_once_with("fixture")


def test_startup_without_movie_service(tmp_path, monkeypatch):
    async def scenario():
        settings = load_settings()
        settings.watchmode_api_key = ""
        settings.db_path = str(tmp_path / "bot.db")
        bot = MovieBot(settings)
        async with bot:
            bot.watchmode.load_genres = AsyncMock(side_effect=TimeoutError)
            bot.tree.sync = AsyncMock()
            monkeypatch.chdir(tmp_path)
            await bot.setup_hook()
            bot.watchmode.load_genres.assert_not_awaited()
            assert bot.get_cog("ModerationCog") is not None
            assert bot.get_cog("MusicCog") is not None
            bot.tree.sync.assert_awaited_once()

    asyncio.run(scenario())


def test_genre_lookup_retries_after_timeout_and_caches_success():
    async def scenario():
        settings = load_settings()
        settings.watchmode_api_key = "fixture"
        watchmode = WatchmodeService(settings)
        watchmode.get = AsyncMock(side_effect=[TimeoutError(), [{"id": 3, "name": "Action"}]])
        cog = MoviesCog(SimpleNamespace(settings=settings, session=object(), watchmode=watchmode))
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await cog.genres.callback(cog, interaction)
        assert "временно" in interaction.followup.send.await_args.args[0]
        await cog.genres.callback(cog, interaction)
        await cog.genres.callback(cog, interaction)
        assert "Боевик" in interaction.followup.send.await_args.args[0]
        assert watchmode.get.await_count == 2

    asyncio.run(scenario())

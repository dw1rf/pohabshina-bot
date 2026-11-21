from __future__ import annotations

import asyncio

from bot_client import MovieBot
from config import load_settings


def test_every_extension_loads_without_command_collisions(tmp_path) -> None:
    async def scenario() -> None:
        settings = load_settings()
        settings.db_path = str(tmp_path / "cog-smoke.sqlite3")
        bot = MovieBot(settings)
        await bot._async_setup_hook()
        try:
            bot.db = await bot._open_database_connection()
            await bot.levels.init_db(bot.db)
            await bot.jails.init_db(bot.db)
            await bot.reputation.init_rep_db(bot.db)
            await bot.reaction_bans.init_db(bot.db)
            await bot.reaction_roles.init_db(bot.db)
            await bot.social_games.init_db(bot.db)

            bot.economy_db = await bot._open_database_connection()
            await bot.economy.init_db(bot.economy_db)
            bot.progression_db = await bot._open_database_connection()
            await bot.progression.init_db(bot.progression_db)
            bot.community_db = await bot._open_database_connection()
            bot.digest_db = await bot._open_database_connection()
            bot.automod_db = await bot._open_database_connection()
            bot.giveaway_db = await bot._open_database_connection()
            bot.delivery_db = await bot._open_database_connection()

            loaded, _, failed = await bot.load_cogs()
            assert failed == []
            assert "cogs.command_stats" in loaded
            assert bot.tree.get_command("command_stats") is not None
            for removed in ("genres", "movie", "movies", "anime_feed", "support_panel", "shop_panel", "support_debug", "rules_minecraft"):
                assert bot.tree.get_command(removed) is None
            assert bot.tree.get_command("rating") and bot.tree.get_command("shop")
            assert "cogs.quick_help" in loaded
            assert "cogs.moderation" in loaded
            assert len(bot.tree.get_commands()) <= 100
        finally:
            await bot.close()

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs.levels import LevelsCog
from cogs.ping_guard import PingGuardCog
from cogs.social_profile import SocialProfileCog
from services.ai_persona_service import AIPersonaService
from utils.message_commands import reputation_change


def test_reputation_command_is_recognized_case_insensitively() -> None:
    assert reputation_change("  +РЕП  ") == 1
    assert reputation_change("-rep") == -1
    assert reputation_change("обычное сообщение") is None


def test_ai_treats_reputation_message_as_a_command() -> None:
    assert AIPersonaService().is_command_like("+реп")
    assert AIPersonaService().is_command_like("-REP")


def test_background_message_writers_ignore_reputation_commands() -> None:
    async def scenario() -> None:
        message = SimpleNamespace(
            content="+реп",
            author=SimpleNamespace(bot=False),
            guild=SimpleNamespace(id=1),
        )

        levels_bot = SimpleNamespace(
            db=object(),
            get_cog=Mock(),
            levels=SimpleNamespace(update_level_progress=AsyncMock()),
        )
        await LevelsCog(levels_bot).on_message(message)
        levels_bot.get_cog.assert_not_called()
        levels_bot.levels.update_level_progress.assert_not_awaited()

        social_games = SimpleNamespace(ensure_guild_settings=AsyncMock())
        profile_bot = SimpleNamespace(db=object(), get_cog=Mock(), social_games=social_games)
        await SocialProfileCog(profile_bot).on_message(message)
        profile_bot.get_cog.assert_not_called()
        social_games.ensure_guild_settings.assert_not_awaited()

        ping_cog = object.__new__(PingGuardCog)
        ping_cog.bot = SimpleNamespace(db=object())
        ping_cog.update_last_seen = AsyncMock()
        await ping_cog.on_message(message)
        ping_cog.update_last_seen.assert_not_awaited()

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite

from cogs.levels import LevelsCog
from cogs.ping_guard import PingGuardCog
from cogs.reputation import ReputationCog
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


def test_social_profile_rolls_back_after_database_error() -> None:
    async def scenario() -> None:
        db = SimpleNamespace(rollback=AsyncMock())
        social_games = SimpleNamespace(
            ensure_guild_settings=AsyncMock(return_value={"profile_analytics_enabled": 1}),
            get_privacy_settings=AsyncMock(
                return_value={"analytics_enabled": True, "store_message_samples": False}
            ),
        )
        bot = SimpleNamespace(db=db, get_cog=Mock(return_value=None), social_games=social_games)
        message = SimpleNamespace(
            content="обычное сообщение",
            author=SimpleNamespace(bot=False, id=2),
            guild=SimpleNamespace(id=1),
        )

        cog = SocialProfileCog(bot)
        cog._aggregate_message = AsyncMock(
            side_effect=aiosqlite.OperationalError("database is locked")
        )

        await cog.on_message(message)

        cog._aggregate_message.assert_awaited_once_with(message, False)
        db.rollback.assert_awaited_once()

    asyncio.run(scenario())


def test_concurrent_reputation_messages_respect_giver_limit() -> None:
    async def scenario() -> None:
        class FakeReputation:
            def __init__(self) -> None:
                self.count = 1

            async def can_give_rep(self, *_args: object) -> bool:
                await asyncio.sleep(0)
                return self.count < 2

            async def add_rep_event(self, *_args: object, **_kwargs: object) -> None:
                await asyncio.sleep(0.01)
                self.count += 1

            async def get_user_rep(self, *_args: object) -> tuple[int, int]:
                return self.count, 0

        reputation = FakeReputation()
        channel = SimpleNamespace(id=5, send=AsyncMock())
        giver = SimpleNamespace(id=7)
        guild = SimpleNamespace(id=3)
        messages = [
            SimpleNamespace(id=99, author=giver, guild=guild, channel=channel),
            SimpleNamespace(id=100, author=giver, guild=guild, channel=channel),
        ]
        receivers = [
            SimpleNamespace(id=42, bot=False),
            SimpleNamespace(id=43, bot=False),
        ]
        bot = SimpleNamespace(
            db=object(),
            reputation_db=object(),
            reputation=reputation,
            command_prefix="!",
        )
        cog = ReputationCog(bot)
        cog._resolve_target_message = AsyncMock(
            side_effect=[
                SimpleNamespace(id=88, author=receivers[0]),
                SimpleNamespace(id=89, author=receivers[1]),
            ]
        )
        cog._sync_member_reputation_nickname = AsyncMock(return_value=True)
        cog._send_reputation_embed = AsyncMock()

        await asyncio.gather(
            cog._handle_reputation_message(messages[0], 1),
            cog._handle_reputation_message(messages[1], 1),
        )

        assert reputation.count == 2
        assert cog._send_reputation_embed.await_count == 1
        assert any("Лимит репутации" in str(call.args[0]) for call in channel.send.await_args_list)

    asyncio.run(scenario())

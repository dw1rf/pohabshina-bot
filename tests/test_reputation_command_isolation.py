from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

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
        cog._send_reputation_card = AsyncMock()

        await asyncio.gather(
            cog._handle_reputation_message(messages[0], 1),
            cog._handle_reputation_message(messages[1], 1),
        )

        assert reputation.count == 2
        assert cog._send_reputation_card.await_count == 1
        assert any("Лимит репутации" in str(call.args[0]) for call in channel.send.await_args_list)

    asyncio.run(scenario())


def test_successful_reputation_response_is_an_image_without_text_or_embed() -> None:
    async def scenario() -> None:
        class Avatar:
            key = "test-avatar"
            url = "https://example.invalid/avatar.png"

            def with_size(self, _size: int) -> "Avatar":
                return self

            async def read(self) -> bytes:
                raise OSError("offline")

        channel = SimpleNamespace(send=AsyncMock())
        receiver = SimpleNamespace(id=42, display_name="+12 Алекс", display_avatar=Avatar())
        guild = SimpleNamespace(id=3, get_member=lambda _user_id: receiver)
        message = SimpleNamespace(id=99, guild=guild, channel=channel)
        cog = object.__new__(ReputationCog)
        cog.bot = SimpleNamespace(get_user=Mock(return_value=receiver))

        await cog._send_reputation_card(message, receiver, 1, 12)

        kwargs = channel.send.await_args.kwargs
        assert isinstance(kwargs["file"], discord.File)
        assert kwargs["file"].filename == "reputation-99.png"
        assert "content" not in kwargs
        assert "embed" not in kwargs

    asyncio.run(scenario())


def test_reputation_admin_command_requires_administrator_and_rejects_zero() -> None:
    async def scenario() -> None:
        group = ReputationCog.reputation_admin_group
        assert group.default_permissions is not None
        assert group.default_permissions.administrator

        reputation = SimpleNamespace(adjust_reputation=AsyncMock())
        cog = object.__new__(ReputationCog)
        cog.bot = SimpleNamespace(db=object(), reputation_db=object(), reputation=reputation)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=3),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        member = SimpleNamespace(bot=False)

        command = group.get_command("change")
        assert command is not None
        await command.callback(cog, interaction, member, 0)

        reputation.adjust_reputation.assert_not_awaited()
        assert "не может быть нулевым" in interaction.response.send_message.await_args.args[0]

    asyncio.run(scenario())


def test_reputation_admin_command_updates_nickname_and_confirms_privately() -> None:
    async def scenario() -> None:
        db = object()
        reputation = SimpleNamespace(adjust_reputation=AsyncMock(return_value=(3, 8)))
        cog = object.__new__(ReputationCog)
        cog.bot = SimpleNamespace(db=db, reputation_db=db, reputation=reputation)
        cog._sync_member_reputation_nickname = AsyncMock(return_value=False)
        interaction = SimpleNamespace(
            id=77,
            guild=SimpleNamespace(id=3),
            user=SimpleNamespace(id=7),
            channel_id=5,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        member = SimpleNamespace(id=42, bot=False, mention="<@42>")

        command = ReputationCog.reputation_admin_group.get_command("change")
        assert command is not None
        await command.callback(cog, interaction, member, 5)

        reputation.adjust_reputation.assert_awaited_once_with(
            db,
            guild_id=3,
            actor_user_id=7,
            receiver_user_id=42,
            channel_id=5,
            interaction_id=77,
            delta=5,
        )
        cog._sync_member_reputation_nickname.assert_awaited_once_with(
            member,
            previous_total=3,
            new_total=8,
        )
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "+3 → +8" in interaction.followup.send.await_args.args[0]
        assert "ник изменить не удалось" in interaction.followup.send.await_args.args[0]

    asyncio.run(scenario())

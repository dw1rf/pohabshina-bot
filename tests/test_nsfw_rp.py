from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.roleplay import RoleplayCog, _is_nsfw_channel_allowed
from cogs.settings import SettingsCog


def test_configured_nsfw_channel_is_the_only_allowed_channel() -> None:
    selected = SimpleNamespace(id=777, is_nsfw=lambda: True)
    other = SimpleNamespace(id=888, is_nsfw=lambda: True)

    assert _is_nsfw_channel_allowed(selected, 777)
    assert not _is_nsfw_channel_allowed(other, 777)


def test_native_nsfw_channel_remains_fallback_until_admin_selects_one() -> None:
    native_nsfw = SimpleNamespace(id=777, is_nsfw=lambda: True)
    regular = SimpleNamespace(id=888, is_nsfw=lambda: False)

    assert _is_nsfw_channel_allowed(native_nsfw, 0)
    assert not _is_nsfw_channel_allowed(regular, 0)


def test_dynamic_rp_commands_have_no_discord_cooldown() -> None:
    cog = object.__new__(RoleplayCog)
    cog.bot = SimpleNamespace()

    callback = cog._make_callback("test_action")

    assert not getattr(callback, "__discord_app_commands_checks__", [])


def test_obsolete_rp_consent_command_is_not_registered() -> None:
    assert all(command.name != "consent" for command in RoleplayCog.rp_group.commands)


def test_obsolete_nsfw_toggle_and_adult_role_commands_are_not_registered() -> None:
    command_names = {command.name for command in SettingsCog.__cog_app_commands__}

    assert "set_nsfw_rp" not in command_names
    assert "set_adult_role" not in command_names


def test_admin_can_select_nsfw_channel_and_enable_commands() -> None:
    async def scenario() -> None:
        set_nsfw_channel = AsyncMock()
        bot = SimpleNamespace(
            db=object(),
            social_games=SimpleNamespace(set_nsfw_channel=set_nsfw_channel),
        )
        cog = object.__new__(SettingsCog)
        cog.bot = bot
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=SimpleNamespace(id=10), response=response)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 777
        channel.mention = "<#777>"
        channel.is_nsfw.return_value = True

        await SettingsCog.set_nsfw_channel.callback(cog, interaction, channel)

        set_nsfw_channel.assert_awaited_once_with(bot.db, 10, 777)
        response.send_message.assert_awaited_once()
        assert "<#777>" in response.send_message.await_args.args[0]

    asyncio.run(scenario())


def test_admin_cannot_select_channel_without_discord_age_gate() -> None:
    async def scenario() -> None:
        set_nsfw_channel = AsyncMock()
        bot = SimpleNamespace(
            db=object(),
            social_games=SimpleNamespace(set_nsfw_channel=set_nsfw_channel),
        )
        cog = object.__new__(SettingsCog)
        cog.bot = bot
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=SimpleNamespace(id=10), response=response)
        channel = Mock(spec=discord.TextChannel)
        channel.is_nsfw.return_value = False

        await SettingsCog.set_nsfw_channel.callback(cog, interaction, channel)

        set_nsfw_channel.assert_not_awaited()
        assert "18+" in response.send_message.await_args.args[0]

    asyncio.run(scenario())


def test_admin_can_select_separate_nsfw_import_channel() -> None:
    async def scenario() -> None:
        set_nsfw_import_channel = AsyncMock()
        bot = SimpleNamespace(
            db=object(),
            social_games=SimpleNamespace(
                ensure_guild_settings=AsyncMock(return_value={"nsfw_channel_id": 777}),
                set_nsfw_import_channel=set_nsfw_import_channel,
            ),
        )
        cog = object.__new__(SettingsCog)
        cog.bot = bot
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=SimpleNamespace(id=10), response=response)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 888
        channel.mention = "<#888>"
        channel.is_nsfw.return_value = True

        await SettingsCog.set_nsfw_import_channel.callback(cog, interaction, channel)

        set_nsfw_import_channel.assert_awaited_once_with(bot.db, 10, 888)
        assert "<#888>" in response.send_message.await_args.args[0]
        assert "<#777>" in response.send_message.await_args.args[0]

    asyncio.run(scenario())


def test_admin_cannot_make_import_channel_the_destination() -> None:
    async def scenario() -> None:
        set_nsfw_import_channel = AsyncMock()
        bot = SimpleNamespace(
            db=object(),
            social_games=SimpleNamespace(
                ensure_guild_settings=AsyncMock(return_value={"nsfw_channel_id": 777}),
                set_nsfw_import_channel=set_nsfw_import_channel,
            ),
        )
        cog = object.__new__(SettingsCog)
        cog.bot = bot
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=SimpleNamespace(id=10), response=response)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 777
        channel.is_nsfw.return_value = True

        await SettingsCog.set_nsfw_import_channel.callback(cog, interaction, channel)

        set_nsfw_import_channel.assert_not_awaited()
        assert "разными" in response.send_message.await_args.args[0]

    asyncio.run(scenario())


def test_rp_action_still_replies_when_optional_telemetry_fails() -> None:
    class SocialGames:
        async def ensure_guild_settings(self, _db: object, _guild_id: int) -> dict[str, int]:
            return {
                "nsfw_rp_enabled": 1,
                "nsfw_channel_id": 777,
                "adult_role_id": 0,
            }

        async def has_rp_consent(
            self,
            _db: object,
            _guild_id: int,
            _user_id: int,
            *,
            nsfw: bool,
        ) -> bool:
            return nsfw

        async def increment_rp_action(self, *_args: object) -> int:
            raise RuntimeError("telemetry database is busy")

    async def scenario() -> None:
        bot = SimpleNamespace(
            db=object(),
            progression_db=object(),
            social_games=SocialGames(),
            progression=SimpleNamespace(record_event=AsyncMock()),
        )
        cog = object.__new__(RoleplayCog)
        cog.bot = bot

        author = Mock(spec=discord.Member)
        author.id = 1
        author.mention = "<@1>"
        author.get_role.return_value = None
        target = Mock(spec=discord.Member)
        target.id = 2
        target.mention = "<@2>"
        target.bot = False
        target.get_role.return_value = None
        response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = SimpleNamespace(
            id=999,
            guild=SimpleNamespace(id=10),
            user=author,
            channel=SimpleNamespace(id=777, is_nsfw=lambda: True),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog._execute_action(
            interaction,
            "test_nsfw",
            target,
            None,
            {"label": "test", "text": "test action", "nsfw": True},
        )

        response.send_message.assert_awaited_once()
        assert "embed" in response.send_message.await_args.kwargs
        assert "ephemeral" not in response.send_message.await_args.kwargs

    asyncio.run(scenario())


def test_rp_progression_uses_ascii_idempotency_key_for_russian_action() -> None:
    class SocialGames:
        async def ensure_guild_settings(self, _db: object, _guild_id: int) -> dict[str, int]:
            return {"nsfw_channel_id": 777}

        async def increment_rp_action(self, *_args: object) -> int:
            return 1

    async def scenario() -> None:
        record_event = AsyncMock()
        bot = SimpleNamespace(
            db=object(),
            progression_db=object(),
            social_games=SocialGames(),
            progression=SimpleNamespace(record_event=record_event),
        )
        cog = object.__new__(RoleplayCog)
        cog.bot = bot
        author = Mock(spec=discord.Member)
        author.id = 1
        author.mention = "<@1>"
        target = Mock(spec=discord.Member)
        target.id = 2
        target.mention = "<@2>"
        response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = SimpleNamespace(
            id=1003,
            guild=SimpleNamespace(id=10),
            user=author,
            channel=SimpleNamespace(id=777, is_nsfw=lambda: True),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog._execute_action(
            interaction,
            "кончить_на_лицо",
            target,
            None,
            {"label": "test", "text": "test action", "nsfw": True},
        )

        assert record_event.await_args.args[5] == "rp:1003"

    asyncio.run(scenario())


def test_nsfw_action_allows_bot_self_target_without_consent_or_role() -> None:
    class SocialGames:
        async def ensure_guild_settings(self, _db: object, _guild_id: int) -> dict[str, int]:
            return {
                "nsfw_rp_enabled": 0,
                "nsfw_channel_id": 777,
                "adult_role_id": 123,
            }

        async def increment_rp_action(self, *_args: object) -> int:
            return 1

    async def scenario() -> None:
        bot = SimpleNamespace(
            db=object(),
            progression_db=None,
            social_games=SocialGames(),
        )
        cog = object.__new__(RoleplayCog)
        cog.bot = bot

        author = Mock(spec=discord.Member)
        author.id = 1
        author.mention = "<@1>"
        author.bot = True
        response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = SimpleNamespace(
            id=1000,
            guild=SimpleNamespace(id=10),
            user=author,
            channel=SimpleNamespace(id=777, is_nsfw=lambda: True),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog._execute_action(
            interaction,
            "test_nsfw",
            author,
            None,
            {"label": "test", "text": "test action", "nsfw": True},
        )

        response.send_message.assert_awaited_once()
        assert "embed" in response.send_message.await_args.kwargs
        assert "ephemeral" not in response.send_message.await_args.kwargs

    asyncio.run(scenario())


def test_rp_action_can_repeat_without_target_cooldown() -> None:
    class SocialGames:
        async def ensure_guild_settings(self, _db: object, _guild_id: int) -> dict[str, int]:
            return {
                "nsfw_rp_enabled": 1,
                "nsfw_channel_id": 777,
                "adult_role_id": 0,
            }

        async def increment_rp_action(self, *_args: object) -> int:
            return 1

    async def scenario() -> None:
        bot = SimpleNamespace(db=object(), progression_db=None, social_games=SocialGames())
        cog = object.__new__(RoleplayCog)
        cog.bot = bot

        author = Mock(spec=discord.Member)
        author.id = 1
        author.mention = "<@1>"
        target = Mock(spec=discord.Member)
        target.id = 2
        target.mention = "<@2>"
        target.bot = False

        responses = []
        for interaction_id in (1001, 1002):
            response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
            interaction = SimpleNamespace(
                id=interaction_id,
                guild=SimpleNamespace(id=10),
                user=author,
                channel=SimpleNamespace(id=777, is_nsfw=lambda: True),
                response=response,
                followup=SimpleNamespace(send=AsyncMock()),
            )
            await cog._execute_action(
                interaction,
                "test_nsfw",
                target,
                None,
                {"label": "test", "text": "test action", "nsfw": True},
            )
            responses.append(response)

        for response in responses:
            response.send_message.assert_awaited_once()
            assert "embed" in response.send_message.await_args.kwargs

    asyncio.run(scenario())

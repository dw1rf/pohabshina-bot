from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import aiosqlite

from cogs.moderation import JailPermissionSyncError, JailView, ModerationCog


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        category_id: int | None = None,
        permissions_synced: bool = False,
        overwrite: discord.PermissionOverwrite | None = None,
    ) -> None:
        self.id = channel_id
        self.category_id = category_id
        self.permissions_synced = permissions_synced
        self.overwrite = overwrite or discord.PermissionOverwrite()
        self.update_calls = 0

    def overwrites_for(self, _role: object) -> discord.PermissionOverwrite:
        return self.overwrite

    async def set_permissions(
        self,
        _role: object,
        *,
        overwrite: discord.PermissionOverwrite,
        reason: str,
    ) -> None:
        assert reason == "Jail role channel lock"
        self.update_calls += 1
        self.overwrite = overwrite


def test_jail_permission_sync_skips_inherited_and_existing_overwrites() -> None:
    async def scenario() -> None:
        jail_category = FakeChannel(100)
        jail_child = FakeChannel(101, category_id=100, permissions_synced=True)
        regular_category = FakeChannel(200)
        synced_child = FakeChannel(201, category_id=200, permissions_synced=True)
        unsynced_child = FakeChannel(202, category_id=200, permissions_synced=False)
        existing_lock = discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            add_reactions=False,
            connect=False,
            speak=False,
            mention_everyone=False,
        )
        already_locked = FakeChannel(300, overwrite=existing_lock)
        guild = SimpleNamespace(
            id=1,
            channels=[
                jail_category,
                jail_child,
                regular_category,
                synced_child,
                unsynced_child,
                already_locked,
            ],
        )

        cog = object.__new__(ModerationCog)
        role = object()
        await cog._lock_regular_channels_for_jail_role(guild, role, jail_category)

        assert regular_category.update_calls == 1
        assert unsynced_child.update_calls == 1
        assert jail_category.update_calls == 0
        assert jail_child.update_calls == 0
        assert synced_child.update_calls == 0
        assert already_locked.update_calls == 0

        await cog._lock_regular_channels_for_jail_role(guild, role, jail_category)
        assert regular_category.update_calls == 1
        assert unsynced_child.update_calls == 1

    asyncio.run(scenario())


def test_jail_intro_falls_back_to_text_when_embed_send_fails() -> None:
    async def scenario() -> None:
        response = SimpleNamespace(status=400, reason="Bad Request")
        error = discord.HTTPException(
            response,
            {"message": "Cannot send embed", "code": 50013},
        )
        channel = SimpleNamespace(id=123, send=AsyncMock(side_effect=[error, None]))
        user = Mock(spec=discord.Member)
        user.mention = "<@42>"
        cog = object.__new__(ModerationCog)

        await cog._send_jail_intro(
            channel,
            user,
            "нарушение правил",
            timedelta(minutes=10),
            datetime.now(UTC) + timedelta(minutes=10),
        )

        assert channel.send.await_count == 2
        fallback = channel.send.await_args_list[1].kwargs
        assert "Подать апелляцию" in fallback["content"]
        assert "Узнать оставшееся время" in fallback["content"]
        assert isinstance(fallback["view"], JailView)
        assert fallback["allowed_mentions"].everyone is False
        assert fallback["allowed_mentions"].roles is False

    asyncio.run(scenario())


def test_jail_permission_sync_fails_closed_on_discord_error() -> None:
    async def scenario() -> None:
        response = SimpleNamespace(status=403, reason="Forbidden")
        error = discord.Forbidden(response, {"message": "Missing permissions", "code": 50013})
        channel = FakeChannel(200)
        channel.set_permissions = AsyncMock(side_effect=error)
        guild = SimpleNamespace(id=1, channels=[channel])
        cog = object.__new__(ModerationCog)

        try:
            await cog._lock_regular_channels_for_jail_role(
                guild,
                object(),
                FakeChannel(100),
            )
        except JailPermissionSyncError:
            pass
        else:
            raise AssertionError("Permission sync failure must abort /jail")

    asyncio.run(scenario())


def test_release_jail_keeps_record_when_role_cannot_be_removed() -> None:
    async def scenario() -> None:
        response = SimpleNamespace(status=403, reason="Forbidden")
        error = discord.Forbidden(response, {"message": "Missing permissions", "code": 50013})
        role = object()
        member = SimpleNamespace(
            id=42,
            roles=[role],
            remove_roles=AsyncMock(side_effect=error),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            get_member=Mock(return_value=member),
            get_role=Mock(return_value=role),
        )
        jails = SimpleNamespace(remove=AsyncMock())
        cog = object.__new__(ModerationCog)
        cog.bot = SimpleNamespace(
            db=object(),
            jail_db=object(),
            jails=jails,
            get_guild=Mock(return_value=guild),
        )
        cog._safe_mod_log = AsyncMock(return_value=True)
        record = SimpleNamespace(guild_id=1, user_id=42, role_id=8, channel_id=9)

        released = await cog.release_jail(record, reason="test")

        assert released is False
        jails.remove.assert_not_awaited()
        cog._safe_mod_log.assert_awaited_once()

    asyncio.run(scenario())


def test_jail_defers_before_waiting_for_database_lookup() -> None:
    async def scenario() -> None:
        lookup_started = asyncio.Event()
        release_lookup = asyncio.Event()

        async def get_active_by_user(_db: object, _guild_id: int, _user_id: int) -> object:
            lookup_started.set()
            await release_lookup.wait()
            return SimpleNamespace(channel_id=456)

        response = SimpleNamespace(
            defer=AsyncMock(),
            is_done=Mock(return_value=True),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=Mock(spec=discord.Member),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        interaction.user.guild_permissions = SimpleNamespace(
            administrator=True,
            manage_guild=False,
            moderate_members=False,
        )
        target = Mock(spec=discord.Member)
        target.id = 42

        cog = object.__new__(ModerationCog)
        cog.bot = SimpleNamespace(
            db=object(),
            jails=SimpleNamespace(get_active_by_user=get_active_by_user),
        )
        cog._validate_jail_target = Mock(return_value=None)

        task = asyncio.create_task(
            ModerationCog.jail.callback(cog, interaction, target, "причина", "10m")
        )
        await asyncio.wait_for(lookup_started.wait(), timeout=1)

        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

        release_lookup.set()
        await asyncio.wait_for(task, timeout=1)
        interaction.followup.send.assert_awaited_once()

    asyncio.run(scenario())


def test_jail_cleans_up_discord_state_when_database_save_fails() -> None:
    async def scenario() -> None:
        response = SimpleNamespace(
            defer=AsyncMock(),
            is_done=Mock(return_value=True),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=Mock(spec=discord.Member),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        interaction.user.id = 7
        interaction.user.guild_permissions = SimpleNamespace(
            administrator=True,
            manage_guild=False,
            moderate_members=False,
        )
        role = SimpleNamespace(id=8)
        channel = SimpleNamespace(id=9, mention="<#9>", delete=AsyncMock())
        target = Mock(spec=discord.Member)
        target.id = 42
        target.mention = "<@42>"
        target.voice = None
        target.add_roles = AsyncMock()
        target.remove_roles = AsyncMock()

        jails = SimpleNamespace(
            get_active_by_user=AsyncMock(return_value=None),
            upsert=AsyncMock(side_effect=aiosqlite.OperationalError("database is locked")),
        )
        cog = object.__new__(ModerationCog)
        cog.bot = SimpleNamespace(db=object(), jails=jails)
        cog._validate_jail_target = Mock(return_value=None)
        cog._get_or_create_jail_category = AsyncMock(return_value=SimpleNamespace(id=10))
        cog._get_or_create_jail_role = AsyncMock(return_value=role)
        cog._lock_regular_channels_for_jail_role = AsyncMock()
        cog._create_jail_channel = AsyncMock(return_value=channel)

        await ModerationCog.jail.callback(cog, interaction, target, "причина", "10m")

        target.remove_roles.assert_awaited_once_with(role, reason="Rollback failed jail database save")
        channel.delete.assert_awaited_once_with(reason="Rollback failed jail database save")
        replies = [str(call.args[0]) for call in interaction.followup.send.await_args_list]
        assert any("баз" in reply.lower() for reply in replies)

    asyncio.run(scenario())


def test_concurrent_jail_for_same_user_is_rejected() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def jail_impl(*_args: object) -> None:
            started.set()
            await release.wait()

        cog = object.__new__(ModerationCog)
        cog._jail_operation_locks = {}
        cog._jail_impl = AsyncMock(side_effect=jail_impl)
        cog._safe_reply = AsyncMock()
        guild = SimpleNamespace(id=1)
        first = SimpleNamespace(guild=guild)
        second = SimpleNamespace(guild=guild)
        user = SimpleNamespace(id=42)

        first_task = asyncio.create_task(
            ModerationCog.jail.callback(cog, first, user, "первая", "10m")
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await ModerationCog.jail.callback(cog, second, user, "вторая", "10m")

        cog._safe_reply.assert_awaited_once_with(
            second,
            "Операция /jail для этого пользователя уже выполняется.",
        )
        assert cog._jail_impl.await_count == 1

        release.set()
        await asyncio.wait_for(first_task, timeout=1)

    asyncio.run(scenario())


def test_manual_unjail_retry_does_not_wait_for_original_expiry() -> None:
    async def scenario() -> None:
        record = SimpleNamespace(
            guild_id=1,
            user_id=42,
            expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
        )
        cog = object.__new__(ModerationCog)
        cog._jail_tasks = {}
        cog.release_jail = AsyncMock(return_value=True)

        with patch("cogs.moderation.asyncio.sleep", new=AsyncMock()) as sleep:
            await cog._release_when_due(record, retry_now=True)

        sleep.assert_not_awaited()
        cog.release_jail.assert_awaited_once()

    asyncio.run(scenario())

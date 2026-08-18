from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.moderation import JailView, ModerationCog


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

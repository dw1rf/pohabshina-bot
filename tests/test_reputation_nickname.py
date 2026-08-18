from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.reputation import ReputationCog, format_reputation_nickname


def test_reputation_prefix_is_added_without_changing_the_name() -> None:
    assert format_reputation_nickname("Алекс", previous_total=0, new_total=1) == "+1 Алекс"


def test_existing_positive_prefix_is_replaced_instead_of_duplicated() -> None:
    assert format_reputation_nickname("+1 Алекс", previous_total=1, new_total=18) == "+18 Алекс"


def test_existing_negative_prefix_is_replaced_instead_of_duplicated() -> None:
    assert format_reputation_nickname("-1 Алекс", previous_total=-1, new_total=-17) == "-17 Алекс"


def test_manual_nickname_change_becomes_the_new_preserved_name() -> None:
    assert format_reputation_nickname("Новый ник", previous_total=4, new_total=5) == "+5 Новый ник"


def test_name_is_not_truncated_when_discord_limit_would_be_exceeded() -> None:
    original_name = "а" * 32

    assert format_reputation_nickname(original_name, previous_total=0, new_total=1) is None


def test_member_nickname_is_edited_with_the_new_total() -> None:
    async def scenario() -> None:
        cog = object.__new__(ReputationCog)
        member = Mock(spec=discord.Member)
        member.id = 42
        member.nick = "+1 Алекс"
        member.display_name = "+1 Алекс"
        member.edit = AsyncMock()

        changed = await cog._sync_member_reputation_nickname(
            member,
            previous_total=1,
            new_total=2,
        )

        assert changed is True
        member.edit.assert_awaited_once_with(
            nick="+2 Алекс",
            reason="Обновление репутации участника",
        )

    asyncio.run(scenario())


def test_nickname_permission_failure_does_not_break_reputation_command() -> None:
    async def scenario() -> None:
        cog = object.__new__(ReputationCog)
        member = Mock(spec=discord.Member)
        member.id = 42
        member.nick = None
        member.display_name = "Алекс"
        member.edit = AsyncMock(side_effect=discord.Forbidden(Mock(), "forbidden"))

        changed = await cog._sync_member_reputation_nickname(
            member,
            previous_total=0,
            new_total=-1,
        )

        assert changed is False

    asyncio.run(scenario())


def test_reputation_change_syncs_member_nickname_with_total_score() -> None:
    async def scenario() -> None:
        receiver = Mock(spec=discord.Member)
        receiver.id = 42
        receiver.bot = False
        giver = SimpleNamespace(id=7)
        channel = SimpleNamespace(id=5, send=AsyncMock())
        message = SimpleNamespace(
            id=99,
            author=giver,
            guild=SimpleNamespace(id=3),
            channel=channel,
        )
        reputation = SimpleNamespace(
            can_give_rep=AsyncMock(return_value=True),
            add_rep_event=AsyncMock(),
            get_user_rep=AsyncMock(return_value=(12, 3)),
        )
        cog = object.__new__(ReputationCog)
        cog.bot = SimpleNamespace(db=object(), reputation=reputation)
        cog._resolve_target_message = AsyncMock(return_value=SimpleNamespace(id=88, author=receiver))
        cog._sync_member_reputation_nickname = AsyncMock(return_value=True)
        cog._send_reputation_embed = AsyncMock()

        await cog._handle_reputation_message(message, value=1)

        cog._sync_member_reputation_nickname.assert_awaited_once_with(
            receiver,
            previous_total=8,
            new_total=9,
        )
        cog._send_reputation_embed.assert_awaited_once_with(message, receiver, 1, 9)

    asyncio.run(scenario())

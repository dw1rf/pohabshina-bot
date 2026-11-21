import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord
import pytest

from cogs.support_shop import SupportShopCog
from services.support_ticket_service import SupportTicketService


@pytest.mark.parametrize("status", [403, 503])
@pytest.mark.parametrize("already_deleted", [False, True])
def test_close_can_be_retried_after_discord_failure(status, already_deleted):
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            db.row_factory = aiosqlite.Row
            tickets = SupportTicketService()
            await tickets.init_db(db)
            await tickets.create_ticket(db, 1, 2, 3)
            failed = discord.HTTPException(SimpleNamespace(status=status, reason="unavailable"), "")
            missing = discord.NotFound(SimpleNamespace(status=404, reason="unknown channel"), "")
            bot = SimpleNamespace(db=db, support_tickets=tickets, fetch_channel=AsyncMock(side_effect=failed))
            cog = SupportShopCog(bot)
            channel = Mock(spec=discord.TextChannel, id=3)
            channel.delete = AsyncMock(side_effect=[failed, missing if already_deleted else None])
            guild = Mock(spec=discord.Guild, id=1)
            guild.get_channel.return_value = None
            interaction = SimpleNamespace(guild=guild, user=Mock(spec=discord.Member, id=2), channel=channel)
            cog._safe_reply = AsyncMock()
            cog._log_ticket_close = AsyncMock()
            await cog.close_ticket_from_button(interaction)
            assert await tickets.get_active_by_channel(db, 1, 3)
            cog._log_ticket_close.assert_not_called()
            existing, resolved = await cog._get_open_ticket_channel(guild, 2)
            assert existing.channel_id == 3 and resolved is None
            await cog.close_ticket_from_button(interaction)
            assert await tickets.get_active_by_channel(db, 1, 3) is None
            cog._log_ticket_close.assert_awaited_once()

    asyncio.run(scenario())

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.ai_chat import AIChatCog


def test_history_filters_by_both_permissions_before_applying_limit():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            db.row_factory = aiosqlite.Row
            cog = AIChatCog(SimpleNamespace(db=db))
            await cog.init_db()
            channels = []
            for channel_id, view, history in [(1, True, True), (2, False, True), (3, True, False)]:
                channel = Mock(spec=discord.TextChannel, id=channel_id)
                channel.permissions_for.return_value = discord.Permissions(view_channel=view, read_message_history=history)
                channels.append(channel)

            rows = [(1, 1, 7, 1, "sample visible", datetime.now(UTC).isoformat())]
            for index in range(12):
                rows.append((1, 2 + index % 2, 7, index + 2, "sample restricted", datetime.now(UTC).isoformat()))
            rows.append((1, 4, 7, 15, "sample missing channel", datetime.now(UTC).isoformat()))
            await db.executemany(
                "INSERT INTO ai_message_memory(guild_id,channel_id,user_id,message_id,content_clean,created_at) VALUES(?,?,?,?,?,?)",
                rows,
            )
            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=1, text_channels=channels), user=Mock(spec=discord.Member),
                response=SimpleNamespace(send_message=AsyncMock()),
            )
            await cog.ai_history.callback(cog, interaction, "sample")
            reply = interaction.response.send_message.await_args
            assert reply.kwargs["ephemeral"]
            assert "sample visible" in reply.args[0]
            assert "restricted" not in reply.args[0] and "missing channel" not in reply.args[0]
            relation = await cog.get_relation(1, 7)
            context = await cog.build_context(1, 1, 7, relation)
            assert "sample visible" in context
            assert "restricted" not in context and "missing channel" not in context

    asyncio.run(scenario())

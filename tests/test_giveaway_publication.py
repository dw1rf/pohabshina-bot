import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.giveaways import GiveawaysCog
from utils.brand import BRAND_COLOR


def test_failed_publication_retries_same_message_without_blocking_writers(tmp_path):
    async def scenario():
        path = tmp_path / "bot.db"
        async with aiosqlite.connect(path) as db, aiosqlite.connect(path, timeout=0.1) as other:
            db.row_factory = aiosqlite.Row
            channel = Mock(spec=discord.TextChannel, id=7)
            bot = SimpleNamespace(giveaway_db=db, get_channel=lambda _: channel,
                                  wait_until_ready=asyncio.Event().wait, add_view=Mock())
            cog = GiveawaysCog(bot)
            await cog.cog_load()
            await cog.cog_unload()

            async def unavailable(**kwargs):
                assert kwargs["embed"].to_dict()["color"] == BRAND_COLOR
                await other.execute("CREATE TABLE unrelated (id INTEGER)")
                await other.commit()
                raise discord.HTTPException(SimpleNamespace(status=503, reason="unavailable"), "")

            message = channel.get_partial_message.return_value
            message.edit = AsyncMock(side_effect=unavailable)
            interaction = SimpleNamespace(
                channel=channel, guild=SimpleNamespace(id=2), user=SimpleNamespace(id=5),
                response=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=32))),
            )
            await cog.create.callback(cog, interaction, "Приз", 10)
            row = await (await db.execute("SELECT * FROM giveaways")).fetchone()
            assert (row["status"], row["message_id"]) == ("pending", 32)
            assert not db.in_transaction
            assert await cog._finish(row["giveaway_id"], 2, reroll=True) == []

            # Resume after an interrupted publication using the persisted message ID.
            restored = GiveawaysCog(bot)
            message.edit = AsyncMock()
            await restored.finish_loop()
            await restored.finish_loop()
            message.edit.assert_awaited_once()
            assert message.edit.await_args.kwargs["view"].children[0].custom_id.endswith(f":{row['giveaway_id']}")
            assert channel.get_partial_message.call_args.args == (32,)
            interaction.response.send_message.assert_awaited_once()
            assert (await (await db.execute("SELECT status FROM giveaways")).fetchone())[0] == "active"

            await db.execute("""CREATE TRIGGER reject_new BEFORE INSERT ON giveaways
                                BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
            await db.commit()
            interaction.edit_original_response = AsyncMock()
            await cog.create.callback(cog, interaction, "Новый приз", 10)
            assert "не удалось" in interaction.edit_original_response.await_args.kwargs["content"]
            assert not db.in_transaction
            await other.execute("INSERT INTO unrelated VALUES (1)")
            await other.commit()

    asyncio.run(scenario())

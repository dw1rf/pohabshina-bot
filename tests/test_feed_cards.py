import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.starboard import StarboardCog
from utils.brand import BRAND_COLOR


def test_starboard_publishes_public_card_and_skips_restricted_source():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            db.row_factory = aiosqlite.Row
            source, target = Mock(spec=discord.TextChannel), Mock(spec=discord.TextChannel)
            source.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
            source.is_nsfw.return_value = target.is_nsfw.return_value = False
            source.overwrites = target.overwrites = {}
            source.fetch_message = AsyncMock(return_value=SimpleNamespace(
                author=SimpleNamespace(bot=False, id=9, display_name="Listener", display_avatar=SimpleNamespace(url="")),
                reactions=[SimpleNamespace(emoji="⭐", count=3)], content="Message", attachments=[], jump_url="https://example.com/message",
            ))
            target.send = AsyncMock(return_value=SimpleNamespace(id=5))
            guild = SimpleNamespace(get_channel={2: source, 3: target}.get, default_role=Mock())
            bot = SimpleNamespace(db=db, get_guild=lambda _: guild, progression_db=db, dispatch=Mock(),
                                  progression=SimpleNamespace(record_event=AsyncMock()))
            cog = StarboardCog(bot)
            await cog.cog_load()
            await db.execute("INSERT INTO starboard_settings(guild_id,channel_id) VALUES (1,3)")
            await db.commit()
            voter = SimpleNamespace(id=7, bot=False)
            payload = SimpleNamespace(guild_id=1, channel_id=2, message_id=4, emoji="⭐", member=voter)
            await cog.on_raw_reaction_add(payload)
            assert target.send.await_args.kwargs["embed"].to_dict()["color"] == BRAND_COLOR
            source.fetch_message.return_value.reactions[0].count = 1
            payload.message_id = 5
            await cog.on_raw_reaction_add(payload)
            await cog.on_raw_reaction_remove(payload)
            voter.bot = True
            await cog.on_raw_reaction_add(payload)
            voter.bot = False
            source.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            await cog.on_raw_reaction_add(payload)
            target.send.assert_awaited_once()
            assert bot.dispatch.call_count == 2
            bot.dispatch.assert_called_with("module_activity", 1, 7, "starboard", "reaction")

    asyncio.run(scenario())

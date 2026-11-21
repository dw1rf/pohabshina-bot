import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.anime_feeds import AnimeFeedsCog
from cogs.starboard import StarboardCog
from utils.brand import BRAND_COLOR, BRAND_PURPLE


def test_anime_feed_publishes_purple_card():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            db.row_factory = aiosqlite.Row
            channel = Mock(spec=discord.TextChannel)
            channel.send = AsyncMock()
            bot = SimpleNamespace(db=db, wait_until_ready=asyncio.Event().wait, get_channel=lambda _: channel)
            cog = AnimeFeedsCog(bot)
            await cog.cog_load()
            await cog.cog_unload()
            await db.execute("INSERT INTO anime_feed_subscriptions(guild_id,channel_id) VALUES (1,2)")
            await db.commit()
            cog._fetch_schedule = AsyncMock(return_value=[{"id": 4, "airingAt": 1, "media": {"title": {"english": "Show"}}}])
            cog._already_posted = AsyncMock(return_value=False)
            await cog.poll_airing()
            channel.send.assert_awaited_once()
            assert channel.send.await_args.kwargs["embed"].colour.to_rgb() == BRAND_PURPLE

    asyncio.run(scenario())


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
            bot = SimpleNamespace(db=db, get_guild=lambda _: guild, progression_db=db,
                                  progression=SimpleNamespace(record_event=AsyncMock()))
            cog = StarboardCog(bot)
            await cog.cog_load()
            await db.execute("INSERT INTO starboard_settings(guild_id,channel_id) VALUES (1,3)")
            await db.commit()
            await cog._refresh(1, 2, 4)
            assert target.send.await_args.kwargs["embed"].to_dict()["color"] == BRAND_COLOR
            source.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            await cog._refresh(1, 2, 6)
            target.send.assert_awaited_once()

    asyncio.run(scenario())

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from utils.brand import BRAND_PURPLE

logger = logging.getLogger(__name__)
ANILIST_URL = "https://graphql.anilist.co"
AIRING_QUERY = """
query ($from: Int!, $to: Int!) {
  Page(page: 1, perPage: 50) {
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to, sort: TIME) {
      id episode airingAt
      media { id title { romaji english native } siteUrl coverImage { large } }
    }
  }
}
"""


class AnimeFeedsCog(commands.Cog):
    anime_group = app_commands.Group(
        name="anime_feed",
        description="Уведомления о новых эпизодах AniList",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.db is None:
            return
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS anime_feed_subscriptions (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS anime_feed_deliveries (
                guild_id INTEGER NOT NULL,
                airing_schedule_id INTEGER NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(guild_id, airing_schedule_id)
            );
            """
        )
        await self.bot.db.commit()
        if not self.poll_airing.is_running():
            self.poll_airing.start()

    async def cog_unload(self) -> None:
        self.poll_airing.cancel()

    @anime_group.command(name="set", description="Включить уведомления в выбранном канале")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def configure(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute(
            """INSERT INTO anime_feed_subscriptions(guild_id, channel_id, enabled) VALUES (?, ?, 1)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, enabled=1""",
            (interaction.guild.id, channel.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"Anime feed включён в {channel.mention}.", ephemeral=True)

    @anime_group.command(name="off", description="Выключить anime feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE anime_feed_subscriptions SET enabled = 0 WHERE guild_id = ?", (interaction.guild.id,))
        await self.bot.db.commit()
        await interaction.response.send_message("Anime feed выключен.", ephemeral=True)

    @anime_group.command(name="status", description="Показать состояние anime feed")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT * FROM anime_feed_subscriptions WHERE guild_id = ?", (interaction.guild.id,))
        row = await cursor.fetchone()
        if row is None:
            await interaction.response.send_message("Anime feed не настроен.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Канал: <#{row['channel_id']}>\nСтатус: {'включён' if row['enabled'] else 'выключен'}", ephemeral=True
        )

    async def _fetch_schedule(self) -> list[dict]:
        if self.bot.session is None:
            return []
        now = int(time.time()) - 15 * 60
        horizon = int(time.time()) + 15 * 60
        async with self.bot.session.post(
            ANILIST_URL,
            json={"query": AIRING_QUERY, "variables": {"from": now, "to": horizon}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        return payload.get("data", {}).get("Page", {}).get("airingSchedules", [])

    @staticmethod
    async def _already_posted(channel: discord.TextChannel, marker: str) -> bool:
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).read_message_history:
            return False
        try:
            async for message in channel.history(limit=50):
                if any(embed.footer and embed.footer.text == marker for embed in message.embeds):
                    return True
        except (discord.Forbidden, discord.HTTPException):
            return False
        return False

    @tasks.loop(minutes=15)
    async def poll_airing(self) -> None:
        if self.bot.db is None:
            return
        try:
            schedule = await self._fetch_schedule()
        except Exception:
            logger.exception("AniList schedule request failed")
            return
        cursor = await self.bot.db.execute("SELECT guild_id, channel_id FROM anime_feed_subscriptions WHERE enabled = 1")
        subscriptions = await cursor.fetchall()
        current = int(time.time())
        for subscription in subscriptions:
            guild_id = int(subscription["guild_id"])
            channel = self.bot.get_channel(int(subscription["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            for airing in schedule:
                if int(airing.get("airingAt", 0)) > current:
                    continue
                schedule_id = int(airing["id"])
                cursor = await self.bot.db.execute(
                    "SELECT 1 FROM anime_feed_deliveries WHERE guild_id = ? AND airing_schedule_id = ?",
                    (guild_id, schedule_id),
                )
                if await cursor.fetchone():
                    continue
                media = airing.get("media") or {}
                marker = f"AniList airing #{schedule_id}"
                if await self._already_posted(channel, marker):
                    await self.bot.db.execute(
                        "INSERT OR IGNORE INTO anime_feed_deliveries(guild_id, airing_schedule_id) VALUES (?, ?)",
                        (guild_id, schedule_id),
                    )
                    await self.bot.db.commit()
                    continue
                titles = media.get("title") or {}
                title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Anime"
                embed = discord.Embed(
                    title=title,
                    url=media.get("siteUrl"),
                    description=f"Вышел эпизод **{airing.get('episode', '?')}**.",
                    color=BRAND_PURPLE,
                )
                cover = (media.get("coverImage") or {}).get("large")
                if cover:
                    embed.set_thumbnail(url=cover)
                embed.set_footer(text=marker)
                try:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not publish AniList feed in channel %s", channel.id)
                    continue
                await self.bot.db.execute(
                    "INSERT OR IGNORE INTO anime_feed_deliveries(guild_id, airing_schedule_id) VALUES (?, ?)",
                    (guild_id, schedule_id),
                )
                await self.bot.db.commit()

    @poll_airing.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(AnimeFeedsCog(bot))

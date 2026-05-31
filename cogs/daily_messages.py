from __future__ import annotations

import logging
import random
from datetime import time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from bot_client import MovieBot

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class DailyMessagesCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.morning_message_loop.start()
        self.night_message_loop.start()

    def cog_unload(self) -> None:
        self.morning_message_loop.cancel()
        self.night_message_loop.cancel()

    @tasks.loop(time=time(hour=8, minute=0, tzinfo=MOSCOW_TZ))
    async def morning_message_loop(self) -> None:
        await self._send_daily_message(
            kind="morning",
            title="╭──── 🌞 ДОБРОЕ УТРО 🌞 ────╮",
            color=discord.Color.gold(),
        )

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=MOSCOW_TZ))
    async def night_message_loop(self) -> None:
        await self._send_daily_message(
            kind="night",
            title="╭──── 🌙 СПОКОЙНОЙ НОЧИ 🌙 ────╮",
            color=discord.Color.dark_purple(),
        )

    @morning_message_loop.before_loop
    @night_message_loop.before_loop
    async def before_daily_message_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_daily_message(self, *, kind: str, title: str, color: discord.Color) -> None:
        channel_id = self.bot.settings.daily_message_channel_id
        if not channel_id:
            logger.info("Daily %s message skipped: DAILY_MESSAGE_CHANNEL_ID is not set", kind)
            return

        channel = await self._resolve_channel(channel_id)
        if channel is None:
            logger.warning("Daily %s message skipped: channel not found or not sendable id=%s", kind, channel_id)
            return

        content = self.bot.engagement_content
        messages = content.list(f"{kind}_messages")
        gifs = content.list(f"{kind}_gifs")
        images = content.list(f"{kind}_images")
        text = random.choice(messages) if messages else ""
        gif_url = random.choice(gifs) if gifs else None
        image_url = random.choice(images) if images else None

        embed = discord.Embed(
            title=title,
            description=f"{text}\n\n╰────────────────────────╯",
            color=color,
        )
        if gif_url:
            embed.set_image(url=gif_url)
        if image_url:
            embed.set_thumbnail(url=image_url)

        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to send daily %s message to channel=%s: %s", kind, channel_id, exc)
            logger.debug("Daily %s message traceback", kind, exc_info=True)
            return

        logger.info("Daily %s message sent: channel=%s", kind, channel_id)

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        cached = self.bot.get_channel(channel_id)
        if cached is None:
            try:
                cached = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to fetch daily message channel %s: %s", channel_id, exc)
                logger.debug("Daily message channel fetch traceback", exc_info=True)
                return None

        if isinstance(cached, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return cached
        return None


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(DailyMessagesCog(bot))

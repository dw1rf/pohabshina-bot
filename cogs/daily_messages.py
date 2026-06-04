from __future__ import annotations

import logging
import random
from datetime import time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


DAILY_VARIANTS = {
    "morning": {
        "title": "╭──── 🌞 ДОБРОЕ УТРО 🌞 ────╮",
        "color": discord.Color.gold(),
    },
    "night": {
        "title": "╭──── 🌙 СПОКОЙНОЙ НОЧИ 🌙 ────╮",
        "color": discord.Color.dark_purple(),
    },
}


class DailyMessagesCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.morning_message_loop.start()
        self.night_message_loop.start()
        logger.info(
            "Daily messages cog loaded: channel=%s timezone=Europe/Moscow morning=08:00 night=00:00",
            self.bot.settings.daily_message_channel_id or "disabled",
        )

    def cog_unload(self) -> None:
        self.morning_message_loop.cancel()
        self.night_message_loop.cancel()

    @tasks.loop(time=time(hour=8, minute=0, tzinfo=MOSCOW_TZ))
    async def morning_message_loop(self) -> None:
        await self._send_daily_message(kind="morning")

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=MOSCOW_TZ))
    async def night_message_loop(self) -> None:
        await self._send_daily_message(kind="night")

    @morning_message_loop.before_loop
    @night_message_loop.before_loop
    async def before_daily_message_loop(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="daily_test", description="Отправить тестовое daily-сообщение в настроенный канал")
    @app_commands.describe(kind="Какое сообщение отправить")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Утро", value="morning"),
            app_commands.Choice(name="Ночь", value="night"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def daily_test(self, interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        sent = await self._send_daily_message(kind=kind.value, source=f"manual user={interaction.user.id}")
        if sent:
            await interaction.followup.send("Тестовое daily-сообщение отправлено.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Не удалось отправить daily-сообщение. Проверьте DAILY_MESSAGE_CHANNEL_ID, права бота и логи.",
                ephemeral=True,
            )

    async def _send_daily_message(self, *, kind: str, source: str = "schedule") -> bool:
        variant = DAILY_VARIANTS[kind]
        channel_id = self.bot.settings.daily_message_channel_id
        if not channel_id:
            logger.info("Daily %s message skipped: DAILY_MESSAGE_CHANNEL_ID is not set source=%s", kind, source)
            return False

        channel = await self._resolve_channel(channel_id)
        if channel is None:
            logger.warning(
                "Daily %s message skipped: channel not found or not sendable id=%s source=%s",
                kind,
                channel_id,
                source,
            )
            return False

        content = self.bot.engagement_content
        messages = content.list(f"{kind}_messages")
        gifs = content.list(f"{kind}_gifs")
        images = content.list(f"{kind}_images")
        text = random.choice(messages) if messages else ""
        gif_url = random.choice(gifs) if gifs else None
        image_url = random.choice(images) if images else None

        embed = discord.Embed(
            title=str(variant["title"]),
            description=f"{text}\n\n╰────────────────────────╯",
            color=variant["color"],
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
            logger.warning(
                "Failed to send daily %s message to channel=%s source=%s: %s",
                kind,
                channel_id,
                source,
                exc,
            )
            logger.debug("Daily %s message traceback", kind, exc_info=True)
            return False

        logger.info("Daily %s message sent: channel=%s source=%s", kind, channel_id, source)
        return True

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
        logger.warning("Daily message channel has unsupported type: id=%s type=%s", channel_id, type(cached).__name__)
        return None


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(DailyMessagesCog(bot))

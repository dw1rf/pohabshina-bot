from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from services.community_ops_service import CommunityOpsService, render_weekly_digest

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def _can_manage(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


class WeeklyDigestCog(commands.Cog):
    digest_group = app_commands.Group(
        name="digest", description="Еженедельная статистика сервера"
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.service = CommunityOpsService()

    async def cog_load(self) -> None:
        if self.bot.digest_db is None:
            return
        await self.service.init_db(self.bot.digest_db)
        if not self.delivery_loop.is_running():
            self.delivery_loop.start()

    async def cog_unload(self) -> None:
        self.delivery_loop.cancel()

    @digest_group.command(name="setup", description="Настроить еженедельный отчёт")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Канал для отчёта",
        weekday="День недели: 0 — понедельник, 6 — воскресенье",
        hour_utc="Час отправки по UTC",
    )
    async def setup_digest(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        weekday: app_commands.Range[int, 0, 6] = 0,
        hour_utc: app_commands.Range[int, 0, 23] = 9,
    ) -> None:
        if interaction.guild is None or self.bot.digest_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.set_digest(
            self.bot.digest_db,
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            weekday=int(weekday),
            hour_utc=int(hour_utc),
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Отчёт будет отправляться: {WEEKDAYS[int(weekday)]}, {int(hour_utc):02d}:00 UTC, канал #{channel.name}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @digest_group.command(name="status", description="Показать расписание отчёта")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.digest_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        settings = await self.service.digest_settings(self.bot.digest_db, interaction.guild.id)
        if settings is None or not int(settings["enabled"]):
            text = "Еженедельный отчёт выключен."
        else:
            day = WEEKDAYS[int(settings["weekday"])]
            text = (
                f"Отчёт включён: {day}, {int(settings['hour_utc']):02d}:00 UTC, "
                f"канал ID {int(settings['channel_id'])}."
            )
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @digest_group.command(name="preview", description="Предпросмотр отчёта без публикации")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.digest_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await interaction.response.defer(ephemeral=True)
        stats = await self.service.collect_weekly_stats(
            self.bot.digest_db, guild_id=interaction.guild.id
        )
        image = render_weekly_digest(interaction.guild.name, stats)
        file = discord.File(
            image,
            filename="weekly-digest-preview.png",
            description="Еженедельная статистика сервера",
        )
        await interaction.followup.send(file=file, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @digest_group.command(name="off", description="Выключить еженедельный отчёт")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable_digest(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.digest_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.disable_digest(self.bot.digest_db, interaction.guild.id)
        await interaction.response.send_message(
            "Еженедельный отчёт выключен.", ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def deliver_pending(self) -> None:
        if self.bot.digest_db is None:
            return
        for settings in await self.service.pending_digests(self.bot.digest_db):
            guild_id = int(settings["guild_id"])
            if not await self.service.claim_digest_delivery(self.bot.digest_db, guild_id=guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(int(settings["channel_id"])) if guild is not None else None
            if guild is None or not isinstance(channel, discord.TextChannel):
                await self.service.release_digest_claim(self.bot.digest_db, guild_id=guild_id)
                logger.warning("Weekly digest channel unavailable: guild=%s", guild_id)
                continue
            try:
                iso_year, iso_week, _ = datetime.now(UTC).isocalendar()
                filename = f"weekly-digest-{guild_id}-{iso_year}-W{iso_week:02d}.png"
                existing_message_id = await self._find_existing_delivery(channel, filename)
                if existing_message_id is not None:
                    await self.service.complete_digest_delivery(
                        self.bot.digest_db, guild_id=guild_id, message_id=existing_message_id
                    )
                    continue
                stats = await self.service.collect_weekly_stats(self.bot.digest_db, guild_id=guild_id)
                file = discord.File(
                    render_weekly_digest(guild.name, stats),
                    filename=filename,
                    description="Еженедельная статистика сервера",
                )
                message = await channel.send(file=file, allowed_mentions=NO_MENTIONS)
            except (discord.Forbidden, discord.HTTPException, OSError):
                await self.service.release_digest_claim(self.bot.digest_db, guild_id=guild_id)
                logger.exception("Could not publish weekly digest: guild=%s", guild_id)
                continue
            await self.service.complete_digest_delivery(
                self.bot.digest_db, guild_id=guild_id, message_id=message.id
            )

    @staticmethod
    async def _find_existing_delivery(
        channel: discord.TextChannel, filename: str
    ) -> int | None:
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).read_message_history:
            return None
        try:
            async for message in channel.history(limit=100):
                if any(attachment.filename == filename for attachment in message.attachments):
                    return message.id
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not reconcile digest history: channel=%s", channel.id)
        return None

    @tasks.loop(minutes=15)
    async def delivery_loop(self) -> None:
        try:
            await self.deliver_pending()
        except Exception:
            logger.exception("Weekly digest iteration failed; it will retry on the next interval")

    @delivery_loop.before_loop
    async def before_delivery_loop(self) -> None:
        await self.bot.wait_until_ready()

    @delivery_loop.error
    async def delivery_loop_error(self, error: BaseException) -> None:
        logger.exception("Weekly digest loop failed; it will retry on the next interval", exc_info=error)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(WeeklyDigestCog(bot))

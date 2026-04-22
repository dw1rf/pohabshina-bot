from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.embeds import parse_color
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access

logger = logging.getLogger(__name__)


class RelayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not self.bot.settings.target_channel_id:
            logger.warning("TARGET_CHANNEL_ID is not configured. /say and relay forwarding are disabled.")
        if not self.bot.settings.control_channel_id:
            logger.warning("CONTROL_CHANNEL_ID is not configured. control-channel relay is disabled.")

    async def _get_target_channel(self) -> discord.TextChannel | None:
        target_id = self.bot.settings.target_channel_id
        if not target_id:
            return None
        channel = self.bot.get_channel(target_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target_id)
            except Exception:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _forward_attachments(self, attachments: list[discord.Attachment]) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in attachments:
            try:
                files.append(await attachment_to_file(attachment))
            except Exception as exc:
                logger.exception("Failed to download attachment %s: %s", attachment.filename, exc)
        return files

    def _has_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    @app_commands.command(name="say", description="Отправить сообщение от имени бота в целевой канал")
    async def say(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        image1: discord.Attachment | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
    ) -> None:
        if not self._has_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say.", ephemeral=True)
            return

        target = await self._get_target_channel()
        if target is None:
            await interaction.response.send_message("TARGET_CHANNEL_ID не задан или канал недоступен.", ephemeral=True)
            return

        files = await self._forward_attachments([a for a in (image1, image2, image3) if a is not None])
        if not text and not files:
            await interaction.response.send_message("Нужно указать текст или хотя бы одно вложение.", ephemeral=True)
            return

        await target.send(content=text or None, files=files or None)
        await interaction.response.send_message("✅ Сообщение отправлено в целевой канал.", ephemeral=True)

    @app_commands.command(name="say_embed", description="Отправить embed от имени бота в целевой канал")
    async def say_embed(
        self,
        interaction: discord.Interaction,
        title: str,
        text: str,
        image: discord.Attachment | None = None,
        color: str | None = None,
    ) -> None:
        if not self._has_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say_embed.", ephemeral=True)
            return

        target = await self._get_target_channel()
        if target is None:
            await interaction.response.send_message("TARGET_CHANNEL_ID не задан или канал недоступен.", ephemeral=True)
            return

        embed = discord.Embed(title=title, description=text, color=parse_color(color))
        files: list[discord.File] = []
        if image:
            files = await self._forward_attachments([image])
            if files:
                embed.set_image(url=f"attachment://{files[0].filename}")

        await target.send(embed=embed, files=files or None)
        await interaction.response.send_message("✅ Embed отправлен в целевой канал.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.bot.settings.control_channel_id or message.channel.id != self.bot.settings.control_channel_id:
            return
        if not self._has_access(message.author):
            return

        target = await self._get_target_channel()
        if target is None:
            logger.warning("Skip forwarding: target channel is unavailable")
            return

        files = await self._forward_attachments(list(message.attachments))
        if not message.content and not files:
            return

        await target.send(content=message.content or None, files=files or None)
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        if self.bot.settings.delete_control_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.warning("Failed to delete control message %s", message.id)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RelayCog(bot))

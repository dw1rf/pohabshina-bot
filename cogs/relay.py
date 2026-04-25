import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.embeds import parse_color_strict
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access

logger = logging.getLogger(__name__)


class RelayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _get_target_channel(self, channel: discord.TextChannel | None) -> discord.TextChannel | None:
        if channel is not None:
            return channel

        if not self.bot.settings.target_channel_id:
            return None

        found = self.bot.get_channel(self.bot.settings.target_channel_id)
        if isinstance(found, discord.TextChannel):
            return found

        try:
            fetched = await self.bot.fetch_channel(self.bot.settings.target_channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Failed to fetch target channel %s", self.bot.settings.target_channel_id)
            return None

        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _forward_attachments(self, attachments: list[discord.Attachment]) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in attachments:
            file = await attachment_to_file(attachment)
            if file is not None:
                files.append(file)
        return files

    def _has_relay_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    def _has_say_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and member.guild_permissions.administrator

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    @staticmethod
    def _has_send_permissions(
        channel: discord.TextChannel | discord.Thread,
        me: discord.Member,
        needs_embed: bool = False,
    ) -> bool:
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            return False
        if needs_embed and not perms.embed_links:
            return False
        return True

    async def _safe_send(
        self,
        channel: discord.TextChannel | discord.Thread,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        files: list[discord.File] | None = None,
    ) -> bool:
        try:
            await channel.send(content=content, embed=embed, files=files or [])
            return True
        except discord.HTTPException as exc:
            logger.exception("Failed to send message into channel %s: %s", channel.id, exc)
            return False

    @app_commands.command(name="say", description="Отправить сообщение от имени бота в текущий канал")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def say(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        if not self._has_say_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Команда доступна только в текстовых каналах сервера.", ephemeral=True)
            return

        me = self._bot_member_in_guild(guild)
        if me is None or not self._has_send_permissions(channel, me):
            await interaction.response.send_message("У бота нет прав отправки сообщений в текущий канал.", ephemeral=True)
            return

        sent = await self._safe_send(channel, content=text)
        if not sent:
            await interaction.response.send_message("Не удалось отправить сообщение. Проверьте права бота.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)

    @app_commands.command(name="say_embed", description="Отправить embed от имени бота в текущий канал")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def say_embed(
        self,
        interaction: discord.Interaction,
        title: str | None = None,
        description: str | None = None,
        text: str | None = None,
        color: str | None = None,
        image_url: str | None = None,
        thumbnail_url: str | None = None,
        footer: str | None = None,
    ) -> None:
        if not self._has_say_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say_embed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Команда доступна только в текстовых каналах сервера.", ephemeral=True)
            return

        me = self._bot_member_in_guild(guild)
        if me is None or not self._has_send_permissions(channel, me, needs_embed=True):
            await interaction.response.send_message("У бота нет прав отправки embed в текущий канал.", ephemeral=True)
            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

        embed_description = description or text
        if not embed_description:
            await interaction.response.send_message("Для /say_embed нужно указать description или text.", ephemeral=True)
            return

        embed = discord.Embed(
            title=title,
            description=embed_description,
            color=parsed_color or discord.Color.blurple(),
        )
        if image_url:
            embed.set_image(url=image_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if footer:
            embed.set_footer(text=footer)

        sent = await self._safe_send(channel, embed=embed)
        if not sent:
            await interaction.response.send_message("Не удалось отправить embed. Проверьте права бота и параметры.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RelayCog(bot))
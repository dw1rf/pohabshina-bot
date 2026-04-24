from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.embeds import parse_color, parse_color_strict
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access, has_elevated_permissions
from utils.permissions import has_bot_relay_access


logger = logging.getLogger(__name__)


class RelayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not self.bot.settings.target_channel_id:
            logger.warning("TARGET_CHANNEL_ID is not configured. /say, /say_embed and forwarding are disabled.")
        if not self.bot.settings.control_channel_id:
            logger.warning("CONTROL_CHANNEL_ID is not configured. control-channel forwarding is disabled.")

    async def _get_target_channel(self, override: discord.TextChannel | None = None) -> discord.TextChannel | None:
        if override is not None:
            return override

    async def _get_target_channel(self) -> discord.TextChannel | None:

        target_id = self.bot.settings.target_channel_id
        if not target_id:
            return None

        channel = self.bot.get_channel(target_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target_id)
            except Exception as exc:
                logger.warning("Failed to fetch target channel %s: %s", target_id, exc)
                return None

        return channel if isinstance(channel, discord.TextChannel) else None

    async def _forward_attachments(self, attachments: list[discord.Attachment]) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in attachments:
            try:
                files.append(await attachment_to_file(attachment))
            except Exception as exc:
                logger.exception("Failed to download attachment %s (%s)", attachment.filename, exc)
        return files

    def _has_relay_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    def _has_say_access(self, member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return has_elevated_permissions(member) or has_bot_relay_access(member, self.bot.settings)
    def _has_access(self, member: discord.Member | discord.User) -> bool:
        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    def _bot_member_in_guild(self, guild: discord.Guild) -> discord.Member | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        return guild.me or guild.get_member(bot_user.id)

    @staticmethod
    def _has_send_permissions(channel: discord.TextChannel, me: discord.Member, needs_embed: bool = False) -> bool:
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            return False
        if needs_embed and not perms.embed_links:
            return False
        return True

    async def _safe_send(
        self,
        channel: discord.TextChannel,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        files: list[discord.File] | None = None,
    ) -> bool:
        try:
            await channel.send(content=content, embed=embed, files=files or None)
            return True
        except discord.HTTPException as exc:
            logger.exception("Failed to send message into channel %s: %s", channel.id, exc)
            return False

    @app_commands.command(name="say", description="Отправить сообщение/embed от имени бота в выбранный или целевой канал")
    @app_commands.command(name="say", description="Отправить сообщение/embed от имени бота в выбранный или целевой канал")
    @app_commands.command(name="say", description="Отправить сообщение от имени бота в целевой канал")

    @app_commands.command(name="say", description="Отправить сообщение/embed от имени бота в выбранный или целевой канал")
    async def say(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        channel: discord.TextChannel | None = None,
        title: str | None = None,
        description: str | None = None,
        color: str | None = None,
        image: discord.Attachment | None = None,
        thumbnail: discord.Attachment | None = None,
        footer: str | None = None,
        author: str | None = None,
        image1: discord.Attachment | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
    ) -> None:
        if not self._has_say_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say.", ephemeral=True)
            return

        target = await self._get_target_channel(channel)
        target = await self._get_target_channel()

        if target is None:
            await interaction.response.send_message("TARGET_CHANNEL_ID не задан или канал недоступен.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        me = self._bot_member_in_guild(guild)
        use_embed = any((title, description, color, image, thumbnail, footer, author))
        if me is None or not self._has_send_permissions(target, me, needs_embed=use_embed):
            await interaction.response.send_message("У бота нет прав отправки сообщений/embed в указанный канал.", ephemeral=True)
            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

            return

            return

            return

            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return


        files = await self._forward_attachments([a for a in (image1, image2, image3) if a is not None])
        embed: discord.Embed | None = None

        if use_embed:
            embed = discord.Embed(
                title=title,
                description=description,
                color=parsed_color or parse_color(None),
            )

            embed_files = await self._forward_attachments([a for a in (image, thumbnail) if a is not None])
            files.extend(embed_files)

            if image and embed_files:
                embed.set_image(url=f"attachment://{embed_files[0].filename}")
            if thumbnail:
                thumb_index = 1 if image else 0
                if len(embed_files) > thumb_index:
                    embed.set_thumbnail(url=f"attachment://{embed_files[thumb_index].filename}")
            if footer:
                embed.set_footer(text=footer)
            if author:
                embed.set_author(name=author)

        if not text and not embed and not files:
            await interaction.response.send_message("Укажите текст, embed-поля или вложения.", ephemeral=True)
            return

        sent = await self._safe_send(target, content=text or None, embed=embed, files=files)
        if not sent:
            await interaction.response.send_message("Не удалось отправить сообщение. Проверьте права бота и вложения.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
        try:
            await target.send(content=text or None, embed=embed, files=files or None)
        except discord.HTTPException as exc:
            logger.exception("Failed to send /say payload into channel %s: %s", target.id, exc)
            await interaction.response.send_message("Не удалось отправить сообщение. Проверьте права бота и вложения.", ephemeral=True)
            return

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)
        if me is None or not self._has_send_permissions(target, me):
            await interaction.response.send_message("У бота нет прав отправки сообщений в целевой канал.", ephemeral=True)
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
        await self.say(
            interaction=interaction,
            text=None,
            channel=None,
            title=title,
            description=text,
            color=color,
            image=image,
        )
        if not self._has_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say_embed.", ephemeral=True)
            return

        target = await self._get_target_channel()
        if target is None:
            await interaction.response.send_message("TARGET_CHANNEL_ID не задан или канал недоступен.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        me = self._bot_member_in_guild(guild)
        if me is None or not self._has_send_permissions(target, me, needs_embed=True):
            await interaction.response.send_message("У бота нет прав отправки embed в целевой канал.", ephemeral=True)
            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

        embed = discord.Embed(title=title, description=text, color=parsed_color or parse_color(None))

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

        if not self.bot.settings.control_channel_id:
            return
        if not self._has_relay_access(message.author):

        if message.channel.id != self.bot.settings.control_channel_id:
            return
        if not self._has_relay_access(message.author):

        if not self._has_access(message.author):

            return

        target = await self._get_target_channel()
        if target is None:
            logger.warning("Skip forwarding message %s: target channel unavailable", message.id)
            return
        if target.id == message.channel.id:
            logger.warning("CONTROL_CHANNEL_ID and TARGET_CHANNEL_ID are equal (%s). Skip forwarding.", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        sent = await self._safe_send(target, content=message.content or None, files=files)
        if not sent:
        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)

            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        sent = await self._safe_send(target, content=message.content or None, files=files)
        if not sent:
        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        sent = await self._safe_send(target, content=message.content or None, files=files)
        if not sent:
        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        sent = await self._safe_send(target, content=message.content or None, files=files)
        if not sent:
        if target.id == message.channel.id:
            logger.warning("CONTROL_CHANNEL_ID and TARGET_CHANNEL_ID are equal (%s). Skip forwarding.", target.id)
            return

        me = self._bot_member_in_guild(message.guild)
        if me is None or not self._has_send_permissions(target, me):
            logger.warning("Bot has no send permissions in target channel %s", target.id)
            return

        files = await self._forward_attachments(list(message.attachments))
        if not message.content and not files:
            return

        try:
            await target.send(content=message.content or None, files=files or None)
        except discord.HTTPException as exc:
            logger.exception("Failed to forward message %s: %s", message.id, exc)

            return

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            logger.debug("Failed to add confirmation reaction for control message %s", message.id)

        if self.bot.settings.delete_control_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.warning("Failed to delete control message %s", message.id)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RelayCog(bot))

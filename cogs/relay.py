from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
codex/refactor-discord-bot-structure-and-add-features-h7krw8
from utils.embeds import parse_color, parse_color_strict
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access, has_elevated_permissions

from utils.embeds import parse_color
from utils.helpers import attachment_to_file
from utils.permissions import has_bot_relay_access
main

logger = logging.getLogger(__name__)


class RelayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not self.bot.settings.target_channel_id:
            logger.warning("TARGET_CHANNEL_ID is not configured. /say and relay forwarding are disabled.")
        if not self.bot.settings.control_channel_id:
            logger.warning("CONTROL_CHANNEL_ID is not configured. control-channel relay is disabled.")

codex/refactor-discord-bot-structure-and-add-features-h7krw8
    async def _get_target_channel(self, override: discord.TextChannel | None = None) -> discord.TextChannel | None:
        if override is not None:
            return override

    async def _get_target_channel(self) -> discord.TextChannel | None:
main
        target_id = self.bot.settings.target_channel_id
        if not target_id:
            return None
        channel = self.bot.get_channel(target_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target_id)
 codex/refactor-discord-bot-structure-and-add-features-h7krw8
            except Exception as exc:
                logger.warning("Failed to fetch target channel %s: %s", target_id, exc)

            except Exception:
 main
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
 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        if not isinstance(member, discord.Member):
            return False
        return has_elevated_permissions(member) or has_bot_relay_access(member, self.bot.settings)

    @staticmethod
    def _has_send_permissions(channel: discord.TextChannel, me: discord.Member, needs_embed: bool) -> bool:
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            return False
        if needs_embed and not perms.embed_links:
            return False
        return True

    @app_commands.command(name="say", description="Отправить текст и/или embed от имени бота")

        return isinstance(member, discord.Member) and has_bot_relay_access(member, self.bot.settings)

    @app_commands.command(name="say", description="Отправить сообщение от имени бота в целевой канал")
 main
    async def say(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        channel: discord.TextChannel | None = None,
        title: str | None = None,
        description: str | None = None,
        color: str | None = None,
        image: discord.Attachment | None = None,
        thumbnail: discord.Attachment | None = None,
        footer: str | None = None,
        author: str | None = None,

 main
        image1: discord.Attachment | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
    ) -> None:
        if not self._has_access(interaction.user):
            await interaction.response.send_message("У вас недостаточно прав для /say.", ephemeral=True)
            return

 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        target = await self._get_target_channel(channel)

        target = await self._get_target_channel()
 main
        if target is None:
            await interaction.response.send_message("TARGET_CHANNEL_ID не задан или канал недоступен.", ephemeral=True)
            return

 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        guild = interaction.guild
        if guild is None or not guild.me:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        use_embed = any([title, description, color, image, thumbnail, footer, author])
        if not self._has_send_permissions(target, guild.me, needs_embed=use_embed):
            await interaction.response.send_message("У бота нет прав отправки сообщений/embed в выбранный канал.", ephemeral=True)
            return

        embed: discord.Embed | None = None
        files: list[discord.File] = await self._forward_attachments([a for a in (image1, image2, image3) if a is not None])

        if use_embed:
            parsed_color = parse_color_strict(color)
            if color and parsed_color is None:
                await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
                return

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

        if not text and embed is None and not files:
            await interaction.response.send_message("Укажите текст, embed-поля или вложения.", ephemeral=True)
            return

        if embed is not None:
            if files:
                await target.send(content=text or None, embed=embed, files=files)
            else:
                await target.send(content=text or None, embed=embed)
        else:
            if files:
                await target.send(content=text or None, files=files)
            else:
                await target.send(content=text or None)

        await interaction.response.send_message("✅ Сообщение отправлено.", ephemeral=True)

        files = await self._forward_attachments([a for a in (image1, image2, image3) if a is not None])
        if not text and not files:
            await interaction.response.send_message("Нужно указать текст или хотя бы одно вложение.", ephemeral=True)
            return

        await target.send(content=text or None, files=files or None)
        await interaction.response.send_message("✅ Сообщение отправлено в целевой канал.", ephemeral=True)
 main

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

 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        guild = interaction.guild
        if guild is None or not guild.me:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not self._has_send_permissions(target, guild.me, needs_embed=True):
            await interaction.response.send_message("У бота нет прав отправки embed в выбранный канал.", ephemeral=True)
            return

        parsed_color = parse_color_strict(color)
        if color and parsed_color is None:
            await interaction.response.send_message("Некорректный HEX цвет. Используйте формат #5865F2.", ephemeral=True)
            return

        embed = discord.Embed(title=title, description=text, color=parsed_color or parse_color(None))

        embed = discord.Embed(title=title, description=text, color=parse_color(color))
 main
        files: list[discord.File] = []
        if image:
            files = await self._forward_attachments([image])
            if files:
                embed.set_image(url=f"attachment://{files[0].filename}")

 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        if files:
            await target.send(embed=embed, files=files)
        else:
            await target.send(embed=embed)

        await target.send(embed=embed, files=files or None)
 main
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

 codex/refactor-discord-bot-structure-and-add-features-h7krw8
        if files:
            await target.send(content=message.content or None, files=files)
        else:
            await target.send(content=message.content or None)

        await target.send(content=message.content or None, files=files or None)
 main
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

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)

WELCOME_COLOR = discord.Color.from_rgb(155, 89, 182)
DEFAULT_THUMBNAIL_URL = os.getenv("WELCOME_THUMBNAIL_URL", "").strip()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _can_manage_guild(user: discord.Member | discord.User) -> bool:
    return isinstance(user, discord.Member) and (user.guild_permissions.manage_guild or user.guild_permissions.administrator)


class WelcomeCog(commands.Cog):
    welcome_group = app_commands.Group(name="welcome", description="Приветствие новых участников")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def init_db(self) -> None:
        if self.bot.db is None:
            logger.error("Welcome cog loaded without database connection")
            return
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                thumbnail_url TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.commit()

    async def get_settings(self, guild_id: int) -> Any | None:
        if self.bot.db is None:
            return None
        cursor = await self.bot.db.execute(
            "SELECT channel_id, enabled, thumbnail_url FROM welcome_settings WHERE guild_id = ?",
            (guild_id,),
        )
        return await cursor.fetchone()

    async def set_channel(self, guild_id: int, channel_id: int, thumbnail_url: str | None = None) -> None:
        if self.bot.db is None:
            return
        clean_thumbnail = (thumbnail_url or "").strip() or None
        await self.bot.db.execute(
            """
            INSERT INTO welcome_settings (guild_id, channel_id, enabled, thumbnail_url, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                enabled = 1,
                thumbnail_url = excluded.thumbnail_url,
                updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, clean_thumbnail, _now_iso()),
        )
        await self.bot.db.commit()

    async def disable(self, guild_id: int) -> None:
        if self.bot.db is None:
            return
        await self.bot.db.execute(
            """
            INSERT INTO welcome_settings (guild_id, enabled, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = 0,
                updated_at = excluded.updated_at
            """,
            (guild_id, _now_iso()),
        )
        await self.bot.db.commit()

    def build_welcome_embed(self, member: discord.Member, thumbnail_url: str | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="👋 Новый участник!",
            description=f"Добро пожаловать, {member.mention} https://discord.com/channels/1491894210050265180/1496281104439709819 https://discord.com/channels/1491894210050265180/1496777131374542888",
            color=WELCOME_COLOR,
        )
        thumb = (thumbnail_url or DEFAULT_THUMBNAIL_URL or "").strip()
        embed.set_thumbnail(url=thumb or member.display_avatar.url)
        return embed

    async def send_welcome(self, member: discord.Member, channel: discord.TextChannel, thumbnail_url: str | None) -> bool:
        me = member.guild.me
        if me is None:
            return False
        perms = channel.permissions_for(me)
        if not perms.send_messages or not perms.embed_links:
            logger.warning("No welcome permissions in channel %s for guild %s", channel.id, member.guild.id)
            return False
        try:
            await channel.send(
                embed=self.build_welcome_embed(member, thumbnail_url),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            return True
        except discord.HTTPException:
            logger.exception("Failed to send welcome message: guild=%s channel=%s member=%s", member.guild.id, channel.id, member.id)
            return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = await self.get_settings(member.guild.id)
        if settings is None or not settings["enabled"] or not settings["channel_id"]:
            return
        channel = member.guild.get_channel(int(settings["channel_id"]))
        if channel is None:
            try:
                fetched = await member.guild.fetch_channel(int(settings["channel_id"]))
            except discord.HTTPException:
                logger.exception("Failed to fetch welcome channel: guild=%s channel=%s", member.guild.id, settings["channel_id"])
                return
            channel = fetched
        if not isinstance(channel, discord.TextChannel):
            return
        await self.send_welcome(member, channel, settings["thumbnail_url"])

    @welcome_group.command(name="set", description="Включить приветствие новых участников в канале")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Канал для приветствий", thumbnail_url="Опциональная картинка справа в embed")
    async def set_welcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        thumbnail_url: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_guild(interaction.user):
            await interaction.response.send_message("Нужны права Manage Server или Administrator.", ephemeral=True)
            return
        await self.set_channel(interaction.guild.id, channel.id, thumbnail_url)
        await interaction.response.send_message(f"Приветствие включено в {channel.mention}.", ephemeral=True)

    @welcome_group.command(name="off", description="Выключить приветствие новых участников")
    @app_commands.default_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_guild(interaction.user):
            await interaction.response.send_message("Нужны права Manage Server или Administrator.", ephemeral=True)
            return
        await self.disable(interaction.guild.id)
        await interaction.response.send_message("Приветствие выключено.", ephemeral=True)

    @welcome_group.command(name="preview", description="Показать пример приветствия")
    @app_commands.default_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        settings = await self.get_settings(interaction.guild.id)
        thumbnail_url = settings["thumbnail_url"] if settings is not None else None
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Не удалось определить участника сервера.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.build_welcome_embed(target, thumbnail_url), ephemeral=True)

    @welcome_group.command(name="status", description="Показать настройки приветствия")
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        settings = await self.get_settings(interaction.guild.id)
        if settings is None or not settings["channel_id"]:
            await interaction.response.send_message("Приветствие не настроено.", ephemeral=True)
            return
        enabled = "включено" if settings["enabled"] else "выключено"
        thumbnail = settings["thumbnail_url"] or DEFAULT_THUMBNAIL_URL or "аватар участника"
        await interaction.response.send_message(
            f"Приветствие: {enabled}\nКанал: <#{settings['channel_id']}>\nКартинка: {thumbnail}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: MovieBot) -> None:
    cog = WelcomeCog(bot)
    await cog.init_db()
    await bot.add_cog(cog)

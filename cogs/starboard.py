from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.brand import BRAND_ACCENT

logger = logging.getLogger(__name__)


class StarboardCog(commands.Cog):
    starboard_group = app_commands.Group(
        name="starboard",
        description="Лучшие сообщения сервера",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._message_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def cog_load(self) -> None:
        if self.bot.db is None:
            return
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS starboard_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                threshold INTEGER NOT NULL DEFAULT 3 CHECK(threshold BETWEEN 1 AND 100),
                excluded_channels_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS starboard_posts (
                guild_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                starboard_message_id INTEGER NOT NULL,
                star_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, source_message_id)
            );
            """
        )
        await self.bot.db.commit()

    async def _settings(self, guild_id: int):
        if self.bot.db is None:
            return None
        cursor = await self.bot.db.execute("SELECT * FROM starboard_settings WHERE guild_id = ?", (guild_id,))
        return await cursor.fetchone()

    @starboard_group.command(name="set", description="Настроить канал и порог звёзд")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def configure(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        threshold: app_commands.Range[int, 1, 100] = 3,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute(
            """INSERT INTO starboard_settings(guild_id, channel_id, threshold, enabled)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
               threshold=excluded.threshold, enabled=1""",
            (interaction.guild.id, channel.id, int(threshold)),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Starboard включён: {channel.mention}, порог — {threshold} ⭐.", ephemeral=True
        )

    @starboard_group.command(name="exclude", description="Добавить или убрать канал из исключений")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def exclude(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message("Сначала настройте `/starboard set`.", ephemeral=True)
            return
        excluded = {int(value) for value in json.loads(settings["excluded_channels_json"] or "[]")}
        if channel.id in excluded:
            excluded.remove(channel.id)
            action = "удалён из исключений"
        else:
            excluded.add(channel.id)
            action = "добавлен в исключения"
        await self.bot.db.execute(
            "UPDATE starboard_settings SET excluded_channels_json = ? WHERE guild_id = ?",
            (json.dumps(sorted(excluded)), interaction.guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"{channel.mention} {action}.", ephemeral=True)

    @starboard_group.command(name="off", description="Выключить starboard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE starboard_settings SET enabled = 0 WHERE guild_id = ?", (interaction.guild.id,))
        await self.bot.db.commit()
        await interaction.response.send_message("Starboard выключен.", ephemeral=True)

    @starboard_group.command(name="status", description="Показать настройки starboard")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message("Starboard ещё не настроен.", ephemeral=True)
            return
        excluded = json.loads(settings["excluded_channels_json"] or "[]")
        await interaction.response.send_message(
            f"Канал: <#{settings['channel_id']}>\nПорог: {settings['threshold']} ⭐\n"
            f"Статус: {'включён' if settings['enabled'] else 'выключен'}\nИсключений: {len(excluded)}",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is not None and str(payload.emoji) == "⭐":
            await self._refresh(payload.guild_id, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is not None and str(payload.emoji) == "⭐":
            await self._refresh(payload.guild_id, payload.channel_id, payload.message_id)

    async def _refresh(self, guild_id: int, channel_id: int, message_id: int) -> None:
        key = (guild_id, message_id)
        lock = self._message_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._refresh_locked(guild_id, channel_id, message_id)

    async def _refresh_locked(self, guild_id: int, channel_id: int, message_id: int) -> None:
        if self.bot.db is None:
            return
        settings = await self._settings(guild_id)
        if settings is None or not settings["enabled"] or channel_id == int(settings["channel_id"]):
            return
        excluded = {int(value) for value in json.loads(settings["excluded_channels_json"] or "[]")}
        if channel_id in excluded:
            return
        guild = self.bot.get_guild(guild_id)
        source_channel = guild.get_channel(channel_id) if guild else None
        target_channel = guild.get_channel(int(settings["channel_id"])) if guild else None
        if not isinstance(source_channel, discord.TextChannel) or not isinstance(target_channel, discord.TextChannel):
            return
        try:
            source = await source_channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if source.author.bot:
            return
        stars = next((reaction.count for reaction in source.reactions if str(reaction.emoji) == "⭐"), 0)
        cursor = await self.bot.db.execute(
            "SELECT starboard_message_id FROM starboard_posts WHERE guild_id = ? AND source_message_id = ?",
            (guild_id, message_id),
        )
        existing = await cursor.fetchone()
        if stars < int(settings["threshold"]):
            if existing:
                try:
                    posted = await target_channel.fetch_message(int(existing["starboard_message_id"]))
                    await posted.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await self.bot.db.execute(
                    "DELETE FROM starboard_posts WHERE guild_id = ? AND source_message_id = ?", (guild_id, message_id)
                )
                await self.bot.db.commit()
            return
        embed = discord.Embed(description=source.content[:4000] or "Сообщение без текста", color=BRAND_ACCENT)
        embed.set_author(name=source.author.display_name, icon_url=source.author.display_avatar.url)
        embed.add_field(name="Источник", value=f"[Перейти к сообщению]({source.jump_url})", inline=False)
        if source.attachments and source.attachments[0].content_type and source.attachments[0].content_type.startswith("image/"):
            embed.set_image(url=source.attachments[0].url)
        content = f"⭐ **{stars}** · {source_channel.mention}"
        if existing:
            try:
                posted = await target_channel.fetch_message(int(existing["starboard_message_id"]))
                await posted.edit(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                existing = None
        if not existing:
            posted = await target_channel.send(content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.execute(
                """INSERT INTO starboard_posts(guild_id, source_channel_id, source_message_id, starboard_message_id, star_count)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, source_message_id) DO UPDATE SET
                   starboard_message_id=excluded.starboard_message_id, star_count=excluded.star_count""",
                (guild_id, channel_id, message_id, posted.id, stars),
            )
            await self.bot.db.commit()
            await self.bot.progression.record_event(
                self.bot.progression_db, guild_id, source.author.id, "starboard_post", 1,
                f"starboard:{guild_id}:{message_id}",
            )
        else:
            await self.bot.db.execute(
                "UPDATE starboard_posts SET star_count = ? WHERE guild_id = ? AND source_message_id = ?",
                (stars, guild_id, message_id),
            )
        await self.bot.db.commit()


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(StarboardCog(bot))

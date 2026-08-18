from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.brand import BRAND_NAME


def enabled_text(value: bool) -> str:
    return "включено" if value else "выключено"


class SettingsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _settings(self, guild_id: int) -> discord.Embed:
        assert self.bot.db is not None
        row = await self.bot.social_games.ensure_guild_settings(self.bot.db, guild_id)
        embed = discord.Embed(title=f"Настройки {BRAND_NAME}", color=discord.Color.blurple())
        embed.add_field(
            name="NSFW-канал",
            value=f"<#{row['nsfw_channel_id']}>" if row["nsfw_channel_id"] else "не выбран",
            inline=True,
        )
        embed.add_field(
            name="Канал импорта NSFW",
            value=f"<#{row['nsfw_import_channel_id']}>" if row["nsfw_import_channel_id"] else "не выбран",
            inline=True,
        )
        embed.add_field(name="Аналитика профилей", value=enabled_text(bool(row["profile_analytics_enabled"])), inline=True)
        embed.add_field(name="Matchmaking", value=enabled_text(bool(row["matchmaking_enabled"])), inline=True)
        embed.add_field(name="NSFW story", value=enabled_text(bool(row["story_nsfw_enabled"])), inline=True)
        embed.add_field(name="Лог-канал", value=f"<#{row['log_channel_id']}>" if row["log_channel_id"] else "не задан", inline=True)
        return embed

    @app_commands.command(name="bot_settings", description="Показать настройки игровых и социальных модулей")
    @app_commands.default_permissions(administrator=True)
    async def show_bot_settings(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self._settings(interaction.guild.id), ephemeral=True)

    async def _set_bool(self, interaction: discord.Interaction, field: str, enabled: bool, label: str) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.social_games.set_guild_flag(self.bot.db, interaction.guild.id, field, int(enabled))
        await interaction.response.send_message(f"{label}: {enabled_text(enabled)}.", ephemeral=True)

    @app_commands.command(name="set_nsfw_channel", description="Выбрать единственный канал для NSFW-команд")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def set_nsfw_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if channel is not None and not channel.is_nsfw():
            await interaction.response.send_message(
                "Сначала включите для канала ограничение 18+ в настройках Discord.",
                ephemeral=True,
            )
            return

        channel_id = channel.id if channel is not None else 0
        await self.bot.social_games.set_nsfw_channel(
            self.bot.db,
            interaction.guild.id,
            channel_id,
        )
        if channel is None:
            text = "Выбранный NSFW-канал сброшен. Работают обычные каналы Discord с отметкой 18+."
        else:
            text = f"NSFW-команды включены только в {channel.mention}."
        await interaction.response.send_message(
            text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="set_nsfw_import_channel", description="Выбрать 18+ канал, из которого бот публикует сообщения")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def set_nsfw_import_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        settings = await self.bot.social_games.ensure_guild_settings(
            self.bot.db,
            interaction.guild.id,
        )
        target_channel_id = int(settings["nsfw_channel_id"])

        if channel is not None and not channel.is_nsfw():
            await interaction.response.send_message(
                "Сначала включите для импорт-канала ограничение 18+ в настройках Discord.",
                ephemeral=True,
            )
            return
        if channel is not None and target_channel_id <= 0:
            await interaction.response.send_message(
                "Сначала выберите канал назначения через /set_nsfw_channel.",
                ephemeral=True,
            )
            return
        if channel is not None and channel.id == target_channel_id:
            await interaction.response.send_message(
                "Канал импорта и канал назначения должны быть разными, чтобы не создать цикл.",
                ephemeral=True,
            )
            return

        channel_id = channel.id if channel is not None else 0
        await self.bot.social_games.set_nsfw_import_channel(
            self.bot.db,
            interaction.guild.id,
            channel_id,
        )
        if channel is None:
            text = "Автопубликация из импорт-канала выключена."
        else:
            text = (
                f"Сообщения из {channel.mention} будут публиковаться ботом в "
                f"<#{target_channel_id}>. Оба канала должны оставаться с ограничением 18+."
            )
        await interaction.response.send_message(
            text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="set_profile_analytics", description="Включить или выключить аналитику профилей")
    @app_commands.default_permissions(administrator=True)
    async def set_profile_analytics(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_bool(interaction, "profile_analytics_enabled", enabled, "Аналитика профилей")

    @app_commands.command(name="set_matchmaking", description="Включить или выключить умные знакомства")
    @app_commands.default_permissions(administrator=True)
    async def set_matchmaking(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_bool(interaction, "matchmaking_enabled", enabled, "Matchmaking")

    @app_commands.command(name="set_story_nsfw", description="Включить или выключить NSFW-ветки истории")
    @app_commands.default_permissions(administrator=True)
    async def set_story_nsfw(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_bool(interaction, "story_nsfw_enabled", enabled, "NSFW story")

    @app_commands.command(name="set_log_channel", description="Задать канал логирования игровых модулей")
    @app_commands.default_permissions(administrator=True)
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.social_games.set_guild_flag(self.bot.db, interaction.guild.id, "log_channel_id", channel.id)
        await interaction.response.send_message(f"Лог-канал установлен: {channel.mention}.", ephemeral=True)

async def setup(bot: MovieBot) -> None:
    await bot.add_cog(SettingsCog(bot))

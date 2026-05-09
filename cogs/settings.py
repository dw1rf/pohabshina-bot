from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot


def enabled_text(value: bool) -> str:
    return "включено" if value else "выключено"


class SettingsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _settings(self, guild_id: int) -> discord.Embed:
        assert self.bot.db is not None
        row = await self.bot.social_games.ensure_guild_settings(self.bot.db, guild_id)
        embed = discord.Embed(title="Настройки бота", color=discord.Color.blurple())
        embed.add_field(name="NSFW RP", value=enabled_text(bool(row["nsfw_rp_enabled"])), inline=True)
        embed.add_field(name="Аналитика профилей", value=enabled_text(bool(row["profile_analytics_enabled"])), inline=True)
        embed.add_field(name="Matchmaking", value=enabled_text(bool(row["matchmaking_enabled"])), inline=True)
        embed.add_field(name="NSFW story", value=enabled_text(bool(row["story_nsfw_enabled"])), inline=True)
        embed.add_field(name="Лог-канал", value=f"<#{row['log_channel_id']}>" if row["log_channel_id"] else "не задан", inline=True)
        embed.add_field(name="18+ роль", value=f"<@&{row['adult_role_id']}>" if row["adult_role_id"] else "не задана", inline=True)
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

    @app_commands.command(name="set_nsfw_rp", description="Включить или выключить NSFW RP на сервере")
    @app_commands.default_permissions(administrator=True)
    async def set_nsfw_rp(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_bool(interaction, "nsfw_rp_enabled", enabled, "NSFW RP")

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

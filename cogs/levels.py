from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import required_messages_for_level


class LevelsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.bot.db:
            return
        content = (message.content or "").strip()
        if len(content) < self.bot.settings.min_message_length:
            return

        _, level, level_up = await self.bot.levels.update_level_progress(
            self.bot.db,
            message.guild.id,
            message.author.id,
            datetime.now(UTC),
        )
        if level_up:
            await message.channel.send(f"🎉 {message.author.mention} достиг(ла) {level} уровня!")

    @app_commands.command(name="rank", description="Показать уровень пользователя")
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return

        target = user or interaction.user
        row = await self.bot.levels.get_rank(self.bot.db, guild.id, target.id)
        if row is None:
            await interaction.response.send_message("У этого пользователя пока нет прогресса по уровням.", ephemeral=True)
            return

        level = int(row["level"])
        message_count = int(row["message_count"])
        next_level = min(self.bot.settings.max_level, level + 1)
        if level >= self.bot.settings.max_level:
            progress_text = "Достигнут максимальный уровень."
        else:
            current_req = required_messages_for_level(level)
            next_req = required_messages_for_level(next_level)
            progress_text = f"{message_count - current_req}/{next_req - current_req} сообщений"

        embed = discord.Embed(title=f"Ранг: {target.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Уровень", value=str(level), inline=True)
        embed.add_field(name="Сообщений", value=str(message_count), inline=True)
        embed.add_field(name="Прогресс", value=progress_text, inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="Показать топ по уровням")
    async def top(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        rows = await self.bot.levels.get_top(self.bot.db, guild.id, limit=10)
        if not rows:
            await interaction.response.send_message("Пока нет данных по сообщениям.", ephemeral=True)
            return

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            member = guild.get_member(int(row["user_id"]))
            name = member.mention if member else f"<@{int(row['user_id'])}>"
            lines.append(f"**{i}.** {name} — ур. {row['level']} • {row['message_count']} сообщений")
        await interaction.response.send_message(
            embed=discord.Embed(title="🏆 Топ участников по уровням", description="\n".join(lines), color=discord.Color.purple())
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(LevelsCog(bot))

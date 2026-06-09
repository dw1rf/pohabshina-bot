from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import required_messages_for_level
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, resolve_display_name

logger = logging.getLogger(__name__)


class LevelsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._last_level_up_gif: str | None = None

    def _get_random_level_up_text(self) -> str:
        messages = self.bot.engagement_content.list("levelup_messages")
        if not messages:
            return "Ты отлично проявляешь себя в жизни сервера."
        return random.choice(messages)

    def _get_random_level_up_gif(self) -> str | None:
        gifs = [
            gif
            for gif in self.bot.engagement_content.list("levelup_gifs")
            if gif.startswith(("http://", "https://"))
        ]
        if not gifs:
            return None
        if len(gifs) == 1:
            self._last_level_up_gif = gifs[0]
            return gifs[0]

        choices = [gif for gif in gifs if gif != self._last_level_up_gif]
        gif_url = random.choice(choices or gifs)
        self._last_level_up_gif = gif_url
        return gif_url

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
            await self._send_level_up_message(message, level)

    async def _send_level_up_message(self, message: discord.Message, level: int) -> None:
        motivation = self._get_random_level_up_text()
        gif_url = self._get_random_level_up_gif()

        embed = discord.Embed(
            title="╭──── ✦ LEVEL UP ✦ ────╮",
            description=(
                f"🎉 Поздравляем, {message.author.mention}!\n\n"
                f"✨ Достигнут новый уровень: **{level}**\n\n"
                f"{motivation}\n\n"
                "╰──────────────────────╯"
            ),
            color=discord.Color.from_rgb(255, 128, 191),
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        if gif_url:
            embed.set_image(url=gif_url)
        embed.set_footer(text="Продолжай быть активным участником сервера")

        await message.channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

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

    async def top(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        rows = await self.bot.levels.get_top(self.bot.db, guild.id, limit=10)
        if not rows:
            await interaction.response.send_message("Пока нет данных для топа.", ephemeral=True)
            return

        await interaction.response.defer()
        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            level = int(row["level"])
            message_count = int(row["message_count"])
            name = await resolve_display_name(self.bot, guild, int(row["user_id"]))
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=name,
                    primary=f"Уровень {level}",
                    secondary=f"{message_count} сообщений",
                    value=message_count,
                )
            )

        try:
            filename = "levels_top.png"
            file = make_leaderboard_file("ТОП УРОВНЕЙ", leaderboard_rows, filename=filename)
            embed = discord.Embed(title="Топ уровней", color=discord.Color.purple())
            embed.set_image(url=f"attachment://{filename}")
        except Exception:
            logger.exception("Failed to generate levels top image")
            await interaction.followup.send(
                "Не удалось создать графический топ. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(LevelsCog(bot))

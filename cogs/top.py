from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, resolve_display_name

logger = logging.getLogger(__name__)


class TopCog(commands.Cog):
    top_group = app_commands.Group(name="top", description="Топы сервера")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @top_group.command(name="messages", description="Топ участников по сообщениям")
    async def messages(self, interaction: discord.Interaction) -> None:
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
            filename = "messages_top.png"
            file = make_leaderboard_file("ТОП СООБЩЕНИЙ", leaderboard_rows, filename=filename)
            embed = discord.Embed(title="Топ сообщений", color=discord.Color.purple())
            embed.set_image(url=f"attachment://{filename}")
        except Exception:
            logger.exception("Failed to generate messages top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return

        await interaction.followup.send(
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @top_group.command(name="reputation", description="Топ участников по репутации")
    async def reputation(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return

        cursor = await self.bot.db.execute(
            """
            SELECT user_id, positive_rep, negative_rep, positive_rep - negative_rep AS total_rep
            FROM user_reputation
            WHERE guild_id = ?
            ORDER BY total_rep DESC, positive_rep DESC, negative_rep ASC, user_id ASC
            LIMIT 10
            """,
            (guild.id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("Пока нет данных для топа репутации.", ephemeral=True)
            return

        await interaction.response.defer()
        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            positive = int(row["positive_rep"])
            negative = int(row["negative_rep"])
            total = int(row["total_rep"])
            name = await resolve_display_name(self.bot, guild, int(row["user_id"]))
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=name,
                    primary=f"Репутация {total:+d}",
                    secondary=f"+{positive} / -{negative}",
                    value=total,
                )
            )

        try:
            filename = "reputation_top.png"
            file = make_leaderboard_file("ТОП РЕПУТАЦИИ", leaderboard_rows, filename=filename)
            embed = discord.Embed(title="Топ репутации", color=discord.Color.gold())
            embed.set_image(url=f"attachment://{filename}")
        except Exception:
            logger.exception("Failed to generate reputation top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return

        await interaction.followup.send(
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @top_group.command(name="relations", description="Топ пар по развитию отношений")
    async def relations(self, interaction: discord.Interaction) -> None:
        weddings_cog = self.bot.get_cog("WeddingsCog")
        if weddings_cog is None or not hasattr(weddings_cog, "send_relationship_top"):
            await interaction.response.send_message("Модуль отношений сейчас недоступен.", ephemeral=True)
            return

        await weddings_cog.send_relationship_top(interaction)  # type: ignore[attr-defined]

    @top_group.command(name="pairs", description="Топ пар по длительности брака")
    async def pairs(self, interaction: discord.Interaction) -> None:
        weddings_cog = self.bot.get_cog("WeddingsCog")
        if weddings_cog is None or not hasattr(weddings_cog, "top_couples"):
            await interaction.response.send_message("Модуль свадеб сейчас недоступен.", ephemeral=True)
            return

        await weddings_cog.top_couples(interaction)  # type: ignore[attr-defined]


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(TopCog(bot))

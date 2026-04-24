from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

PRAISES = [
    "Ты сегодня просто сияешь!",
    "У тебя отличный вкус и вайб 😎",
    "Ты очень крут(а), так держать!",
    "Ты делаешь этот сервер лучше.",
    "Ты молодец, продолжай в том же духе!",
]


class FunCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @staticmethod
    def _target_text(interaction: discord.Interaction, user: discord.Member, action: str) -> str:
        if interaction.user.id == user.id:
            return f"😅 {interaction.user.mention} попытался(ась) {action} себя."
        return f"{interaction.user.mention} {action} {user.mention}"

    @app_commands.command(name="самый_красивый", description="Случайно выбрать самого красивого участника")
    @app_commands.checks.cooldown(1, 5)
    async def most_beautiful(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return

        members = [m for m in guild.members if not m.bot]
        if not members:
            await interaction.response.send_message("На сервере пока нет подходящих участников для выбора.")
            return

        await interaction.response.send_message(f"👑 Самый красивый сегодня — {random.choice(members).mention}")

    @app_commands.command(name="обнять", description="Обнять участника")
    @app_commands.checks.cooldown(1, 5)
    async def hug(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(f"🤗 {interaction.user.mention} обнял(а) {user.mention}")

    @app_commands.command(name="похвалить", description="Похвалить участника")
    @app_commands.checks.cooldown(1, 5)
    async def praise(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(f"🌟 {user.mention}, {random.choice(PRAISES)}")

    @app_commands.command(name="легенда", description="Назвать участника легендой")
    @app_commands.checks.cooldown(1, 5)
    async def legend(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(f"✨ Сегодня {user.mention} официально признан легендой сервера")

    @app_commands.command(name="минет", description="Рофл-команда 18+")
    @app_commands.checks.cooldown(1, 5)
    async def minet(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "сделал(а) минет"))

    @app_commands.command(name="шлёпнуть", description="Шлёпнуть участника")
    @app_commands.checks.cooldown(1, 5)
    async def slap(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "шлёпнул(а)"))

    @app_commands.command(name="пригласить_на_чай", description="Пригласить участника на чай")
    @app_commands.checks.cooldown(1, 5)
    async def invite_tea(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "пригласил(а) на чай"))

    @app_commands.command(name="поцеловать_ступню", description="Поцеловать ступню участника")
    @app_commands.checks.cooldown(1, 5)
    async def kiss_foot(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "поцеловал(а) ступню"))

    @app_commands.command(name="надуть", description="Надуть участника (шутка)")
    @app_commands.checks.cooldown(1, 5)
    async def inflate(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "надул(а)"))

    @app_commands.command(name="кастрировать", description="Кастрировать участника (ролка)")
    @app_commands.checks.cooldown(1, 5)
    async def castrate(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "кастрировал(а)"))

    @app_commands.command(name="убить", description="Убить участника (ролка)")
    @app_commands.checks.cooldown(1, 5)
    async def kill(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_message(self._target_text(interaction, user, "виртуально убил(а)"))


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(FunCog(bot))

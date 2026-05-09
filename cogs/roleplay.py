from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import RP_ACTIONS

logger = logging.getLogger(__name__)


class RoleplayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._target_cooldowns: dict[tuple[int, int, str], datetime] = {}
        self._registered: list[str] = []
        for name, payload in RP_ACTIONS.items():
            if self.bot.tree.get_command(name) is not None:
                logger.warning("RP command /%s skipped because it already exists", name)
                continue
            command = app_commands.Command(name=name, description=f"RP-действие: {payload['label']}", callback=self._make_callback(name))
            self.bot.tree.add_command(command)
            self._registered.append(name)

    async def cog_unload(self) -> None:
        for name in self._registered:
            self.bot.tree.remove_command(name)

    def _make_callback(self, action_key: str):
        @app_commands.describe(target="Участник RP-сцены", comment="Необязательный короткий комментарий")
        @app_commands.checks.cooldown(1, 15)
        async def callback(interaction: discord.Interaction, target: discord.Member, comment: str | None = None) -> None:
            await self._handle_action(interaction, action_key, target, comment)
        return callback

    async def _handle_action(self, interaction: discord.Interaction, action_key: str, target: discord.Member, comment: str | None) -> None:
        try:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("RP-команды доступны только на сервере.", ephemeral=True)
                return
            author = interaction.user
            if target.bot:
                await interaction.response.send_message("Нельзя использовать RP-команды на ботах.", ephemeral=True)
                return
            if target.id == author.id:
                await interaction.response.send_message("Эта RP-команда требует второго участника.", ephemeral=True)
                return

            if self.bot.db is None:
                await interaction.response.send_message("База данных временно недоступна.", ephemeral=True)
                return

            payload = RP_ACTIONS[action_key]
            nsfw = bool(payload["nsfw"])
            guild_settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
            if nsfw:
                if not bool(guild_settings["nsfw_rp_enabled"]):
                    await interaction.response.send_message("NSFW RP выключен администратором сервера.", ephemeral=True)
                    return
                if not getattr(interaction.channel, "is_nsfw", lambda: False)():
                    await interaction.response.send_message("Эта команда доступна только в NSFW-канале.", ephemeral=True)
                    return

            missing_consent = []
            for member in (author, target):
                if not await self.bot.social_games.has_rp_consent(self.bot.db, interaction.guild.id, member.id, nsfw=nsfw):
                    missing_consent.append(member.mention)
            if missing_consent:
                await interaction.response.send_message(
                    "Для RP-команды нужно согласие обоих участников через /rp_consent. "
                    f"Не хватает согласия: {', '.join(missing_consent)}.",
                    ephemeral=True,
                )
                return

            cd_key = (interaction.guild.id, target.id, action_key)
            now = datetime.now(UTC)
            if cd_key in self._target_cooldowns and self._target_cooldowns[cd_key] > now:
                left = int((self._target_cooldowns[cd_key] - now).total_seconds())
                await interaction.response.send_message(f"Не спамьте одного участника. Подождите {left} сек.", ephemeral=True)
                return
            self._target_cooldowns[cd_key] = now + timedelta(seconds=60)

            description = f"{author.mention} и {target.mention}: {payload['text']}"
            if comment:
                description += f"\n\n{discord.utils.escape_markdown(comment)[:300]}"
            embed = discord.Embed(
                description=description,
                color=discord.Color.purple() if nsfw else discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed)
        except Exception:
            logger.exception("RP action failed: guild=%s action=%s", getattr(interaction.guild, "id", None), action_key)
            if interaction.response.is_done():
                await interaction.followup.send("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)
            else:
                await interaction.response.send_message("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)

    @app_commands.command(name="rp_consent", description="Устаревшая настройка RP-согласия")
    async def rp_consent(self, interaction: discord.Interaction, sfw: bool, nsfw: bool = False) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if nsfw and not sfw:
            await interaction.response.send_message("NSFW-согласие требует включённого обычного RP-согласия.", ephemeral=True)
            return
        await self.bot.social_games.set_rp_consent(self.bot.db, interaction.guild.id, interaction.user.id, sfw=sfw, nsfw=nsfw)
        await interaction.response.send_message("RP-согласие сохранено. Для NSFW-сцен также нужны NSFW-канал, настройка сервера и согласие второго участника.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RoleplayCog(bot))

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import RP_ACTIONS

logger = logging.getLogger(__name__)


class RPConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member, target: discord.Member, action_label: str, final_text: str, comment: str | None) -> None:
        super().__init__(timeout=120)
        self.author = author
        self.target = target
        self.action_label = action_label
        self.final_text = final_text
        self.comment = comment

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("Ответить может только участник, которому предложили RP-действие.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        desc = f"{self.author.mention} и {self.target.mention}: {self.final_text}"
        if self.comment:
            desc += f"\n\nКомментарий: {discord.utils.escape_markdown(self.comment)[:300]}"
        embed = discord.Embed(title="RP-действие принято", description=desc, color=discord.Color.green(), timestamp=datetime.now(UTC))
        embed.set_footer(text="Взаимное согласие подтверждено кнопкой. Можно остановить сцену в любой момент.")
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(title="RP-действие отклонено", description="Предложение спокойно отклонено. Уважайте границы друг друга.", color=discord.Color.light_grey())
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


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
            if interaction.guild is None or not isinstance(interaction.user, discord.Member) or self.bot.db is None:
                await interaction.response.send_message("RP-команды доступны только на сервере.", ephemeral=True)
                return
            author = interaction.user
            if target.bot:
                await interaction.response.send_message("Нельзя использовать RP-команды на ботах.", ephemeral=True)
                return
            if target.id == author.id:
                await interaction.response.send_message("Эта RP-команда требует второго участника.", ephemeral=True)
                return

            payload = RP_ACTIONS[action_key]
            nsfw = bool(payload["nsfw"])
            settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
            if nsfw:
                if not bool(settings["nsfw_rp_enabled"]):
                    await interaction.response.send_message("NSFW RP выключено на этом сервере.", ephemeral=True)
                    return
                if not getattr(interaction.channel, "is_nsfw", lambda: False)():
                    await interaction.response.send_message("NSFW RP доступно только в каналах с флагом NSFW.", ephemeral=True)
                    return
                adult_role_id = int(settings["adult_role_id"] or 0)
                if adult_role_id and (adult_role_id not in {role.id for role in author.roles} or adult_role_id not in {role.id for role in target.roles}):
                    await interaction.response.send_message("Для NSFW RP у обоих участников должна быть роль 18+.", ephemeral=True)
                    return

            author_ok = await self.bot.social_games.has_rp_consent(self.bot.db, interaction.guild.id, author.id, nsfw=nsfw)
            target_ok = await self.bot.social_games.has_rp_consent(self.bot.db, interaction.guild.id, target.id, nsfw=nsfw)
            if not author_ok or not target_ok:
                await interaction.response.send_message("Оба участника должны заранее включить RP-согласие командой /rp_consent.", ephemeral=True)
                return

            cd_key = (interaction.guild.id, target.id, action_key)
            now = datetime.now(UTC)
            if cd_key in self._target_cooldowns and self._target_cooldowns[cd_key] > now:
                left = int((self._target_cooldowns[cd_key] - now).total_seconds())
                await interaction.response.send_message(f"Не спамьте одного участника. Подождите {left} сек.", ephemeral=True)
                return
            self._target_cooldowns[cd_key] = now + timedelta(seconds=60)

            embed = discord.Embed(
                title="RP-действие",
                description=f"{author.mention} предлагает действие: **{payload['label']}** для {target.mention}",
                color=discord.Color.purple() if nsfw else discord.Color.blurple(),
                timestamp=now,
            )
            embed.set_footer(text="Нужно согласие второго участника. Развлекательная RP-сцена, не факт и не принуждение.")
            await interaction.response.send_message(embed=embed, view=RPConfirmView(author, target, str(payload["label"]), str(payload["text"]), comment))
        except Exception:
            logger.exception("RP action failed: guild=%s action=%s", getattr(interaction.guild, "id", None), action_key)
            if interaction.response.is_done():
                await interaction.followup.send("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)
            else:
                await interaction.response.send_message("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)

    @app_commands.command(name="rp_consent", description="Настроить согласие на RP-взаимодействия")
    async def rp_consent(self, interaction: discord.Interaction, sfw: bool, nsfw: bool = False) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.social_games.set_rp_consent(self.bot.db, interaction.guild.id, interaction.user.id, sfw=sfw, nsfw=nsfw)
        await interaction.response.send_message(f"RP-согласие обновлено: SFW={sfw}, NSFW={nsfw}.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RoleplayCog(bot))

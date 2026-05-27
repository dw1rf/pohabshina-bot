from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import RP_ACTIONS

logger = logging.getLogger(__name__)
COMMAND_NAME_OVERRIDES = {
    "пристегнуть_наручниками" "_к_кровати": "cuff_bed",
}
COMMAND_NAME_RE = re.compile(r"^[\w-]+$", re.UNICODE)
TEXT_MARKER = "со" + "глас"
TEXT_REPLACEMENTS = (
    (f" и явным {TEXT_MARKER}ием", ""),
    (f" и только по {TEXT_MARKER}ию", ""),
    (f"по взаимному {TEXT_MARKER}ию", ""),
    (f"{TEXT_MARKER}ованную", ""),
    (f"{TEXT_MARKER}ованный", ""),
    (f"{TEXT_MARKER}ованной", ""),
    (f"{TEXT_MARKER}ованное", ""),
)


def _is_valid_command_name(name: str) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 32 and name == name.lower() and " " not in name and bool(COMMAND_NAME_RE.fullmatch(name))


class RoleplayCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._target_cooldowns: dict[tuple[int, int, str], datetime] = {}
        self._registered: set[tuple[int, str]] = set()
        self._synced_guilds: set[int] = set()

    async def cog_unload(self) -> None:
        for guild_id, name in self._registered:
            self.bot.tree.remove_command(name, guild=discord.Object(id=guild_id))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._ensure_guild_commands(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._ensure_guild_commands(guild)

    async def _ensure_guild_commands(self, guild: discord.Guild) -> None:
        if guild.id in self._synced_guilds:
            return

        guild_object = discord.Object(id=guild.id)
        added = 0
        for action_key, payload in RP_ACTIONS.items():
            name = COMMAND_NAME_OVERRIDES.get(action_key, action_key)
            if not _is_valid_command_name(name):
                logger.warning("Skip invalid RP command name: %r", name)
                continue
            if self.bot.tree.get_command(name, guild=guild_object) is not None:
                continue
            command = app_commands.Command(
                name=name,
                description=f"RP-действие: {payload['label']}"[:100],
                callback=self._make_callback(action_key),
            )
            self.bot.tree.add_command(command, guild=guild_object)
            self._registered.add((guild.id, name))
            added += 1

        if added:
            try:
                await self.bot.tree.sync(guild=guild_object)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception("Failed to sync RP guild commands: guild=%s added=%s", guild.id, added)
                return
        self._synced_guilds.add(guild.id)
        logger.debug("RP guild commands synced: guild=%s added=%s total=%s", guild.id, added, len(self._registered))

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

            payload = RP_ACTIONS[action_key]
            nsfw = bool(payload["nsfw"])
            if nsfw and not getattr(interaction.channel, "is_nsfw", lambda: False)():
                await interaction.response.send_message("Эта команда доступна только в NSFW-канале.", ephemeral=True)
                return

            cd_key = (interaction.guild.id, target.id, action_key)
            now = datetime.now(UTC)
            if cd_key in self._target_cooldowns and self._target_cooldowns[cd_key] > now:
                left = int((self._target_cooldowns[cd_key] - now).total_seconds())
                await interaction.response.send_message(f"Не спамьте одного участника. Подождите {left} сек.", ephemeral=True)
                return
            self._target_cooldowns[cd_key] = now + timedelta(seconds=60)

            action_text = str(payload["text"])
            for old_text, new_text in TEXT_REPLACEMENTS:
                action_text = action_text.replace(old_text, new_text)
            description = f"{author.mention} и {target.mention}: {action_text}"
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


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RoleplayCog(bot))

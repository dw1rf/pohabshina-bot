from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import format_dt, truncate_text

logger = logging.getLogger(__name__)


class ReactionBanCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @staticmethod
    def _can_manage_reaction_bans(user: discord.Member | discord.User) -> bool:
        if not isinstance(user, discord.Member):
            return False
        permissions = user.guild_permissions
        return permissions.administrator or permissions.manage_messages

    @staticmethod
    def _guild(interaction: discord.Interaction) -> discord.Guild | None:
        return interaction.guild

    async def _ensure_command_context(self, interaction: discord.Interaction) -> discord.Guild | None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message(
                "Эта команда доступна только на сервере.",
                ephemeral=True,
            )
            return None

        if not self._can_manage_reaction_bans(interaction.user):
            await interaction.response.send_message(
                "У вас нет прав для управления запретом реакций.",
                ephemeral=True,
            )
            return None

        return guild

    @app_commands.command(name="reaction_ban", description="Запретить пользователю ставить реакции")
    @app_commands.describe(user="Пользователь Discord", reason="Необязательная причина")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def reaction_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str | None = None,
    ) -> None:
        guild = await self._ensure_command_context(interaction)
        if guild is None or not self.bot.db:
            return

        cleaned_reason = reason.strip() if reason and reason.strip() else None
        created = await self.bot.reaction_bans.add_ban(
            self.bot.db,
            guild.id,
            user.id,
            interaction.user.id,
            cleaned_reason,
        )

        if not created:
            await interaction.response.send_message(
                "Этот пользователь уже не может ставить реакции.",
                ephemeral=True,
            )
            return

        logger.info(
            "Reaction ban added: guild=%s user=%s moderator=%s reason=%s",
            guild.id,
            user.id,
            interaction.user.id,
            cleaned_reason,
        )
        await interaction.response.send_message(
            f"Пользователю {user.mention} запрещено ставить реакции.",
            ephemeral=True,
        )

    @app_commands.command(name="reaction_unban", description="Снять запрет на реакции с пользователя")
    @app_commands.describe(user="Пользователь Discord")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def reaction_unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        guild = await self._ensure_command_context(interaction)
        if guild is None or not self.bot.db:
            return

        deleted = await self.bot.reaction_bans.remove_ban(self.bot.db, guild.id, user.id)
        if not deleted:
            await interaction.response.send_message(
                "У этого пользователя нет запрета на реакции.",
                ephemeral=True,
            )
            return

        logger.info(
            "Reaction ban removed: guild=%s user=%s moderator=%s",
            guild.id,
            user.id,
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"Запрет на реакции для {user.mention} снят.",
            ephemeral=True,
        )

    @app_commands.command(name="reaction_ban_list", description="Показать список пользователей с запретом реакций")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def reaction_ban_list(self, interaction: discord.Interaction) -> None:
        guild = await self._ensure_command_context(interaction)
        if guild is None or not self.bot.db:
            return

        rows = await self.bot.reaction_bans.list_bans(self.bot.db, guild.id)
        if not rows:
            await interaction.response.send_message(
                "Запретов на реакции нет.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        total = len(rows)
        for row in rows:
            reason = truncate_text(row["reason"] or "без причины", 80)
            moderator = f"<@{row['moderator_id']}>" if row["moderator_id"] else "неизвестно"
            created_at = format_dt(row["created_at"] or "")
            line = f"• <@{row['user_id']}> — {reason} | модератор: {moderator} | {created_at}"
            if len("\n".join(lines + [line])) > 1800:
                break
            lines.append(line)

        suffix = f"\n\nПоказано {len(lines)} из {total}." if len(lines) < total else ""
        await interaction.response.send_message(
            "Пользователи с запретом реакций:\n" + "\n".join(lines) + suffix,
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User | discord.Member) -> None:
        if user.bot or not self.bot.db:
            return

        guild = reaction.message.guild
        if guild is None:
            return

        ban = await self.bot.reaction_bans.get_ban(self.bot.db, guild.id, user.id)
        if ban is None:
            return

        logger.info(
            "Banned reaction attempt: guild=%s user=%s channel=%s message=%s emoji=%s reason=%s",
            guild.id,
            user.id,
            reaction.message.channel.id,
            reaction.message.id,
            reaction.emoji,
            ban["reason"],
        )

        try:
            await reaction.remove(user)
        except discord.Forbidden as exc:
            logger.warning(
                "Failed to remove reaction: missing permissions. guild=%s user=%s message=%s error=%s",
                guild.id,
                user.id,
                reaction.message.id,
                exc,
            )
        except discord.NotFound as exc:
            logger.info(
                "Failed to remove reaction: reaction was already gone. guild=%s user=%s message=%s error=%s",
                guild.id,
                user.id,
                reaction.message.id,
                exc,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "Failed to remove reaction: Discord HTTP error. guild=%s user=%s message=%s error=%s",
                guild.id,
                user.id,
                reaction.message.id,
                exc,
            )
        else:
            logger.info(
                "Reaction removed: guild=%s user=%s channel=%s message=%s emoji=%s",
                guild.id,
                user.id,
                reaction.message.channel.id,
                reaction.message.id,
                reaction.emoji,
            )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ReactionBanCog(bot))

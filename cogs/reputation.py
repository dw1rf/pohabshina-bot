from __future__ import annotations

import asyncio
import logging
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.leaderboard_image import make_reputation_file, resolve_avatar_bytes
from utils.message_commands import REPUTATION_COMMANDS, reputation_change

logger = logging.getLogger(__name__)

REP_COMMANDS = REPUTATION_COMMANDS

DISCORD_NICKNAME_MAX_LENGTH = 32


def format_reputation_nickname(
    current_name: str,
    *,
    previous_total: int,
    new_total: int,
) -> str | None:
    """Build a signed reputation prefix while preserving the member's name."""
    previous_prefix = f"{previous_total:+d} "
    base_name = (
        current_name[len(previous_prefix) :]
        if current_name.startswith(previous_prefix)
        else current_name
    )
    nickname = f"{new_total:+d} {base_name}"
    if len(nickname) > DISCORD_NICKNAME_MAX_LENGTH:
        return None
    return nickname


class ReputationCog(commands.Cog):
    reputation_admin_group = app_commands.Group(
        name="reputation_admin",
        description="Административное управление репутацией",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.last_messages_by_channel: dict[int, deque[discord.Message]] = {}
        self._giver_locks: dict[tuple[int, int], asyncio.Lock] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        value = reputation_change(message.content)
        if value is not None:
            await self._handle_reputation_message(message, value)
            return

        if self._is_regular_target_message(message):
            channel_messages = self.last_messages_by_channel.setdefault(
                message.channel.id,
                deque(maxlen=50),
            )
            channel_messages.append(message)

    def _is_regular_target_message(self, message: discord.Message) -> bool:
        content = (message.content or "").strip()
        if not content:
            return False
        if content.startswith("/"):
            return False

        command_prefix = self.bot.command_prefix
        prefixes: tuple[str, ...]
        if isinstance(command_prefix, str):
            prefixes = (command_prefix,)
        elif isinstance(command_prefix, (list, tuple)):
            prefixes = tuple(prefix for prefix in command_prefix if isinstance(prefix, str))
        else:
            prefixes = ()
        return not prefixes or not content.startswith(prefixes)

    async def _handle_reputation_message(self, message: discord.Message, value: int) -> None:
        db = getattr(self.bot, "reputation_db", None) or self.bot.db
        if db is None:
            logger.warning("Reputation command ignored because database is not initialized")
            return

        try:
            target_message = await self._resolve_target_message(message)
            if target_message is None:
                await message.channel.send(
                    "Не понял, кому выдать репутацию. Ответьте на сообщение игрока "
                    "или напишите +реп/-реп сразу после его сообщения."
                )
                return

            receiver = target_message.author
            if receiver.id == message.author.id:
                await message.channel.send("Нельзя менять репутацию самому себе.")
                return
            if receiver.bot:
                await message.channel.send("Ботам репутацию менять нельзя.")
                return

            locks = getattr(self, "_giver_locks", None)
            if locks is None:
                locks = self._giver_locks = {}
            giver_key = (message.guild.id, message.author.id)
            giver_lock = locks.setdefault(giver_key, asyncio.Lock())
            # Keep per-giver locks for the cog lifetime. Removing a lock after
            # release can race a queued waiter and let a third task create a
            # second lock for the same giver.
            async with giver_lock:
                can_give = await self.bot.reputation.can_give_rep(
                    db,
                    message.guild.id,
                    message.author.id,
                )
                if not can_give:
                    await message.channel.send("Лимит репутации: 2 раза в 24 часа.")
                    return

                rep_type = "plus" if value > 0 else "minus"
                await self.bot.reputation.add_rep_event(
                    db,
                    guild_id=message.guild.id,
                    giver_user_id=message.author.id,
                    receiver_user_id=receiver.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    rep_type=rep_type,
                    target_message_id=target_message.id,
                )
            positive_rep, negative_rep = await self.bot.reputation.get_user_rep(
                db,
                message.guild.id,
                receiver.id,
            )
            total_rep = positive_rep - negative_rep
            if isinstance(receiver, discord.Member):
                await self._sync_member_reputation_nickname(
                    receiver,
                    previous_total=total_rep - value,
                    new_total=total_rep,
                )
            try:
                await self._send_reputation_card(message, receiver, value, total_rep)
            except Exception:
                logger.exception("Reputation changed but the result card could not be sent: message=%s", message.id)
                await message.channel.send("Репутация изменена, но не удалось отправить картинку.")
        except Exception:
            logger.exception("Failed to process reputation message %s", message.id)
            await message.channel.send("Произошла ошибка при изменении репутации. Попробуйте позже.")

    async def _sync_member_reputation_nickname(
        self,
        member: discord.Member,
        *,
        previous_total: int,
        new_total: int,
    ) -> bool:
        current_name = member.nick or member.display_name
        nickname = format_reputation_nickname(
            current_name,
            previous_total=previous_total,
            new_total=new_total,
        )
        if nickname is None:
            logger.warning(
                "Reputation nickname skipped because it would exceed Discord's limit: guild=%s user=%s",
                member.guild.id,
                member.id,
            )
            return False

        try:
            await member.edit(
                nick=nickname,
                reason="Обновление репутации участника",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Failed to update reputation nickname: guild=%s user=%s",
                member.guild.id,
                member.id,
                exc_info=True,
            )
            return False
        return True

    async def _send_reputation_card(
        self,
        message: discord.Message,
        receiver: discord.Member | discord.User,
        value: int,
        total_rep: int,
    ) -> None:
        display_name = receiver.display_name
        reputation_prefix = f"{total_rep:+d} "
        if display_name.startswith(reputation_prefix):
            display_name = display_name[len(reputation_prefix) :]
        avatar = await resolve_avatar_bytes(self.bot, message.guild, receiver.id)
        file = make_reputation_file(
            display_name,
            value,
            total_rep,
            avatar=avatar,
            filename=f"reputation-{message.id}.png",
        )
        await message.channel.send(
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @reputation_admin_group.command(name="change", description="Изменить репутацию участника на указанное число")
    @app_commands.describe(member="Участник", amount="Положительное число добавит репутацию, отрицательное — убавит")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_change_reputation(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, -1_000_000, 1_000_000],
    ) -> None:
        db = getattr(self.bot, "reputation_db", None) or self.bot.db
        if interaction.guild is None or db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if int(amount) == 0:
            await interaction.response.send_message("Изменение репутации не может быть нулевым.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Ботам репутацию менять нельзя.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            previous_total, new_total = await self.bot.reputation.adjust_reputation(
                db,
                guild_id=interaction.guild.id,
                actor_user_id=interaction.user.id,
                receiver_user_id=member.id,
                channel_id=interaction.channel_id or 0,
                interaction_id=interaction.id,
                delta=int(amount),
            )
        except Exception:
            logger.exception(
                "Failed to adjust reputation from admin command: guild=%s actor=%s receiver=%s delta=%s",
                interaction.guild.id,
                interaction.user.id,
                member.id,
                amount,
            )
            await interaction.followup.send(
                "Не удалось изменить репутацию. Попробуйте позже.",
                ephemeral=True,
            )
            return
        nickname_changed = await self._sync_member_reputation_nickname(
            member,
            previous_total=previous_total,
            new_total=new_total,
        )
        nickname_status = "Ник обновлён." if nickname_changed else "Репутация сохранена, но ник изменить не удалось."
        await interaction.followup.send(
            f"Репутация {member.mention}: **{previous_total:+d} → {new_total:+d}**. {nickname_status}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _resolve_target_message(self, message: discord.Message) -> discord.Message | None:
        reply_target = await self._resolve_reply_target(message)
        if reply_target is not None:
            return reply_target

        channel_messages = self.last_messages_by_channel.get(message.channel.id)
        if channel_messages is None:
            return None

        for target in reversed(channel_messages):
            if target.guild is None or target.guild.id != message.guild.id:
                continue
            if target.author.bot or target.author.id == message.author.id:
                continue
            return target
        return None

    async def _resolve_reply_target(self, message: discord.Message) -> discord.Message | None:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None

        if isinstance(reference.resolved, discord.Message):
            return reference.resolved

        channel = message.channel
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(reference.message_id)  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Failed to fetch replied message %s", reference.message_id)
            return None


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ReputationCog(bot))

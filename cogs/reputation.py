from __future__ import annotations

import asyncio
import logging
import random
from collections import deque

import discord
from discord.ext import commands

from bot_client import MovieBot
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
            await self._send_reputation_embed(message, receiver, value, total_rep)
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

    async def _send_reputation_embed(
        self,
        message: discord.Message,
        receiver: discord.Member | discord.User,
        value: int,
        total_rep: int,
    ) -> None:
        is_positive = value > 0
        change_text = "➕ Получена положительная репутация" if is_positive else "➖ Получена отрицательная репутация"
        change_value = f"{value:+d}"
        color = discord.Color.green() if is_positive else discord.Color.red()
        phrases = self.bot.engagement_content.list("reputation_messages")
        phrase = random.choice(phrases) if phrases else "💬 Репутация показывает доверие сообщества."

        embed = discord.Embed(
            title="╭──── ❤️ РЕПУТАЦИЯ ❤️ ────╮",
            description=(
                f"👤 {message.author.mention} оценил участника {receiver.mention}\n\n"
                f"{change_text}\n\n"
                f"📈 Изменение репутации: **{change_value}**\n\n"
                f"⭐ Всего репутации: **{total_rep}**\n\n"
                f"{phrase}\n\n"
                "╰────────────────────────╯"
            ),
            color=color,
        )
        embed.set_thumbnail(url=receiver.display_avatar.url)
        await message.channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True),
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

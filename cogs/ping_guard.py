from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)

PING_WINDOW = timedelta(minutes=10)
PING_LIMIT = 3
DM_COOLDOWN = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


async def notify_user_smart(
    bot: MovieBot,
    guild: discord.Guild,
    target_user: discord.abc.User,
    public_channel: discord.abc.Messageable | None,
    text: str,
    reason: str,
    actor: discord.abc.User | None = None,
    allow_public_first: bool = True,
) -> bool:
    """Notify a user while moving repeated notifications to DM.

    Returns True when the notification was delivered to DM. Public fallback returns False.
    """
    if bot.db is None:
        return False
    now = utcnow()
    since = iso(now - DM_COOLDOWN)
    actor_id = actor.id if actor else None
    cursor = await bot.db.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM smart_notifications
        WHERE guild_id = ? AND target_user_id = ? AND reason = ? AND created_at >= ?
        """,
        (guild.id, target_user.id, reason, since),
    )
    row = await cursor.fetchone()
    recent_count = int(row["cnt"] if row else 0)
    if recent_count > 0:
        logger.debug("Smart notification suppressed by cooldown: guild=%s target=%s reason=%s", guild.id, target_user.id, reason)
        return True
    send_dm = recent_count > 0 or not allow_public_first

    if send_dm:
        try:
            await target_user.send(text)
        except discord.Forbidden:
            logger.info("Smart notification DM is closed: guild=%s target=%s reason=%s", guild.id, target_user.id, reason)
        except discord.HTTPException:
            logger.exception("Smart notification DM failed: guild=%s target=%s reason=%s", guild.id, target_user.id, reason)
        else:
            await bot.db.execute(
                """
                INSERT INTO smart_notifications
                    (guild_id, target_user_id, actor_id, reason, public_channel_id, sent_to_dm, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (guild.id, target_user.id, actor_id, reason, getattr(public_channel, "id", None), iso(now)),
            )
            await bot.db.commit()
            return True

    if public_channel is not None:
        try:
            await public_channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Smart notification public fallback failed: guild=%s target=%s reason=%s", guild.id, target_user.id, reason)
            return False
        await bot.db.execute(
            """
            INSERT INTO smart_notifications
                (guild_id, target_user_id, actor_id, reason, public_channel_id, sent_to_dm, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (guild.id, target_user.id, actor_id, reason, getattr(public_channel, "id", None), iso(now)),
        )
        await bot.db.commit()
    return False


class PingGuardCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await self.init_db()

    async def init_db(self) -> None:
        if self.bot.db is None:
            raise RuntimeError("Database is not initialized")
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_last_seen (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_message_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS ping_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS smart_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                actor_id INTEGER,
                reason TEXT NOT NULL,
                public_channel_id INTEGER,
                sent_to_dm INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ping_events_pair_created
                ON ping_events(guild_id, author_id, target_user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_smart_notifications_target_reason
                ON smart_notifications(guild_id, target_user_id, reason, created_at);
            """
        )
        await self.bot.db.commit()

    async def update_last_seen(self, guild_id: int, user_id: int) -> None:
        if self.bot.db is None:
            return
        await self.bot.db.execute(
            """
            INSERT INTO user_last_seen (guild_id, user_id, last_message_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                last_message_at = excluded.last_message_at
            """,
            (guild_id, user_id, iso(utcnow())),
        )
        await self.bot.db.commit()

    async def target_recently_active(self, guild_id: int, user_id: int) -> bool:
        if self.bot.db is None:
            return True
        cursor = await self.bot.db.execute(
            "SELECT last_message_at FROM user_last_seen WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        last_seen = datetime.fromisoformat(str(row["last_message_at"]))
        return last_seen >= utcnow() - PING_WINDOW

    async def record_ping(self, guild_id: int, channel_id: int, author_id: int, target_user_id: int) -> int:
        assert self.bot.db is not None
        now = utcnow()
        await self.bot.db.execute(
            """
            INSERT INTO ping_events (guild_id, channel_id, author_id, target_user_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, author_id, target_user_id, iso(now)),
        )
        since = iso(now - PING_WINDOW)
        cursor = await self.bot.db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM ping_events
            WHERE guild_id = ? AND author_id = ? AND target_user_id = ? AND created_at >= ?
            """,
            (guild_id, author_id, target_user_id, since),
        )
        row = await cursor.fetchone()
        await self.bot.db.commit()
        return int(row["cnt"] if row else 0)

    async def warn_channel(self, message: discord.Message) -> None:
        try:
            if (
                self.bot.settings.ping_guard_delete_repeats
                and isinstance(message.channel, discord.TextChannel)
                and message.guild
                and message.guild.me
                and message.channel.permissions_for(message.guild.me).manage_messages
            ):
                await message.delete()
                await message.channel.send(
                    "Я уже отправил уведомление в ЛС. Дальше без спама в общий чат.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await message.reply(
                "Я уже отправил уведомление в ЛС. Дальше без спама в общий чат.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Failed to warn about repeated pings: guild=%s", message.guild.id if message.guild else None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.db is None:
            return
        await self.update_last_seen(message.guild.id, message.author.id)
        targets = [member for member in message.mentions if not member.bot and member.id != message.author.id]
        if not targets or len(targets) > 3:
            return

        notified = False
        for target in targets:
            count = await self.record_ping(message.guild.id, message.channel.id, message.author.id, target.id)
            if count <= PING_LIMIT:
                continue
            if await self.target_recently_active(message.guild.id, target.id):
                continue
            if notified:
                continue
            channel_name = getattr(message.channel, "mention", f"#{getattr(message.channel, 'name', 'канал')}")
            dm_text = (
                f"{message.author.display_name} пытается до тебя достучаться на сервере {message.guild.name}. "
                f"Канал: {channel_name}."
            )
            sent_dm = await notify_user_smart(
                self.bot,
                message.guild,
                target,
                message.channel,
                dm_text,
                reason=f"ping_guard:{message.author.id}:{target.id}",
                actor=message.author,
                allow_public_first=False,
            )
            if sent_dm:
                await self.warn_channel(message)
            notified = True


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(PingGuardCog(bot))

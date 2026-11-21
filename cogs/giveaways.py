from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from utils.brand import BRAND_COLOR
from utils.sqlite_writes import sqlite_write_lock

logger = logging.getLogger(__name__)


@asynccontextmanager
async def giveaway_write(db: aiosqlite.Connection):
    async with sqlite_write_lock(db):
        try:
            yield
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


class GiveawayView(discord.ui.View):
    def __init__(self, bot: MovieBot, giveaway_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.giveaway_id = giveaway_id
        button = discord.ui.Button(
            label="Участвовать",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"vulgarities:giveaway:join:{giveaway_id}",
        )
        button.callback = self.join
        self.add_item(button)

    async def join(self, interaction: discord.Interaction) -> None:
        if self.bot.giveaway_db is None or interaction.guild is None:
            await interaction.response.send_message("Розыгрыш недоступен.", ephemeral=True)
            return
        cursor = await self.bot.giveaway_db.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?",
            (self.giveaway_id, interaction.guild.id),
        )
        giveaway = await cursor.fetchone()
        if giveaway is not None and giveaway["status"] == "pending":
            await interaction.response.send_message("Розыгрыш публикуется. Нажмите кнопку ещё раз чуть позже.", ephemeral=True)
            return
        if giveaway is None or giveaway["status"] != "active":
            await interaction.response.send_message("Этот розыгрыш уже завершён.", ephemeral=True)
            return
        required_role_id = int(giveaway["required_role_id"] or 0)
        if required_role_id and isinstance(interaction.user, discord.Member) and interaction.user.get_role(required_role_id) is None:
            await interaction.response.send_message(f"Для участия нужна роль <@&{required_role_id}>.", ephemeral=True)
            return
        async with giveaway_write(self.bot.giveaway_db):
            await self.bot.giveaway_db.execute(
                "INSERT OR IGNORE INTO giveaway_entries(giveaway_id, user_id) VALUES (?, ?)",
                (self.giveaway_id, interaction.user.id),
            )
        await interaction.response.send_message("Ты участвуешь в розыгрыше 🎉", ephemeral=True)


class GiveawaysCog(commands.Cog):
    giveaway_group = app_commands.Group(
        name="giveaway",
        description="Розыгрыши с автозавершением",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._finish_lock = asyncio.Lock()
        self._publish_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if self.bot.giveaway_db is None:
            return
        await self.bot.giveaway_db.executescript(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                giveaway_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                host_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                required_role_id INTEGER,
                winner_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(giveaway_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                draw_number INTEGER NOT NULL DEFAULT 1,
                selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(giveaway_id, user_id, draw_number)
            );
            """
        )
        await self.bot.giveaway_db.commit()
        cursor = await self.bot.giveaway_db.execute("SELECT giveaway_id, message_id FROM giveaways WHERE status = 'active' AND message_id IS NOT NULL")
        for row in await cursor.fetchall():
            self.bot.add_view(GiveawayView(self.bot, int(row["giveaway_id"])), message_id=int(row["message_id"]))
        if not self.finish_loop.is_running():
            self.finish_loop.start()

    async def cog_unload(self) -> None:
        self.finish_loop.cancel()

    def _embed(self, prize: str, ends_at: datetime, host_id: int, required_role_id: int = 0) -> discord.Embed:
        embed = discord.Embed(title="🎉 Розыгрыш", description=f"**Приз:** {prize}", color=BRAND_COLOR)
        embed.add_field(name="Завершение", value=discord.utils.format_dt(ends_at, "R"), inline=False)
        embed.add_field(name="Организатор", value=f"<@{host_id}>")
        if required_role_id:
            embed.add_field(name="Условие", value=f"Роль <@&{required_role_id}>")
        embed.set_footer(text="Нажми кнопку ниже, чтобы участвовать")
        return embed

    @giveaway_group.command(name="create", description="Создать розыгрыш")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def create(
        self,
        interaction: discord.Interaction,
        prize: app_commands.Range[str, 1, 200],
        duration_minutes: app_commands.Range[int, 1, 43200],
        winners: app_commands.Range[int, 1, 20] = 1,
        required_role: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel) or self.bot.giveaway_db is None:
            await interaction.response.send_message("Команда доступна только в текстовом канале сервера.", ephemeral=True)
            return
        ends_at = datetime.now(UTC) + timedelta(minutes=int(duration_minutes))
        # Reserve a real Discord message so a retry always edits the same post.
        response = await interaction.response.send_message("Готовлю розыгрыш…")
        try:
            message_id = response.message_id or (await interaction.original_response()).id
            async with giveaway_write(self.bot.giveaway_db):
                cursor = await self.bot.giveaway_db.execute(
                    """INSERT INTO giveaways(guild_id, channel_id, message_id, host_id, prize, ends_at, required_role_id, winner_count, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (interaction.guild.id, interaction.channel.id, message_id, interaction.user.id, str(prize),
                     ends_at.isoformat(), required_role.id if required_role else None, int(winners)),
                )
        except (aiosqlite.Error, discord.HTTPException):
            logger.exception("Failed to register giveaway in channel %s", interaction.channel.id)
            await interaction.edit_original_response(content="Создать розыгрыш не удалось. Попробуйте ещё раз.")
            return
        await self._publish(int(cursor.lastrowid), interaction.channel)

    async def _publish(self, giveaway_id: int, channel: discord.TextChannel) -> None:
        db = self.bot.giveaway_db
        assert db is not None
        async with self._publish_lock:
            cursor = await db.execute("SELECT * FROM giveaways WHERE giveaway_id = ? AND status = 'pending'", (giveaway_id,))
            row = await cursor.fetchone()
            if row is None:
                return
            ends_at = datetime.fromisoformat(row["ends_at"])
            message = channel.get_partial_message(row["message_id"])
            try:
                if ends_at <= datetime.now(UTC):
                    await message.edit(content="Розыгрыш отменён: время участия истекло.", embed=None, view=None)
                    status = "cancelled"
                else:
                    await message.edit(
                        content=None,
                        embed=self._embed(row["prize"], ends_at, row["host_id"], row["required_role_id"]),
                        view=GiveawayView(self.bot, giveaway_id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    status = "active"
            except discord.NotFound:
                status = "cancelled"
            except discord.HTTPException:
                logger.warning("Could not publish giveaway %s; publication will retry", giveaway_id)
                return
            async with giveaway_write(db):
                await db.execute("UPDATE giveaways SET status = ? WHERE giveaway_id = ?", (status, giveaway_id))

    async def _finish(self, giveaway_id: int, guild_id: int, *, reroll: bool = False) -> list[int]:
        async with self._finish_lock:
            return await self._finish_locked(giveaway_id, guild_id, reroll=reroll)

    async def _finish_locked(self, giveaway_id: int, guild_id: int, *, reroll: bool = False) -> list[int]:
        if self.bot.giveaway_db is None:
            return []
        cursor = await self.bot.giveaway_db.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?",
            (giveaway_id, guild_id),
        )
        giveaway = await cursor.fetchone()
        allowed_statuses = ("active", "ended") if reroll else ("active",)
        if giveaway is None or giveaway["status"] not in allowed_statuses:
            return []
        cursor = await self.bot.giveaway_db.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
        candidates = [int(row["user_id"]) for row in await cursor.fetchall()]
        count = min(int(giveaway["winner_count"]), len(candidates))
        winners: list[int] = []
        pool = candidates[:]
        for _ in range(count):
            winner = secrets.choice(pool)
            pool.remove(winner)
            winners.append(winner)
        cursor = await self.bot.giveaway_db.execute("SELECT COALESCE(MAX(draw_number), 0) + 1 AS draw FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,))
        draw_number = int((await cursor.fetchone())["draw"])
        async with giveaway_write(self.bot.giveaway_db):
            for winner in winners:
                await self.bot.giveaway_db.execute(
                    "INSERT INTO giveaway_winners(giveaway_id, user_id, draw_number) VALUES (?, ?, ?)",
                    (giveaway_id, winner, draw_number),
                )
            if not reroll:
                await self.bot.giveaway_db.execute("UPDATE giveaways SET status = 'ended' WHERE giveaway_id = ?", (giveaway_id,))
        guild = self.bot.get_guild(int(giveaway["guild_id"]))
        channel = guild.get_channel(int(giveaway["channel_id"])) if guild else None
        if isinstance(channel, discord.TextChannel):
            mentions = ", ".join(f"<@{user_id}>" for user_id in winners) or "участников нет"
            prefix = "Перевыбор" if reroll else "Розыгрыш завершён"
            await channel.send(
                f"🎉 **{prefix}: {giveaway['prize']}**\nПобедители: {mentions}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            if not reroll and giveaway["message_id"]:
                try:
                    message = await channel.fetch_message(int(giveaway["message_id"]))
                    await message.edit(view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        return winners

    @giveaway_group.command(name="end", description="Досрочно завершить розыгрыш")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        winners = await self._finish(giveaway_id, interaction.guild.id)
        await interaction.response.send_message(
            "Розыгрыш завершён." if winners else "Розыгрыш не найден, уже завершён или не имеет участников.", ephemeral=True
        )

    @giveaway_group.command(name="reroll", description="Повторно выбрать победителей")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        winners = await self._finish(giveaway_id, interaction.guild.id, reroll=True)
        await interaction.response.send_message("Победители выбраны заново." if winners else "Нет участников.", ephemeral=True)

    @tasks.loop(seconds=30)
    async def finish_loop(self) -> None:
        if self.bot.giveaway_db is None:
            return
        cursor = await self.bot.giveaway_db.execute("SELECT giveaway_id, channel_id FROM giveaways WHERE status = 'pending'")
        for row in await cursor.fetchall():
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                await self._publish(row["giveaway_id"], channel)
        cursor = await self.bot.giveaway_db.execute(
            "SELECT giveaway_id, guild_id FROM giveaways WHERE status = 'active' AND ends_at <= ?", (datetime.now(UTC).isoformat(),)
        )
        for row in await cursor.fetchall():
            try:
                await self._finish(int(row["giveaway_id"]), int(row["guild_id"]))
            except Exception:
                logger.exception("Failed to finish giveaway %s", row["giveaway_id"])

    @finish_loop.before_loop
    async def before_finish_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(GiveawaysCog(bot))

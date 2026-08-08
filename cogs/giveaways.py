from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from utils.brand import BRAND_ACCENT

logger = logging.getLogger(__name__)


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
        if giveaway is None or giveaway["status"] != "active":
            await interaction.response.send_message("Этот розыгрыш уже завершён.", ephemeral=True)
            return
        required_role_id = int(giveaway["required_role_id"] or 0)
        if required_role_id and isinstance(interaction.user, discord.Member) and interaction.user.get_role(required_role_id) is None:
            await interaction.response.send_message(f"Для участия нужна роль <@&{required_role_id}>.", ephemeral=True)
            return
        await self.bot.giveaway_db.execute(
            "INSERT OR IGNORE INTO giveaway_entries(giveaway_id, user_id) VALUES (?, ?)",
            (self.giveaway_id, interaction.user.id),
        )
        await self.bot.giveaway_db.commit()
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
        embed = discord.Embed(title="🎉 Розыгрыш", description=f"**Приз:** {prize}", color=BRAND_ACCENT)
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
        cursor = await self.bot.giveaway_db.execute(
            """INSERT INTO giveaways(guild_id, channel_id, host_id, prize, ends_at, required_role_id, winner_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (interaction.guild.id, interaction.channel.id, interaction.user.id, str(prize), ends_at.isoformat(), required_role.id if required_role else None, int(winners)),
        )
        giveaway_id = int(cursor.lastrowid)
        view = GiveawayView(self.bot, giveaway_id)
        await interaction.response.send_message(
            embed=self._embed(str(prize), ends_at, interaction.user.id, required_role.id if required_role else 0),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        message = await interaction.original_response()
        await self.bot.giveaway_db.execute("UPDATE giveaways SET message_id = ? WHERE giveaway_id = ?", (message.id, giveaway_id))
        await self.bot.giveaway_db.commit()

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
        if giveaway is None or (giveaway["status"] != "active" and not reroll):
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
        for winner in winners:
            await self.bot.giveaway_db.execute(
                "INSERT INTO giveaway_winners(giveaway_id, user_id, draw_number) VALUES (?, ?, ?)",
                (giveaway_id, winner, draw_number),
            )
        if not reroll:
            await self.bot.giveaway_db.execute("UPDATE giveaways SET status = 'ended' WHERE giveaway_id = ?", (giveaway_id,))
        await self.bot.giveaway_db.commit()
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

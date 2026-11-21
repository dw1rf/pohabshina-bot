from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

MAX_DAYS = 90
PAGE_SIZE = 15


class CommandStatsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.db: aiosqlite.Connection | None = None
        self.last_cleanup: date | None = None

    async def cog_load(self) -> None:
        # A completed command can be recorded while another cog is mid-transaction.
        self.db = await aiosqlite.connect(self.bot.settings.db_path, isolation_level=None, timeout=10)
        try:
            await self.db.executescript(
                """CREATE TABLE IF NOT EXISTS command_usage_daily (
                       guild_id INTEGER NOT NULL, day TEXT NOT NULL, user_id INTEGER NOT NULL,
                       command TEXT NOT NULL, uses INTEGER NOT NULL,
                       PRIMARY KEY (guild_id, day, user_id, command)
                   ) WITHOUT ROWID;
                   CREATE TRIGGER IF NOT EXISTS command_usage_privacy
                   AFTER UPDATE OF analytics_enabled ON user_privacy_settings
                   WHEN NEW.analytics_enabled = 0
                   BEGIN
                       DELETE FROM command_usage_daily WHERE guild_id = NEW.guild_id AND user_id = NEW.user_id;
                   END;"""
            )
            await self._prune(datetime.now(UTC).date())
        except BaseException:
            await self.cog_unload()
            raise

    async def cog_unload(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _prune(self, today: date) -> None:
        if self.db is not None and self.last_cleanup != today:
            first_day = today - timedelta(days=MAX_DAYS - 1)
            await self.db.execute("DELETE FROM command_usage_daily WHERE day < ?", (first_day.isoformat(),))
            self.last_cleanup = today

    async def _record(self, guild_id: int, user_id: int, command: str) -> None:
        if self.db is None:
            return
        today = datetime.now(UTC).date()
        await self._prune(today)
        await self.db.execute(
            """INSERT INTO command_usage_daily(guild_id, day, user_id, command, uses)
               SELECT ?, ?, ?, ?, 1 WHERE NOT EXISTS (
                   SELECT 1 FROM user_privacy_settings WHERE guild_id = ? AND user_id = ? AND analytics_enabled = 0
               )
               ON CONFLICT(guild_id, day, user_id, command) DO UPDATE SET uses = uses + 1""",
            (guild_id, today.isoformat(), user_id, command, guild_id, user_id),
        )

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command) -> None:
        if interaction.guild_id and not interaction.user.bot and command.qualified_name != "command_stats":
            await self._record(interaction.guild_id, interaction.user.id, "/" + command.qualified_name)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if ctx.guild and ctx.command and not ctx.author.bot and ctx.interaction is None:
            await self._record(ctx.guild.id, ctx.author.id, "!" + ctx.command.qualified_name)

    @app_commands.command(name="command_stats", description="Какими командами пользуются участники сервера")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Команды одного участника", command="Например /music play или !play",
                           days="Календарные дни UTC, включая сегодня", page="Номер страницы")
    async def command_stats(
        self, interaction: discord.Interaction, member: discord.Member | None = None,
        command: app_commands.Range[str, 1, 100] | None = None, days: app_commands.Range[int, 1, 90] = 30,
        page: app_commands.Range[int, 1, 1000000] = 1,
    ) -> None:
        if self.db is None or interaction.guild_id is None:
            await interaction.response.send_message("Статистика сейчас недоступна.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        conditions = ["guild_id = ?", "day BETWEEN ? AND ?"]
        params: list[int | str] = [interaction.guild_id, start.isoformat(), today.isoformat()]
        if member is not None:
            conditions.append("user_id = ?")
            params.append(member.id)
        if command is not None:
            command = " ".join(command.lower().split())
            if not command.startswith(("/", "!")):
                command = "/" + command
            conditions.append("command = ?")
            params.append(command)

        group = "user_id" if command else "command"
        async with self.db.execute(
            f"""SELECT {group}, SUM(uses) AS total, COUNT(DISTINCT user_id), COUNT(*) OVER ()
                FROM command_usage_daily WHERE {' AND '.join(conditions)}
                GROUP BY {group} ORDER BY total DESC, {group} LIMIT ? OFFSET ?""",
            (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            text = "За выбранный период вызовов не найдено." if page == 1 else "Такой страницы нет."
            await interaction.followup.send(text, ephemeral=True)
            return
        lines = []
        for key, total, users, _ in rows:
            if command:
                lines.append(f"<@{key}> — {total}")
            else:
                lines.append(f"`{key}` — {total} · участников: {users}")
        embed = discord.Embed(title="Использование команд", description="\n".join(lines))
        if command:
            embed.add_field(name="Команда", value=f"`{command}`")
        if member is not None:
            embed.add_field(name="Участник", value=member.mention)
        pages = (rows[0][3] + PAGE_SIZE - 1) // PAGE_SIZE
        embed.set_footer(text=f"{start:%d.%m.%Y} — {today:%d.%m.%Y} UTC · Страница {page}/{pages}")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(CommandStatsCog(bot))

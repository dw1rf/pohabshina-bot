from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

MAX_DAYS = 90
PAGE_SIZE = 15
PASSIVE_MODULES = {"nsfw_import", "ping_guard"}


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
                   END;
                   CREATE TABLE IF NOT EXISTS module_interactions_daily (
                       guild_id INTEGER NOT NULL, day TEXT NOT NULL, user_id INTEGER NOT NULL,
                       module TEXT NOT NULL, kind TEXT NOT NULL, uses INTEGER NOT NULL,
                       PRIMARY KEY (guild_id, day, user_id, module, kind)
                   ) WITHOUT ROWID;
                   CREATE TRIGGER IF NOT EXISTS module_interactions_privacy
                   AFTER UPDATE OF analytics_enabled ON user_privacy_settings
                   WHEN NEW.analytics_enabled = 0
                   BEGIN
                       DELETE FROM module_interactions_daily WHERE guild_id = NEW.guild_id AND user_id = NEW.user_id;
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
            await self.db.execute("DELETE FROM module_interactions_daily WHERE day < ?", (first_day.isoformat(),))
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
    async def on_module_activity(self, guild_id: int, user_id: int, module: str, kind: str) -> None:
        if self.db is None:
            return
        today = datetime.now(UTC).date()
        await self._prune(today)
        await self.db.execute(
            """INSERT INTO module_interactions_daily(guild_id, day, user_id, module, kind, uses)
               SELECT ?, ?, ?, ?, ?, 1 WHERE NOT EXISTS (
                   SELECT 1 FROM user_privacy_settings WHERE guild_id = ? AND user_id = ? AND analytics_enabled = 0
               )
               ON CONFLICT(guild_id, day, user_id, module, kind) DO UPDATE SET uses = uses + 1""",
            (guild_id, today.isoformat(), user_id, module, kind, guild_id, user_id),
        )

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command) -> None:
        if interaction.guild_id and not interaction.user.bot and command.qualified_name != "command_stats":
            await self._record(interaction.guild_id, interaction.user.id, "/" + command.qualified_name)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if ctx.guild and ctx.command and not ctx.author.bot and ctx.interaction is None:
            await self._record(ctx.guild.id, ctx.author.id, "!" + ctx.command.qualified_name)

    def _command_owners(self, guild: discord.Object) -> dict[str, str]:
        owners = {}
        # Guild-specific commands shadow global names. Include dynamically registered RP commands.
        for scope in (None, guild):
            for cmd in self.bot.tree.walk_commands(guild=scope):
                if isinstance(cmd, app_commands.Command):
                    owners["/" + cmd.qualified_name] = cmd.callback.__module__.removeprefix("cogs.")
        for cmd in self.bot.walk_commands():
            owners["!" + cmd.qualified_name] = cmd.callback.__module__.removeprefix("cogs.")
        return owners

    async def _module_report(
        self, interaction: discord.Interaction, member: discord.Member | None,
        start: date, today: date, page: int, inactive_only: bool,
    ) -> None:
        assert self.db is not None and interaction.guild_id is not None
        modules = {name.removeprefix("cogs.") for name in self.bot.extensions if name.startswith("cogs.")}
        modules.discard("command_stats")
        owners = self._command_owners(discord.Object(id=interaction.guild_id))
        pairs = [(command, module) for command, module in owners.items() if module in modules]
        mapping = ("VALUES " + ",".join("(?, ?)" for _ in pairs)) if pairs else "SELECT NULL, NULL WHERE 0"
        condition = "guild_id = ? AND day BETWEEN ? AND ?"
        filters = [interaction.guild_id, start.isoformat(), today.isoformat()]
        if member is not None:
            condition += " AND user_id = ?"
            filters.append(member.id)
        # Combine both sources before counting distinct people. Historical command
        # aggregates remain usable without copying them into a second counter.
        async with self.db.execute(
            f"""WITH owner(name, module) AS ({mapping}), activity AS (
                    SELECT owner.module, user_id, 'command' AS kind, uses FROM command_usage_daily
                    JOIN owner ON command = owner.name WHERE {condition}
                    UNION ALL
                    SELECT module, user_id, kind, uses FROM module_interactions_daily WHERE {condition}
                )
                SELECT module, SUM(uses), COUNT(DISTINCT user_id),
                       SUM(CASE WHEN kind='command' THEN uses ELSE 0 END),
                       SUM(CASE WHEN kind='component' THEN uses ELSE 0 END),
                       SUM(CASE WHEN kind='reaction' THEN uses ELSE 0 END),
                       SUM(CASE WHEN kind='reputation' THEN uses ELSE 0 END)
                FROM activity GROUP BY module""",
            (*[value for pair in pairs for value in pair], *filters, *filters),
        ) as cursor:
            counts = {row[0]: row[1:] for row in await cursor.fetchall()}
        stats = [(name, *counts.get(name, (0, 0, 0, 0, 0, 0))) for name in modules]
        if inactive_only:
            stats = [row for row in stats if row[1] == 0 and row[0] not in PASSIVE_MODULES]
        stats.sort(key=lambda row: (-row[1], -row[2], row[0]))
        rows = stats[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        if not rows:
            text = "Модулей без учтённых действий нет." if not stats and inactive_only else "Такой страницы нет."
            await interaction.followup.send(text, ephemeral=True)
            return
        lines = []
        labels = ("команды", "кнопки/меню", "реакции", "репутация")
        for name, total, users, *kinds in rows:
            if name in PASSIVE_MODULES and not total:
                lines.append(f"**{name}** — автоматический модуль, активность не измеряется")
                continue
            detail = ", ".join(f"{label}: {n}" for label, n in zip(labels, kinds) if n)
            lines.append(f"**{name}** — {total} · участников: {users}" + (f" ({detail})" if detail else ""))
        embed = discord.Embed(title="Активность по модулям", description="\n".join(lines))
        if member is not None:
            embed.add_field(name="Участник", value=member.mention)
        pages = (len(stats) + PAGE_SIZE - 1) // PAGE_SIZE
        embed.set_footer(text=f"{start:%d.%m.%Y} — {today:%d.%m.%Y} UTC · Страница {page}/{pages}\n"
                              "Кнопки, реакции и репутация учитываются с установки обновления. Фоновые задачи и просмотры вне отчёта.")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="command_stats", description="Использование команд и модулей участниками сервера")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Команды одного участника", command="Например /music play или !play",
                           days="Календарные дни UTC, включая сегодня", page="Номер страницы",
                           view="commands — команды, modules — все модули, inactive — модули без учтённых действий")
    async def command_stats(
        self, interaction: discord.Interaction, member: discord.Member | None = None,
        command: app_commands.Range[str, 1, 100] | None = None, days: app_commands.Range[int, 1, 90] = 30,
        page: app_commands.Range[int, 1, 1000000] = 1,
        view: Literal["commands", "modules", "inactive"] = "commands",
    ) -> None:
        if self.db is None or interaction.guild_id is None:
            await interaction.response.send_message("Статистика сейчас недоступна.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        if view != "commands":
            if command is not None:
                await interaction.followup.send("Для фильтра по команде выберите вид commands.", ephemeral=True)
                return
            await self._module_report(interaction, member, start, today, page, view == "inactive")
            return
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

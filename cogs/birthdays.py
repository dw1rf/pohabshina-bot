from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot

logger = logging.getLogger(__name__)

MONTHS_GENITIVE: tuple[str, ...] = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
BIRTHDAY_CHECK_HOUR = 9


def format_birthday(day: int, month: int, year: int | None = None) -> str:
    base = f"{day} {MONTHS_GENITIVE[month - 1]}"
    if year is not None:
        return f"{base} {year}"
    return base


def validate_birthday(day: int, month: int, year: int | None, current_year: int) -> str | None:
    if not 1 <= day <= 31:
        return "День должен быть от 1 до 31."
    if not 1 <= month <= 12:
        return "Месяц должен быть от 1 до 12."
    if year is not None:
        if year < 1900:
            return "Год рождения не может быть меньше 1900."
        if year > current_year:
            return "Год рождения не может быть больше текущего года."
        check_year = year
    else:
        check_year = 2000

    try:
        date(check_year, month, day)
    except ValueError:
        return "Такой даты не существует. Проверьте день и месяц."
    return None


def next_birthday_date(day: int, month: int, today: date) -> date:
    for year in range(today.year, today.year + 8):
        try:
            birthday = date(year, month, day)
        except ValueError:
            continue
        if birthday >= today:
            return birthday
    return date(today.year + 8, 3, 1)


class BirthdaysCog(commands.Cog):
    birthday_group = app_commands.Group(name="др", description="Дни рождения")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._db_lock = asyncio.Lock()
        self._timezone = self._load_timezone()

    async def cog_load(self) -> None:
        if self.bot.db is None:
            logger.warning("Birthdays cog loaded without database connection")
            return
        await self._init_db(self.bot.db)
        self.birthday_check_loop.start()
        logger.info(
            "Birthdays cog loaded: channel=%s timezone=%s check_after=%s:00",
            self.bot.settings.birthday_channel_id or "disabled",
            self._timezone.key,
            BIRTHDAY_CHECK_HOUR,
        )

    def cog_unload(self) -> None:
        self.birthday_check_loop.cancel()

    def _load_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.bot.settings.timezone)
        except ZoneInfoNotFoundError:
            logger.warning("Invalid TIMEZONE=%s; fallback to Europe/Moscow", self.bot.settings.timezone)
            return ZoneInfo("Europe/Moscow")

    async def _init_db(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS birthdays (
                discord_user_id INTEGER PRIMARY KEY,
                day INTEGER NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER DEFAULT NULL,
                last_congratulated_date TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_birthdays_month_day
            ON birthdays (month, day, last_congratulated_date)
            """
        )
        await db.commit()

    @birthday_group.command(name="установить", description="Сохранить дату рождения")
    @app_commands.describe(day="День рождения", month="Месяц рождения", year="Год рождения, если хотите показывать возраст")
    @app_commands.rename(day="день", month="месяц", year="год")
    async def set_birthday(self, interaction: discord.Interaction, day: int, month: int, year: int | None = None) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        now = datetime.now(self._timezone)
        error = validate_birthday(day, month, year, now.year)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        ts = datetime.now(UTC).isoformat()
        async with self._db_lock:
            await self.bot.db.execute(
                """
                INSERT INTO birthdays (discord_user_id, day, month, year, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_user_id)
                DO UPDATE SET
                    day = excluded.day,
                    month = excluded.month,
                    year = excluded.year,
                    updated_at = excluded.updated_at
                """,
                (interaction.user.id, day, month, year, ts, ts),
            )
            await self.bot.db.commit()

        await interaction.response.send_message(
            f"Дата рождения сохранена: {format_birthday(day, month, year)}.",
            ephemeral=True,
        )

    @birthday_group.command(name="удалить", description="Удалить сохраненную дату рождения")
    async def delete_birthday(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        async with self._db_lock:
            cursor = await self.bot.db.execute(
                "DELETE FROM birthdays WHERE discord_user_id = ?",
                (interaction.user.id,),
            )
            await self.bot.db.commit()
            removed = cursor.rowcount > 0

        if removed:
            await interaction.response.send_message("Дата рождения удалена.", ephemeral=True)
        else:
            await interaction.response.send_message("У вас пока нет сохраненной даты рождения.", ephemeral=True)

    @birthday_group.command(name="посмотреть", description="Показать свою сохраненную дату рождения")
    async def show_birthday(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        row = await self._get_user_birthday(interaction.user.id)
        if row is None:
            await interaction.response.send_message("У вас пока нет сохраненной даты рождения.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Ваша дата рождения: {format_birthday(int(row['day']), int(row['month']), row['year'])}.",
            ephemeral=True,
        )

    @birthday_group.command(name="список", description="Показать ближайшие дни рождения")
    async def list_birthdays(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        rows = await self._fetch_all_birthdays()
        if not rows:
            await interaction.response.send_message("Сохраненных дней рождения пока нет.", ephemeral=True)
            return

        today = datetime.now(self._timezone).date()
        upcoming = sorted(
            (
                (next_birthday_date(int(row["day"]), int(row["month"]), today), row)
                for row in rows
            ),
            key=lambda item: (item[0], int(item[1]["discord_user_id"])),
        )[:10]
        lines = []
        for birthday_date, row in upcoming:
            days_left = (birthday_date - today).days
            suffix = "сегодня" if days_left == 0 else f"через {days_left} дн."
            lines.append(
                f"<@{int(row['discord_user_id'])}> — {format_birthday(int(row['day']), int(row['month']))} ({suffix})"
            )

        embed = discord.Embed(
            title="Ближайшие дни рождения",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _get_user_birthday(self, user_id: int) -> aiosqlite.Row | None:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            "SELECT * FROM birthdays WHERE discord_user_id = ?",
            (user_id,),
        )
        return await cursor.fetchone()

    async def _fetch_all_birthdays(self) -> list[aiosqlite.Row]:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            "SELECT discord_user_id, day, month FROM birthdays ORDER BY month, day"
        )
        return await cursor.fetchall()

    @tasks.loop(minutes=60)
    async def birthday_check_loop(self) -> None:
        if self.bot.db is None:
            return

        now = datetime.now(self._timezone)
        if now.hour < BIRTHDAY_CHECK_HOUR:
            return
        channel_id = self.bot.settings.birthday_channel_id
        if not channel_id:
            logger.debug("Birthday check skipped: BIRTHDAY_CHANNEL_ID is not set")
            return

        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return

        today = now.date()
        rows = await self._get_todays_birthdays(today)
        for row in rows:
            await self._congratulate(channel, row, today)

    @birthday_check_loop.before_loop
    async def before_birthday_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _get_todays_birthdays(self, today: date) -> list[aiosqlite.Row]:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            """
            SELECT * FROM birthdays
            WHERE month = ?
              AND day = ?
              AND (last_congratulated_date IS NULL OR last_congratulated_date != ?)
            ORDER BY discord_user_id
            """,
            (today.month, today.day, today.isoformat()),
        )
        return await cursor.fetchall()

    async def _congratulate(self, channel: discord.abc.Messageable, row: aiosqlite.Row, today: date) -> None:
        assert self.bot.db is not None
        user_id = int(row["discord_user_id"])
        year = row["year"]
        if year is not None:
            age = today.year - int(year)
            text = (
                f"🎉 Сегодня день рождения у <@{user_id}>!\n\n"
                f"Поздравляем с {age}-летием!\n"
                "Желаем хорошего настроения, удачи и приятного общения на сервере 💛"
            )
        else:
            text = (
                f"🎉 Сегодня день рождения у <@{user_id}>!\n\n"
                "Поздравляем с праздником!\n"
                "Желаем хорошего настроения, удачи и приятного общения на сервере 💛"
            )

        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to send birthday greeting: user=%s channel=%s error=%s", user_id, getattr(channel, "id", None), exc)
            logger.debug("Birthday greeting traceback", exc_info=True)
            return

        async with self._db_lock:
            await self.bot.db.execute(
                "UPDATE birthdays SET last_congratulated_date = ?, updated_at = ? WHERE discord_user_id = ?",
                (today.isoformat(), datetime.now(UTC).isoformat(), user_id),
            )
            await self.bot.db.commit()

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        cached = self.bot.get_channel(channel_id)
        if cached is None:
            try:
                cached = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to fetch birthday channel %s: %s", channel_id, exc)
                logger.debug("Birthday channel fetch traceback", exc_info=True)
                return None

        if isinstance(cached, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return cached
        logger.warning("Birthday channel has unsupported type: id=%s type=%s", channel_id, type(cached).__name__)
        return None


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(BirthdaysCog(bot))

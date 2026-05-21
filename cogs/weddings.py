from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, resolve_display_name

logger = logging.getLogger(__name__)

WEDDINGS_DB_PATH = Path("data") / "weddings.db"
PROPOSAL_TTL = timedelta(minutes=5)
WEDDING_COLOR = discord.Color.from_rgb(255, 105, 180)
GOLD_COLOR = discord.Color.gold()
DIVORCE_COLOR = discord.Color.red()
RELATIONSHIP_COLOR = discord.Color.from_rgb(255, 105, 180)

RELATIONSHIP_LEVELS: tuple[tuple[int, str], ...] = (
    (0, "Знакомые"),
    (100, "Симпатия"),
    (300, "Влюблённые"),
    (700, "Крепкий союз"),
    (1200, "Родственные души"),
    (2000, "Легендарная пара"),
    (3500, "Вечный союз"),
    (6000, "Любовь вне времени"),
)
RELATIONSHIP_ACTIONS: dict[str, dict[str, Any]] = {
    "gift": {"title": "🎁 Подарок для партнёра", "xp": 25, "cooldown": timedelta(days=1)},
    "date": {"title": "🌙 Свидание пары", "xp": 35, "cooldown": timedelta(hours=6)},
    "hug": {"title": "🤗 Объятия", "xp": 5, "cooldown": timedelta(minutes=30)},
    "kiss": {"title": "💋 Поцелуй", "xp": 5, "cooldown": timedelta(minutes=30)},
}


def calculate_relationship_level(xp: int) -> int:
    level = 1
    for index, (required_xp, _) in enumerate(RELATIONSHIP_LEVELS, start=1):
        if xp >= required_xp:
            level = index
        else:
            break
    return level


def get_relationship_level_title(level: int) -> str:
    if 1 <= level <= len(RELATIONSHIP_LEVELS):
        return RELATIONSHIP_LEVELS[level - 1][1]
    return RELATIONSHIP_LEVELS[0][1]


def get_next_level_xp(level: int) -> int | None:
    if level >= len(RELATIONSHIP_LEVELS):
        return None
    return RELATIONSHIP_LEVELS[level][0]


def build_progress_bar(current_xp: int, level: int) -> str:
    next_xp = get_next_level_xp(level)
    current_level_xp = RELATIONSHIP_LEVELS[max(level - 1, 0)][0]
    if next_xp is None:
        return "██████████ 100%"
    required = max(next_xp - current_level_xp, 1)
    gained = max(current_xp - current_level_xp, 0)
    percent = min(max(gained / required, 0), 1)
    filled = int(percent * 10)
    return f"{'█' * filled}{'░' * (10 - filled)} {percent:.0%}"


def _format_timedelta_ru(delta: timedelta) -> str:
    total_minutes = max(int(delta.total_seconds() // 60), 1)
    if total_minutes >= 60:
        hours, minutes = divmod(total_minutes, 60)
        if minutes:
            return f"{hours} ч. {minutes} мин."
        return f"{hours} ч."
    return f"{total_minutes} мин."


def utcnow() -> datetime:
    return datetime.utcnow()


def to_iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_dt(value: str | datetime) -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    return dt.strftime("%d.%m.%Y %H:%M")


def days_together(married_at: str) -> int:
    delta = utcnow() - parse_iso(married_at)
    return max(delta.days, 0)


async def init_weddings_db(db_path: Path = WEDDINGS_DB_PATH) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wedding_marriages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            proposer_id INTEGER NOT NULL,
            partner_id INTEGER NOT NULL,
            married_at TEXT NOT NULL,
            divorced_at TEXT DEFAULT NULL,
            divorce_initiator_id INTEGER DEFAULT NULL,
            divorce_reason TEXT DEFAULT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wedding_active_members (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            marriage_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wedding_proposals (
            guild_id INTEGER NOT NULL,
            proposer_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            proposed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, proposer_id, receiver_id)
        )
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wedding_marriages_guild_active
        ON wedding_marriages (guild_id, divorced_at, married_at)
        """
    )
    cursor = await db.execute("PRAGMA table_info(wedding_marriages)")
    existing_columns = {str(row["name"]) for row in await cursor.fetchall()}
    relationship_columns = {
        "relationship_xp": "INTEGER DEFAULT 0",
        "relationship_level": "INTEGER DEFAULT 1",
        "relationship_streak_days": "INTEGER DEFAULT 0",
        "relationship_last_daily": "TEXT DEFAULT NULL",
        "relationship_last_interaction_at": "TEXT DEFAULT NULL",
    }
    for column_name, column_definition in relationship_columns.items():
        if column_name not in existing_columns:
            await db.execute(f"ALTER TABLE wedding_marriages ADD COLUMN {column_name} {column_definition}")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS relationship_xp_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            marriage_id INTEGER NOT NULL,
            actor_id INTEGER DEFAULT NULL,
            action TEXT NOT NULL,
            xp_amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wedding_active_members_marriage
        ON wedding_active_members (guild_id, marriage_id)
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wedding_marriages_relationship_top
        ON wedding_marriages (guild_id, relationship_level, relationship_xp, relationship_streak_days)
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_relationship_xp_logs_marriage_action
        ON relationship_xp_logs (guild_id, marriage_id, action, created_at)
        """
    )
    await db.commit()
    return db


class ProposalView(discord.ui.View):
    def __init__(
        self,
        cog: WeddingsCog,
        guild_id: int,
        proposer_id: int,
        receiver_id: int,
        expires_at: str,
    ) -> None:
        super().__init__(timeout=PROPOSAL_TTL.total_seconds())
        self.cog = cog
        self.guild_id = guild_id
        self.proposer_id = proposer_id
        self.receiver_id = receiver_id
        self.expires_at = expires_at
        self.completed = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("Это предложение адресовано не вам.", ephemeral=True)
            return False
        if self.completed:
            await interaction.response.send_message("Это предложение уже завершено.", ephemeral=True)
            return False
        return True

    async def _disable_buttons(self) -> None:
        self.completed = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _edit_message(self, *, embed: discord.Embed | None = None, content: str | None = None) -> None:
        await self._disable_buttons()
        try:
            if self.message is not None:
                await self.message.edit(content=content, embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to edit proposal message")

    async def on_timeout(self) -> None:
        if self.completed:
            return
        try:
            removed = await self.cog.delete_proposal(self.guild_id, self.proposer_id, self.receiver_id)
            if removed:
                await self._edit_message(content="⏰ Время вышло. Предложение больше не активно.")
            else:
                await self._disable_buttons()
        except Exception:
            logger.exception("Failed to expire wedding proposal")
            await self._disable_buttons()

    @discord.ui.button(label="💍 Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        marriage = await self.cog.accept_proposal(self.guild_id, self.proposer_id, self.receiver_id)
        if marriage is None:
            await self._disable_buttons()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "Предложение уже истекло, отменено или кто-то из участников уже вступил в другой брак.",
                ephemeral=True,
            )
            return

        embed = await self.cog.build_wedding_embed(interaction, marriage)
        await self._disable_buttons()
        await interaction.response.edit_message(content=None, embed=embed, view=self)

    @discord.ui.button(label="💔 Отказать", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.delete_proposal(self.guild_id, self.proposer_id, self.receiver_id)
        embed = discord.Embed(
            description=f"{interaction.user.mention} отказал(а) в предложении.",
            color=DIVORCE_COLOR,
            timestamp=utcnow(),
        )
        await self._disable_buttons()
        await interaction.response.edit_message(content=None, embed=embed, view=self)


class RelationshipMenuView(discord.ui.View):
    def __init__(self, cog: WeddingsCog, owner_id: int, guild_id: int, marriage_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.marriage_id = marriage_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню отношений открыто не для вас.", ephemeral=True)
            return False
        return True

    async def _run_action(self, interaction: discord.Interaction, action: str) -> None:
        await self.cog.handle_relationship_action(interaction, self, action)

    @discord.ui.button(label="🎁 Подарок", style=discord.ButtonStyle.primary, row=0)
    async def gift(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_action(interaction, "gift")

    @discord.ui.button(label="🌙 Свидание", style=discord.ButtonStyle.primary, row=0)
    async def date(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_action(interaction, "date")

    @discord.ui.button(label="🤗 Обнять", style=discord.ButtonStyle.secondary, row=1)
    async def hug(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_action(interaction, "hug")

    @discord.ui.button(label="💋 Поцеловать", style=discord.ButtonStyle.secondary, row=1)
    async def kiss(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_action(interaction, "kiss")

    @discord.ui.button(label="🎁 Ежедневный бонус", style=discord.ButtonStyle.success, row=2)
    async def daily(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_action(interaction, "daily")

    @discord.ui.button(label="💑 Профиль пары", style=discord.ButtonStyle.secondary, row=3)
    async def profile(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        marriage = await self.cog.get_active_marriage(self.guild_id, self.owner_id)
        if marriage is None:
            await interaction.response.send_message("Вы не состоите в браке.", ephemeral=True)
            return
        embed = await self.cog.build_couple_embed(marriage)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🏆 Топ отношений", style=discord.ButtonStyle.primary, row=3)
    async def top(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_relationship_top(interaction, public=True)

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.secondary, row=4)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        marriage = await self.cog.get_active_marriage(self.guild_id, self.owner_id)
        if marriage is None:
            await interaction.response.send_message("Вы не состоите в браке.", ephemeral=True)
            return
        embed = await self.cog.build_relationship_menu_embed(marriage)
        await interaction.response.edit_message(embed=embed, view=self)


class WeddingsCog(commands.Cog):
    wedding_group = app_commands.Group(name="wedding", description="Свадьбы и отношения")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.db = await init_weddings_db()
        await self.cleanup_expired_proposals()
        logger.info("Weddings cog loaded with database %s", WEDDINGS_DB_PATH)

    async def cog_unload(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    def _connection(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Weddings database is not initialized")
        return self.db

    async def cleanup_expired_proposals(self) -> None:
        db = self._connection()
        async with self._db_lock:
            await db.execute(
                "DELETE FROM wedding_proposals WHERE expires_at <= ?",
                (to_iso(),),
            )
            await db.commit()

    async def delete_proposal(self, guild_id: int, proposer_id: int, receiver_id: int) -> bool:
        db = self._connection()
        async with self._db_lock:
            cursor = await db.execute(
                """
                DELETE FROM wedding_proposals
                WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ?
                """,
                (guild_id, proposer_id, receiver_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_married(self, guild_id: int, user_id: int) -> bool:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT 1 FROM wedding_active_members
            WHERE guild_id = ? AND user_id = ?
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_active_marriage(self, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT m.*
            FROM wedding_active_members AS a
            JOIN wedding_marriages AS m ON m.id = a.marriage_id
            WHERE a.guild_id = ? AND a.user_id = ? AND m.divorced_at IS NULL
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        return await cursor.fetchone()

    async def _proposal_exists(self, guild_id: int, proposer_id: int, receiver_id: int) -> bool:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT 1 FROM wedding_proposals
            WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ? AND expires_at > ?
            LIMIT 1
            """,
            (guild_id, proposer_id, receiver_id, to_iso()),
        )
        row = await cursor.fetchone()
        return row is not None

    async def _safe_fetch_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to fetch user %s", user_id)
            return None

    @staticmethod
    def _mention(user_id: int, user: discord.abc.User | None = None) -> str:
        return user.mention if user is not None else f"<@{user_id}>"

    @staticmethod
    def _partner_id(row: aiosqlite.Row, user_id: int) -> int:
        proposer_id = int(row["proposer_id"])
        partner_id = int(row["partner_id"])
        return partner_id if proposer_id == user_id else proposer_id

    async def log_relationship_action(
        self,
        guild_id: int,
        marriage_id: int,
        actor_id: int | None,
        action: str,
        xp_amount: int,
        created_at: str,
    ) -> None:
        db = self._connection()
        await db.execute(
            """
            INSERT INTO relationship_xp_logs (guild_id, marriage_id, actor_id, action, xp_amount, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, marriage_id, actor_id, action, xp_amount, created_at),
        )

    async def check_relationship_action_cooldown(
        self,
        guild_id: int,
        marriage_id: int,
        action: str,
        cooldown: timedelta,
        now: datetime | None = None,
    ) -> timedelta | None:
        db = self._connection()
        current_time = now or utcnow()
        cursor = await db.execute(
            """
            SELECT created_at FROM relationship_xp_logs
            WHERE guild_id = ? AND marriage_id = ? AND action = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (guild_id, marriage_id, action),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        available_at = parse_iso(row["created_at"]) + cooldown
        if available_at > current_time:
            return available_at - current_time
        return None

    def update_daily_relationship_streak(self, marriage: aiosqlite.Row, today: datetime) -> tuple[int, int]:
        today_date = today.date()
        last_daily = marriage["relationship_last_daily"]
        current_streak = int(marriage["relationship_streak_days"] or 0)
        if last_daily:
            last_date = parse_iso(last_daily).date()
            if last_date == today_date:
                return current_streak, 0
            if last_date == today_date - timedelta(days=1):
                new_streak = current_streak + 1
            else:
                new_streak = 1
        else:
            new_streak = 1
        xp_amount = 20 + min(new_streak * 2, 30)
        return new_streak, xp_amount

    async def add_relationship_xp(
        self,
        guild_id: int,
        marriage_id: int,
        actor_id: int,
        action: str,
        xp_amount: int,
        *,
        streak_days: int | None = None,
    ) -> tuple[aiosqlite.Row | None, int, int, int]:
        db = self._connection()
        now = to_iso()
        cursor = await db.execute(
            """
            SELECT * FROM wedding_marriages
            WHERE guild_id = ? AND id = ? AND divorced_at IS NULL
            LIMIT 1
            """,
            (guild_id, marriage_id),
        )
        marriage = await cursor.fetchone()
        if marriage is None:
            return None, 0, 0, 0

        old_level = int(marriage["relationship_level"] or 1)
        new_xp = int(marriage["relationship_xp"] or 0) + xp_amount
        new_level = calculate_relationship_level(new_xp)
        if streak_days is None:
            await db.execute(
                """
                UPDATE wedding_marriages
                SET relationship_xp = ?, relationship_level = ?, relationship_last_interaction_at = ?
                WHERE guild_id = ? AND id = ? AND divorced_at IS NULL
                """,
                (new_xp, new_level, now, guild_id, marriage_id),
            )
        else:
            await db.execute(
                """
                UPDATE wedding_marriages
                SET relationship_xp = ?, relationship_level = ?, relationship_streak_days = ?,
                    relationship_last_daily = ?, relationship_last_interaction_at = ?
                WHERE guild_id = ? AND id = ? AND divorced_at IS NULL
                """,
                (new_xp, new_level, streak_days, now, now, guild_id, marriage_id),
            )
        await self.log_relationship_action(guild_id, marriage_id, actor_id, action, xp_amount, now)
        cursor = await db.execute("SELECT * FROM wedding_marriages WHERE guild_id = ? AND id = ?", (guild_id, marriage_id))
        updated = await cursor.fetchone()
        return updated, old_level, new_level, xp_amount

    async def perform_relationship_action(
        self, guild_id: int, actor: discord.abc.User, action: str
    ) -> tuple[aiosqlite.Row | None, str | None, int, int, int]:
        if actor.bot:
            return None, "XP отношений не начисляется ботам.", 0, 0, 0
        db = self._connection()
        async with self._db_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT m.*
                    FROM wedding_active_members AS a
                    JOIN wedding_marriages AS m ON m.id = a.marriage_id
                    WHERE a.guild_id = ? AND a.user_id = ? AND m.divorced_at IS NULL
                    LIMIT 1
                    """,
                    (guild_id, actor.id),
                )
                marriage = await cursor.fetchone()
                if marriage is None:
                    await db.commit()
                    return None, "Вы не состоите в браке.", 0, 0, 0

                marriage_id = int(marriage["id"])
                now = utcnow()
                if action == "daily":
                    last_daily = marriage["relationship_last_daily"]
                    if last_daily and parse_iso(last_daily).date() == now.date():
                        next_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
                        await db.commit()
                        return marriage, f"Это действие пока недоступно. Попробуйте через {_format_timedelta_ru(next_day - now)}", 0, 0, 0
                    streak_days, xp_amount = self.update_daily_relationship_streak(marriage, now)
                    updated, old_level, new_level, actual_xp = await self.add_relationship_xp(
                        guild_id, marriage_id, actor.id, action, xp_amount, streak_days=streak_days
                    )
                else:
                    config = RELATIONSHIP_ACTIONS[action]
                    remaining = await self.check_relationship_action_cooldown(
                        guild_id, marriage_id, action, config["cooldown"], now
                    )
                    if remaining is not None:
                        await db.commit()
                        return marriage, f"Это действие пока недоступно. Попробуйте через {_format_timedelta_ru(remaining)}", 0, 0, 0
                    updated, old_level, new_level, actual_xp = await self.add_relationship_xp(
                        guild_id, marriage_id, actor.id, action, int(config["xp"])
                    )

                await db.commit()
                return updated, None, old_level, new_level, actual_xp
            except Exception:
                await db.rollback()
                logger.exception("Failed to perform relationship action %s", action)
                return None, "Не удалось начислить XP отношений.", 0, 0, 0

    async def build_couple_embed(self, marriage: aiosqlite.Row) -> discord.Embed:
        proposer = await self._safe_fetch_user(int(marriage["proposer_id"]))
        partner = await self._safe_fetch_user(int(marriage["partner_id"]))
        proposer_mention = self._mention(int(marriage["proposer_id"]), proposer)
        partner_mention = self._mention(int(marriage["partner_id"]), partner)
        level = int(marriage["relationship_level"] or 1)
        xp = int(marriage["relationship_xp"] or 0)
        next_xp = get_next_level_xp(level)
        xp_text = f"{xp} XP" if next_xp is None else f"{xp} / {next_xp} XP"
        embed = discord.Embed(title="💑 Информация о паре", color=WEDDING_COLOR, timestamp=utcnow())
        embed.add_field(name="Участники пары", value=f"{proposer_mention} + {partner_mention}", inline=False)
        embed.add_field(name="Дата свадьбы", value=format_dt(marriage["married_at"]), inline=True)
        embed.add_field(name="Сколько дней вместе", value=f"{days_together(marriage['married_at'])} дн.", inline=True)
        embed.add_field(name="Статус", value="В браке", inline=True)
        embed.add_field(name="Уровень отношений", value=f"{level} — {get_relationship_level_title(level)}", inline=True)
        embed.add_field(name="XP", value=xp_text, inline=True)
        embed.add_field(name="Серия", value=f"{int(marriage['relationship_streak_days'] or 0)} дн.", inline=True)
        embed.add_field(name="Прогресс", value=build_progress_bar(xp, level), inline=False)
        thumb_user = partner or proposer
        if thumb_user is not None:
            embed.set_thumbnail(url=thumb_user.display_avatar.url)
        return embed

    async def build_relationship_menu_embed(self, marriage: aiosqlite.Row) -> discord.Embed:
        proposer = await self._safe_fetch_user(int(marriage["proposer_id"]))
        partner = await self._safe_fetch_user(int(marriage["partner_id"]))
        proposer_mention = self._mention(int(marriage["proposer_id"]), proposer)
        partner_mention = self._mention(int(marriage["partner_id"]), partner)
        level = int(marriage["relationship_level"] or 1)
        xp = int(marriage["relationship_xp"] or 0)
        next_xp = get_next_level_xp(level)
        xp_text = f"{xp} / максимум" if next_xp is None else f"{xp} / {next_xp}"
        embed = discord.Embed(title="💞 Отношения пары", color=RELATIONSHIP_COLOR, timestamp=utcnow())
        embed.description = (
            f"**Пара:** {proposer_mention} + {partner_mention}\n"
            f"**Уровень:** {level} — {get_relationship_level_title(level)}\n"
            f"**XP:** {xp_text}\n"
            f"**Прогресс:** {build_progress_bar(xp, level)}\n"
            f"**Серия:** {int(marriage['relationship_streak_days'] or 0)} дн.\n"
            f"**В браке:** {days_together(marriage['married_at'])} дн."
        )
        embed.set_footer(text="Нажмите кнопку, чтобы развивать отношения пары.")
        return embed

    async def build_relationship_action_embed(
        self,
        actor: discord.abc.User,
        marriage: aiosqlite.Row,
        action: str,
        xp_amount: int,
        old_level: int,
        new_level: int,
    ) -> discord.Embed:
        partner_id = self._partner_id(marriage, actor.id)
        partner = await self._safe_fetch_user(partner_id)
        partner_mention = self._mention(partner_id, partner)
        if action == "gift":
            title = "🎁 Подарок для партнёра"
            description = f"{actor.mention} сделал(а) подарок {partner_mention}\n+{xp_amount} XP к отношениям пары"
        elif action == "date":
            title = "🌙 Свидание пары"
            description = f"{actor.mention} и {partner_mention} провели время вместе\n+{xp_amount} XP к отношениям пары"
        elif action == "hug":
            title = "🤗 Объятия"
            description = f"{actor.mention} обнял(а) {partner_mention}\n+{xp_amount} XP к отношениям пары"
        elif action == "kiss":
            title = "💋 Поцелуй"
            description = f"{actor.mention} поцеловал(а) {partner_mention}\n+{xp_amount} XP к отношениям пары"
        else:
            title = "🎁 Ежедневный бонус пары"
            description = (
                f"Пара {actor.mention} и {partner_mention} получила +{xp_amount} XP\n"
                f"Серия: {int(marriage['relationship_streak_days'] or 0)} дн."
            )
        embed = discord.Embed(title=title, description=description, color=RELATIONSHIP_COLOR, timestamp=utcnow())
        if new_level > old_level:
            embed.add_field(
                name="💞 Новый уровень",
                value=f"Теперь пара: {new_level} — {get_relationship_level_title(new_level)}",
                inline=False,
            )
        return embed

    async def handle_relationship_action(
        self, interaction: discord.Interaction, view: RelationshipMenuView, action: str
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        marriage, error_text, old_level, new_level, xp_amount = await self.perform_relationship_action(
            interaction.guild.id, interaction.user, action
        )
        if error_text is not None:
            await interaction.followup.send(error_text, ephemeral=True)
            return
        if marriage is None:
            await interaction.followup.send("Вы не состоите в браке.", ephemeral=True)
            return
        public_embed = await self.build_relationship_action_embed(interaction.user, marriage, action, xp_amount, old_level, new_level)
        if interaction.channel is not None:
            await interaction.channel.send(embed=public_embed)
        menu_embed = await self.build_relationship_menu_embed(marriage)
        view.marriage_id = int(marriage["id"])
        await interaction.edit_original_response(embed=menu_embed, view=view)

    async def get_relationship_top_rows(self, guild_id: int) -> list[aiosqlite.Row]:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT * FROM wedding_marriages
            WHERE guild_id = ? AND divorced_at IS NULL
            ORDER BY relationship_level DESC, relationship_xp DESC, relationship_streak_days DESC, married_at ASC
            LIMIT 10
            """,
            (guild_id,),
        )
        return list(await cursor.fetchall())

    async def build_relationship_top_payload(
        self, guild_id: int
    ) -> tuple[discord.Embed, discord.File] | None:
        rows = await self.get_relationship_top_rows(guild_id)
        if not rows:
            return None

        guild = self.bot.get_guild(guild_id)
        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            proposer_name = await resolve_display_name(self.bot, guild, int(row["proposer_id"]), max_len=28)
            partner_name = await resolve_display_name(self.bot, guild, int(row["partner_id"]), max_len=28)
            level = int(row["relationship_level"] or 1)
            xp = int(row["relationship_xp"] or 0)
            streak = int(row["relationship_streak_days"] or 0)
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=f"{proposer_name} + {partner_name}",
                    primary=f"Уровень {level}  XP {xp}",
                    secondary=f"Серия {streak} дн.",
                    value=xp,
                )
            )

        filename = "relationship_top.png"
        file = make_leaderboard_file("ТОП ОТНОШЕНИЙ", leaderboard_rows, filename=filename)
        embed = discord.Embed(title="Топ отношений", color=GOLD_COLOR, timestamp=utcnow())
        embed.set_image(url=f"attachment://{filename}")
        return embed, file

    async def send_relationship_top(self, interaction: discord.Interaction, *, public: bool = False) -> None:
        if interaction.guild is None:
            if interaction.response.is_done():
                await interaction.followup.send("Эта команда доступна только на сервере.", ephemeral=True)
            else:
                await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            payload = await self.build_relationship_top_payload(interaction.guild.id)
        except Exception:
            logger.exception("Failed to generate relationship top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return
        if payload is None:
            await interaction.followup.send("Пока нет данных для топа.", ephemeral=True)
            return

        embed, file = payload
        kwargs: dict[str, Any] = {
            "embed": embed,
            "file": file,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if public and interaction.channel is not None:
            await interaction.channel.send(**kwargs)
            await interaction.followup.send("Топ отношений отправлен в канал.", ephemeral=True)
        elif interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def create_proposal(self, guild_id: int, proposer_id: int, receiver_id: int) -> tuple[bool, str, str | None]:
        db = self._connection()
        proposed_at = to_iso()
        expires_at = to_iso(utcnow() + PROPOSAL_TTL)
        async with self._db_lock:
            await db.execute("DELETE FROM wedding_proposals WHERE expires_at <= ?", (proposed_at,))

            if await self.is_married(guild_id, proposer_id):
                await db.commit()
                return False, "Вы уже состоите в браке.", None
            if await self.is_married(guild_id, receiver_id):
                await db.commit()
                return False, "Этот пользователь уже состоит в браке.", None
            if await self._proposal_exists(guild_id, proposer_id, receiver_id):
                await db.commit()
                return False, "У вас уже есть активное предложение этому пользователю.", None

            await db.execute(
                """
                INSERT INTO wedding_proposals (guild_id, proposer_id, receiver_id, proposed_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, proposer_id, receiver_id, proposed_at, expires_at),
            )
            await db.commit()
            return True, "", expires_at

    async def accept_proposal(self, guild_id: int, proposer_id: int, receiver_id: int) -> aiosqlite.Row | None:
        db = self._connection()
        now = to_iso()
        async with self._db_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT * FROM wedding_proposals
                    WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ?
                    LIMIT 1
                    """,
                    (guild_id, proposer_id, receiver_id),
                )
                proposal = await cursor.fetchone()
                if proposal is None or proposal["expires_at"] <= now:
                    await db.execute(
                        """
                        DELETE FROM wedding_proposals
                        WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ?
                        """,
                        (guild_id, proposer_id, receiver_id),
                    )
                    await db.commit()
                    return None

                for user_id in (proposer_id, receiver_id):
                    cursor = await db.execute(
                        """
                        SELECT 1 FROM wedding_active_members
                        WHERE guild_id = ? AND user_id = ?
                        LIMIT 1
                        """,
                        (guild_id, user_id),
                    )
                    if await cursor.fetchone() is not None:
                        await db.execute(
                            """
                            DELETE FROM wedding_proposals
                            WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ?
                            """,
                            (guild_id, proposer_id, receiver_id),
                        )
                        await db.commit()
                        return None

                married_at = now
                cursor = await db.execute(
                    """
                    INSERT INTO wedding_marriages (guild_id, proposer_id, partner_id, married_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (guild_id, proposer_id, receiver_id, married_at),
                )
                marriage_id = cursor.lastrowid
                await db.executemany(
                    """
                    INSERT INTO wedding_active_members (guild_id, user_id, marriage_id)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (guild_id, proposer_id, marriage_id),
                        (guild_id, receiver_id, marriage_id),
                    ),
                )
                await db.execute(
                    """
                    DELETE FROM wedding_proposals
                    WHERE guild_id = ? AND proposer_id = ? AND receiver_id = ?
                    """,
                    (guild_id, proposer_id, receiver_id),
                )
                await db.commit()

                cursor = await db.execute(
                    "SELECT * FROM wedding_marriages WHERE id = ?",
                    (marriage_id,),
                )
                return await cursor.fetchone()
            except Exception:
                await db.rollback()
                logger.exception("Failed to accept wedding proposal")
                return None

    async def close_marriage(self, guild_id: int, user_id: int, reason: str) -> aiosqlite.Row | None:
        db = self._connection()
        now = to_iso()
        async with self._db_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT m.*
                    FROM wedding_active_members AS a
                    JOIN wedding_marriages AS m ON m.id = a.marriage_id
                    WHERE a.guild_id = ? AND a.user_id = ? AND m.divorced_at IS NULL
                    LIMIT 1
                    """,
                    (guild_id, user_id),
                )
                marriage = await cursor.fetchone()
                if marriage is None:
                    await db.commit()
                    return None

                marriage_id = int(marriage["id"])
                await db.execute(
                    """
                    UPDATE wedding_marriages
                    SET divorced_at = ?, divorce_initiator_id = ?, divorce_reason = ?
                    WHERE id = ? AND divorced_at IS NULL
                    """,
                    (now, user_id, reason, marriage_id),
                )
                await db.execute(
                    """
                    DELETE FROM wedding_active_members
                    WHERE guild_id = ? AND marriage_id = ?
                    """,
                    (guild_id, marriage_id),
                )
                await db.commit()

                cursor = await db.execute("SELECT * FROM wedding_marriages WHERE id = ?", (marriage_id,))
                return await cursor.fetchone()
            except Exception:
                await db.rollback()
                logger.exception("Failed to close marriage")
                return None

    async def build_wedding_embed(self, interaction: discord.Interaction, marriage: aiosqlite.Row) -> discord.Embed:
        proposer = await self._safe_fetch_user(int(marriage["proposer_id"]))
        partner = await self._safe_fetch_user(int(marriage["partner_id"]))
        proposer_mention = self._mention(int(marriage["proposer_id"]), proposer)
        partner_mention = self._mention(int(marriage["partner_id"]), partner)
        embed = discord.Embed(
            title="🎉 Свадьба состоялась!",
            description=f"{proposer_mention} и {partner_mention} теперь в браке!",
            color=WEDDING_COLOR,
            timestamp=utcnow(),
        )
        embed.add_field(name="Дата свадьбы", value=format_dt(marriage["married_at"]), inline=True)
        embed.add_field(name="Вместе с", value=f"{proposer_mention} + {partner_mention}", inline=True)
        embed.add_field(name="Статус", value="В браке", inline=True)
        if partner is not None:
            embed.set_thumbnail(url=partner.display_avatar.url)
        elif interaction.user is not None:
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
        return embed

    @wedding_group.command(name="marry", description="Сделать предложение пользователю")
    @app_commands.describe(partner="Пользователь, которому вы хотите сделать предложение")
    async def propose(self, interaction: discord.Interaction, partner: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Не удалось определить участника сервера.", ephemeral=True)
            return
        if partner.id == interaction.user.id:
            await interaction.response.send_message("Нельзя сделать предложение самому себе.", ephemeral=True)
            return
        if partner.bot:
            await interaction.response.send_message("Нельзя сделать предложение боту.", ephemeral=True)
            return

        ok, error_text, expires_at = await self.create_proposal(interaction.guild.id, interaction.user.id, partner.id)
        if not ok or expires_at is None:
            await interaction.response.send_message(error_text, ephemeral=True)
            return

        embed = discord.Embed(
            title="💍 Свадебное предложение",
            description=(
                f"{interaction.user.mention} сделал(а) предложение {partner.mention}!\n"
                "У пользователя есть 5 минут, чтобы принять или отказать."
            ),
            color=WEDDING_COLOR,
            timestamp=utcnow(),
        )
        embed.add_field(name="Кто сделал предложение", value=interaction.user.mention, inline=True)
        embed.add_field(name="Кому сделали предложение", value=partner.mention, inline=True)
        embed.add_field(name="Действует до", value=format_dt(expires_at), inline=False)
        embed.set_thumbnail(url=partner.display_avatar.url)

        view = ProposalView(self, interaction.guild.id, interaction.user.id, partner.id, expires_at)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @wedding_group.command(name="divorce", description="Расторгнуть текущий брак")
    @app_commands.describe(reason="Причина развода")
    async def divorce(self, interaction: discord.Interaction, reason: str = "Не указана") -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        reason = reason.strip() or "Не указана"
        marriage = await self.close_marriage(interaction.guild.id, interaction.user.id, reason)
        if marriage is None:
            await interaction.response.send_message("Вы не состоите в браке.", ephemeral=True)
            return

        partner_id = self._partner_id(marriage, interaction.user.id)
        partner = await self._safe_fetch_user(partner_id)
        embed = discord.Embed(title="💔 Брак расторгнут", color=DIVORCE_COLOR, timestamp=utcnow())
        embed.add_field(name="Инициатор", value=interaction.user.mention, inline=True)
        embed.add_field(name="Партнёр", value=self._mention(partner_id, partner), inline=True)
        embed.add_field(name="Причина", value=reason[:1024], inline=False)
        embed.add_field(name="Дата развода", value=format_dt(marriage["divorced_at"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @wedding_group.command(name="profile", description="Показать информацию о паре")
    @app_commands.describe(user="Пользователь, чью пару нужно показать")
    async def couple(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        marriage = await self.get_active_marriage(interaction.guild.id, target.id)
        if marriage is None:
            await interaction.response.send_message("Этот пользователь не состоит в браке.", ephemeral=True)
            return

        embed = await self.build_couple_embed(marriage)
        await interaction.response.send_message(embed=embed)


    @wedding_group.command(name="relationships", description="Открыть меню развития отношений пары")
    async def relationships(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message("Боты не могут развивать отношения.", ephemeral=True)
            return
        marriage = await self.get_active_marriage(interaction.guild.id, interaction.user.id)
        if marriage is None:
            await interaction.response.send_message("Вы не состоите в браке.", ephemeral=True)
            return
        embed = await self.build_relationship_menu_embed(marriage)
        view = RelationshipMenuView(self, interaction.user.id, interaction.guild.id, int(marriage["id"]))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def relationship_top(self, interaction: discord.Interaction) -> None:
        await self.send_relationship_top(interaction)

    async def top_couples(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT * FROM wedding_marriages
            WHERE guild_id = ? AND divorced_at IS NULL
            ORDER BY married_at ASC
            LIMIT 10
            """,
            (interaction.guild.id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("Пока нет активных пар.", ephemeral=True)
            return

        await interaction.response.defer()
        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            proposer_name = await resolve_display_name(self.bot, interaction.guild, int(row["proposer_id"]), max_len=28)
            partner_name = await resolve_display_name(self.bot, interaction.guild, int(row["partner_id"]), max_len=28)
            days = days_together(row["married_at"])
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=f"{proposer_name} + {partner_name}",
                    primary=f"Вместе {days} дн.",
                    value=max(days, 1),
                )
            )

        try:
            filename = "couples_top.png"
            file = make_leaderboard_file("ТОП ПАР ПО ДЛИТЕЛЬНОСТИ БРАКА", leaderboard_rows, filename=filename)
            embed = discord.Embed(title="Топ пар", color=GOLD_COLOR, timestamp=utcnow())
            embed.set_image(url=f"attachment://{filename}")
        except Exception:
            logger.exception("Failed to generate couples top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return

        await interaction.followup.send(
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @wedding_group.command(name="stats", description="Статистика свадеб на сервере")
    async def wedding_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        db = self._connection()
        guild_id = interaction.guild.id
        stats: dict[str, Any] = {}
        queries = {
            "active": "SELECT COUNT(*) AS value FROM wedding_marriages WHERE guild_id = ? AND divorced_at IS NULL",
            "total": "SELECT COUNT(*) AS value FROM wedding_marriages WHERE guild_id = ?",
            "divorces": "SELECT COUNT(*) AS value FROM wedding_marriages WHERE guild_id = ? AND divorced_at IS NOT NULL",
        }
        for key, query in queries.items():
            cursor = await db.execute(query, (guild_id,))
            row = await cursor.fetchone()
            stats[key] = int(row["value"] if row is not None else 0)

        cursor = await db.execute(
            """
            SELECT * FROM wedding_marriages
            WHERE guild_id = ? AND divorced_at IS NULL
            ORDER BY married_at ASC
            LIMIT 1
            """,
            (guild_id,),
        )
        longest = await cursor.fetchone()
        if longest is None:
            longest_text = "Пока нет активных пар."
        else:
            proposer = await self._safe_fetch_user(int(longest["proposer_id"]))
            partner = await self._safe_fetch_user(int(longest["partner_id"]))
            longest_text = (
                f"{self._mention(int(longest['proposer_id']), proposer)} + "
                f"{self._mention(int(longest['partner_id']), partner)}\n"
                f"Вместе: {days_together(longest['married_at'])} дн."
            )

        cursor = await db.execute(
            """
            SELECT * FROM wedding_marriages
            WHERE guild_id = ? AND divorced_at IS NULL
            ORDER BY relationship_level DESC, relationship_xp DESC, relationship_streak_days DESC, married_at ASC
            LIMIT 1
            """,
            (guild_id,),
        )
        most_developed = await cursor.fetchone()
        if most_developed is None:
            most_developed_text = "Пока нет активных пар."
        else:
            proposer = await self._safe_fetch_user(int(most_developed["proposer_id"]))
            partner = await self._safe_fetch_user(int(most_developed["partner_id"]))
            level = int(most_developed["relationship_level"] or 1)
            most_developed_text = (
                f"{self._mention(int(most_developed['proposer_id']), proposer)} + "
                f"{self._mention(int(most_developed['partner_id']), partner)}\n"
                f"Уровень {level} — {get_relationship_level_title(level)} · "
                f"{int(most_developed['relationship_xp'] or 0)} XP"
            )

        cursor = await db.execute(
            """
            SELECT AVG(relationship_level) AS value
            FROM wedding_marriages
            WHERE guild_id = ? AND divorced_at IS NULL
            """,
            (guild_id,),
        )
        avg_row = await cursor.fetchone()
        avg_level = float(avg_row["value"] or 0) if avg_row is not None else 0.0

        embed = discord.Embed(title="📊 Статистика свадеб", color=GOLD_COLOR, timestamp=utcnow())
        embed.add_field(name="Активных браков", value=str(stats["active"]), inline=True)
        embed.add_field(name="Всего свадеб за всё время", value=str(stats["total"]), inline=True)
        embed.add_field(name="Всего разводов", value=str(stats["divorces"]), inline=True)
        embed.add_field(name="Средний уровень отношений", value=f"{avg_level:.1f}", inline=True)
        embed.add_field(name="Самая долгая активная пара", value=longest_text, inline=False)
        embed.add_field(name="Самая развитая активная пара", value=most_developed_text, inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(WeddingsCog(bot))

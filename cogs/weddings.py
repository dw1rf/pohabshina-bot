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

logger = logging.getLogger(__name__)

WEDDINGS_DB_PATH = Path("data") / "weddings.db"
PROPOSAL_TTL = timedelta(minutes=5)
WEDDING_COLOR = discord.Color.from_rgb(255, 105, 180)
GOLD_COLOR = discord.Color.gold()
DIVORCE_COLOR = discord.Color.red()


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
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wedding_active_members_marriage
        ON wedding_active_members (guild_id, marriage_id)
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


class WeddingsCog(commands.Cog):
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

    @app_commands.command(name="предложить", description="Сделать предложение пользователю")
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

    @app_commands.command(name="развод", description="Расторгнуть текущий брак")
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

    @app_commands.command(name="пара", description="Показать информацию о паре")
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

        proposer = await self._safe_fetch_user(int(marriage["proposer_id"]))
        partner = await self._safe_fetch_user(int(marriage["partner_id"]))
        proposer_mention = self._mention(int(marriage["proposer_id"]), proposer)
        partner_mention = self._mention(int(marriage["partner_id"]), partner)
        embed = discord.Embed(title="💑 Информация о паре", color=WEDDING_COLOR, timestamp=utcnow())
        embed.add_field(name="Участники пары", value=f"{proposer_mention} + {partner_mention}", inline=False)
        embed.add_field(name="Дата свадьбы", value=format_dt(marriage["married_at"]), inline=True)
        embed.add_field(name="Сколько дней вместе", value=f"{days_together(marriage['married_at'])} дн.", inline=True)
        embed.add_field(name="Статус", value="В браке", inline=True)
        thumb_user = partner or proposer
        if thumb_user is not None:
            embed.set_thumbnail(url=thumb_user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="топ_пар", description="Топ пар по длительности брака")
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

        lines: list[str] = []
        for index, row in enumerate(rows, start=1):
            proposer = await self._safe_fetch_user(int(row["proposer_id"]))
            partner = await self._safe_fetch_user(int(row["partner_id"]))
            lines.append(
                f"#{index} — {self._mention(int(row['proposer_id']), proposer)} + "
                f"{self._mention(int(row['partner_id']), partner)}\n"
                f"Вместе: {days_together(row['married_at'])} дн."
            )

        embed = discord.Embed(
            title="🏆 Топ пар по длительности брака",
            description="\n\n".join(lines),
            color=GOLD_COLOR,
            timestamp=utcnow(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="браки_статистика", description="Статистика свадеб на сервере")
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

        embed = discord.Embed(title="📊 Статистика свадеб", color=GOLD_COLOR, timestamp=utcnow())
        embed.add_field(name="Активных браков", value=str(stats["active"]), inline=True)
        embed.add_field(name="Всего свадеб за всё время", value=str(stats["total"]), inline=True)
        embed.add_field(name="Всего разводов", value=str(stats["divorces"]), inline=True)
        embed.add_field(name="Самая долгая активная пара", value=longest_text, inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(WeddingsCog(bot))

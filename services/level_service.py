from __future__ import annotations

from datetime import datetime

import aiosqlite

from config import Settings
from utils.helpers import calculate_level, now_iso


class LevelService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warning_totals (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                warn_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0,
                last_message_at TEXT,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        await db.commit()

    async def add_warning(self, db: aiosqlite.Connection, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        ts = now_iso()
        await db.execute(
            """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, reason, ts),
        )
        await db.execute(
            """
            INSERT INTO warning_totals (guild_id, user_id, warn_count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET warn_count = warn_count + 1, updated_at = excluded.updated_at
            """,
            (guild_id, user_id, ts),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT warn_count FROM warning_totals WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return int(row["warn_count"] if row else 0)

    async def get_warnings(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> tuple[int, list[aiosqlite.Row]]:
        cursor = await db.execute(
            "SELECT warn_count FROM warning_totals WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        total_row = await cursor.fetchone()
        total = int(total_row["warn_count"] if total_row else 0)

        cursor = await db.execute(
            """
            SELECT moderator_id, reason, created_at
            FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (guild_id, user_id),
        )
        return total, await cursor.fetchall()

    async def clear_warnings(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> None:
        await db.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.execute("DELETE FROM warning_totals WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()

    async def update_level_progress(self, db: aiosqlite.Connection, guild_id: int, user_id: int, message_ts: datetime) -> tuple[int, int, bool]:
        cursor = await db.execute(
            "SELECT message_count, level, last_message_at FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()

        if row is None:
            message_count = 1
            new_level = calculate_level(message_count, self.settings.max_level)
            await db.execute(
                "INSERT INTO levels (guild_id, user_id, message_count, level, last_message_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, message_count, new_level, message_ts.isoformat()),
            )
            await db.commit()
            return message_count, new_level, new_level > 0

        if row["last_message_at"]:
            try:
                last_dt = datetime.fromisoformat(row["last_message_at"])
                if (message_ts - last_dt).total_seconds() < self.settings.level_cooldown_seconds:
                    return int(row["message_count"]), int(row["level"]), False
            except ValueError:
                pass

        message_count = int(row["message_count"]) + 1
        old_level = int(row["level"])
        new_level = calculate_level(message_count, self.settings.max_level)
        await db.execute(
            "UPDATE levels SET message_count = ?, level = ?, last_message_at = ? WHERE guild_id = ? AND user_id = ?",
            (message_count, new_level, message_ts.isoformat(), guild_id, user_id),
        )
        await db.commit()
        return message_count, new_level, new_level > old_level

    async def get_rank(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT message_count, level FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return await cursor.fetchone()

    async def get_top(self, db: aiosqlite.Connection, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """
            SELECT user_id, message_count, level
            FROM levels
            WHERE guild_id = ?
            ORDER BY level DESC, message_count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return await cursor.fetchall()

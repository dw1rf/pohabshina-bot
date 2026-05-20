from __future__ import annotations

import aiosqlite

from utils.helpers import now_iso


class ReactionBanService:
    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reaction_bans (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                moderator_id INTEGER,
                created_at TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await db.commit()

    async def add_ban(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str | None,
    ) -> bool:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO reaction_bans (guild_id, user_id, reason, moderator_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, reason, moderator_id, now_iso()),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def remove_ban(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> bool:
        cursor = await db.execute(
            "DELETE FROM reaction_bans WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def get_ban(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, reason, moderator_id, created_at
            FROM reaction_bans
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        return await cursor.fetchone()

    async def list_bans(self, db: aiosqlite.Connection, guild_id: int) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """
            SELECT user_id, reason, moderator_id, created_at
            FROM reaction_bans
            WHERE guild_id = ?
            ORDER BY created_at DESC, user_id ASC
            """,
            (guild_id,),
        )
        return await cursor.fetchall()

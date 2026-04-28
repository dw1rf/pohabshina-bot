from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite


@dataclass(slots=True)
class SupportTicket:
    guild_id: int
    user_id: int
    channel_id: int
    created_at: str
    active: bool


class SupportTicketService:
    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                closed_at TEXT,
                closed_by INTEGER,
                last_service_key TEXT,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE INDEX IF NOT EXISTS idx_support_tickets_active_user
            ON support_tickets (guild_id, user_id, active);
            """
        )
        await db.commit()

    async def get_active_by_user(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> SupportTicket | None:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, channel_id, created_at, active
            FROM support_tickets
            WHERE guild_id = ? AND user_id = ? AND active = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SupportTicket(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            channel_id=int(row["channel_id"]),
            created_at=str(row["created_at"]),
            active=bool(row["active"]),
        )

    async def get_active_by_channel(self, db: aiosqlite.Connection, guild_id: int, channel_id: int) -> SupportTicket | None:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, channel_id, created_at, active
            FROM support_tickets
            WHERE guild_id = ? AND channel_id = ? AND active = 1
            LIMIT 1
            """,
            (guild_id, channel_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SupportTicket(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            channel_id=int(row["channel_id"]),
            created_at=str(row["created_at"]),
            active=bool(row["active"]),
        )

    async def create_ticket(self, db: aiosqlite.Connection, guild_id: int, user_id: int, channel_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """
            INSERT INTO support_tickets (guild_id, user_id, channel_id, created_at, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (guild_id, user_id, channel_id, now),
        )
        await db.commit()

    async def close_ticket(self, db: aiosqlite.Connection, guild_id: int, channel_id: int, closed_by: int) -> int:
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            """
            UPDATE support_tickets
            SET active = 0,
                closed_at = ?,
                closed_by = ?
            WHERE guild_id = ? AND channel_id = ? AND active = 1
            """,
            (now, closed_by, guild_id, channel_id),
        )
        await db.commit()
        return int(cursor.rowcount or 0)

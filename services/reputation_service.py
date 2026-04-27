from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite


class ReputationService:
    async def init_rep_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reputation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                giver_user_id INTEGER NOT NULL,
                receiver_user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                rep_type TEXT NOT NULL CHECK(rep_type IN ('plus', 'minus')),
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rep_events_giver_time
            ON reputation_events (guild_id, giver_user_id, rep_type, created_at);

            CREATE TABLE IF NOT EXISTS user_reputation (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                positive_rep INTEGER NOT NULL DEFAULT 0,
                negative_rep INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        await db.commit()

    async def can_give_rep(self, db: aiosqlite.Connection, guild_id: int, giver_id: int, limit: int = 2) -> bool:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM reputation_events
            WHERE guild_id = ?
              AND giver_user_id = ?
              AND rep_type = 'plus'
              AND created_at >= ?
            """,
            (guild_id, giver_id, since),
        )
        row = await cursor.fetchone()
        return int(row["cnt"] if row else 0) < limit

    async def add_rep_event(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        giver_user_id: int,
        receiver_user_id: int,
        channel_id: int,
        message_id: int,
        rep_type: str,
    ) -> None:
        now_ts = datetime.now(UTC).isoformat()
        await db.execute(
            """
            INSERT INTO reputation_events (
                guild_id, giver_user_id, receiver_user_id, channel_id, message_id, rep_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, giver_user_id, receiver_user_id, channel_id, message_id, rep_type, now_ts),
        )

        if rep_type == "plus":
            update_sql = """
                INSERT INTO user_reputation (guild_id, user_id, positive_rep, negative_rep, updated_at)
                VALUES (?, ?, 1, 0, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET positive_rep = positive_rep + 1, updated_at = excluded.updated_at
            """
        else:
            update_sql = """
                INSERT INTO user_reputation (guild_id, user_id, positive_rep, negative_rep, updated_at)
                VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET negative_rep = negative_rep + 1, updated_at = excluded.updated_at
            """
        await db.execute(update_sql, (guild_id, receiver_user_id, now_ts))
        await db.commit()

    async def get_user_rep(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> tuple[int, int]:
        cursor = await db.execute(
            """
            SELECT positive_rep, negative_rep
            FROM user_reputation
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return 0, 0
        return int(row["positive_rep"]), int(row["negative_rep"])

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from utils.sqlite_writes import sqlite_write_lock

logger = logging.getLogger(__name__)


class ReputationService:
    def __init__(self, *, lock_retry_delays: tuple[float, ...] = (0.1, 0.25, 0.5)) -> None:
        self._lock_retry_delays = lock_retry_delays

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
                amount INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rep_events_giver_time
            ON reputation_events (guild_id, giver_user_id, created_at);

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
        await self._ensure_event_columns(db)
        await db.commit()

    async def _ensure_event_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(reputation_events)")
        rows = await cursor.fetchall()
        columns = {
            str(row["name"] if isinstance(row, aiosqlite.Row) else row[1])
            for row in rows
        }
        if "target_message_id" not in columns:
            await db.execute("ALTER TABLE reputation_events ADD COLUMN target_message_id INTEGER")
        if "amount" not in columns:
            await db.execute("ALTER TABLE reputation_events ADD COLUMN amount INTEGER NOT NULL DEFAULT 1")
        if "source" not in columns:
            await db.execute("ALTER TABLE reputation_events ADD COLUMN source TEXT NOT NULL DEFAULT 'user'")

    async def can_give_rep(self, db: aiosqlite.Connection, guild_id: int, giver_id: int, limit: int = 2) -> bool:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM reputation_events
            WHERE guild_id = ?
              AND giver_user_id = ?
              AND source = 'user'
              AND created_at >= ?
            """,
            (guild_id, giver_id, since),
        )
        row = await cursor.fetchone()
        return int((row["cnt"] if isinstance(row, aiosqlite.Row) else row[0]) if row else 0) < limit

    async def add_rep_event(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        giver_user_id: int,
        receiver_user_id: int,
        channel_id: int,
        message_id: int,
        rep_type: str,
        target_message_id: int | None = None,
    ) -> None:
        now_ts = datetime.now(UTC).isoformat()
        event_sql = """
            INSERT INTO reputation_events (
                guild_id,
                giver_user_id,
                receiver_user_id,
                channel_id,
                message_id,
                rep_type,
                amount,
                source,
                created_at,
                target_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 'user', ?, ?)
            """
        event_parameters = (
            guild_id,
            giver_user_id,
            receiver_user_id,
            channel_id,
            message_id,
            rep_type,
            now_ts,
            target_message_id,
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
        async with sqlite_write_lock(db):
            for attempt in range(len(self._lock_retry_delays) + 1):
                try:
                    await db.execute(event_sql, event_parameters)
                    await db.execute(update_sql, (guild_id, receiver_user_id, now_ts))
                    await db.commit()
                    return
                except aiosqlite.OperationalError as exc:
                    await db.rollback()
                    is_locked = "database is locked" in str(exc).lower()
                    if not is_locked or attempt >= len(self._lock_retry_delays):
                        raise
                    delay = self._lock_retry_delays[attempt]
                    logger.warning(
                        "SQLite busy while writing reputation transaction; retrying in %.2fs (attempt %s/%s)",
                        delay,
                        attempt + 1,
                        len(self._lock_retry_delays),
                    )
                    await asyncio.sleep(delay)
                except BaseException:
                    await db.rollback()
                    raise

    async def adjust_reputation(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        actor_user_id: int,
        receiver_user_id: int,
        channel_id: int,
        interaction_id: int,
        delta: int,
    ) -> tuple[int, int]:
        if delta == 0:
            raise ValueError("Reputation adjustment cannot be zero")

        now_ts = datetime.now(UTC).isoformat()
        rep_type = "plus" if delta > 0 else "minus"
        amount = abs(delta)
        if delta > 0:
            update_sql = """
                INSERT INTO user_reputation (guild_id, user_id, positive_rep, negative_rep, updated_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET positive_rep = positive_rep + excluded.positive_rep, updated_at = excluded.updated_at
            """
        else:
            update_sql = """
                INSERT INTO user_reputation (guild_id, user_id, positive_rep, negative_rep, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET negative_rep = negative_rep + excluded.negative_rep, updated_at = excluded.updated_at
            """

        async with sqlite_write_lock(db):
            for attempt in range(len(self._lock_retry_delays) + 1):
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    previous_positive, previous_negative = await self.get_user_rep(db, guild_id, receiver_user_id)
                    previous_total = previous_positive - previous_negative
                    await db.execute(
                        """
                        INSERT INTO reputation_events (
                            guild_id,
                            giver_user_id,
                            receiver_user_id,
                            channel_id,
                            message_id,
                            rep_type,
                            amount,
                            source,
                            created_at,
                            target_message_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'admin', ?, NULL)
                        """,
                        (
                            guild_id,
                            actor_user_id,
                            receiver_user_id,
                            channel_id,
                            interaction_id,
                            rep_type,
                            amount,
                            now_ts,
                        ),
                    )
                    await db.execute(update_sql, (guild_id, receiver_user_id, amount, now_ts))
                    await db.commit()
                    return previous_total, previous_total + delta
                except aiosqlite.OperationalError as exc:
                    await db.rollback()
                    is_locked = "database is locked" in str(exc).lower()
                    if not is_locked or attempt >= len(self._lock_retry_delays):
                        raise
                    delay = self._lock_retry_delays[attempt]
                    logger.warning(
                        "SQLite busy while writing admin reputation adjustment; retrying in %.2fs (attempt %s/%s)",
                        delay,
                        attempt + 1,
                        len(self._lock_retry_delays),
                    )
                    await asyncio.sleep(delay)
                except BaseException:
                    await db.rollback()
                    raise

        raise RuntimeError("Reputation adjustment retry loop exited unexpectedly")

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
        if isinstance(row, aiosqlite.Row):
            return int(row["positive_rep"]), int(row["negative_rep"])
        return int(row[0]), int(row[1])

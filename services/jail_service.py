from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiosqlite

from utils.sqlite_writes import sqlite_write_lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JailRecord:
    guild_id: int
    user_id: int
    channel_id: int
    role_id: int
    reason: str
    moderator_id: int
    started_at: str
    expires_at: str


class JailService:
    def __init__(self, *, lock_retry_delays: tuple[float, ...] = (0.1, 0.25, 0.5)) -> None:
        self._lock_retry_delays = lock_retry_delays

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS active_jails (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                moderator_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_active_jails_expires_at
            ON active_jails (expires_at);
            """
        )
        await db.commit()

    async def upsert(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        role_id: int,
        reason: str,
        moderator_id: int,
        started_at: str,
        expires_at: str,
    ) -> None:
        async with sqlite_write_lock(db):
            await self._upsert_unlocked(
                db,
                guild_id=guild_id,
                user_id=user_id,
                channel_id=channel_id,
                role_id=role_id,
                reason=reason,
                moderator_id=moderator_id,
                started_at=started_at,
                expires_at=expires_at,
            )

    async def _upsert_unlocked(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        role_id: int,
        reason: str,
        moderator_id: int,
        started_at: str,
        expires_at: str,
    ) -> None:
        for attempt in range(len(self._lock_retry_delays) + 1):
            try:
                await db.execute(
                    """
                    INSERT INTO active_jails (
                        guild_id, user_id, channel_id, role_id, reason, moderator_id, started_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        role_id = excluded.role_id,
                        reason = excluded.reason,
                        moderator_id = excluded.moderator_id,
                        started_at = excluded.started_at,
                        expires_at = excluded.expires_at
                    """,
                    (guild_id, user_id, channel_id, role_id, reason, moderator_id, started_at, expires_at),
                )
                await db.commit()
                return
            except aiosqlite.OperationalError as exc:
                await db.rollback()
                is_locked = "database is locked" in str(exc).lower()
                if not is_locked or attempt >= len(self._lock_retry_delays):
                    raise
                delay = self._lock_retry_delays[attempt]
                logger.warning(
                    "SQLite busy while saving jail; retrying in %.2fs (attempt %s/%s)",
                    delay,
                    attempt + 1,
                    len(self._lock_retry_delays),
                )
                await asyncio.sleep(delay)
            except BaseException:
                await db.rollback()
                raise

    async def get_active_by_user(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> JailRecord | None:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, channel_id, role_id, reason, moderator_id, started_at, expires_at
            FROM active_jails
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return self._record_from_row(row) if row else None

    async def get_active_by_channel(self, db: aiosqlite.Connection, guild_id: int, channel_id: int) -> JailRecord | None:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, channel_id, role_id, reason, moderator_id, started_at, expires_at
            FROM active_jails
            WHERE guild_id = ? AND channel_id = ?
            """,
            (guild_id, channel_id),
        )
        row = await cursor.fetchone()
        return self._record_from_row(row) if row else None

    async def list_active(self, db: aiosqlite.Connection) -> list[JailRecord]:
        cursor = await db.execute(
            """
            SELECT guild_id, user_id, channel_id, role_id, reason, moderator_id, started_at, expires_at
            FROM active_jails
            ORDER BY expires_at ASC
            """
        )
        return [self._record_from_row(row) for row in await cursor.fetchall()]

    async def remove(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> int:
        async with sqlite_write_lock(db):
            for attempt in range(len(self._lock_retry_delays) + 1):
                try:
                    cursor = await db.execute(
                        "DELETE FROM active_jails WHERE guild_id = ? AND user_id = ?",
                        (guild_id, user_id),
                    )
                    await db.commit()
                    return int(cursor.rowcount or 0)
                except aiosqlite.OperationalError as exc:
                    await db.rollback()
                    is_locked = "database is locked" in str(exc).lower()
                    if not is_locked or attempt >= len(self._lock_retry_delays):
                        raise
                    await asyncio.sleep(self._lock_retry_delays[attempt])
                except BaseException:
                    await db.rollback()
                    raise

    @staticmethod
    def _record_from_row(row: aiosqlite.Row) -> JailRecord:
        return JailRecord(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            channel_id=int(row["channel_id"]),
            role_id=int(row["role_id"]),
            reason=str(row["reason"]),
            moderator_id=int(row["moderator_id"]),
            started_at=str(row["started_at"]),
            expires_at=str(row["expires_at"]),
        )

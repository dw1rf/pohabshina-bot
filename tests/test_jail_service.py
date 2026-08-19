from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from services.jail_service import JailService


def test_jail_upsert_retries_a_short_sqlite_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "jail-lock.sqlite3"
        db = await aiosqlite.connect(path)
        blocker = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        blocker.row_factory = aiosqlite.Row
        service = JailService(lock_retry_delays=(0.02, 0.04, 0.08))
        try:
            for connection in (db, blocker):
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA busy_timeout=20")
            await service.init_db(db)
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute("CREATE TABLE IF NOT EXISTS temporary_lock (id INTEGER)")

            async def release_lock() -> None:
                await asyncio.sleep(0.08)
                await blocker.commit()

            release_task = asyncio.create_task(release_lock())
            await service.upsert(
                db,
                guild_id=1,
                user_id=2,
                channel_id=3,
                role_id=4,
                reason="test",
                moderator_id=5,
                started_at="2026-08-19T00:00:00+00:00",
                expires_at="2026-08-19T01:00:00+00:00",
            )
            await release_task

            record = await service.get_active_by_user(db, 1, 2)
            assert record is not None
            assert record.channel_id == 3
            assert db.in_transaction is False
        finally:
            await blocker.close()
            await db.close()

    asyncio.run(scenario())


def test_jail_upsert_rolls_back_when_cancelled() -> None:
    async def scenario() -> None:
        db = SimpleNamespace(
            execute=AsyncMock(),
            commit=AsyncMock(side_effect=asyncio.CancelledError()),
            rollback=AsyncMock(),
        )
        service = JailService(lock_retry_delays=())

        try:
            await service.upsert(
                db,
                guild_id=1,
                user_id=2,
                channel_id=3,
                role_id=4,
                reason="test",
                moderator_id=5,
                started_at="2026-08-19T00:00:00+00:00",
                expires_at="2026-08-19T01:00:00+00:00",
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("CancelledError must propagate")

        db.rollback.assert_awaited_once()

    asyncio.run(scenario())


def test_jail_remove_retries_a_short_sqlite_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "jail-remove-lock.sqlite3"
        db = await aiosqlite.connect(path)
        blocker = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        blocker.row_factory = aiosqlite.Row
        service = JailService(lock_retry_delays=(0.02, 0.04, 0.08))
        try:
            for connection in (db, blocker):
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA busy_timeout=20")
            await service.init_db(db)
            await service.upsert(
                db,
                guild_id=1,
                user_id=2,
                channel_id=3,
                role_id=4,
                reason="test",
                moderator_id=5,
                started_at="2026-08-19T00:00:00+00:00",
                expires_at="2026-08-19T01:00:00+00:00",
            )
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute("CREATE TABLE IF NOT EXISTS temporary_lock (id INTEGER)")

            async def release_lock() -> None:
                await asyncio.sleep(0.08)
                await blocker.commit()

            release_task = asyncio.create_task(release_lock())
            assert await service.remove(db, 1, 2) == 1
            await release_task
            assert await service.get_active_by_user(db, 1, 2) is None
        finally:
            await blocker.close()
            await db.close()

    asyncio.run(scenario())


def test_jail_service_persists_active_record() -> None:
    async def scenario() -> None:
        db = await aiosqlite.connect(tempfile.mktemp(suffix=".sqlite3"))
        db.row_factory = aiosqlite.Row
        service = JailService()
        await service.init_db(db)

        await service.upsert(
            db,
            guild_id=1,
            user_id=2,
            channel_id=3,
            role_id=4,
            reason="spam",
            moderator_id=5,
            started_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-01T00:10:00+00:00",
        )

        record = await service.get_active_by_user(db, 1, 2)
        assert record is not None
        assert record.channel_id == 3
        assert record.reason == "spam"
        assert len(await service.list_active(db)) == 1

        assert await service.remove(db, 1, 2) == 1
        assert await service.get_active_by_user(db, 1, 2) is None
        await db.close()

    asyncio.run(scenario())

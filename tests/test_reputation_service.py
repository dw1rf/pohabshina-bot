from __future__ import annotations

import asyncio

import aiosqlite

from services.reputation_service import ReputationService


def test_reputation_write_retries_a_short_sqlite_lock(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "reputation-lock.sqlite3"
        db = await aiosqlite.connect(path)
        blocker = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        blocker.row_factory = aiosqlite.Row
        service = ReputationService(lock_retry_delays=(0.02, 0.04, 0.08))
        try:
            for connection in (db, blocker):
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA busy_timeout=20")
            await service.init_rep_db(db)
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute("CREATE TABLE IF NOT EXISTS temporary_lock (id INTEGER)")

            async def release_lock() -> None:
                await asyncio.sleep(0.08)
                await blocker.commit()

            release_task = asyncio.create_task(release_lock())
            await service.add_rep_event(
                db,
                guild_id=1,
                giver_user_id=2,
                receiver_user_id=3,
                channel_id=4,
                message_id=5,
                rep_type="plus",
            )
            await release_task

            assert await service.get_user_rep(db, 1, 3) == (1, 0)
        finally:
            await blocker.close()
            await db.close()

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
import tempfile

import aiosqlite

from services.jail_service import JailService


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

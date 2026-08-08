from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite

from cogs.giveaways import GiveawaysCog


def test_giveaway_finish_is_guild_scoped_and_serialized() -> None:
    async def scenario() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.executescript(
            """
            CREATE TABLE giveaways(
                giveaway_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER, host_id INTEGER NOT NULL,
                prize TEXT NOT NULL, ends_at TEXT NOT NULL, required_role_id INTEGER,
                winner_count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE giveaway_entries(
                giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(giveaway_id,user_id)
            );
            CREATE TABLE giveaway_winners(
                giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                draw_number INTEGER NOT NULL DEFAULT 1,
                selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(giveaway_id,user_id,draw_number)
            );
            INSERT INTO giveaways(guild_id,channel_id,host_id,prize,ends_at,winner_count)
                VALUES (1,10,100,'A','2000-01-01T00:00:00+00:00',1);
            INSERT INTO giveaway_entries(giveaway_id,user_id) VALUES (1,501),(1,502);
            INSERT INTO giveaways(guild_id,channel_id,host_id,prize,ends_at,winner_count)
                VALUES (2,20,200,'B','2000-01-01T00:00:00+00:00',1);
            INSERT INTO giveaway_entries(giveaway_id,user_id) VALUES (2,601);
            """
        )
        await db.commit()
        bot = SimpleNamespace(giveaway_db=db, get_guild=lambda _guild_id: None)
        cog = GiveawaysCog(bot)

        first, second = await asyncio.gather(cog._finish(1, 1), cog._finish(1, 1))
        assert sorted((bool(first), bool(second))) == [False, True]
        assert await cog._finish(2, 1) == []
        row = await (await db.execute("SELECT status FROM giveaways WHERE giveaway_id=2")).fetchone()
        assert row["status"] == "active"
        await db.close()

    asyncio.run(scenario())

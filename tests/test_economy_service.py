from __future__ import annotations

import asyncio

import aiosqlite

from services.economy_service import EconomyService


async def _database():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    service = EconomyService()
    await service.init_db(db)
    return db, service


def test_economy_is_server_local_and_idempotent() -> None:
    async def scenario() -> None:
        db, service = await _database()
        first = await service.change_balance(db, 1, 10, 500, reason="test", idempotency_key="same")
        duplicate = await service.change_balance(db, 1, 10, 500, reason="test", idempotency_key="same")
        await service.change_balance(db, 2, 10, 75, reason="test", idempotency_key="same")
        guild_one = await service.wallet(db, 1, 10)
        guild_two = await service.wallet(db, 2, 10)
        assert first.ok is True
        assert duplicate.ok is False
        assert int(guild_one["balance"]) == 500
        assert int(guild_two["balance"]) == 75
        denied = await service.change_balance(db, 1, 10, -501, reason="test", idempotency_key="negative")
        assert denied.ok is False
        assert int((await service.wallet(db, 1, 10))["balance"]) == 500
        await db.close()

    asyncio.run(scenario())


def test_market_listing_can_only_be_bought_once() -> None:
    async def scenario() -> None:
        db, service = await _database()
        await service.change_balance(db, 1, 20, 1000, reason="seed", idempotency_key="buyer-a")
        await service.change_balance(db, 1, 21, 1000, reason="seed", idempotency_key="buyer-b")
        await service.add_item(db, 1, 10, 1, 1)
        await db.commit()
        listed = await service.market_create(db, 1, 10, 1, 1, 300)
        assert listed.ok
        listing_id = int((await service.market_list(db, 1))[0]["listing_id"])
        results = await asyncio.gather(
            service.market_buy(db, 1, 20, listing_id, idempotency_key="buy-a"),
            service.market_buy(db, 1, 21, listing_id, idempotency_key="buy-b"),
        )
        assert sum(result.ok for result in results) == 1
        cursor = await db.execute("SELECT status FROM economy_market WHERE listing_id=?", (listing_id,))
        assert (await cursor.fetchone())["status"] == "sold"
        cursor = await db.execute("SELECT SUM(quantity) AS total FROM economy_inventory WHERE guild_id=1 AND item_id=1")
        assert int((await cursor.fetchone())["total"]) == 1
        await db.close()

    asyncio.run(scenario())


def test_event_collection_survives_duplicate_interaction() -> None:
    async def scenario() -> None:
        db, service = await _database()
        event_id = await service.start_event(db, 1, "Season", 30)
        first = await service.collect_event(db, 1, 10, idempotency_key="event-key")
        duplicate = await service.collect_event(db, 1, 10, idempotency_key="event-key")
        cursor = await db.execute("SELECT tokens FROM economy_event_progress WHERE event_id=? AND user_id=10", (event_id,))
        progress = await cursor.fetchone()
        assert first.ok is True
        assert duplicate.ok is False
        assert int(progress["tokens"]) == first.amount
        await db.close()

    asyncio.run(scenario())

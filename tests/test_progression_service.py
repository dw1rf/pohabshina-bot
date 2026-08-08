from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite

from services.economy_service import EconomyService
from services.progression_service import ProgressionService


def run(coro):
    return asyncio.run(coro)


async def memory_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    return db


def test_one_event_projects_to_achievements_season_quest_and_club_war_once() -> None:
    async def scenario() -> None:
        db = await memory_db()
        service = ProgressionService()
        await service.init_db(db)
        await service.create_achievement(db, "talkative", "message", 2, reward_coins=100)
        await service.start_season(db, 10, "Night season", days=30)
        quest_id = await service.create_guild_quest(db, 10, "message", 2, days=7, reward_coins=50)
        await db.executescript(
            """
            CREATE TABLE club_communities(
                club_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                name TEXT NOT NULL, bank INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE club_members(
                club_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                role TEXT NOT NULL, joined_at TEXT NOT NULL, PRIMARY KEY(club_id, user_id)
            );
            INSERT INTO club_communities(club_id, guild_id, owner_id, name) VALUES (7, 10, 42, 'Noir');
            INSERT INTO club_members(club_id, guild_id, user_id, role, joined_at)
            VALUES (7, 10, 42, 'leader', 'now');
            """
        )
        await service.start_club_war(db, 10, days=7)

        assert await service.record_event(db, 10, 42, "message", 1, "message:100") is True
        assert await service.record_event(db, 10, 42, "message", 1, "message:100") is False
        assert await service.record_event(db, 10, 42, "message", 1, "message:101") is True

        progress = await service.get_user_progress(db, 10, 42)
        assert progress["achievements"]["talkative"]["value"] == 2
        assert progress["achievements"]["talkative"]["unlocked"] is True
        assert progress["season"]["points"] == 2
        quests = await service.get_active_guild_quests(db, 10, 42)
        assert quests[0]["quest_id"] == quest_id
        assert quests[0]["current_value"] == 2
        assert quests[0]["user_value"] == 2
        war = await service.get_club_war(db, 10, 42)
        assert war is not None and war["score"] == 2
        await db.close()

    run(scenario())


def test_cosmetics_must_be_unlocked_before_equipping() -> None:
    async def scenario() -> None:
        db = await memory_db()
        service = ProgressionService()
        await service.init_db(db)
        await service.register_cosmetic(db, "background", "events", "Event night")
        assert await service.equip_cosmetic(db, 1, 2, "background", "events") is False
        assert await service.unlock_cosmetic(db, 1, 2, "background", "events", "test") is True
        assert await service.unlock_cosmetic(db, 1, 2, "background", "events", "test") is False
        assert await service.equip_cosmetic(db, 1, 2, "background", "events") is True
        available = await service.list_available_cosmetics(db, 1, 2)
        assert available[0]["equipped"] is True
        await db.close()

    run(scenario())


def test_season_and_server_quest_claims_are_restart_safe() -> None:
    async def scenario() -> None:
        db = await memory_db()
        service = ProgressionService()
        await service.init_db(db)
        season_id = await service.start_season(db, 5, "Season", days=30)
        await service.add_season_tier(db, season_id, 1, 2, reward_coins=75, cosmetic_key="rose")
        quest_id = await service.create_guild_quest(db, 5, "message", 1, days=2, reward_coins=40)
        await service.record_event(db, 5, 9, "message", 2, "message:claim")

        reward = await service.claim_season_tier(db, 5, 9, 1)
        assert reward is not None and reward["coins"] == 75
        assert await service.claim_season_tier(db, 5, 9, 1) is None
        quest_reward = await service.claim_guild_quest(db, 5, 9, quest_id)
        assert quest_reward is not None and quest_reward["coins"] == 40
        assert await service.claim_guild_quest(db, 5, 9, quest_id) is None
        await db.close()

    run(scenario())


def test_pet_evolution_and_daily_boss_are_deterministic_and_idempotent() -> None:
    async def scenario() -> None:
        db = await memory_db()
        service = ProgressionService()
        await service.init_db(db)
        pet = await service.add_pet_xp(db, 3, 4, 500, idempotency_key="pet-xp:1")
        assert pet["stage"] == 2
        assert (await service.add_pet_xp(db, 3, 4, 500, idempotency_key="pet-xp:1"))["xp"] == 500
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        boss = await service.start_daily_boss(db, 3, now=now, max_hp=100)
        first = await service.attack_daily_boss(db, 3, 4, 60, "boss-hit:1", now=now)
        assert first is not None and first["hp"] == 40
        assert await service.attack_daily_boss(db, 3, 4, 60, "boss-hit:1", now=now) is None
        assert await service.attack_daily_boss(db, 3, 4, 60, "boss-hit:2", now=now) is None
        later = now + timedelta(hours=4)
        final = await service.attack_daily_boss(db, 3, 4, 60, "boss-hit:3", now=later)
        assert final is not None and final["hp"] == 0 and final["status"] == "defeated"
        assert (await service.get_daily_boss(db, 3, now=now))["boss_id"] == boss
        await db.close()

    run(scenario())


def test_relationship_milestone_claim_is_scoped_and_one_time() -> None:
    async def scenario() -> None:
        db = await memory_db()
        service = ProgressionService()
        await service.init_db(db)
        relationship_id = await service.create_relationship(db, 2, 10, 11, xp=300)
        reward = await service.claim_relationship_milestone(db, 2, relationship_id, 10, "xp_250", 250)
        assert reward is not None and reward["relationship_id"] == relationship_id
        assert await service.claim_relationship_milestone(db, 2, relationship_id, 11, "xp_250", 250) is None
        assert await service.claim_relationship_milestone(db, 99, relationship_id, 10, "xp_500", 500) is None
        await db.close()

    run(scenario())


def test_pet_equipment_and_club_skill_upgrade_are_atomic() -> None:
    async def scenario() -> None:
        db = await memory_db()
        try:
            service = ProgressionService()
            await service.init_db(db)
            await service.register_pet_equipment(db, "moon_claw", "weapon", attack=5)
            await service.grant_pet_equipment(db, 8, 20, "moon_claw", 1)
            assert await service.equip_pet_item(db, 8, 20, "moon_claw") is True
            stats = await service.get_pet_equipment_stats(db, 8, 20)
            assert stats == {"attack": 5, "defense": 0, "speed": 0}

            await db.executescript(
                """
                CREATE TABLE club_communities(
                    club_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL, bank INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE club_members(
                    club_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    role TEXT NOT NULL, joined_at TEXT NOT NULL, PRIMARY KEY(club_id, user_id)
                );
                INSERT INTO club_communities VALUES (3, 8, 20, 'Noir', 2000);
                INSERT INTO club_members VALUES (3, 8, 20, 'leader', 'now');
                INSERT INTO club_members VALUES (3, 8, 21, 'member', 'now');
                """
            )
            assert await service.upgrade_club_skill(db, 8, 21, "season_bonus") is None
            upgrade = await service.upgrade_club_skill(db, 8, 20, "season_bonus")
            assert upgrade is not None and upgrade["level"] == 1 and upgrade["bank"] == 1500
        finally:
            await db.close()

    run(scenario())


def test_club_war_reward_can_be_claimed_once_by_leader() -> None:
    async def scenario() -> None:
        db = await memory_db()
        try:
            service = ProgressionService()
            await service.init_db(db)
            await db.executescript(
                """
                CREATE TABLE club_communities(
                    club_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL, bank INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE club_members(
                    club_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    role TEXT NOT NULL, joined_at TEXT NOT NULL, PRIMARY KEY(club_id, user_id)
                );
                INSERT INTO club_communities VALUES (5, 9, 30, 'Noir', 0);
                INSERT INTO club_members VALUES (5, 9, 30, 'leader', 'now');
                """
            )
            war_id = await service.start_club_war(db, 9, days=7)
            await service.add_club_war_score(db, 9, 30, 10, "war:event:1")
            await db.execute("UPDATE club_war_seasons SET ends_at='2000-01-01T00:00:00+00:00' WHERE war_id=?", (war_id,))
            await db.commit()
            await service.close_expired_club_wars(db)
            reward = await service.claim_club_war_reward(db, 9, 30, war_id)
            assert reward is not None and reward["bank_reward"] == 1000
            assert await service.claim_club_war_reward(db, 9, 30, war_id) is None
        finally:
            await db.close()

    run(scenario())


def test_economy_and_progression_write_concurrently_on_domain_connections(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "domains.sqlite3"
        economy_db = await aiosqlite.connect(path)
        progression_db = await aiosqlite.connect(path)
        try:
            for db in (economy_db, progression_db):
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA busy_timeout=10000")
            economy = EconomyService()
            progression = ProgressionService()
            await economy.init_db(economy_db)
            await progression.init_db(progression_db)

            for index in range(10):
                balance, projected = await asyncio.gather(
                    economy.change_balance(
                        economy_db,
                        1,
                        10,
                        1,
                        reason="concurrency-test",
                        idempotency_key=f"economy:{index}",
                    ),
                    progression.record_event(
                        progression_db,
                        1,
                        10,
                        "message",
                        1,
                        f"message:{index}",
                    ),
                )
                assert balance.ok
                assert projected
        finally:
            await economy_db.close()
            await progression_db.close()

    run(scenario())


def test_forget_user_progress_removes_message_event_and_profile_state() -> None:
    async def scenario() -> None:
        db = await memory_db()
        try:
            service = ProgressionService()
            await service.init_db(db)
            await service.create_achievement(db, "speaker", "message", 1, name="Speaker")
            await service.record_event(db, 1, 10, "message", 1, "message:123")
            await service.forget_user_progress(db, 1, 10)
            assert await (await db.execute(
                "SELECT 1 FROM progression_events WHERE guild_id=1 AND user_id=10"
            )).fetchone() is None
            progress = await service.get_user_progress(db, 1, 10)
            assert progress["achievements"]["speaker"]["value"] == 0
            assert not progress["achievements"]["speaker"]["unlocked"]
        finally:
            await db.close()

    run(scenario())

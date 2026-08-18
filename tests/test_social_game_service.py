from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from pathlib import Path

import aiosqlite

from cogs.social_game_content import RP_ACTIONS, SERVICE_INSTRUCTIONS
from services.social_game_service import SocialGameService


class SocialGameServiceTests(unittest.TestCase):
    def test_tables_and_privacy_flags_persist(self) -> None:
        async def scenario() -> None:
            path = tempfile.mktemp(suffix=".sqlite3")
            db = await aiosqlite.connect(path)
            db.row_factory = aiosqlite.Row
            service = SocialGameService()
            await service.init_db(db)
            guild_settings = await service.ensure_guild_settings(db, 1)
            self.assertEqual(guild_settings["profile_analytics_enabled"], 1)
            self.assertEqual(guild_settings["matchmaking_enabled"], 1)
            default_privacy = await service.get_privacy_settings(db, 1, 42)
            self.assertTrue(default_privacy["analytics_enabled"])
            self.assertTrue(default_privacy["public_profile"])
            self.assertTrue(default_privacy["matchmaking_enabled"])
            self.assertFalse(default_privacy["store_message_samples"])
            await service.set_rp_consent(db, 1, 42, sfw=True, nsfw=False)
            self.assertTrue(await service.has_rp_consent(db, 1, 42, nsfw=False))
            self.assertFalse(await service.has_rp_consent(db, 1, 42, nsfw=True))
            await service.forget_profile_data(db, 1, 42)
            privacy_after = await service.get_privacy_settings(db, 1, 42)
            self.assertFalse(privacy_after["analytics_enabled"])
            self.assertFalse(privacy_after["public_profile"])
            self.assertFalse(privacy_after["matchmaking_enabled"])
            await db.close()

        asyncio.run(scenario())

    def test_migration_preserves_legacy_opt_out(self) -> None:
        async def scenario() -> None:
            path = tempfile.mktemp(suffix=".sqlite3")
            db = await aiosqlite.connect(path)
            db.row_factory = aiosqlite.Row
            await db.executescript(
                """
                CREATE TABLE user_privacy_settings (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    profile_opt_in INTEGER NOT NULL DEFAULT 0,
                    profile_public INTEGER NOT NULL DEFAULT 0,
                    match_opt_in INTEGER NOT NULL DEFAULT 0,
                    clone_opt_in INTEGER NOT NULL DEFAULT 0,
                    clone_public INTEGER NOT NULL DEFAULT 0,
                    store_message_samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                INSERT INTO user_privacy_settings (guild_id, user_id, profile_opt_in, profile_public, match_opt_in, updated_at)
                VALUES (1, 42, 0, 0, 0, '2026-01-01T00:00:00+00:00');
                """
            )
            service = SocialGameService()
            await service.init_db(db)
            privacy = await service.get_privacy_settings(db, 1, 42)
            self.assertFalse(privacy["analytics_enabled"])
            self.assertFalse(privacy["public_profile"])
            self.assertFalse(privacy["matchmaking_enabled"])
            await db.close()

        asyncio.run(scenario())

    def test_nsfw_channel_migration_and_selection_persist(self) -> None:
        async def scenario() -> None:
            db = await aiosqlite.connect(":memory:")
            db.row_factory = aiosqlite.Row
            await db.executescript(
                """
                CREATE TABLE guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    nsfw_rp_enabled INTEGER NOT NULL DEFAULT 0,
                    profile_analytics_enabled INTEGER NOT NULL DEFAULT 1,
                    matchmaking_enabled INTEGER NOT NULL DEFAULT 1,
                    story_nsfw_enabled INTEGER NOT NULL DEFAULT 0,
                    log_channel_id INTEGER NOT NULL DEFAULT 0,
                    adult_role_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO guild_settings (guild_id, updated_at)
                VALUES (1, '2026-01-01T00:00:00+00:00');
                """
            )

            service = SocialGameService()
            await service.init_db(db)
            await service.set_nsfw_channel(db, 1, 777)
            settings = await service.ensure_guild_settings(db, 1)

            self.assertEqual(settings["nsfw_channel_id"], 777)
            self.assertEqual(settings["nsfw_rp_enabled"], 1)
            await db.close()

        asyncio.run(scenario())

    def test_config_contains_safe_defaults(self) -> None:
        self.assertIn("unban", SERVICE_INSTRUCTIONS)
        self.assertTrue(any(payload["nsfw"] for payload in RP_ACTIONS.values()))
        self.assertTrue(all("text" in payload for payload in RP_ACTIONS.values()))
        self.assertIn("прижать_к_стенке", RP_ACTIONS)
        self.assertIn("французский_поцелуй", RP_ACTIONS)
        self.assertTrue(all("text" in payload and "label" in payload for payload in RP_ACTIONS.values()))

    def test_sfw_manifest_contains_more_than_sixty_actions(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "data" / "roleplay_sfw.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actions = manifest["actions"]
        self.assertGreaterEqual(len(actions), 60)
        self.assertTrue(all(len(values) == 3 for values in actions.values()))

    def test_pet_and_club_migration_preserves_existing_profiles(self) -> None:
        async def scenario() -> None:
            db = await aiosqlite.connect(":memory:")
            db.row_factory = aiosqlite.Row
            service = SocialGameService()
            await service.init_db(db)
            await db.execute(
                """INSERT INTO pets(guild_id, owner_id, name, type, level, xp, created_at, updated_at)
                   VALUES (1, 42, 'Искра', 'лиса', 7, 33, '2026-01-01', '2026-01-01')"""
            )
            await db.execute(
                """INSERT INTO club_profiles(guild_id, owner_id, name, level, coins_earned, created_at, updated_at)
                   VALUES (1, 42, 'Ночные', 5, 900, '2026-01-01', '2026-01-01')"""
            )
            await db.commit()
            await service.init_db(db)
            cursor = await db.execute("SELECT name, level, rating FROM pets WHERE guild_id=1 AND owner_id=42")
            pet = await cursor.fetchone()
            self.assertEqual((pet["name"], pet["level"], pet["rating"]), ("Искра", 7, 1000))
            cursor = await db.execute(
                """SELECT c.name, c.bank, m.role FROM club_communities c
                   JOIN club_members m ON m.club_id=c.club_id WHERE c.guild_id=1 AND c.owner_id=42"""
            )
            community = await cursor.fetchone()
            self.assertEqual((community["name"], community["bank"], community["role"]), ("Ночные", 900, "leader"))
            await db.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

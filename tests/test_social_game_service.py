from __future__ import annotations

import asyncio
import tempfile
import unittest

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
            self.assertTrue(default_privacy["store_message_samples"])
            await service.set_rp_consent(db, 1, 42, sfw=True, nsfw=False)
            self.assertTrue(await service.has_rp_consent(db, 1, 42, nsfw=False))
            self.assertFalse(await service.has_rp_consent(db, 1, 42, nsfw=True))
            await service.forget_profile_data(db, 1, 42)
            privacy_after = await service.get_privacy_settings(db, 1, 42)
            self.assertTrue(privacy_after["analytics_enabled"])
            self.assertTrue(privacy_after["public_profile"])
            self.assertTrue(privacy_after["matchmaking_enabled"])
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

    def test_config_contains_safe_defaults(self) -> None:
        self.assertIn("unban", SERVICE_INSTRUCTIONS)
        self.assertTrue(any(payload["nsfw"] for payload in RP_ACTIONS.values()))
        self.assertTrue(all("text" in payload for payload in RP_ACTIONS.values()))


if __name__ == "__main__":
    unittest.main()

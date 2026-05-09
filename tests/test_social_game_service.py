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
            await service.set_privacy_flag(db, 1, 42, "profile_opt_in", 1)
            await service.set_rp_consent(db, 1, 42, sfw=True, nsfw=False)
            privacy = await service.ensure_privacy(db, 1, 42)
            self.assertEqual(privacy["profile_opt_in"], 1)
            self.assertTrue(await service.has_rp_consent(db, 1, 42, nsfw=False))
            self.assertFalse(await service.has_rp_consent(db, 1, 42, nsfw=True))
            await service.forget_user(db, 1, 42)
            privacy_after = await service.ensure_privacy(db, 1, 42)
            self.assertEqual(privacy_after["profile_opt_in"], 0)
            await db.close()

        asyncio.run(scenario())

    def test_config_contains_safe_defaults(self) -> None:
        self.assertIn("unban", SERVICE_INSTRUCTIONS)
        self.assertTrue(any(payload["nsfw"] for payload in RP_ACTIONS.values()))
        self.assertTrue(all("text" in payload for payload in RP_ACTIONS.values()))


if __name__ == "__main__":
    unittest.main()

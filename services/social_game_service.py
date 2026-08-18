from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiosqlite


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class SocialGameService:
    """SQLite-backed storage for social analytics and game modules."""

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                nsfw_rp_enabled INTEGER NOT NULL DEFAULT 0,
                nsfw_channel_id INTEGER NOT NULL DEFAULT 0,
                profile_analytics_enabled INTEGER NOT NULL DEFAULT 1,
                matchmaking_enabled INTEGER NOT NULL DEFAULT 1,
                story_nsfw_enabled INTEGER NOT NULL DEFAULT 0,
                log_channel_id INTEGER NOT NULL DEFAULT 0,
                adult_role_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rp_consent_settings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rp_opt_in INTEGER NOT NULL DEFAULT 0,
                nsfw_rp_opt_in INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS rp_action_counters (
                guild_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                action_key TEXT NOT NULL,
                action_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, actor_id, target_id, action_key)
            );

            CREATE TABLE IF NOT EXISTS user_privacy_settings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                analytics_enabled INTEGER NOT NULL DEFAULT 1,
                public_profile INTEGER NOT NULL DEFAULT 1,
                matchmaking_enabled INTEGER NOT NULL DEFAULT 1,
                profile_opt_in INTEGER NOT NULL DEFAULT 1,
                profile_public INTEGER NOT NULL DEFAULT 1,
                match_opt_in INTEGER NOT NULL DEFAULT 1,
                clone_opt_in INTEGER NOT NULL DEFAULT 0,
                clone_public INTEGER NOT NULL DEFAULT 0,
                store_message_samples INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS user_activity_aggregates (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                total_length INTEGER NOT NULL DEFAULT 0,
                avg_length REAL NOT NULL DEFAULT 0,
                emoji_count INTEGER NOT NULL DEFAULT 0,
                question_count INTEGER NOT NULL DEFAULT 0,
                mention_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                words_json TEXT NOT NULL DEFAULT '{}',
                channels_json TEXT NOT NULL DEFAULT '{}',
                activity_days_json TEXT NOT NULL DEFAULT '{}',
                mentions_json TEXT NOT NULL DEFAULT '{}',
                reply_targets_json TEXT NOT NULL DEFAULT '{}',
                sample_short TEXT NOT NULL DEFAULT '',
                sample_recent TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS user_message_samples (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_sample TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS user_weekly_style_stats (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                avg_length REAL NOT NULL DEFAULT 0,
                emoji_count INTEGER NOT NULL DEFAULT 0,
                question_count INTEGER NOT NULL DEFAULT 0,
                words_json TEXT NOT NULL DEFAULT '{}',
                sample TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (guild_id, user_id, week_start)
            );

            CREATE TABLE IF NOT EXISTS user_social_edges (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                other_user_id INTEGER NOT NULL,
                reply_count INTEGER NOT NULL DEFAULT 0,
                mention_count INTEGER NOT NULL DEFAULT 0,
                shared_channel_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, other_user_id)
            );

            CREATE TABLE IF NOT EXISTS pets (
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                hunger INTEGER NOT NULL DEFAULT 80,
                happiness INTEGER NOT NULL DEFAULT 80,
                energy INTEGER NOT NULL DEFAULT 80,
                health INTEGER NOT NULL DEFAULT 100,
                streak INTEGER NOT NULL DEFAULT 0,
                last_feed_at TEXT,
                last_walk_at TEXT,
                last_play_at TEXT,
                last_daily_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, owner_id)
            );

            CREATE TABLE IF NOT EXISTS pet_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS story_progress (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                current_scene_id TEXT NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                coins INTEGER NOT NULL DEFAULT 0,
                inventory_json TEXT NOT NULL DEFAULT '[]',
                last_daily_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS story_scenes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                choices_json TEXT NOT NULL,
                nsfw INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS social_edges (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                weight INTEGER NOT NULL DEFAULT 0,
                replies_count INTEGER NOT NULL DEFAULT 0,
                mentions_count INTEGER NOT NULL DEFAULT 0,
                same_thread_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, target_user_id)
            );

            CREATE TABLE IF NOT EXISTS club_profiles (
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                coins_earned INTEGER NOT NULL DEFAULT 0,
                staff INTEGER NOT NULL DEFAULT 1,
                interior_level INTEGER NOT NULL DEFAULT 1,
                ads_level INTEGER NOT NULL DEFAULT 1,
                last_work_at TEXT,
                last_daily_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, owner_id)
            );

            CREATE TABLE IF NOT EXISTS club_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await self._migrate_guild_settings(db)
        await self._migrate_privacy_defaults(db)
        await self._migrate_activity_schema(db)
        await self._migrate_pet_battles(db)
        await self._migrate_club_communities(db)
        await db.execute(
            "INSERT OR IGNORE INTO social_schema_migrations(name, applied_at) VALUES ('social_games_v2', ?)",
            (utcnow_iso(),),
        )
        await db.commit()

    async def _migrate_guild_settings(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(guild_settings)")
        columns = {
            str(row["name"] if isinstance(row, aiosqlite.Row) else row[1])
            for row in await cursor.fetchall()
        }
        if "nsfw_channel_id" not in columns:
            await db.execute(
                "ALTER TABLE guild_settings ADD COLUMN nsfw_channel_id INTEGER NOT NULL DEFAULT 0"
            )

    async def _migrate_pet_battles(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(pets)")
        columns = {str(row["name"] if isinstance(row, aiosqlite.Row) else row[1]) for row in await cursor.fetchall()}
        additions = {
            "attack": "ALTER TABLE pets ADD COLUMN attack INTEGER NOT NULL DEFAULT 10",
            "defense": "ALTER TABLE pets ADD COLUMN defense INTEGER NOT NULL DEFAULT 10",
            "speed": "ALTER TABLE pets ADD COLUMN speed INTEGER NOT NULL DEFAULT 10",
            "rating": "ALTER TABLE pets ADD COLUMN rating INTEGER NOT NULL DEFAULT 1000",
            "wins": "ALTER TABLE pets ADD COLUMN wins INTEGER NOT NULL DEFAULT 0",
            "losses": "ALTER TABLE pets ADD COLUMN losses INTEGER NOT NULL DEFAULT 0",
            "last_adventure_at": "ALTER TABLE pets ADD COLUMN last_adventure_at TEXT",
        }
        for column, sql in additions.items():
            if column not in columns:
                await db.execute(sql)
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pet_inventory (
                guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, item_key TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                PRIMARY KEY(guild_id, owner_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS pet_battles (
                battle_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                attacker_id INTEGER NOT NULL, defender_id INTEGER NOT NULL, winner_id INTEGER NOT NULL,
                rating_delta INTEGER NOT NULL, battle_type TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )

    async def _migrate_club_communities(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS club_communities (
                club_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                name TEXT NOT NULL, bank INTEGER NOT NULL DEFAULT 0 CHECK(bank >= 0),
                skill_level INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(guild_id, owner_id)
            );
            CREATE TABLE IF NOT EXISTS club_members (
                club_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member', joined_at TEXT NOT NULL,
                PRIMARY KEY(club_id, user_id), UNIQUE(guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS club_applications (
                club_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(club_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS club_community_inventory (
                club_id INTEGER NOT NULL, item_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                PRIMARY KEY(club_id, item_id)
            );
            """
        )
        now = utcnow_iso()
        await db.execute(
            """INSERT OR IGNORE INTO club_communities(guild_id, owner_id, name, bank, skill_level, created_at, updated_at)
               SELECT guild_id, owner_id, name, MAX(coins_earned, 0), MAX(level, 1), created_at, updated_at FROM club_profiles"""
        )
        await db.execute(
            """INSERT OR IGNORE INTO club_members(club_id, guild_id, user_id, role, joined_at)
               SELECT club_id, guild_id, owner_id, 'leader', ? FROM club_communities""",
            (now,),
        )

    async def _migrate_activity_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(user_activity_aggregates)")
        columns = {str(row["name"] if isinstance(row, aiosqlite.Row) else row[1]) for row in await cursor.fetchall()}
        migrations = {
            "avg_length": "ALTER TABLE user_activity_aggregates ADD COLUMN avg_length REAL NOT NULL DEFAULT 0",
            "activity_days_json": "ALTER TABLE user_activity_aggregates ADD COLUMN activity_days_json TEXT NOT NULL DEFAULT '{}'",
            "mentions_json": "ALTER TABLE user_activity_aggregates ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '{}'",
            "reply_targets_json": "ALTER TABLE user_activity_aggregates ADD COLUMN reply_targets_json TEXT NOT NULL DEFAULT '{}'",
            "last_seen_at": "ALTER TABLE user_activity_aggregates ADD COLUMN last_seen_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                await db.execute(sql)
        cursor = await db.execute("PRAGMA table_info(user_privacy_settings)")
        privacy_columns = {str(row["name"] if isinstance(row, aiosqlite.Row) else row[1]) for row in await cursor.fetchall()}
        if "created_at" not in privacy_columns:
            await db.execute("ALTER TABLE user_privacy_settings ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_message_samples (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_sample TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS social_edges (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                weight INTEGER NOT NULL DEFAULT 0,
                replies_count INTEGER NOT NULL DEFAULT 0,
                mentions_count INTEGER NOT NULL DEFAULT 0,
                same_thread_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, target_user_id)
            );
            """
        )

    async def _migrate_privacy_defaults(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(user_privacy_settings)")
        columns = {str(row["name"] if isinstance(row, aiosqlite.Row) else row[1]) for row in await cursor.fetchall()}
        migrations = {
            "analytics_enabled": ("ALTER TABLE user_privacy_settings ADD COLUMN analytics_enabled INTEGER NOT NULL DEFAULT 1", "profile_opt_in"),
            "public_profile": ("ALTER TABLE user_privacy_settings ADD COLUMN public_profile INTEGER NOT NULL DEFAULT 1", "profile_public"),
            "matchmaking_enabled": ("ALTER TABLE user_privacy_settings ADD COLUMN matchmaking_enabled INTEGER NOT NULL DEFAULT 1", "match_opt_in"),
        }
        for column, (sql, legacy_column) in migrations.items():
            if column not in columns:
                await db.execute(sql)
                if legacy_column in columns:
                    await db.execute(f"UPDATE user_privacy_settings SET {column} = {legacy_column}")
        # Keep legacy columns populated for compatibility with older code paths.
        for column in ("profile_opt_in", "profile_public", "match_opt_in"):
            if column in columns:
                await db.execute(f"UPDATE user_privacy_settings SET {column} = 1 WHERE {column} IS NULL")

    async def ensure_guild_settings(self, db: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row:
        now = utcnow_iso()
        await db.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id, updated_at) VALUES (?, ?)",
            (guild_id, now),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        assert row is not None
        return row

    async def set_guild_flag(self, db: aiosqlite.Connection, guild_id: int, field: str, value: int) -> None:
        if field not in {"nsfw_rp_enabled", "nsfw_channel_id", "profile_analytics_enabled", "matchmaking_enabled", "story_nsfw_enabled", "log_channel_id", "adult_role_id"}:
            raise ValueError("Unsupported guild setting")
        await self.ensure_guild_settings(db, guild_id)
        await db.execute(f"UPDATE guild_settings SET {field} = ?, updated_at = ? WHERE guild_id = ?", (value, utcnow_iso(), guild_id))
        await db.commit()

    async def set_nsfw_channel(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        channel_id: int,
    ) -> None:
        await self.ensure_guild_settings(db, guild_id)
        await db.execute(
            """
            UPDATE guild_settings
            SET nsfw_channel_id = ?,
                nsfw_rp_enabled = CASE WHEN ? > 0 THEN 1 ELSE nsfw_rp_enabled END,
                updated_at = ?
            WHERE guild_id = ?
            """,
            (channel_id, channel_id, utcnow_iso(), guild_id),
        )
        await db.commit()

    async def ensure_privacy(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> aiosqlite.Row:
        await db.execute(
            """
            INSERT OR IGNORE INTO user_privacy_settings
                (guild_id, user_id, analytics_enabled, public_profile, matchmaking_enabled, profile_opt_in, profile_public, match_opt_in, created_at, updated_at)
            VALUES (?, ?, 1, 1, 1, 1, 1, 1, ?, ?)
            """,
            (guild_id, user_id, utcnow_iso(), utcnow_iso()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM user_privacy_settings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = await cur.fetchone()
        assert row is not None
        return row

    async def get_privacy_settings(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, bool]:
        cur = await db.execute(
            "SELECT * FROM user_privacy_settings WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return {
                "analytics_enabled": True,
                "public_profile": True,
                "matchmaking_enabled": True,
                "store_message_samples": False,
                "clone_opt_in": False,
                "clone_public": False,
            }
        keys = set(row.keys())
        analytics = bool(row["analytics_enabled"] if "analytics_enabled" in keys else row["profile_opt_in"])
        public = bool(row["public_profile"] if "public_profile" in keys else row["profile_public"])
        matchmaking = bool(row["matchmaking_enabled"] if "matchmaking_enabled" in keys else row["match_opt_in"])
        return {
            "analytics_enabled": analytics,
            "public_profile": public,
            "matchmaking_enabled": matchmaking,
            "store_message_samples": bool(row["store_message_samples"]),
            "clone_opt_in": bool(row["clone_opt_in"]),
            "clone_public": bool(row["clone_public"]),
        }

    async def set_privacy_flag(self, db: aiosqlite.Connection, guild_id: int, user_id: int, field: str, value: int) -> None:
        aliases = {
            "profile_opt_in": "analytics_enabled",
            "profile_public": "public_profile",
            "match_opt_in": "matchmaking_enabled",
        }
        canonical = aliases.get(field, field)
        if canonical not in {"analytics_enabled", "public_profile", "matchmaking_enabled", "clone_opt_in", "clone_public", "store_message_samples"}:
            raise ValueError("Unsupported privacy setting")
        await self.ensure_privacy(db, guild_id, user_id)
        updates = [f"{canonical} = ?", "updated_at = ?"]
        params: list[Any] = [int(value), utcnow_iso()]
        legacy_by_canonical = {
            "analytics_enabled": "profile_opt_in",
            "public_profile": "profile_public",
            "matchmaking_enabled": "match_opt_in",
        }
        legacy = legacy_by_canonical.get(canonical)
        if legacy:
            updates.append(f"{legacy} = ?")
            params.append(int(value))
        params.extend([guild_id, user_id])
        await db.execute(f"UPDATE user_privacy_settings SET {', '.join(updates)} WHERE guild_id = ? AND user_id = ?", tuple(params))
        await db.commit()

    async def set_profile_privacy(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        *,
        analytics_enabled: bool,
        public_profile: bool,
        matchmaking_enabled: bool,
    ) -> None:
        await self.ensure_privacy(db, guild_id, user_id)
        await db.execute(
            """
            UPDATE user_privacy_settings
            SET analytics_enabled = ?, public_profile = ?, matchmaking_enabled = ?,
                profile_opt_in = ?, profile_public = ?, match_opt_in = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                int(analytics_enabled), int(public_profile), int(matchmaking_enabled),
                int(analytics_enabled), int(public_profile), int(matchmaking_enabled),
                utcnow_iso(), guild_id, user_id,
            ),
        )
        await db.commit()

    async def forget_profile_data(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> None:
        for table, column in (
            ("user_activity_aggregates", "user_id"),
            ("user_message_samples", "user_id"),
            ("user_weekly_style_stats", "user_id"),
            ("user_social_edges", "user_id"),
            ("social_edges", "user_id"),
        ):
            await db.execute(f"DELETE FROM {table} WHERE guild_id = ? AND {column} = ?", (guild_id, user_id))
        await db.execute("DELETE FROM user_social_edges WHERE guild_id = ? AND other_user_id = ?", (guild_id, user_id))
        await db.execute("DELETE FROM social_edges WHERE guild_id = ? AND target_user_id = ?", (guild_id, user_id))
        await self.set_profile_privacy(
            db,
            guild_id,
            user_id,
            analytics_enabled=False,
            public_profile=False,
            matchmaking_enabled=False,
        )

    async def forget_user(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> None:
        for table, column in (
            ("user_privacy_settings", "user_id"), ("rp_consent_settings", "user_id"),
            ("rp_action_counters", "actor_id"),
            ("user_activity_aggregates", "user_id"), ("user_message_samples", "user_id"),
            ("user_weekly_style_stats", "user_id"), ("user_social_edges", "user_id"),
            ("social_edges", "user_id"), ("pets", "owner_id"), ("pet_actions", "owner_id"),
            ("story_progress", "user_id"), ("club_profiles", "owner_id"), ("club_transactions", "owner_id"),
        ):
            await db.execute(f"DELETE FROM {table} WHERE guild_id = ? AND {column} = ?", (guild_id, user_id))
        await db.execute("DELETE FROM user_social_edges WHERE guild_id = ? AND other_user_id = ?", (guild_id, user_id))
        await db.execute("DELETE FROM social_edges WHERE guild_id = ? AND target_user_id = ?", (guild_id, user_id))
        await db.execute("DELETE FROM rp_action_counters WHERE guild_id = ? AND target_id = ?", (guild_id, user_id))
        await db.commit()

    async def set_rp_consent(self, db: aiosqlite.Connection, guild_id: int, user_id: int, *, sfw: bool | None = None, nsfw: bool | None = None) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO rp_consent_settings (guild_id, user_id, updated_at) VALUES (?, ?, ?)",
            (guild_id, user_id, utcnow_iso()),
        )
        assignments: list[str] = ["updated_at = ?"]
        params: list[Any] = [utcnow_iso()]
        if sfw is not None:
            assignments.append("rp_opt_in = ?")
            params.append(int(sfw))
        if nsfw is not None:
            assignments.append("nsfw_rp_opt_in = ?")
            params.append(int(nsfw))
        params.extend([guild_id, user_id])
        await db.execute(f"UPDATE rp_consent_settings SET {', '.join(assignments)} WHERE guild_id = ? AND user_id = ?", tuple(params))
        await db.commit()

    async def has_rp_consent(self, db: aiosqlite.Connection, guild_id: int, user_id: int, *, nsfw: bool) -> bool:
        cur = await db.execute("SELECT rp_opt_in, nsfw_rp_opt_in FROM rp_consent_settings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        row = await cur.fetchone()
        return bool(row and row["rp_opt_in"] and (not nsfw or row["nsfw_rp_opt_in"]))

    async def increment_rp_action(self, db: aiosqlite.Connection, guild_id: int, actor_id: int, target_id: int, action_key: str) -> int:
        await db.execute(
            """INSERT INTO rp_action_counters(guild_id, actor_id, target_id, action_key, action_count, updated_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(guild_id, actor_id, target_id, action_key) DO UPDATE SET
               action_count=action_count+1, updated_at=excluded.updated_at""",
            (guild_id, actor_id, target_id, action_key, utcnow_iso()),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT action_count FROM rp_action_counters WHERE guild_id=? AND actor_id=? AND target_id=? AND action_key=?",
            (guild_id, actor_id, target_id, action_key),
        )
        row = await cur.fetchone()
        return int(row["action_count"] if row else 1)

    async def seed_story_scenes(self, db: aiosqlite.Connection, scenes: list[dict[str, Any]]) -> None:
        for scene in scenes:
            await db.execute(
                "INSERT OR IGNORE INTO story_scenes (id, title, text, choices_json, nsfw) VALUES (?, ?, ?, ?, ?)",
                (scene["id"], scene["title"], scene["text"], json.dumps(scene.get("choices", []), ensure_ascii=False), int(scene.get("nsfw", False))),
            )
        await db.commit()

    @staticmethod
    def week_start(value: datetime) -> str:
        day = value.date() - timedelta(days=value.weekday())
        return day.isoformat()

    @staticmethod
    def today() -> str:
        return date.today().isoformat()

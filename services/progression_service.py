from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite


KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat()


def row_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ProgressionService:
    """Local, guild-scoped progression driven by idempotent domain events."""

    MIGRATION_VERSION = "gameplay_v2_catalogs"
    SEASON_WEIGHTS = {
        "message": 1,
        "economy_action": 3,
        "rp_action": 2,
        "pet_adventure": 5,
        "pet_win": 8,
        "club_contribution": 4,
        "relationship_xp": 3,
        "suggestion_accepted": 15,
        "starboard_post": 10,
    }

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pet_stage_thresholds = (250, 1000, 2500)

    def set_pet_stage_thresholds(self, thresholds: tuple[int, int, int]) -> None:
        values = tuple(int(value) for value in thresholds)
        if len(values) != 3 or values[0] <= 0 or not values[0] < values[1] < values[2]:
            raise ValueError("Pet stage thresholds must be three increasing positive integers")
        self._pet_stage_thresholds = values

    @staticmethod
    def _key(value: str, label: str = "key") -> str:
        cleaned = value.strip().lower()
        if not KEY_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid {label}")
        return cleaned

    @staticmethod
    def _positive_id(value: int, label: str) -> int:
        value = int(value)
        if value <= 0:
            raise ValueError(f"{label} must be positive")
        return value

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations(
                name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS progression_events(
                guild_id INTEGER NOT NULL, event_key TEXT NOT NULL,
                user_id INTEGER NOT NULL, event_type TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0), metadata_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL, PRIMARY KEY(guild_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS progression_reward_outbox(
                reward_key TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0, item_id INTEGER, quantity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL, delivered_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gameplay_delivery_outbox(
                delivery_key TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0, economy_xp INTEGER NOT NULL DEFAULT 0,
                pet_xp INTEGER NOT NULL DEFAULT 0, event_type TEXT, event_amount INTEGER NOT NULL DEFAULT 0,
                economy_delivered INTEGER NOT NULL DEFAULT 0,
                progression_delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS achievement_catalog(
                achievement_key TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                threshold INTEGER NOT NULL CHECK(threshold > 0), reward_coins INTEGER NOT NULL DEFAULT 0,
                cosmetic_type TEXT, cosmetic_key TEXT, hidden INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS achievement_progress(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, achievement_key TEXT NOT NULL,
                value INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id, achievement_key)
            );
            CREATE TABLE IF NOT EXISTS achievement_unlocks(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, achievement_key TEXT NOT NULL,
                source_event_key TEXT NOT NULL, unlocked_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id, achievement_key)
            );
            CREATE TABLE IF NOT EXISTS cosmetic_catalog(
                cosmetic_type TEXT NOT NULL, cosmetic_key TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(cosmetic_type, cosmetic_key)
            );
            CREATE TABLE IF NOT EXISTS cosmetic_unlocks(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, cosmetic_type TEXT NOT NULL,
                cosmetic_key TEXT NOT NULL, source TEXT NOT NULL, unlocked_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id, cosmetic_type, cosmetic_key)
            );
            CREATE TABLE IF NOT EXISTS profile_customization(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                background_key TEXT, accent_key TEXT, badge_key TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS progression_seasons(
                season_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
                starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','ended','cancelled'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_progression_season
                ON progression_seasons(guild_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS season_tiers(
                season_id INTEGER NOT NULL, tier INTEGER NOT NULL, required_points INTEGER NOT NULL,
                reward_coins INTEGER NOT NULL DEFAULT 0, cosmetic_key TEXT,
                PRIMARY KEY(season_id, tier)
            );
            CREATE TABLE IF NOT EXISTS season_progress(
                season_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(season_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS season_reward_claims(
                season_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                tier INTEGER NOT NULL, claimed_at TEXT NOT NULL,
                PRIMARY KEY(season_id, user_id, tier)
            );
            CREATE TABLE IF NOT EXISTS guild_quests(
                quest_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, event_type TEXT NOT NULL,
                target_value INTEGER NOT NULL CHECK(target_value > 0), current_value INTEGER NOT NULL DEFAULT 0,
                reward_coins INTEGER NOT NULL DEFAULT 0, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','expired','cancelled'))
            );
            CREATE TABLE IF NOT EXISTS guild_quest_contributions(
                quest_id INTEGER NOT NULL, user_id INTEGER NOT NULL, value INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY(quest_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS guild_quest_claims(
                quest_id INTEGER NOT NULL, user_id INTEGER NOT NULL, claimed_at TEXT NOT NULL,
                PRIMARY KEY(quest_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS pet_progression(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                stage INTEGER NOT NULL DEFAULT 1, species_key TEXT NOT NULL DEFAULT 'shadow_cat',
                updated_at TEXT NOT NULL, PRIMARY KEY(guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS pet_progression_receipts(
                guild_id INTEGER NOT NULL, event_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(guild_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS pet_equipment_catalog(
                item_key TEXT PRIMARY KEY, slot TEXT NOT NULL,
                attack INTEGER NOT NULL DEFAULT 0, defense INTEGER NOT NULL DEFAULT 0,
                speed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pet_equipment_inventory(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item_key TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                PRIMARY KEY(guild_id,user_id,item_key)
            );
            CREATE TABLE IF NOT EXISTS pet_equipped_items(
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, slot TEXT NOT NULL,
                item_key TEXT NOT NULL, equipped_at TEXT NOT NULL,
                PRIMARY KEY(guild_id,user_id,slot)
            );
            CREATE TABLE IF NOT EXISTS pet_daily_bosses(
                boss_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, day_key TEXT NOT NULL,
                hp INTEGER NOT NULL, max_hp INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, UNIQUE(guild_id, day_key)
            );
            CREATE TABLE IF NOT EXISTS pet_boss_contributions(
                boss_id INTEGER NOT NULL, user_id INTEGER NOT NULL, damage INTEGER NOT NULL DEFAULT 0,
                last_attack_at TEXT, PRIMARY KEY(boss_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS pet_boss_receipts(
                boss_id INTEGER NOT NULL, event_key TEXT NOT NULL, user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(boss_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS club_war_seasons(
                war_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_club_war
                ON club_war_seasons(guild_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS club_war_scores(
                war_id INTEGER NOT NULL, club_id INTEGER NOT NULL, score INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY(war_id, club_id)
            );
            CREATE TABLE IF NOT EXISTS club_war_contributions(
                war_id INTEGER NOT NULL, event_key TEXT NOT NULL, club_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, points INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(war_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS club_war_claims(
                war_id INTEGER NOT NULL, club_id INTEGER NOT NULL, claimed_by INTEGER NOT NULL,
                bank_reward INTEGER NOT NULL, claimed_at TEXT NOT NULL,
                PRIMARY KEY(war_id,club_id)
            );
            CREATE TABLE IF NOT EXISTS club_skill_levels(
                club_id INTEGER NOT NULL, skill_key TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0 CHECK(level BETWEEN 0 AND 5), updated_at TEXT NOT NULL,
                PRIMARY KEY(club_id,skill_key)
            );
            CREATE TABLE IF NOT EXISTS local_relationships(
                relationship_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                user1_id INTEGER NOT NULL, user2_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relationship_milestone_claims(
                relationship_id INTEGER NOT NULL, milestone_key TEXT NOT NULL, claimed_by INTEGER NOT NULL,
                claimed_at TEXT NOT NULL, PRIMARY KEY(relationship_id, milestone_key)
            );
            CREATE TABLE IF NOT EXISTS feature_settings(
                guild_id INTEGER NOT NULL, feature_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, feature_key)
            );
            """
        )
        achievement_columns = {
            str(row["name"])
            for row in await (await db.execute("PRAGMA table_info(achievement_catalog)")).fetchall()
        }
        if "name" not in achievement_columns:
            await db.execute("ALTER TABLE achievement_catalog ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (self.MIGRATION_VERSION, iso()),
        )
        await db.commit()

    async def create_achievement(
        self,
        db: aiosqlite.Connection,
        key: str,
        event_type: str,
        threshold: int,
        *,
        reward_coins: int = 0,
        cosmetic_type: str | None = None,
        cosmetic_key: str | None = None,
        hidden: bool = False,
        name: str = "",
    ) -> None:
        key = self._key(key, "achievement key")
        event_type = self._key(event_type, "event type")
        if threshold <= 0 or reward_coins < 0:
            raise ValueError("Invalid achievement values")
        await db.execute(
            """INSERT INTO achievement_catalog
               (achievement_key,event_type,threshold,reward_coins,cosmetic_type,cosmetic_key,hidden,name)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(achievement_key) DO UPDATE SET
               event_type=excluded.event_type, threshold=excluded.threshold, reward_coins=excluded.reward_coins,
               cosmetic_type=excluded.cosmetic_type, cosmetic_key=excluded.cosmetic_key,
               hidden=excluded.hidden, name=excluded.name""",
            (key, event_type, threshold, reward_coins, cosmetic_type, cosmetic_key, int(hidden), name.strip()[:80]),
        )
        await db.commit()

    async def start_season(self, db: aiosqlite.Connection, guild_id: int, name: str, *, days: int = 30) -> int:
        guild_id = self._positive_id(guild_id, "guild_id")
        if not 1 <= days <= 180 or not name.strip():
            raise ValueError("Invalid season")
        now = utcnow()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("UPDATE progression_seasons SET status='ended' WHERE guild_id=? AND status='active'", (guild_id,))
                cursor = await db.execute(
                    "INSERT INTO progression_seasons(guild_id,name,starts_at,ends_at) VALUES (?,?,?,?)",
                    (guild_id, name.strip()[:80], iso(now), iso(now + timedelta(days=days))),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return int(cursor.lastrowid)

    async def add_season_tier(
        self, db: aiosqlite.Connection, season_id: int, tier: int, required_points: int,
        *, reward_coins: int = 0, cosmetic_key: str | None = None,
    ) -> None:
        if min(season_id, tier, required_points) <= 0 or reward_coins < 0:
            raise ValueError("Invalid season tier")
        await db.execute(
            """INSERT INTO season_tiers(season_id,tier,required_points,reward_coins,cosmetic_key)
               VALUES (?,?,?,?,?) ON CONFLICT(season_id,tier) DO UPDATE SET
               required_points=excluded.required_points,reward_coins=excluded.reward_coins,cosmetic_key=excluded.cosmetic_key""",
            (season_id, tier, required_points, reward_coins, cosmetic_key),
        )
        await db.commit()

    async def create_guild_quest(
        self, db: aiosqlite.Connection, guild_id: int, event_type: str, target_value: int,
        *, days: int = 7, reward_coins: int = 0,
    ) -> int:
        event_type = self._key(event_type, "event type")
        if target_value <= 0 or not 1 <= days <= 90 or reward_coins < 0:
            raise ValueError("Invalid guild quest")
        now = utcnow()
        cursor = await db.execute(
            """INSERT INTO guild_quests
               (guild_id,event_type,target_value,reward_coins,starts_at,ends_at)
               VALUES (?,?,?,?,?,?)""",
            (guild_id, event_type, target_value, reward_coins, iso(now), iso(now + timedelta(days=days))),
        )
        await db.commit()
        return int(cursor.lastrowid)

    async def start_club_war(self, db: aiosqlite.Connection, guild_id: int, *, days: int = 7) -> int:
        if not 1 <= days <= 90:
            raise ValueError("Invalid club war duration")
        now = utcnow()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("UPDATE club_war_seasons SET status='ended' WHERE guild_id=? AND status='active'", (guild_id,))
                cursor = await db.execute(
                    "INSERT INTO club_war_seasons(guild_id,starts_at,ends_at) VALUES (?,?,?)",
                    (guild_id, iso(now), iso(now + timedelta(days=days))),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return int(cursor.lastrowid)

    async def _table_exists(self, db: aiosqlite.Connection, table: str) -> bool:
        cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return await cursor.fetchone() is not None

    async def record_event(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int,
        event_type: str, amount: int, idempotency_key: str,
        *, metadata: dict[str, Any] | None = None,
    ) -> bool:
        event_type = self._key(event_type, "event type")
        event_key = self._key(idempotency_key, "idempotency key")
        guild_id = self._positive_id(guild_id, "guild_id")
        user_id = self._positive_id(user_id, "user_id")
        amount = int(amount)
        if amount <= 0 or amount > 1_000_000:
            raise ValueError("Invalid event amount")
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        now = utcnow()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    await db.execute(
                        """INSERT INTO progression_events
                           (guild_id,event_key,user_id,event_type,amount,metadata_json,occurred_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (guild_id, event_key, user_id, event_type, amount, metadata_json, iso(now)),
                    )
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    return False

                cursor = await db.execute(
                    "SELECT * FROM achievement_catalog WHERE event_type=?", (event_type,)
                )
                for achievement in await cursor.fetchall():
                    key = str(achievement["achievement_key"])
                    await db.execute(
                        """INSERT INTO achievement_progress(guild_id,user_id,achievement_key,value,updated_at)
                           VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id,achievement_key) DO UPDATE SET
                           value=value+excluded.value,updated_at=excluded.updated_at""",
                        (guild_id, user_id, key, amount, iso(now)),
                    )
                    value_row = await (await db.execute(
                        "SELECT value FROM achievement_progress WHERE guild_id=? AND user_id=? AND achievement_key=?",
                        (guild_id, user_id, key),
                    )).fetchone()
                    if int(value_row["value"]) >= int(achievement["threshold"]):
                        inserted = await db.execute(
                            """INSERT OR IGNORE INTO achievement_unlocks
                               (guild_id,user_id,achievement_key,source_event_key,unlocked_at) VALUES (?,?,?,?,?)""",
                            (guild_id, user_id, key, event_key, iso(now)),
                        )
                        if inserted.rowcount:
                            cosmetic_type = achievement["cosmetic_type"]
                            cosmetic_key = achievement["cosmetic_key"]
                            if cosmetic_type and cosmetic_key:
                                await self._unlock_cosmetic_tx(
                                    db, guild_id, user_id, str(cosmetic_type), str(cosmetic_key), f"achievement:{key}", now
                                )
                            reward = int(achievement["reward_coins"])
                            if reward:
                                await self._queue_reward_tx(
                                    db, f"achievement:{guild_id}:{user_id}:{key}", guild_id, user_id, reward, f"achievement:{key}", now
                                )

                cursor = await db.execute(
                    """SELECT season_id FROM progression_seasons
                       WHERE guild_id=? AND status='active' AND starts_at<=? AND ends_at>?
                       ORDER BY season_id DESC LIMIT 1""",
                    (guild_id, iso(now), iso(now)),
                )
                season = await cursor.fetchone()
                if season is not None:
                    points = amount * self.SEASON_WEIGHTS.get(event_type, 1)
                    if await self._table_exists(db, "club_members"):
                        bonus_row = await (await db.execute(
                            """SELECT COALESCE(s.level,0) AS level FROM club_members m
                               LEFT JOIN club_skill_levels s ON s.club_id=m.club_id AND s.skill_key='season_bonus'
                               WHERE m.guild_id=? AND m.user_id=?""",
                            (guild_id, user_id),
                        )).fetchone()
                        if bonus_row is not None:
                            points = max(points, (points * (100 + int(bonus_row["level"]) * 5) + 99) // 100)
                    await db.execute(
                        """INSERT INTO season_progress(season_id,guild_id,user_id,points,updated_at)
                           VALUES (?,?,?,?,?) ON CONFLICT(season_id,user_id) DO UPDATE SET
                           points=points+excluded.points,updated_at=excluded.updated_at""",
                        (int(season["season_id"]), guild_id, user_id, points, iso(now)),
                    )

                cursor = await db.execute(
                    """SELECT * FROM guild_quests WHERE guild_id=? AND event_type=? AND status='active'
                       AND starts_at<=? AND ends_at>?""",
                    (guild_id, event_type, iso(now), iso(now)),
                )
                for quest in await cursor.fetchall():
                    remaining = max(int(quest["target_value"]) - int(quest["current_value"]), 0)
                    contribution = min(amount, remaining)
                    if contribution <= 0:
                        continue
                    await db.execute(
                        """UPDATE guild_quests SET current_value=current_value+?,
                           status=CASE WHEN current_value+?>=target_value THEN 'completed' ELSE status END
                           WHERE quest_id=?""",
                        (contribution, contribution, int(quest["quest_id"])),
                    )
                    await db.execute(
                        """INSERT INTO guild_quest_contributions(quest_id,user_id,value,updated_at)
                           VALUES (?,?,?,?) ON CONFLICT(quest_id,user_id) DO UPDATE SET
                           value=value+excluded.value,updated_at=excluded.updated_at""",
                        (int(quest["quest_id"]), user_id, contribution, iso(now)),
                    )

                if await self._table_exists(db, "club_members"):
                    member = await (await db.execute(
                        "SELECT club_id FROM club_members WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                    )).fetchone()
                    war = await (await db.execute(
                        """SELECT war_id FROM club_war_seasons WHERE guild_id=? AND status='active'
                           AND starts_at<=? AND ends_at>? ORDER BY war_id DESC LIMIT 1""",
                        (guild_id, iso(now), iso(now)),
                    )).fetchone()
                    if member is not None and war is not None:
                        club_id, war_id = int(member["club_id"]), int(war["war_id"])
                        await db.execute(
                            """INSERT OR IGNORE INTO club_war_contributions
                               (war_id,event_key,club_id,user_id,points,created_at) VALUES (?,?,?,?,?,?)""",
                            (war_id, event_key, club_id, user_id, amount, iso(now)),
                        )
                        await db.execute(
                            """INSERT INTO club_war_scores(war_id,club_id,score,updated_at) VALUES (?,?,?,?)
                               ON CONFLICT(war_id,club_id) DO UPDATE SET score=score+excluded.score,updated_at=excluded.updated_at""",
                            (war_id, club_id, amount, iso(now)),
                        )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def get_user_progress(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any]:
        cursor = await db.execute(
            """SELECT c.achievement_key,c.name,COALESCE(p.value,0) AS value,c.threshold,c.hidden,
                      CASE WHEN u.achievement_key IS NULL THEN 0 ELSE 1 END AS unlocked
               FROM achievement_catalog c LEFT JOIN achievement_progress p
                    ON p.achievement_key=c.achievement_key AND p.guild_id=? AND p.user_id=?
               LEFT JOIN achievement_unlocks u ON u.guild_id=? AND u.user_id=?
                    AND u.achievement_key=c.achievement_key
               ORDER BY c.achievement_key""",
            (guild_id, user_id, guild_id, user_id),
        )
        achievements = {
            str(row["achievement_key"]): {
                "value": int(row["value"]), "threshold": int(row["threshold"]),
                "unlocked": bool(row["unlocked"]), "hidden": bool(row["hidden"]),
                "name": str(row["name"] or ""),
            }
            for row in await cursor.fetchall()
        }
        season_row = await (await db.execute(
            """SELECT s.season_id,s.name,COALESCE(p.points,0) AS points,s.ends_at
               FROM progression_seasons s LEFT JOIN season_progress p
               ON p.season_id=s.season_id AND p.user_id=?
               WHERE s.guild_id=? AND s.status='active' AND s.ends_at>? ORDER BY s.season_id DESC LIMIT 1""",
            (user_id, guild_id, iso()),
        )).fetchone()
        return {"achievements": achievements, "season": row_dict(season_row)}

    async def register_cosmetic(self, db: aiosqlite.Connection, cosmetic_type: str, key: str, name: str) -> None:
        cosmetic_type = self._key(cosmetic_type, "cosmetic type")
        key = self._key(key, "cosmetic key")
        if cosmetic_type not in {"background", "accent", "badge"} or not name.strip():
            raise ValueError("Invalid cosmetic")
        await db.execute(
            "INSERT OR REPLACE INTO cosmetic_catalog(cosmetic_type,cosmetic_key,name) VALUES (?,?,?)",
            (cosmetic_type, key, name.strip()[:80]),
        )
        await db.commit()

    async def _unlock_cosmetic_tx(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int,
        cosmetic_type: str, key: str, source: str, now: datetime,
    ) -> bool:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO cosmetic_unlocks
               (guild_id,user_id,cosmetic_type,cosmetic_key,source,unlocked_at) VALUES (?,?,?,?,?,?)""",
            (guild_id, user_id, cosmetic_type, key, source[:120], iso(now)),
        )
        return bool(cursor.rowcount)

    async def unlock_cosmetic(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int,
        cosmetic_type: str, key: str, source: str,
    ) -> bool:
        catalog = await (await db.execute(
            "SELECT 1 FROM cosmetic_catalog WHERE cosmetic_type=? AND cosmetic_key=?", (cosmetic_type, key)
        )).fetchone()
        if catalog is None:
            return False
        inserted = await self._unlock_cosmetic_tx(db, guild_id, user_id, cosmetic_type, key, source, utcnow())
        await db.commit()
        return inserted

    async def equip_cosmetic(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, cosmetic_type: str, key: str,
    ) -> bool:
        if cosmetic_type not in {"background", "accent", "badge"}:
            return False
        unlocked = await (await db.execute(
            """SELECT 1 FROM cosmetic_unlocks WHERE guild_id=? AND user_id=?
               AND cosmetic_type=? AND cosmetic_key=?""",
            (guild_id, user_id, cosmetic_type, key),
        )).fetchone()
        if unlocked is None:
            return False
        column = f"{cosmetic_type}_key"
        await db.execute(
            "INSERT OR IGNORE INTO profile_customization(guild_id,user_id,updated_at) VALUES (?,?,?)",
            (guild_id, user_id, iso()),
        )
        await db.execute(
            f"UPDATE profile_customization SET {column}=?,updated_at=? WHERE guild_id=? AND user_id=?",
            (key, iso(), guild_id, user_id),
        )
        await db.commit()
        return True

    async def list_available_cosmetics(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        cursor = await db.execute(
            """SELECT u.cosmetic_type,u.cosmetic_key,c.name,
               CASE u.cosmetic_type WHEN 'background' THEN p.background_key
                    WHEN 'accent' THEN p.accent_key ELSE p.badge_key END AS selected
               FROM cosmetic_unlocks u JOIN cosmetic_catalog c
                 ON c.cosmetic_type=u.cosmetic_type AND c.cosmetic_key=u.cosmetic_key
               LEFT JOIN profile_customization p ON p.guild_id=u.guild_id AND p.user_id=u.user_id
               WHERE u.guild_id=? AND u.user_id=? ORDER BY u.cosmetic_type,u.cosmetic_key""",
            (guild_id, user_id),
        )
        return [dict(row, equipped=str(row["selected"] or "") == str(row["cosmetic_key"])) for row in await cursor.fetchall()]

    async def get_profile_customization(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any]:
        row = await (await db.execute(
            "SELECT * FROM profile_customization WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )).fetchone()
        return row_dict(row) or {"background_key": None, "accent_key": None, "badge_key": None}

    async def forget_user_progress(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int
    ) -> None:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "progression_events",
                    "progression_reward_outbox",
                    "achievement_progress",
                    "achievement_unlocks",
                    "cosmetic_unlocks",
                    "profile_customization",
                    "season_progress",
                    "season_reward_claims",
                    "pet_progression",
                    "pet_progression_receipts",
                    "pet_equipment_inventory",
                    "pet_equipped_items",
                ):
                    await db.execute(
                        f"DELETE FROM {table} WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                    )
                for table in ("guild_quest_contributions", "guild_quest_claims"):
                    await db.execute(
                        f"""DELETE FROM {table} WHERE user_id=? AND quest_id IN
                            (SELECT quest_id FROM guild_quests WHERE guild_id=?)""",
                        (user_id, guild_id),
                    )
                await db.execute(
                    """DELETE FROM pet_boss_contributions WHERE user_id=? AND boss_id IN
                       (SELECT boss_id FROM pet_daily_bosses WHERE guild_id=?)""",
                    (user_id, guild_id),
                )
                await db.execute(
                    """DELETE FROM pet_boss_receipts WHERE user_id=? AND boss_id IN
                       (SELECT boss_id FROM pet_daily_bosses WHERE guild_id=?)""",
                    (user_id, guild_id),
                )
                await db.execute(
                    """DELETE FROM club_war_contributions WHERE user_id=? AND war_id IN
                       (SELECT war_id FROM club_war_seasons WHERE guild_id=?)""",
                    (user_id, guild_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _queue_reward_tx(
        self, db: aiosqlite.Connection, reward_key: str, guild_id: int, user_id: int,
        coins: int, source: str, now: datetime,
    ) -> None:
        await db.execute(
            """INSERT OR IGNORE INTO progression_reward_outbox
               (reward_key,guild_id,user_id,coins,source,created_at) VALUES (?,?,?,?,?,?)""",
            (reward_key, guild_id, user_id, coins, source, iso(now)),
        )

    async def claim_season_tier(self, db: aiosqlite.Connection, guild_id: int, user_id: int, tier: int) -> dict[str, Any] | None:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (await db.execute(
                    """SELECT s.season_id,t.required_points,t.reward_coins,t.cosmetic_key,COALESCE(p.points,0) AS points
                       FROM progression_seasons s JOIN season_tiers t ON t.season_id=s.season_id
                       LEFT JOIN season_progress p ON p.season_id=s.season_id AND p.user_id=?
                       WHERE s.guild_id=? AND s.status='active' AND s.ends_at>? AND t.tier=?
                       ORDER BY s.season_id DESC LIMIT 1""",
                    (user_id, guild_id, iso(), tier),
                )).fetchone()
                if row is None or int(row["points"]) < int(row["required_points"]):
                    await db.rollback()
                    return None
                try:
                    await db.execute(
                        "INSERT INTO season_reward_claims(season_id,guild_id,user_id,tier,claimed_at) VALUES (?,?,?,?,?)",
                        (int(row["season_id"]), guild_id, user_id, tier, iso()),
                    )
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    return None
                cosmetic = row["cosmetic_key"]
                if cosmetic:
                    exists = await (await db.execute(
                        "SELECT 1 FROM cosmetic_catalog WHERE cosmetic_type='badge' AND cosmetic_key=?", (str(cosmetic),)
                    )).fetchone()
                    if exists:
                        await self._unlock_cosmetic_tx(db, guild_id, user_id, "badge", str(cosmetic), "season", utcnow())
                coins = int(row["reward_coins"])
                await self._queue_reward_tx(
                    db, f"season:{row['season_id']}:{user_id}:{tier}", guild_id, user_id, coins, "season_tier", utcnow()
                )
                await db.commit()
                return {"coins": coins, "cosmetic_key": cosmetic, "season_id": int(row["season_id"]), "tier": tier}
            except Exception:
                await db.rollback()
                raise

    async def get_active_season(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any] | None:
        progress = await self.get_user_progress(db, guild_id, user_id)
        return progress["season"]

    async def get_active_guild_quests(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        cursor = await db.execute(
            """SELECT q.*,COALESCE(c.value,0) AS user_value FROM guild_quests q
               LEFT JOIN guild_quest_contributions c ON c.quest_id=q.quest_id AND c.user_id=?
               WHERE q.guild_id=? AND q.status IN ('active','completed') AND q.ends_at>?
               ORDER BY q.quest_id DESC""",
            (user_id, guild_id, iso()),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def claim_guild_quest(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, quest_id: int,
    ) -> dict[str, Any] | None:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (await db.execute(
                    """SELECT q.reward_coins,q.status,COALESCE(c.value,0) AS contribution
                       FROM guild_quests q LEFT JOIN guild_quest_contributions c
                       ON c.quest_id=q.quest_id AND c.user_id=?
                       WHERE q.quest_id=? AND q.guild_id=?""",
                    (user_id, quest_id, guild_id),
                )).fetchone()
                if row is None or row["status"] != "completed" or int(row["contribution"]) <= 0:
                    await db.rollback()
                    return None
                try:
                    await db.execute(
                        "INSERT INTO guild_quest_claims(quest_id,user_id,claimed_at) VALUES (?,?,?)",
                        (quest_id, user_id, iso()),
                    )
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    return None
                coins = int(row["reward_coins"])
                await self._queue_reward_tx(
                    db, f"guild-quest:{quest_id}:{user_id}", guild_id, user_id, coins, "guild_quest", utcnow()
                )
                await db.commit()
                return {"coins": coins, "quest_id": quest_id}
            except Exception:
                await db.rollback()
                raise

    async def add_pet_xp(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, amount: int,
        *, idempotency_key: str,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("XP must be positive")
        event_key = self._key(idempotency_key, "idempotency key")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (await db.execute(
                    "SELECT 1 FROM pet_progression_receipts WHERE guild_id=? AND event_key=?", (guild_id, event_key)
                )).fetchone()
                if existing is None:
                    await db.execute(
                        "INSERT INTO pet_progression_receipts(guild_id,event_key,user_id,created_at) VALUES (?,?,?,?)",
                        (guild_id, event_key, user_id, iso()),
                    )
                    await db.execute(
                        """INSERT INTO pet_progression(guild_id,user_id,xp,stage,updated_at) VALUES (?,?,?,?,?)
                           ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=xp+excluded.xp,updated_at=excluded.updated_at""",
                        (guild_id, user_id, amount, 1, iso()),
                    )
                    first, second, third = self._pet_stage_thresholds
                    await db.execute(
                        """UPDATE pet_progression SET stage=CASE WHEN xp>=? THEN 4 WHEN xp>=? THEN 3
                           WHEN xp>=? THEN 2 ELSE 1 END WHERE guild_id=? AND user_id=?""",
                        (third, second, first, guild_id, user_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_pet_progress(db, guild_id, user_id)

    async def get_pet_progress(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any]:
        row = await (await db.execute(
            "SELECT * FROM pet_progression WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )).fetchone()
        return row_dict(row) or {"guild_id": guild_id, "user_id": user_id, "xp": 0, "stage": 1, "species_key": "shadow_cat"}

    async def register_pet_equipment(
        self, db: aiosqlite.Connection, item_key: str, slot: str,
        *, attack: int = 0, defense: int = 0, speed: int = 0,
    ) -> None:
        item_key = self._key(item_key, "pet equipment key")
        slot = self._key(slot, "pet equipment slot")
        if slot not in {"weapon", "armor", "charm"} or min(attack, defense, speed) < 0:
            raise ValueError("Invalid pet equipment")
        await db.execute(
            """INSERT INTO pet_equipment_catalog(item_key,slot,attack,defense,speed) VALUES (?,?,?,?,?)
               ON CONFLICT(item_key) DO UPDATE SET slot=excluded.slot,attack=excluded.attack,
               defense=excluded.defense,speed=excluded.speed""",
            (item_key, slot, attack, defense, speed),
        )
        await db.commit()

    async def grant_pet_equipment(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_key: str, quantity: int = 1,
    ) -> None:
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        exists = await (await db.execute(
            "SELECT 1 FROM pet_equipment_catalog WHERE item_key=?", (item_key,)
        )).fetchone()
        if exists is None:
            raise ValueError("Unknown pet equipment")
        await db.execute(
            """INSERT INTO pet_equipment_inventory(guild_id,user_id,item_key,quantity) VALUES (?,?,?,?)
               ON CONFLICT(guild_id,user_id,item_key) DO UPDATE SET quantity=quantity+excluded.quantity""",
            (guild_id, user_id, item_key, quantity),
        )
        await db.commit()

    async def equip_pet_item(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_key: str,
    ) -> bool:
        row = await (await db.execute(
            """SELECT c.slot FROM pet_equipment_inventory i JOIN pet_equipment_catalog c ON c.item_key=i.item_key
               WHERE i.guild_id=? AND i.user_id=? AND i.item_key=? AND i.quantity>0""",
            (guild_id, user_id, item_key),
        )).fetchone()
        if row is None:
            return False
        await db.execute(
            """INSERT INTO pet_equipped_items(guild_id,user_id,slot,item_key,equipped_at) VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id,user_id,slot) DO UPDATE SET item_key=excluded.item_key,equipped_at=excluded.equipped_at""",
            (guild_id, user_id, str(row["slot"]), item_key, iso()),
        )
        await db.commit()
        return True

    async def unequip_pet_item(self, db: aiosqlite.Connection, guild_id: int, user_id: int, slot: str) -> bool:
        cursor = await db.execute(
            "DELETE FROM pet_equipped_items WHERE guild_id=? AND user_id=? AND slot=?", (guild_id, user_id, slot)
        )
        await db.commit()
        return bool(cursor.rowcount)

    async def get_pet_equipment_stats(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, int]:
        row = await (await db.execute(
            """SELECT COALESCE(SUM(c.attack),0) AS attack,COALESCE(SUM(c.defense),0) AS defense,
                      COALESCE(SUM(c.speed),0) AS speed
               FROM pet_equipped_items e JOIN pet_equipment_catalog c ON c.item_key=e.item_key
               WHERE e.guild_id=? AND e.user_id=?""",
            (guild_id, user_id),
        )).fetchone()
        return {"attack": int(row["attack"]), "defense": int(row["defense"]), "speed": int(row["speed"])}

    async def list_pet_equipment(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        cursor = await db.execute(
            """SELECT i.item_key,i.quantity,c.slot,c.attack,c.defense,c.speed,
                      CASE WHEN e.item_key=i.item_key THEN 1 ELSE 0 END AS equipped
               FROM pet_equipment_inventory i JOIN pet_equipment_catalog c ON c.item_key=i.item_key
               LEFT JOIN pet_equipped_items e ON e.guild_id=i.guild_id AND e.user_id=i.user_id
                    AND e.slot=c.slot
               WHERE i.guild_id=? AND i.user_id=? AND i.quantity>0 ORDER BY c.slot,i.item_key""",
            (guild_id, user_id),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def start_daily_boss(
        self, db: aiosqlite.Connection, guild_id: int, *, now: datetime | None = None, max_hp: int = 5000,
    ) -> int:
        now = (now or utcnow()).astimezone(UTC)
        if max_hp <= 0:
            raise ValueError("Invalid boss hp")
        day_key = now.date().isoformat()
        await db.execute(
            """INSERT OR IGNORE INTO pet_daily_bosses
               (guild_id,day_key,hp,max_hp,starts_at,ends_at) VALUES (?,?,?,?,?,?)""",
            (guild_id, day_key, max_hp, max_hp, iso(now), iso(now + timedelta(days=1))),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT boss_id FROM pet_daily_bosses WHERE guild_id=? AND day_key=?", (guild_id, day_key)
        )).fetchone()
        assert row is not None
        return int(row["boss_id"])

    async def get_daily_boss(
        self, db: aiosqlite.Connection, guild_id: int, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        day_key = (now or utcnow()).astimezone(UTC).date().isoformat()
        row = await (await db.execute(
            "SELECT * FROM pet_daily_bosses WHERE guild_id=? AND day_key=?", (guild_id, day_key)
        )).fetchone()
        return row_dict(row)

    async def attack_daily_boss(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, damage: int,
        idempotency_key: str, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        now = (now or utcnow()).astimezone(UTC)
        event_key = self._key(idempotency_key, "idempotency key")
        if damage <= 0 or damage > 1_000_000:
            raise ValueError("Invalid damage")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                boss = await (await db.execute(
                    "SELECT * FROM pet_daily_bosses WHERE guild_id=? AND day_key=?",
                    (guild_id, now.date().isoformat()),
                )).fetchone()
                if boss is None or boss["status"] != "active":
                    await db.rollback()
                    return None
                duplicate = await (await db.execute(
                    "SELECT 1 FROM pet_boss_receipts WHERE boss_id=? AND event_key=?", (int(boss["boss_id"]), event_key)
                )).fetchone()
                if duplicate is not None:
                    await db.rollback()
                    return None
                contribution = await (await db.execute(
                    "SELECT last_attack_at FROM pet_boss_contributions WHERE boss_id=? AND user_id=?",
                    (int(boss["boss_id"]), user_id),
                )).fetchone()
                if contribution and contribution["last_attack_at"]:
                    last = datetime.fromisoformat(str(contribution["last_attack_at"]))
                    if now - last < timedelta(hours=3):
                        await db.rollback()
                        return None
                actual = min(damage, int(boss["hp"]))
                new_hp = int(boss["hp"]) - actual
                status = "defeated" if new_hp == 0 else "active"
                await db.execute(
                    "UPDATE pet_daily_bosses SET hp=?,status=? WHERE boss_id=?",
                    (new_hp, status, int(boss["boss_id"])),
                )
                await db.execute(
                    """INSERT INTO pet_boss_contributions(boss_id,user_id,damage,last_attack_at) VALUES (?,?,?,?)
                       ON CONFLICT(boss_id,user_id) DO UPDATE SET damage=damage+excluded.damage,last_attack_at=excluded.last_attack_at""",
                    (int(boss["boss_id"]), user_id, actual, iso(now)),
                )
                await db.execute(
                    "INSERT INTO pet_boss_receipts(boss_id,event_key,user_id,created_at) VALUES (?,?,?,?)",
                    (int(boss["boss_id"]), event_key, user_id, iso(now)),
                )
                await db.commit()
                return {"boss_id": int(boss["boss_id"]), "hp": new_hp, "status": status, "damage": actual}
            except Exception:
                await db.rollback()
                raise

    async def add_club_war_score(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, points: int, event_key: str,
    ) -> bool:
        return await self.record_event(db, guild_id, user_id, "club_contribution", points, event_key)

    async def upgrade_club_skill(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, skill_key: str,
    ) -> dict[str, int] | None:
        skill_key = self._key(skill_key, "club skill key")
        if skill_key not in {"season_bonus", "pet_bonus", "storage"}:
            return None
        if not await self._table_exists(db, "club_members") or not await self._table_exists(db, "club_communities"):
            return None
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                member = await (await db.execute(
                    """SELECT m.club_id,m.role,c.bank FROM club_members m JOIN club_communities c ON c.club_id=m.club_id
                       WHERE m.guild_id=? AND m.user_id=?""",
                    (guild_id, user_id),
                )).fetchone()
                if member is None or member["role"] != "leader":
                    await db.rollback()
                    return None
                level_row = await (await db.execute(
                    "SELECT level FROM club_skill_levels WHERE club_id=? AND skill_key=?",
                    (int(member["club_id"]), skill_key),
                )).fetchone()
                current = int(level_row["level"] if level_row else 0)
                if current >= 5:
                    await db.rollback()
                    return None
                cost = 500 * (current + 1)
                if int(member["bank"]) < cost:
                    await db.rollback()
                    return None
                new_level = current + 1
                await db.execute(
                    "UPDATE club_communities SET bank=bank-? WHERE club_id=?", (cost, int(member["club_id"]))
                )
                await db.execute(
                    """INSERT INTO club_skill_levels(club_id,skill_key,level,updated_at) VALUES (?,?,?,?)
                       ON CONFLICT(club_id,skill_key) DO UPDATE SET level=excluded.level,updated_at=excluded.updated_at""",
                    (int(member["club_id"]), skill_key, new_level, iso()),
                )
                await db.commit()
                return {"club_id": int(member["club_id"]), "level": new_level, "bank": int(member["bank"]) - cost, "cost": cost}
            except Exception:
                await db.rollback()
                raise

    async def get_club_skills(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        if not await self._table_exists(db, "club_members"):
            return []
        member = await (await db.execute(
            "SELECT club_id,role FROM club_members WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )).fetchone()
        if member is None:
            return []
        cursor = await db.execute(
            "SELECT skill_key,level FROM club_skill_levels WHERE club_id=? ORDER BY skill_key", (int(member["club_id"]),)
        )
        values = {str(row["skill_key"]): int(row["level"]) for row in await cursor.fetchall()}
        return [
            {"skill_key": key, "level": values.get(key, 0), "role": str(member["role"])}
            for key in ("season_bonus", "pet_bonus", "storage")
        ]

    async def get_club_war(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any] | None:
        await self.close_expired_club_wars(db)
        if not await self._table_exists(db, "club_members"):
            return None
        row = await (await db.execute(
            """SELECT w.war_id,w.starts_at,w.ends_at,m.club_id,COALESCE(s.score,0) AS score
               FROM club_war_seasons w JOIN club_members m ON m.guild_id=w.guild_id AND m.user_id=?
               LEFT JOIN club_war_scores s ON s.war_id=w.war_id AND s.club_id=m.club_id
               WHERE w.guild_id=? AND w.status='active' AND w.ends_at>? ORDER BY w.war_id DESC LIMIT 1""",
            (user_id, guild_id, iso()),
        )).fetchone()
        return row_dict(row)

    async def close_expired_club_wars(self, db: aiosqlite.Connection) -> int:
        cursor = await db.execute(
            "UPDATE club_war_seasons SET status='ended' WHERE status='active' AND ends_at<=?", (iso(),)
        )
        await db.commit()
        return int(cursor.rowcount)

    async def claim_club_war_reward(
        self, db: aiosqlite.Connection, guild_id: int, user_id: int, war_id: int = 0,
    ) -> dict[str, int] | None:
        if not await self._table_exists(db, "club_members") or not await self._table_exists(db, "club_communities"):
            return None
        await self.close_expired_club_wars(db)
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                member = await (await db.execute(
                    """SELECT m.club_id,m.role FROM club_members m WHERE m.guild_id=? AND m.user_id=?""",
                    (guild_id, user_id),
                )).fetchone()
                if member is None or member["role"] != "leader":
                    await db.rollback()
                    return None
                if war_id <= 0:
                    war = await (await db.execute(
                        """SELECT war_id FROM club_war_seasons WHERE guild_id=? AND status='ended'
                           ORDER BY war_id DESC LIMIT 1""", (guild_id,)
                    )).fetchone()
                    if war is None:
                        await db.rollback()
                        return None
                    war_id = int(war["war_id"])
                valid = await (await db.execute(
                    "SELECT 1 FROM club_war_seasons WHERE war_id=? AND guild_id=? AND status='ended'",
                    (war_id, guild_id),
                )).fetchone()
                if valid is None:
                    await db.rollback()
                    return None
                scores = await (await db.execute(
                    "SELECT club_id,score FROM club_war_scores WHERE war_id=? ORDER BY score DESC,club_id ASC",
                    (war_id,),
                )).fetchall()
                rank = next((index for index, row in enumerate(scores, start=1) if int(row["club_id"]) == int(member["club_id"])), None)
                if rank is None:
                    await db.rollback()
                    return None
                bank_reward = 1000 if rank == 1 else 500 if rank == 2 else 250
                try:
                    await db.execute(
                        "INSERT INTO club_war_claims(war_id,club_id,claimed_by,bank_reward,claimed_at) VALUES (?,?,?,?,?)",
                        (war_id, int(member["club_id"]), user_id, bank_reward, iso()),
                    )
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    return None
                await db.execute(
                    "UPDATE club_communities SET bank=bank+? WHERE club_id=?",
                    (bank_reward, int(member["club_id"])),
                )
                await db.commit()
                return {"war_id": war_id, "rank": rank, "bank_reward": bank_reward, "club_id": int(member["club_id"])}
            except Exception:
                await db.rollback()
                raise

    async def create_relationship(
        self, db: aiosqlite.Connection, guild_id: int, user1_id: int, user2_id: int, *, xp: int = 0,
    ) -> int:
        if user1_id == user2_id or xp < 0:
            raise ValueError("Invalid relationship")
        cursor = await db.execute(
            "INSERT INTO local_relationships(guild_id,user1_id,user2_id,xp,created_at) VALUES (?,?,?,?,?)",
            (guild_id, user1_id, user2_id, xp, iso()),
        )
        await db.commit()
        return int(cursor.lastrowid)

    async def claim_relationship_milestone(
        self, db: aiosqlite.Connection, guild_id: int, relationship_id: int, user_id: int,
        milestone_key: str, required_xp: int,
    ) -> dict[str, Any] | None:
        milestone_key = self._key(milestone_key, "milestone key")
        relationship = await (await db.execute(
            """SELECT * FROM local_relationships WHERE relationship_id=? AND guild_id=? AND active=1
               AND (user1_id=? OR user2_id=?)""",
            (relationship_id, guild_id, user_id, user_id),
        )).fetchone()
        if relationship is None or int(relationship["xp"]) < required_xp:
            return None
        try:
            await db.execute(
                "INSERT INTO relationship_milestone_claims(relationship_id,milestone_key,claimed_by,claimed_at) VALUES (?,?,?,?)",
                (relationship_id, milestone_key, user_id, iso()),
            )
        except aiosqlite.IntegrityError:
            await db.rollback()
            return None
        await db.commit()
        return {"relationship_id": relationship_id, "milestone_key": milestone_key, "xp": int(relationship["xp"])}

    async def get_relationship(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, Any] | None:
        row = await (await db.execute(
            """SELECT * FROM local_relationships WHERE guild_id=? AND active=1
               AND (user1_id=? OR user2_id=?) ORDER BY relationship_id DESC LIMIT 1""",
            (guild_id, user_id, user_id),
        )).fetchone()
        return row_dict(row)

    async def pending_rewards(self, db: aiosqlite.Connection, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await db.execute(
            "SELECT * FROM progression_reward_outbox WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_reward_delivered(self, db: aiosqlite.Connection, reward_key: str) -> None:
        await db.execute(
            "UPDATE progression_reward_outbox SET delivered_at=? WHERE reward_key=? AND delivered_at IS NULL",
            (iso(), reward_key),
        )
        await db.commit()

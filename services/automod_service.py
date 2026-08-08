from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

import aiosqlite


LINK_RE = re.compile(
    r"(?:https?://[^\s<>]+|www\.[^\s<>]+|discord(?:app)?\.com/invite/[\w-]+|discord\.gg/[\w-]+)",
    re.IGNORECASE,
)
ALLOWED_ACTIONS = {"logged", "deleted", "timed_out", "permission_missing", "quarantined", "quarantine_failed"}
RULE_COLUMNS = {
    "burst": ("burst_enabled", "burst_count"),
    "duplicate": ("duplicate_enabled", "duplicate_count"),
    "mass_mentions": ("mass_mentions_enabled", "mass_mentions"),
    "link_flood": ("link_flood_enabled", "link_count"),
    "raid": ("raid_enabled", "raid_join_count"),
}


@dataclass(slots=True, frozen=True)
class AutomodDecision:
    rules: tuple[str, ...]
    action_mode: str
    content_hash: str
    metadata: dict[str, int]


@dataclass(slots=True, frozen=True)
class RaidDecision:
    raid_detected: bool
    suspicious_account: bool
    should_quarantine: bool
    quarantine_role_id: int
    recent_joins: int


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _normalize_for_hash(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content or "").casefold()
    visible = "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(visible.split())[:4000]


class AutomodService:
    MIGRATION_VERSION = "automod_v1"

    """Deterministic, local-only moderation state. Raw message text is never persisted."""

    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS automod_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                action_mode TEXT NOT NULL DEFAULT 'log'
                    CHECK(action_mode IN ('log', 'delete', 'timeout')),
                log_channel_id INTEGER NOT NULL DEFAULT 0,
                burst_enabled INTEGER NOT NULL DEFAULT 1,
                burst_count INTEGER NOT NULL DEFAULT 6,
                burst_window_seconds INTEGER NOT NULL DEFAULT 10,
                duplicate_enabled INTEGER NOT NULL DEFAULT 1,
                duplicate_count INTEGER NOT NULL DEFAULT 3,
                duplicate_window_seconds INTEGER NOT NULL DEFAULT 60,
                mass_mentions_enabled INTEGER NOT NULL DEFAULT 1,
                mass_mentions INTEGER NOT NULL DEFAULT 5,
                link_flood_enabled INTEGER NOT NULL DEFAULT 1,
                link_count INTEGER NOT NULL DEFAULT 3,
                timeout_minutes INTEGER NOT NULL DEFAULT 10,
                raid_enabled INTEGER NOT NULL DEFAULT 0,
                raid_join_count INTEGER NOT NULL DEFAULT 8,
                raid_window_seconds INTEGER NOT NULL DEFAULT 30,
                min_account_age_hours INTEGER NOT NULL DEFAULT 24,
                quarantine_role_id INTEGER NOT NULL DEFAULT 0,
                content_hash_salt TEXT NOT NULL,
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automod_exemptions (
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('channel', 'role')),
                target_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, kind, target_id)
            );
            CREATE TABLE IF NOT EXISTS automod_strikes (
                strike_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rule TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_automod_strikes_user
                ON automod_strikes(guild_id, user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS automod_incidents (
                incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                rule TEXT NOT NULL,
                action TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_automod_incidents_guild
                ON automod_incidents(guild_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS automod_message_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 0,
                link_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_automod_events_recent
                ON automod_message_events(guild_id, user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS automod_raid_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                account_age_hours INTEGER NOT NULL,
                quarantined INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_automod_raid_recent
                ON automod_raid_state(guild_id, joined_at DESC);
            """
        )
        await db.execute(
            """DELETE FROM automod_incidents
               WHERE message_id IS NOT NULL AND incident_id NOT IN (
                   SELECT MIN(incident_id) FROM automod_incidents
                   WHERE message_id IS NOT NULL GROUP BY guild_id,message_id,rule
               )"""
        )
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_automod_incident_message_rule
               ON automod_incidents(guild_id,message_id,rule) WHERE message_id IS NOT NULL"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (self.MIGRATION_VERSION, _utc().isoformat()),
        )
        await db.commit()

    async def get_settings(self, db: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row:
        cursor = await db.execute("SELECT * FROM automod_settings WHERE guild_id=?", (guild_id,))
        row = await cursor.fetchone()
        if row is not None:
            return row
        async with self._write_lock:
            await db.execute(
                """INSERT OR IGNORE INTO automod_settings(
                       guild_id, content_hash_salt, updated_at
                   ) VALUES (?, ?, ?)""",
                (guild_id, secrets.token_hex(32), _utc().isoformat()),
            )
            await db.commit()
        cursor = await db.execute("SELECT * FROM automod_settings WHERE guild_id=?", (guild_id,))
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Не удалось создать настройки автомодерации.")
        return row

    async def configure(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        actor_id: int,
        enabled: bool | None = None,
        action_mode: str | None = None,
        log_channel_id: int | None = None,
        burst_count: int | None = None,
        burst_window_seconds: int | None = None,
        duplicate_count: int | None = None,
        duplicate_window_seconds: int | None = None,
        mass_mentions: int | None = None,
        link_count: int | None = None,
        timeout_minutes: int | None = None,
        raid_enabled: bool | None = None,
        raid_join_count: int | None = None,
        raid_window_seconds: int | None = None,
        min_account_age_hours: int | None = None,
        quarantine_role_id: int | None = None,
    ) -> aiosqlite.Row:
        await self.get_settings(db, guild_id)
        if action_mode is not None and action_mode not in {"log", "delete", "timeout"}:
            raise ValueError("Допустимые режимы: log, delete, timeout.")
        values: dict[str, Any] = {
            "enabled": int(enabled) if enabled is not None else None,
            "action_mode": action_mode,
            "log_channel_id": log_channel_id,
            "burst_count": burst_count,
            "burst_window_seconds": burst_window_seconds,
            "duplicate_count": duplicate_count,
            "duplicate_window_seconds": duplicate_window_seconds,
            "mass_mentions": mass_mentions,
            "link_count": link_count,
            "timeout_minutes": timeout_minutes,
            "raid_enabled": int(raid_enabled) if raid_enabled is not None else None,
            "raid_join_count": raid_join_count,
            "raid_window_seconds": raid_window_seconds,
            "min_account_age_hours": min_account_age_hours,
            "quarantine_role_id": quarantine_role_id,
        }
        numeric = {key: value for key, value in values.items() if key != "action_mode" and value is not None}
        if any(int(value) < 0 for value in numeric.values()):
            raise ValueError("Числовые настройки не могут быть отрицательными.")
        assignments = [f"{key}=?" for key, value in values.items() if value is not None]
        parameters = [value for value in values.values() if value is not None]
        assignments.extend(("updated_by=?", "updated_at=?"))
        parameters.extend((actor_id, _utc().isoformat(), guild_id))
        await db.execute(
            f"UPDATE automod_settings SET {', '.join(assignments)} WHERE guild_id=?", parameters
        )
        await db.commit()
        return await self.get_settings(db, guild_id)

    async def set_rule(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        rule: str,
        enabled: bool,
        actor_id: int,
        threshold: int | None = None,
    ) -> None:
        if rule not in RULE_COLUMNS:
            raise ValueError("Неизвестное правило автомодерации.")
        if threshold is not None and threshold < 1:
            raise ValueError("Порог должен быть не меньше 1.")
        await self.get_settings(db, guild_id)
        enabled_column, threshold_column = RULE_COLUMNS[rule]
        assignments = [f"{enabled_column}=?", "updated_by=?", "updated_at=?"]
        parameters: list[Any] = [int(enabled), actor_id, _utc().isoformat()]
        if threshold is not None:
            assignments.append(f"{threshold_column}=?")
            parameters.append(threshold)
        parameters.append(guild_id)
        await db.execute(
            f"UPDATE automod_settings SET {', '.join(assignments)} WHERE guild_id=?", parameters
        )
        await db.commit()

    async def set_exemption(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        kind: str,
        target_id: int,
        actor_id: int,
    ) -> None:
        if kind not in {"channel", "role"}:
            raise ValueError("Исключением может быть только канал или роль.")
        await db.execute(
            """INSERT INTO automod_exemptions(guild_id, kind, target_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, kind, target_id) DO UPDATE SET
                   created_by=excluded.created_by, created_at=excluded.created_at""",
            (guild_id, kind, target_id, actor_id, _utc().isoformat()),
        )
        await db.commit()

    async def remove_exemption(
        self, db: aiosqlite.Connection, *, guild_id: int, kind: str, target_id: int
    ) -> None:
        await db.execute(
            "DELETE FROM automod_exemptions WHERE guild_id=? AND kind=? AND target_id=?",
            (guild_id, kind, target_id),
        )
        await db.commit()

    async def is_exempt(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        channel_id: int,
        role_ids: Sequence[int],
    ) -> bool:
        cursor = await db.execute(
            """SELECT kind, target_id FROM automod_exemptions
               WHERE guild_id=? AND (kind='channel' OR kind='role')""",
            (guild_id,),
        )
        roles = set(role_ids)
        return any(
            (row["kind"] == "channel" and int(row["target_id"]) == channel_id)
            or (row["kind"] == "role" and int(row["target_id"]) in roles)
            for row in await cursor.fetchall()
        )

    @staticmethod
    def _hash_content(content: str, salt: str) -> str:
        return hmac.new(
            bytes.fromhex(salt), _normalize_for_hash(content).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    async def evaluate_message(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        content: str,
        mention_count: int = 0,
        now: datetime | None = None,
    ) -> AutomodDecision | None:
        settings = await self.get_settings(db, guild_id)
        if not int(settings["enabled"]):
            return None
        current = _utc(now)
        stamp = current.isoformat()
        content_hash = self._hash_content(content, str(settings["content_hash_salt"]))
        link_count = len(LINK_RE.findall(content or ""))
        max_window = max(
            int(settings["burst_window_seconds"]), int(settings["duplicate_window_seconds"]), 120
        )
        cutoff = (current - timedelta(seconds=max_window)).isoformat()

        async with self._write_lock:
            await db.execute(
                """DELETE FROM automod_message_events
                   WHERE guild_id=? AND created_at<?""",
                (guild_id, cutoff),
            )
            await db.execute(
                """INSERT INTO automod_message_events(
                       guild_id, user_id, channel_id, content_hash, mention_count, link_count, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, channel_id, content_hash, mention_count, link_count, stamp),
            )
            burst_cutoff = (
                current - timedelta(seconds=int(settings["burst_window_seconds"]))
            ).isoformat()
            duplicate_cutoff = (
                current - timedelta(seconds=int(settings["duplicate_window_seconds"]))
            ).isoformat()
            cursor = await db.execute(
                """SELECT COUNT(*) AS count FROM automod_message_events
                   WHERE guild_id=? AND user_id=? AND created_at>=?""",
                (guild_id, user_id, burst_cutoff),
            )
            burst_total = int((await cursor.fetchone())["count"])
            cursor = await db.execute(
                """SELECT COUNT(*) AS count FROM automod_message_events
                   WHERE guild_id=? AND user_id=? AND content_hash=? AND created_at>=?""",
                (guild_id, user_id, content_hash, duplicate_cutoff),
            )
            duplicate_total = int((await cursor.fetchone())["count"])
            await db.commit()

        rule: str | None = None
        if int(settings["mass_mentions_enabled"]) and mention_count >= int(settings["mass_mentions"]):
            rule = "mass_mentions"
        elif int(settings["link_flood_enabled"]) and link_count >= int(settings["link_count"]):
            rule = "link_flood"
        elif (
            int(settings["duplicate_enabled"])
            and len(_normalize_for_hash(content)) >= 4
            and duplicate_total >= int(settings["duplicate_count"])
        ):
            rule = "duplicate"
        elif int(settings["burst_enabled"]) and burst_total >= int(settings["burst_count"]):
            rule = "burst"
        if rule is None:
            return None
        return AutomodDecision(
            rules=(rule,),
            action_mode=str(settings["action_mode"]),
            content_hash=content_hash,
            metadata={
                "mentions": mention_count,
                "links": link_count,
                "burst_count": burst_total,
                "duplicate_count": duplicate_total,
            },
        )

    async def record_incident(
        self,
        db: aiosqlite.Connection,
        *,
        decision: AutomodDecision,
        guild_id: int,
        user_id: int,
        channel_id: int,
        message_id: int | None,
        action: str,
    ) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError("Недопустимое автоматическое действие.")
        stamp = _utc().isoformat()
        metadata = json.dumps(decision.metadata, ensure_ascii=True, sort_keys=True)
        async with self._write_lock:
            for rule in decision.rules:
                inserted = await db.execute(
                    """INSERT OR IGNORE INTO automod_incidents(
                           guild_id, user_id, channel_id, message_id, rule, action,
                           content_hash, metadata_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        user_id,
                        channel_id,
                        message_id,
                        rule,
                        action,
                        decision.content_hash,
                        metadata,
                        stamp,
                    ),
                )
                if inserted.rowcount == 1:
                    await db.execute(
                        """INSERT INTO automod_strikes(guild_id, user_id, rule, points, created_at)
                           VALUES (?, ?, ?, 1, ?)""",
                        (guild_id, user_id, rule, stamp),
                    )
            await db.commit()

    async def record_member_join(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        user_id: int,
        account_created_at: datetime,
        now: datetime | None = None,
    ) -> RaidDecision:
        settings = await self.get_settings(db, guild_id)
        current = _utc(now)
        created = _utc(account_created_at)
        age_hours = max(0, int((current - created).total_seconds() // 3600))
        if not int(settings["enabled"]) or not int(settings["raid_enabled"]):
            return RaidDecision(False, False, False, int(settings["quarantine_role_id"]), 0)
        window = int(settings["raid_window_seconds"])
        cutoff = (current - timedelta(seconds=window)).isoformat()
        async with self._write_lock:
            await db.execute(
                "DELETE FROM automod_raid_state WHERE guild_id=? AND joined_at<?",
                (guild_id, cutoff),
            )
            await db.execute(
                """INSERT INTO automod_raid_state(
                       guild_id, user_id, joined_at, account_age_hours, quarantined
                   ) VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                       joined_at=excluded.joined_at,
                       account_age_hours=excluded.account_age_hours,
                       quarantined=0""",
                (guild_id, user_id, current.isoformat(), age_hours),
            )
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM automod_raid_state WHERE guild_id=? AND joined_at>=?",
                (guild_id, cutoff),
            )
            recent = int((await cursor.fetchone())["count"])
            await db.commit()
        raid = recent >= int(settings["raid_join_count"])
        suspicious = age_hours < int(settings["min_account_age_hours"])
        role_id = int(settings["quarantine_role_id"])
        return RaidDecision(raid, suspicious, raid and suspicious and role_id > 0, role_id, recent)

    async def mark_quarantined(
        self, db: aiosqlite.Connection, *, guild_id: int, user_id: int
    ) -> None:
        await db.execute(
            "UPDATE automod_raid_state SET quarantined=1 WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        await db.commit()

    async def raid_status(self, db: aiosqlite.Connection, guild_id: int) -> dict[str, int]:
        settings = await self.get_settings(db, guild_id)
        cutoff = (
            _utc() - timedelta(seconds=int(settings["raid_window_seconds"]))
        ).isoformat()
        cursor = await db.execute(
            """SELECT COUNT(*) AS joins, COALESCE(SUM(quarantined), 0) AS quarantined
               FROM automod_raid_state WHERE guild_id=? AND joined_at>=?""",
            (guild_id, cutoff),
        )
        row = await cursor.fetchone()
        return {
            "enabled": int(settings["raid_enabled"]),
            "joins": int(row["joins"]),
            "quarantined": int(row["quarantined"]),
            "threshold": int(settings["raid_join_count"]),
            "window_seconds": int(settings["raid_window_seconds"]),
        }

    async def disable_raid(
        self, db: aiosqlite.Connection, *, guild_id: int, actor_id: int
    ) -> None:
        await self.configure(db, guild_id=guild_id, actor_id=actor_id, raid_enabled=False)

from __future__ import annotations

import asyncio
import io
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite
from PIL import Image, ImageDraw, ImageOps

from utils.brand import BRAND_ACCENT, BRAND_NAME, theme_path
from utils.leaderboard_image import load_font_stack


class CommunityOpsError(RuntimeError):
    """Base error for user-facing community operations."""


class SuggestionCooldown(CommunityOpsError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"Повторить можно через {self.retry_after} сек.")


class ActiveSuggestionLimit(CommunityOpsError):
    pass


class SuggestionNotFound(CommunityOpsError):
    pass


@dataclass(slots=True, frozen=True)
class SuggestionRecord:
    suggestion_id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    author_id: int
    body: str
    status: str
    decision_reason: str | None


@dataclass(slots=True, frozen=True)
class VoteTally:
    upvotes: int
    downvotes: int


@dataclass(slots=True, frozen=True)
class WeeklyDigestStats:
    active_members: int = 0
    total_messages: int = 0
    weekly_messages: int = 0
    top_user_id: int | None = None
    top_user_messages: int = 0
    suggestions_created: int = 0
    suggestions_approved: int = 0
    starboard_posts: int = 0
    giveaways_finished: int = 0


_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


def normalize_user_text(value: str, *, max_length: int = 1000) -> str:
    """Normalize display text and remove invisible direction/control characters."""
    normalized = unicodedata.normalize("NFKC", value or "")
    cleaned: list[str] = []
    for char in normalized:
        if char in _BIDI_CONTROLS:
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"} and char not in {"\n", "\t"}:
            continue
        cleaned.append(char)

    lines = [" ".join(line.split()) for line in "".join(cleaned).splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        raise ValueError("Текст предложения не может быть пустым.")
    return text[:max_length].rstrip()


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _suggestion_from_row(row: aiosqlite.Row) -> SuggestionRecord:
    return SuggestionRecord(
        suggestion_id=int(row["suggestion_id"]),
        guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        author_id=int(row["author_id"]),
        body=str(row["body"]),
        status=str(row["status"]),
        decision_reason=str(row["decision_reason"]) if row["decision_reason"] else None,
    )


class CommunityOpsService:
    """Restart-safe SQLite state for suggestions and weekly server digests."""

    MIGRATION_VERSION = "community_ops_v1"

    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS community_suggestion_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS community_suggestions (
                suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                author_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'approved', 'rejected', 'cancelled')),
                decision_reason TEXT,
                decided_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_community_suggestions_active
                ON community_suggestions(guild_id, status, created_at DESC);
            CREATE TABLE IF NOT EXISTS community_suggestion_votes (
                suggestion_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(suggestion_id, user_id),
                FOREIGN KEY(suggestion_id) REFERENCES community_suggestions(suggestion_id)
            );
            CREATE TABLE IF NOT EXISTS community_suggestion_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id INTEGER,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS community_projection_outbox (
                event_key TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS community_digest_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                weekday INTEGER NOT NULL DEFAULT 0 CHECK(weekday BETWEEN 0 AND 6),
                hour_utc INTEGER NOT NULL DEFAULT 9 CHECK(hour_utc BETWEEN 0 AND 23),
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS community_digest_deliveries (
                guild_id INTEGER NOT NULL,
                iso_year INTEGER NOT NULL,
                iso_week INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed' CHECK(status IN ('claimed', 'sent')),
                claimed_at TEXT NOT NULL,
                sent_at TEXT,
                message_id INTEGER,
                PRIMARY KEY(guild_id, iso_year, iso_week)
            );
            """
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (self.MIGRATION_VERSION, _utc().isoformat()),
        )
        await db.commit()

    async def set_suggestion_channel(
        self, db: aiosqlite.Connection, *, guild_id: int, channel_id: int, actor_id: int
    ) -> None:
        now = _utc().isoformat()
        await db.execute(
            """INSERT INTO community_suggestion_settings(guild_id, channel_id, enabled, updated_by, updated_at)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
                   enabled=1, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (guild_id, channel_id, actor_id, now),
        )
        await db.execute(
            "INSERT INTO community_suggestion_audit(guild_id, actor_id, action, detail, created_at) VALUES (?, ?, 'setup', ?, ?)",
            (guild_id, actor_id, str(channel_id), now),
        )
        await db.commit()

    async def disable_suggestions(self, db: aiosqlite.Connection, *, guild_id: int, actor_id: int) -> None:
        now = _utc().isoformat()
        await db.execute(
            "UPDATE community_suggestion_settings SET enabled=0, updated_by=?, updated_at=? WHERE guild_id=?",
            (actor_id, now, guild_id),
        )
        await db.execute(
            "INSERT INTO community_suggestion_audit(guild_id, actor_id, action, created_at) VALUES (?, ?, 'disabled', ?)",
            (guild_id, actor_id, now),
        )
        await db.commit()

    async def suggestion_settings(self, db: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM community_suggestion_settings WHERE guild_id=? AND enabled=1", (guild_id,)
        )
        return await cursor.fetchone()

    async def create_suggestion(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        channel_id: int,
        author_id: int,
        body: str,
        now: datetime | None = None,
        cooldown_seconds: int = 300,
        active_limit: int = 3,
    ) -> SuggestionRecord:
        created = _utc(now)
        safe_body = normalize_user_text(body)
        async with self._write_lock:
            cursor = await db.execute(
                """SELECT COUNT(*) AS count FROM community_suggestions
                   WHERE guild_id=? AND author_id=? AND status='active'""",
                (guild_id, author_id),
            )
            active = int((await cursor.fetchone())["count"])
            if active >= active_limit:
                raise ActiveSuggestionLimit(
                    f"У вас уже {active_limit} активных предложений. Дождитесь решения по одному из них."
                )

            cursor = await db.execute(
                """SELECT created_at FROM community_suggestions
                   WHERE guild_id=? AND author_id=? ORDER BY suggestion_id DESC LIMIT 1""",
                (guild_id, author_id),
            )
            latest = await cursor.fetchone()
            if latest is not None:
                try:
                    elapsed = (created - datetime.fromisoformat(str(latest["created_at"]))).total_seconds()
                except ValueError:
                    elapsed = cooldown_seconds
                if elapsed < cooldown_seconds:
                    raise SuggestionCooldown(int(cooldown_seconds - elapsed + 0.999))

            stamp = created.isoformat()
            cursor = await db.execute(
                """INSERT INTO community_suggestions(
                       guild_id, channel_id, author_id, body, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, author_id, safe_body, stamp, stamp),
            )
            suggestion_id = int(cursor.lastrowid or 0)
            await db.execute(
                """INSERT INTO community_suggestion_audit(
                       suggestion_id, guild_id, actor_id, action, created_at
                   ) VALUES (?, ?, ?, 'created', ?)""",
                (suggestion_id, guild_id, author_id, stamp),
            )
            await db.commit()
        record = await self.get_suggestion(db, suggestion_id)
        if record is None:
            raise SuggestionNotFound("Предложение не удалось сохранить.")
        return record

    async def bind_suggestion_message(
        self, db: aiosqlite.Connection, suggestion_id: int, *, message_id: int
    ) -> None:
        await db.execute(
            "UPDATE community_suggestions SET message_id=?, updated_at=? WHERE suggestion_id=?",
            (message_id, _utc().isoformat(), suggestion_id),
        )
        await db.commit()

    async def get_suggestion(
        self, db: aiosqlite.Connection, suggestion_id: int
    ) -> SuggestionRecord | None:
        cursor = await db.execute(
            "SELECT * FROM community_suggestions WHERE suggestion_id=?", (suggestion_id,)
        )
        row = await cursor.fetchone()
        return _suggestion_from_row(row) if row is not None else None

    async def list_active_suggestions(self, db: aiosqlite.Connection) -> list[SuggestionRecord]:
        cursor = await db.execute(
            "SELECT * FROM community_suggestions WHERE status='active' AND message_id IS NOT NULL"
        )
        return [_suggestion_from_row(row) for row in await cursor.fetchall()]

    async def cancel_orphaned_suggestions(
        self, db: aiosqlite.Connection, *, now: datetime | None = None
    ) -> int:
        cutoff = (_utc(now) - timedelta(minutes=10)).isoformat()
        async with self._write_lock:
            cursor = await db.execute(
                """UPDATE community_suggestions
                   SET status='cancelled', decision_reason='publication interrupted',
                       decided_at=?, updated_at=?
                   WHERE status='active' AND message_id IS NULL AND created_at<=?""",
                (_utc(now).isoformat(), _utc(now).isoformat(), cutoff),
            )
            await db.commit()
            return int(cursor.rowcount)

    async def cast_vote(
        self, db: aiosqlite.Connection, suggestion_id: int, *, user_id: int, vote: int
    ) -> VoteTally:
        if vote not in {-1, 1}:
            raise ValueError("Голос должен быть 1 или -1.")
        async with self._write_lock:
            suggestion = await self.get_suggestion(db, suggestion_id)
            if suggestion is None or suggestion.status != "active":
                raise SuggestionNotFound("Активное предложение не найдено.")
            await db.execute(
                """INSERT INTO community_suggestion_votes(suggestion_id, user_id, vote, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(suggestion_id, user_id) DO UPDATE SET
                       vote=excluded.vote, updated_at=excluded.updated_at""",
                (suggestion_id, user_id, vote, _utc().isoformat()),
            )
            await db.commit()
        return await self.vote_tally(db, suggestion_id)

    async def vote_tally(self, db: aiosqlite.Connection, suggestion_id: int) -> VoteTally:
        cursor = await db.execute(
            """SELECT COALESCE(SUM(vote=1), 0) AS upvotes,
                      COALESCE(SUM(vote=-1), 0) AS downvotes
               FROM community_suggestion_votes WHERE suggestion_id=?""",
            (suggestion_id,),
        )
        row = await cursor.fetchone()
        return VoteTally(int(row["upvotes"]), int(row["downvotes"]))

    async def decide_suggestion(
        self,
        db: aiosqlite.Connection,
        suggestion_id: int,
        *,
        status: str,
        actor_id: int,
        reason: str,
        now: datetime | None = None,
    ) -> SuggestionRecord:
        if status not in {"approved", "rejected", "cancelled"}:
            raise ValueError("Недопустимый статус решения.")
        safe_reason = normalize_user_text(reason, max_length=500)
        stamp = _utc(now).isoformat()
        async with self._write_lock:
            cursor = await db.execute(
                """UPDATE community_suggestions SET status=?, decision_reason=?, decided_by=?,
                       decided_at=?, updated_at=? WHERE suggestion_id=? AND status='active'""",
                (status, safe_reason, actor_id, stamp, stamp, suggestion_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise SuggestionNotFound("Активное предложение не найдено.")
            cursor = await db.execute(
                "SELECT guild_id FROM community_suggestions WHERE suggestion_id=?", (suggestion_id,)
            )
            row = await cursor.fetchone()
            await db.execute(
                """INSERT INTO community_suggestion_audit(
                       suggestion_id, guild_id, actor_id, action, detail, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (suggestion_id, int(row["guild_id"]), actor_id, status, safe_reason, stamp),
            )
            if status == "approved":
                author = await (await db.execute(
                    "SELECT author_id FROM community_suggestions WHERE suggestion_id=?",
                    (suggestion_id,),
                )).fetchone()
                await db.execute(
                    """INSERT OR IGNORE INTO community_projection_outbox
                       (event_key,guild_id,user_id,event_type,amount,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        f"suggestion-approved:{suggestion_id}",
                        int(row["guild_id"]),
                        int(author["author_id"]),
                        "suggestion_accepted",
                        1,
                        stamp,
                    ),
                )
            await db.commit()
        result = await self.get_suggestion(db, suggestion_id)
        if result is None:
            raise SuggestionNotFound("Предложение не найдено.")
        return result

    async def pending_projections(
        self, db: aiosqlite.Connection, *, limit: int = 50
    ) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """SELECT * FROM community_projection_outbox
               WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        )
        return await cursor.fetchall()

    async def mark_projection_delivered(
        self, db: aiosqlite.Connection, event_key: str
    ) -> None:
        await db.execute(
            "UPDATE community_projection_outbox SET delivered_at=? WHERE event_key=?",
            (_utc().isoformat(), event_key),
        )
        await db.commit()

    async def set_digest(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        channel_id: int,
        weekday: int,
        hour_utc: int,
        actor_id: int,
    ) -> None:
        if weekday not in range(7) or hour_utc not in range(24):
            raise ValueError("Некорректное расписание дайджеста.")
        await db.execute(
            """INSERT INTO community_digest_settings(
                   guild_id, channel_id, enabled, weekday, hour_utc, updated_by, updated_at
               ) VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, enabled=1,
                   weekday=excluded.weekday, hour_utc=excluded.hour_utc,
                   updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (guild_id, channel_id, weekday, hour_utc, actor_id, _utc().isoformat()),
        )
        await db.commit()

    async def disable_digest(self, db: aiosqlite.Connection, guild_id: int) -> None:
        await db.execute("UPDATE community_digest_settings SET enabled=0 WHERE guild_id=?", (guild_id,))
        await db.commit()

    async def digest_settings(self, db: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute("SELECT * FROM community_digest_settings WHERE guild_id=?", (guild_id,))
        return await cursor.fetchone()

    async def pending_digests(self, db: aiosqlite.Connection, now: datetime | None = None) -> list[aiosqlite.Row]:
        current = _utc(now)
        iso_year, iso_week, _ = current.isocalendar()
        # A process can stop after claiming a delivery but before sending it.
        # Expire that lease so the next process can reconcile/retry it.
        async with self._write_lock:
            await db.execute(
                """DELETE FROM community_digest_deliveries
                   WHERE status='claimed' AND claimed_at<=?""",
                ((current - timedelta(minutes=30)).isoformat(),),
            )
            await db.commit()
        cursor = await db.execute(
            """SELECT settings.* FROM community_digest_settings AS settings
               WHERE settings.enabled=1
                 AND (settings.weekday < ? OR (settings.weekday = ? AND settings.hour_utc <= ?))
                 AND NOT EXISTS (
                     SELECT 1 FROM community_digest_deliveries AS deliveries
                     WHERE deliveries.guild_id=settings.guild_id
                       AND deliveries.iso_year=? AND deliveries.iso_week=?
                 )
               ORDER BY settings.guild_id""",
            (current.weekday(), current.weekday(), current.hour, iso_year, iso_week),
        )
        return await cursor.fetchall()

    async def claim_digest_delivery(
        self, db: aiosqlite.Connection, *, guild_id: int, now: datetime | None = None
    ) -> bool:
        current = _utc(now)
        iso_year, iso_week, _ = current.isocalendar()
        async with self._write_lock:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO community_digest_deliveries(
                       guild_id, iso_year, iso_week, status, claimed_at
                   ) VALUES (?, ?, ?, 'claimed', ?)""",
                (guild_id, iso_year, iso_week, current.isoformat()),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def complete_digest_delivery(
        self,
        db: aiosqlite.Connection,
        *,
        guild_id: int,
        message_id: int,
        now: datetime | None = None,
    ) -> None:
        current = _utc(now)
        iso_year, iso_week, _ = current.isocalendar()
        await db.execute(
            """UPDATE community_digest_deliveries SET status='sent', sent_at=?, message_id=?
               WHERE guild_id=? AND iso_year=? AND iso_week=?""",
            (current.isoformat(), message_id, guild_id, iso_year, iso_week),
        )
        await db.commit()

    async def release_digest_claim(
        self, db: aiosqlite.Connection, *, guild_id: int, now: datetime | None = None
    ) -> None:
        current = _utc(now)
        iso_year, iso_week, _ = current.isocalendar()
        await db.execute(
            """DELETE FROM community_digest_deliveries
               WHERE guild_id=? AND iso_year=? AND iso_week=? AND status='claimed'""",
            (guild_id, iso_year, iso_week),
        )
        await db.commit()

    async def _has_columns(
        self, db: aiosqlite.Connection, table: str, required: set[str]
    ) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if await cursor.fetchone() is None:
            return False
        cursor = await db.execute(f'PRAGMA table_info("{table}")')
        columns = {str(row["name"] if isinstance(row, aiosqlite.Row) else row[1]) for row in await cursor.fetchall()}
        return required.issubset(columns)

    async def collect_weekly_stats(
        self, db: aiosqlite.Connection, *, guild_id: int, now: datetime | None = None
    ) -> WeeklyDigestStats:
        current = _utc(now)
        monday = (current.date() - timedelta(days=current.weekday())).isoformat()
        start = f"{monday}T00:00:00+00:00"
        values: dict[str, int | None] = {
            "active_members": 0,
            "total_messages": 0,
            "weekly_messages": 0,
            "top_user_id": None,
            "top_user_messages": 0,
            "suggestions_created": 0,
            "suggestions_approved": 0,
            "starboard_posts": 0,
            "giveaways_finished": 0,
        }

        if await self._has_columns(db, "levels", {"guild_id", "user_id", "message_count"}):
            cursor = await db.execute(
                """SELECT COUNT(*) AS members, COALESCE(SUM(message_count), 0) AS messages
                   FROM levels WHERE guild_id=?""",
                (guild_id,),
            )
            row = await cursor.fetchone()
            values["active_members"] = int(row["members"])
            values["total_messages"] = int(row["messages"])

        if await self._has_columns(
            db, "user_weekly_style_stats", {"guild_id", "user_id", "week_start", "message_count"}
        ):
            cursor = await db.execute(
                """SELECT user_id, message_count FROM user_weekly_style_stats
                   WHERE guild_id=? AND week_start=?
                   ORDER BY message_count DESC, user_id ASC""",
                (guild_id, monday),
            )
            rows = await cursor.fetchall()
            values["weekly_messages"] = sum(int(row["message_count"]) for row in rows)
            if rows:
                values["top_user_id"] = int(rows[0]["user_id"])
                values["top_user_messages"] = int(rows[0]["message_count"])

        cursor = await db.execute(
            """SELECT
                   COALESCE(SUM(datetime(created_at) >= datetime(?)), 0) AS created_count,
                   COALESCE(SUM(status='approved' AND datetime(updated_at) >= datetime(?)), 0) AS approved_count
               FROM community_suggestions WHERE guild_id=?""",
            (start, start, guild_id),
        )
        row = await cursor.fetchone()
        values["suggestions_created"] = int(row["created_count"])
        values["suggestions_approved"] = int(row["approved_count"])

        if await self._has_columns(db, "starboard_posts", {"guild_id", "created_at"}):
            cursor = await db.execute(
                """SELECT COUNT(*) AS count FROM starboard_posts
                   WHERE guild_id=? AND datetime(created_at)>=datetime(?)""",
                (guild_id, start),
            )
            values["starboard_posts"] = int((await cursor.fetchone())["count"])

        if await self._has_columns(db, "giveaways", {"guild_id", "status", "ends_at"}):
            cursor = await db.execute(
                """SELECT COUNT(*) AS count FROM giveaways
                   WHERE guild_id=? AND status='ended' AND datetime(ends_at)>=datetime(?)""",
                (guild_id, start),
            )
            values["giveaways_finished"] = int((await cursor.fetchone())["count"])

        return WeeklyDigestStats(**values)


def render_weekly_digest(guild_name: str, stats: WeeklyDigestStats) -> io.BytesIO:
    """Render a local summary graphic containing counters only, never message samples."""
    size = (1200, 675)
    background = theme_path("events")
    if background is None:
        image = Image.new("RGBA", size, (9, 10, 14, 255))
    else:
        with Image.open(background) as source:
            image = ImageOps.fit(
                source.convert("RGBA"), size, method=Image.Resampling.LANCZOS, centering=(1.0, 0.5)
            )

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    for x in range(size[0]):
        position = x / max(size[0] - 1, 1)
        alpha = 228 if position <= 0.58 else int(228 - 190 * ((position - 0.58) / 0.42))
        overlay_draw.line((x, 0, x, size[1]), fill=(5, 6, 9, max(30, alpha)))
    image.alpha_composite(overlay)
    draw = ImageDraw.Draw(image, "RGBA")
    eyebrow_font = load_font_stack(17, bold=True)
    title_font = load_font_stack(47, bold=True)
    guild_font = load_font_stack(29, bold=True, kind="name")
    label_font = load_font_stack(16, bold=True)
    value_font = load_font_stack(34, bold=True)

    eyebrow_font.draw(draw, (72, 54), BRAND_NAME.upper(), fill=(207, 195, 202, 240))
    title_font.draw(draw, (72, 88), "Итоги недели", fill=(251, 248, 250, 255))
    safe_name = normalize_user_text(guild_name, max_length=44).replace("\n", " ")
    guild_font.draw(draw, (72, 163), safe_name, fill=(231, 223, 228, 255))
    draw.line((72, 216, 788, 216), fill=(255, 255, 255, 38), width=1)

    metrics = (
        ("СООБЩЕНИЯ ЗА НЕДЕЛЮ", f"{stats.weekly_messages:,}".replace(",", " ")),
        ("УЧАСТНИКИ", str(stats.active_members)),
        ("ПРИНЯТЫЕ ИДЕИ", str(stats.suggestions_approved)),
        ("ПОСТЫ В STARBOARD", str(stats.starboard_posts)),
    )
    for index, (label, value) in enumerate(metrics):
        column = index % 2
        row = index // 2
        x = 72 + column * 354
        y = 252 + row * 146
        draw.rounded_rectangle((x, y, x + 326, y + 118), radius=12, fill=(19, 17, 22, 166))
        draw.rounded_rectangle(
            (x, y + 18, x + 4, y + 100), radius=2, fill=(*BRAND_ACCENT, 220)
        )
        label_font.draw(draw, (x + 26, y + 22), label, fill=(168, 157, 166, 255))
        value_font.draw(draw, (x + 26, y + 53), value, fill=(249, 246, 248, 255))

    label_font.draw(
        draw,
        (72, 578),
        "ТОЛЬКО АГРЕГИРОВАННАЯ СТАТИСТИКА",
        fill=(159, 148, 156, 215),
    )
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True, compress_level=7)
    output.seek(0)
    return output

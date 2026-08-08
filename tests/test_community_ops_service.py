from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite
from PIL import Image

from services.community_ops_service import (
    ActiveSuggestionLimit,
    CommunityOpsService,
    SuggestionCooldown,
    normalize_user_text,
    render_weekly_digest,
)


async def _database() -> tuple[aiosqlite.Connection, CommunityOpsService]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    service = CommunityOpsService()
    await service.init_db(db)
    return db, service


def test_normalize_user_text_removes_controls_and_bidi() -> None:
    assert normalize_user_text("  Идея\u202e\x00\n\n  для   бота  ") == "Идея\nдля бота"


def test_suggestions_enforce_cooldown_active_limit_and_update_votes() -> None:
    async def scenario() -> None:
        db, service = await _database()
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        await service.set_suggestion_channel(db, guild_id=1, channel_id=50, actor_id=7)

        suggestion = await service.create_suggestion(
            db, guild_id=1, channel_id=50, author_id=10, body="  Добавить\u202e турнир  ", now=now
        )
        assert suggestion.body == "Добавить турнир"
        try:
            await service.create_suggestion(
                db, guild_id=1, channel_id=50, author_id=10, body="Слишком быстро", now=now + timedelta(seconds=2)
            )
        except SuggestionCooldown as exc:
            assert exc.retry_after > 0
        else:
            raise AssertionError("cooldown must reject a repeated suggestion")

        await service.bind_suggestion_message(db, suggestion.suggestion_id, message_id=900)
        tally = await service.cast_vote(db, suggestion.suggestion_id, user_id=20, vote=1)
        assert (tally.upvotes, tally.downvotes) == (1, 0)
        tally = await service.cast_vote(db, suggestion.suggestion_id, user_id=20, vote=-1)
        assert (tally.upvotes, tally.downvotes) == (0, 1)

        second = await service.create_suggestion(
            db,
            guild_id=1,
            channel_id=50,
            author_id=11,
            body="Вторая идея",
            now=now,
            cooldown_seconds=0,
        )
        assert second.suggestion_id != suggestion.suggestion_id
        third = await service.create_suggestion(
            db,
            guild_id=1,
            channel_id=50,
            author_id=11,
            body="Третья идея того же автора",
            now=now,
            cooldown_seconds=0,
        )
        try:
            await service.create_suggestion(
                db,
                guild_id=1,
                channel_id=50,
                author_id=11,
                body="Лишняя идея того же автора",
                now=now,
                cooldown_seconds=0,
                active_limit=2,
            )
        except ActiveSuggestionLimit:
            pass
        else:
            raise AssertionError("active suggestion limit must prevent one author from flooding")

        another_author = await service.create_suggestion(
            db, guild_id=1, channel_id=50, author_id=12, body="Идея другого автора", now=now
        )
        assert another_author.suggestion_id > third.suggestion_id

        decided = await service.decide_suggestion(
            db, suggestion.suggestion_id, status="approved", actor_id=7, reason="Берём в работу", now=now
        )
        assert decided.status == "approved"
        cursor = await db.execute(
            "SELECT action FROM community_suggestion_audit WHERE suggestion_id=? ORDER BY audit_id",
            (suggestion.suggestion_id,),
        )
        assert [row["action"] for row in await cursor.fetchall()] == ["created", "approved"]
        await db.close()

    asyncio.run(scenario())


def test_weekly_digest_is_due_once_and_handles_missing_source_tables() -> None:
    async def scenario() -> None:
        db, service = await _database()
        now = datetime(2026, 8, 8, 12, 30, tzinfo=UTC)  # Saturday
        await service.set_digest(db, guild_id=1, channel_id=70, weekday=5, hour_utc=12, actor_id=7)
        due = await service.pending_digests(db, now)
        assert [int(row["guild_id"]) for row in due] == [1]

        stats = await service.collect_weekly_stats(db, guild_id=1, now=now)
        assert stats.total_messages == 0
        assert stats.weekly_messages == 0

        assert await service.claim_digest_delivery(db, guild_id=1, now=now) is True
        assert await service.claim_digest_delivery(db, guild_id=1, now=now) is False
        assert await service.pending_digests(db, now) == []
        await db.close()

    asyncio.run(scenario())


def test_digest_aggregates_only_counters_and_renders_local_png() -> None:
    async def scenario() -> None:
        db, service = await _database()
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        await db.executescript(
            """
            CREATE TABLE levels (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL, level INTEGER NOT NULL,
                last_message_at TEXT
            );
            CREATE TABLE user_weekly_style_stats (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL, message_count INTEGER NOT NULL,
                avg_length REAL NOT NULL, emoji_count INTEGER NOT NULL,
                question_count INTEGER NOT NULL, words_json TEXT NOT NULL,
                sample TEXT NOT NULL
            );
            """
        )
        await db.execute("INSERT INTO levels VALUES (1, 10, 120, 4, ?)", (now.isoformat(),))
        await db.execute("INSERT INTO levels VALUES (1, 11, 80, 3, ?)", (now.isoformat(),))
        monday = (now.date() - timedelta(days=now.weekday())).isoformat()
        await db.execute(
            "INSERT INTO user_weekly_style_stats VALUES (1, 10, ?, 25, 10, 2, 1, '{}', 'SECRET RAW TEXT')",
            (monday,),
        )
        await db.commit()

        stats = await service.collect_weekly_stats(db, guild_id=1, now=now)
        assert stats.active_members == 2
        assert stats.total_messages == 200
        assert stats.weekly_messages == 25
        assert stats.top_user_id == 10
        assert "SECRET" not in repr(stats)

        image = render_weekly_digest("Тестовый сервер", stats)
        with Image.open(image) as png:
            assert png.format == "PNG"
            assert png.size == (1200, 675)
        await db.close()

    asyncio.run(scenario())


def test_active_views_and_weekly_claim_survive_database_reopen() -> None:
    async def scenario(path: Path) -> None:
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        first = await aiosqlite.connect(path)
        first.row_factory = aiosqlite.Row
        service = CommunityOpsService()
        await service.init_db(first)
        suggestion = await service.create_suggestion(
            first, guild_id=1, channel_id=50, author_id=10, body="Пережить рестарт", now=now
        )
        await service.bind_suggestion_message(first, suggestion.suggestion_id, message_id=900)
        await service.set_digest(first, guild_id=1, channel_id=70, weekday=5, hour_utc=12, actor_id=7)
        assert await service.claim_digest_delivery(first, guild_id=1, now=now)
        await first.close()

        reopened = await aiosqlite.connect(path)
        reopened.row_factory = aiosqlite.Row
        restored_service = CommunityOpsService()
        await restored_service.init_db(reopened)
        active = await restored_service.list_active_suggestions(reopened)
        assert [(row.suggestion_id, row.message_id) for row in active] == [(suggestion.suggestion_id, 900)]
        assert await restored_service.claim_digest_delivery(reopened, guild_id=1, now=now) is False
        assert await restored_service.pending_digests(
            reopened, now + timedelta(minutes=31)
        )
        await reopened.close()

    with TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory) / "community.sqlite3"))

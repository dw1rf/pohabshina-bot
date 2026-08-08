from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite

from services.automod_service import AutomodService


async def _database() -> tuple[aiosqlite.Connection, AutomodService]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    service = AutomodService()
    await service.init_db(db)
    return db, service


def test_default_is_off_and_log_only() -> None:
    async def scenario() -> None:
        db, service = await _database()
        settings = await service.get_settings(db, 1)
        assert int(settings["enabled"]) == 0
        assert settings["action_mode"] == "log"
        assert int(settings["raid_enabled"]) == 0
        decision = await service.evaluate_message(
            db,
            guild_id=1,
            user_id=10,
            channel_id=20,
            content="https://a.example https://b.example https://c.example",
            mention_count=20,
        )
        assert decision is None
        await db.close()

    asyncio.run(scenario())


def test_deterministic_message_rules_and_no_raw_text_storage() -> None:
    async def scenario() -> None:
        db, service = await _database()
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        await service.configure(
            db,
            guild_id=1,
            actor_id=99,
            enabled=True,
            action_mode="log",
            burst_count=3,
            burst_window_seconds=10,
            duplicate_count=3,
            duplicate_window_seconds=60,
            mass_mentions=4,
            link_count=3,
        )

        assert await service.evaluate_message(
            db, guild_id=1, user_id=10, channel_id=20, content="one", now=now
        ) is None
        assert await service.evaluate_message(
            db, guild_id=1, user_id=10, channel_id=20, content="two", now=now + timedelta(seconds=1)
        ) is None
        burst = await service.evaluate_message(
            db, guild_id=1, user_id=10, channel_id=20, content="three", now=now + timedelta(seconds=2)
        )
        assert burst is not None and burst.rules == ("burst",)

        for offset in range(2):
            assert await service.evaluate_message(
                db,
                guild_id=1,
                user_id=11,
                channel_id=20,
                content="SECRET duplicate payload",
                now=now + timedelta(seconds=offset),
            ) is None
        duplicate = await service.evaluate_message(
            db,
            guild_id=1,
            user_id=11,
            channel_id=20,
            content="  secret   DUPLICATE payload  ",
            now=now + timedelta(seconds=3),
        )
        assert duplicate is not None and duplicate.rules == ("duplicate",)

        mentions = await service.evaluate_message(
            db,
            guild_id=1,
            user_id=12,
            channel_id=20,
            content="hello everyone",
            mention_count=4,
            now=now,
        )
        assert mentions is not None and mentions.rules == ("mass_mentions",)
        links = await service.evaluate_message(
            db,
            guild_id=1,
            user_id=13,
            channel_id=20,
            content="https://one.example discord.gg/test http://two.example/path",
            now=now,
        )
        assert links is not None and links.rules == ("link_flood",)

        await service.record_incident(
            db,
            decision=duplicate,
            guild_id=1,
            user_id=11,
            channel_id=20,
            message_id=500,
            action="logged",
        )
        await service.record_incident(
            db,
            decision=duplicate,
            guild_id=1,
            user_id=11,
            channel_id=20,
            message_id=500,
            action="logged",
        )
        cursor = await db.execute(
            "SELECT content_hash, metadata_json FROM automod_incidents WHERE message_id=500"
        )
        stored = await cursor.fetchone()
        assert "SECRET" not in stored["content_hash"]
        assert "SECRET" not in stored["metadata_json"]
        assert int((await (await db.execute(
            "SELECT COUNT(*) AS total FROM automod_incidents WHERE message_id=500"
        )).fetchone())["total"]) == 1
        assert int((await (await db.execute(
            "SELECT COUNT(*) AS total FROM automod_strikes WHERE guild_id=1 AND user_id=11"
        )).fetchone())["total"]) == 1
        cursor = await db.execute(
            "SELECT content_hash FROM automod_message_events WHERE guild_id=1 AND user_id=11"
        )
        assert all("SECRET" not in row["content_hash"] for row in await cursor.fetchall())
        await db.close()

    asyncio.run(scenario())


def test_exemptions_are_server_local_and_persistent() -> None:
    async def scenario() -> None:
        db, service = await _database()
        await service.set_exemption(db, guild_id=1, kind="channel", target_id=20, actor_id=99)
        await service.set_exemption(db, guild_id=1, kind="role", target_id=30, actor_id=99)
        assert await service.is_exempt(db, guild_id=1, channel_id=20, role_ids=[]) is True
        assert await service.is_exempt(db, guild_id=1, channel_id=21, role_ids=[30]) is True
        assert await service.is_exempt(db, guild_id=2, channel_id=20, role_ids=[30]) is False
        await service.remove_exemption(db, guild_id=1, kind="channel", target_id=20)
        assert await service.is_exempt(db, guild_id=1, channel_id=20, role_ids=[]) is False
        await db.close()

    asyncio.run(scenario())


def test_duplicate_window_expires_without_a_false_positive() -> None:
    async def scenario() -> None:
        db, service = await _database()
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        await service.configure(
            db,
            guild_id=1,
            actor_id=99,
            enabled=True,
            duplicate_count=2,
            duplicate_window_seconds=30,
        )
        await service.set_rule(
            db, guild_id=1, rule="burst", enabled=False, actor_id=99
        )
        assert await service.evaluate_message(
            db, guild_id=1, user_id=10, channel_id=20, content="regular repeated phrase", now=now
        ) is None
        assert await service.evaluate_message(
            db,
            guild_id=1,
            user_id=10,
            channel_id=20,
            content="regular repeated phrase",
            now=now + timedelta(seconds=31),
        ) is None
        await db.close()

    asyncio.run(scenario())


def test_incidents_create_strikes_but_never_accept_ban_action() -> None:
    async def scenario() -> None:
        db, service = await _database()
        await service.configure(db, guild_id=1, actor_id=99, enabled=True, mass_mentions=2)
        decision = await service.evaluate_message(
            db, guild_id=1, user_id=10, channel_id=20, content="hello", mention_count=2
        )
        assert decision is not None
        await service.record_incident(
            db,
            decision=decision,
            guild_id=1,
            user_id=10,
            channel_id=20,
            message_id=500,
            action="logged",
        )
        cursor = await db.execute("SELECT COUNT(*) AS count FROM automod_strikes WHERE user_id=10")
        assert int((await cursor.fetchone())["count"]) == 1
        try:
            await service.record_incident(
                db,
                decision=decision,
                guild_id=1,
                user_id=10,
                channel_id=20,
                message_id=501,
                action="banned",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("automod must never expose an automatic ban action")
        await db.close()

    asyncio.run(scenario())


def test_raid_window_survives_restart_and_only_quarantines_young_accounts() -> None:
    async def scenario(path: Path) -> None:
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        first = await aiosqlite.connect(path)
        first.row_factory = aiosqlite.Row
        service = AutomodService()
        await service.init_db(first)
        await service.configure(
            first,
            guild_id=1,
            actor_id=99,
            enabled=True,
            raid_enabled=True,
            raid_join_count=3,
            raid_window_seconds=30,
            min_account_age_hours=24,
            quarantine_role_id=777,
        )
        for user_id in (10, 11):
            result = await service.record_member_join(
                first,
                guild_id=1,
                user_id=user_id,
                account_created_at=now - timedelta(days=30),
                now=now,
            )
            assert result.raid_detected is False
        await first.close()

        reopened = await aiosqlite.connect(path)
        reopened.row_factory = aiosqlite.Row
        restored = AutomodService()
        await restored.init_db(reopened)
        young = await restored.record_member_join(
            reopened,
            guild_id=1,
            user_id=12,
            account_created_at=now - timedelta(hours=1),
            now=now + timedelta(seconds=5),
        )
        assert young.raid_detected is True
        assert young.suspicious_account is True
        assert young.should_quarantine is True
        assert young.quarantine_role_id == 777

        old = await restored.record_member_join(
            reopened,
            guild_id=1,
            user_id=13,
            account_created_at=now - timedelta(days=60),
            now=now + timedelta(seconds=6),
        )
        assert old.raid_detected is True
        assert old.suspicious_account is False
        assert old.should_quarantine is False
        await reopened.close()

    with TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory) / "automod.sqlite3"))


def test_cog_admin_commands_have_runtime_permission_checks_and_no_ban_command() -> None:
    from cogs.automod import AutomodCog

    assert AutomodCog.setup_automod.checks
    assert AutomodCog.set_rule.checks
    assert AutomodCog.set_exemption.checks
    assert AutomodCog.raid_off.checks
    assert AutomodCog.disable_automod.checks
    assert all(command.name != "ban" for command in AutomodCog.automod_group.commands)

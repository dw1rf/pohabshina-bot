from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import aiosqlite

from bot_client import MovieBot
from config import load_settings
from services.economy_service import EconomyService
from services.progression_service import ProgressionService


def test_pre_migration_backup_is_created_once_and_preserves_data() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "bot.db"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE legacy_levels(user_id INTEGER PRIMARY KEY, level INTEGER NOT NULL)")
            db.execute("INSERT INTO legacy_levels VALUES (42, 17)")
            db.commit()

        settings = load_settings()
        settings.db_path = str(db_path)
        bot = MovieBot(settings)
        bot._backup_database_before_migrations()
        backups = list((db_path.parent / "backups").glob("bot-pre-economy-*.db"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute("SELECT level FROM legacy_levels WHERE user_id=42").fetchone()[0] == 17

        async def migrate() -> None:
            db = await aiosqlite.connect(db_path)
            db.row_factory = aiosqlite.Row
            await EconomyService().init_db(db)
            await db.close()

        asyncio.run(migrate())
        bot._backup_database_before_migrations()
        assert len(list((db_path.parent / "backups").glob("bot-pre-economy-*.db"))) == 1


def test_gameplay_migration_gets_its_own_recoverable_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "bot.db"

        async def prepare() -> None:
            db = await aiosqlite.connect(db_path)
            db.row_factory = aiosqlite.Row
            await EconomyService().init_db(db)
            await db.execute("CREATE TABLE legacy_pets(owner_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            await db.execute("INSERT INTO legacy_pets VALUES (7, 'Мурка')")
            await db.commit()
            await db.close()

        asyncio.run(prepare())
        settings = load_settings()
        settings.db_path = str(db_path)
        bot = MovieBot(settings)
        bot._backup_database_before_migrations()
        backups = list((db_path.parent / "backups").glob("bot-pre-gameplay-*.db"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute("SELECT name FROM legacy_pets WHERE owner_id=7").fetchone()[0] == "Мурка"

        async def migrate() -> None:
            db = await aiosqlite.connect(db_path)
            db.row_factory = aiosqlite.Row
            await ProgressionService().init_db(db)
            await db.close()

        asyncio.run(migrate())
        bot._backup_database_before_migrations()
        assert len(list((db_path.parent / "backups").glob("bot-pre-gameplay-*.db"))) == 1

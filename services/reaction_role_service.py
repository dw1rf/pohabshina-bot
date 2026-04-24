from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite


@dataclass(slots=True)
class ReactionRoleMessage:
    guild_id: int
    channel_id: int
    message_id: int
    title: str
    description: str


@dataclass(slots=True)
class ReactionRoleBinding:
    guild_id: int
    message_id: int
    emoji_key: str
    emoji_display: str
    role_id: int


class ReactionRoleService:
    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reaction_role_messages (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS reaction_role_bindings (
                guild_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                emoji_key TEXT NOT NULL,
                emoji_display TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, message_id, emoji_key)
            );
            """
        )
        await db.commit()

    async def create_message(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
        title: str,
        description: str,
        created_by: int,
    ) -> None:
        await db.execute(
            """
            INSERT INTO reaction_role_messages (guild_id, channel_id, message_id, title, description, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, message_id, title, description, created_by, datetime.now(UTC).isoformat()),
        )
        await db.commit()

    async def delete_message(self, db: aiosqlite.Connection, guild_id: int, message_id: int) -> None:
        await db.execute(
            "DELETE FROM reaction_role_bindings WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        await db.execute(
            "DELETE FROM reaction_role_messages WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        await db.commit()

    async def get_message(self, db: aiosqlite.Connection, guild_id: int, message_id: int) -> ReactionRoleMessage | None:
        cursor = await db.execute(
            """
            SELECT guild_id, channel_id, message_id, title, description
            FROM reaction_role_messages
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild_id, message_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return ReactionRoleMessage(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
        )

    async def list_messages(self, db: aiosqlite.Connection, guild_id: int) -> list[ReactionRoleMessage]:
        cursor = await db.execute(
            """
            SELECT guild_id, channel_id, message_id, title, description
            FROM reaction_role_messages
            WHERE guild_id = ?
            ORDER BY message_id DESC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return [
            ReactionRoleMessage(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                message_id=int(row["message_id"]),
                title=str(row["title"]),
                description=str(row["description"]),
            )
            for row in rows
        ]

    async def upsert_binding(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        message_id: int,
        emoji_key: str,
        emoji_display: str,
        role_id: int,
    ) -> None:
        await db.execute(
            """
            INSERT INTO reaction_role_bindings (guild_id, message_id, emoji_key, emoji_display, role_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, message_id, emoji_key)
            DO UPDATE SET role_id = excluded.role_id, emoji_display = excluded.emoji_display
            """,
            (guild_id, message_id, emoji_key, emoji_display, role_id, datetime.now(UTC).isoformat()),
        )
        await db.commit()

    async def delete_binding(self, db: aiosqlite.Connection, guild_id: int, message_id: int, emoji_key: str) -> None:
        await db.execute(
            "DELETE FROM reaction_role_bindings WHERE guild_id = ? AND message_id = ? AND emoji_key = ?",
            (guild_id, message_id, emoji_key),
        )
        await db.commit()

    async def list_bindings(self, db: aiosqlite.Connection, guild_id: int, message_id: int) -> list[ReactionRoleBinding]:
        cursor = await db.execute(
            """
            SELECT guild_id, message_id, emoji_key, emoji_display, role_id
            FROM reaction_role_bindings
            WHERE guild_id = ? AND message_id = ?
            ORDER BY emoji_display ASC
            """,
            (guild_id, message_id),
        )
        rows = await cursor.fetchall()
        return [
            ReactionRoleBinding(
                guild_id=int(row["guild_id"]),
                message_id=int(row["message_id"]),
                emoji_key=str(row["emoji_key"]),
                emoji_display=str(row["emoji_display"]),
                role_id=int(row["role_id"]),
            )
            for row in rows
        ]

    async def find_binding(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        message_id: int,
        emoji_key: str,
    ) -> ReactionRoleBinding | None:
        cursor = await db.execute(
            """
            SELECT guild_id, message_id, emoji_key, emoji_display, role_id
            FROM reaction_role_bindings
            WHERE guild_id = ? AND message_id = ? AND emoji_key = ?
            """,
            (guild_id, message_id, emoji_key),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return ReactionRoleBinding(
            guild_id=int(row["guild_id"]),
            message_id=int(row["message_id"]),
            emoji_key=str(row["emoji_key"]),
            emoji_display=str(row["emoji_display"]),
            role_id=int(row["role_id"]),
        )

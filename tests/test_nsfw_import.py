from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.nsfw_import import NSFWImportCog, collect_import_payload
from services.social_game_service import SocialGameService


def test_forwarded_snapshot_content_and_media_are_collected() -> None:
    attachment = SimpleNamespace(filename="image.png")
    embed = discord.Embed(title="Галерея")
    message = SimpleNamespace(
        content="",
        attachments=[],
        embeds=[],
        message_snapshots=[
            SimpleNamespace(
                content="Текст из пересланного сообщения",
                attachments=[attachment],
                embeds=[embed],
            )
        ],
    )

    content, attachments, embeds = collect_import_payload(message)

    assert content == "Текст из пересланного сообщения"
    assert attachments == [attachment]
    assert embeds == [embed]


def test_import_message_is_republished_once_by_bot() -> None:
    async def scenario() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        service = SocialGameService()
        await service.init_db(db)
        await service.set_nsfw_channel(db, 1, 20)
        await service.set_nsfw_import_channel(db, 1, 10)

        uploaded_file = object()
        attachment = SimpleNamespace(
            filename="image.png",
            size=1024,
            url="https://cdn.discordapp.com/image.png",
            is_spoiler=lambda: False,
            to_file=AsyncMock(return_value=uploaded_file),
        )
        snapshot = SimpleNamespace(
            content="Импортированный пост",
            attachments=[attachment],
            embeds=[],
        )
        sent_message = SimpleNamespace(id=222)
        target = SimpleNamespace(
            id=20,
            is_nsfw=lambda: True,
            send=AsyncMock(return_value=sent_message),
        )
        import_channel = SimpleNamespace(id=10, is_nsfw=lambda: True)
        guild = SimpleNamespace(id=1, filesize_limit=8 * 1024 * 1024)
        message = SimpleNamespace(
            id=111,
            guild=guild,
            channel=import_channel,
            author=SimpleNamespace(bot=False),
            content="",
            attachments=[],
            embeds=[],
            message_snapshots=[snapshot],
            add_reaction=AsyncMock(),
        )
        bot = SimpleNamespace(
            db=db,
            delivery_db=db,
            social_games=service,
            get_channel=lambda channel_id: target if channel_id == 20 else None,
        )
        cog = NSFWImportCog(bot)

        await cog.on_message(message)
        await cog.on_message(message)

        target.send.assert_awaited_once()
        kwargs = target.send.await_args.kwargs
        assert kwargs["content"] == "Импортированный пост"
        assert kwargs["files"] == [uploaded_file]
        assert isinstance(kwargs["allowed_mentions"], discord.AllowedMentions)
        attachment.to_file.assert_awaited_once()
        message.add_reaction.assert_awaited_once_with("✅")
        assert await service.was_nsfw_imported(db, 1, 111)
        await db.close()

    asyncio.run(scenario())


def test_import_fails_closed_when_destination_is_not_age_restricted() -> None:
    async def scenario() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        service = SocialGameService()
        await service.init_db(db)
        await service.set_nsfw_channel(db, 1, 20)
        await service.set_nsfw_import_channel(db, 1, 10)

        target = SimpleNamespace(id=20, is_nsfw=lambda: False, send=AsyncMock())
        message = SimpleNamespace(
            id=111,
            guild=SimpleNamespace(id=1, filesize_limit=8 * 1024 * 1024),
            channel=SimpleNamespace(id=10, is_nsfw=lambda: True),
            author=SimpleNamespace(bot=False),
            content="post",
            attachments=[],
            embeds=[],
            message_snapshots=[],
            add_reaction=AsyncMock(),
        )
        bot = SimpleNamespace(
            db=db,
            delivery_db=db,
            social_games=service,
            get_channel=lambda _channel_id: target,
        )

        await NSFWImportCog(bot).on_message(message)

        target.send.assert_not_awaited()
        assert not await service.was_nsfw_imported(db, 1, 111)
        await db.close()

    asyncio.run(scenario())

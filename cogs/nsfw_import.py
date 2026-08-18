from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)

MAX_FILES = 10
MAX_EMBEDS = 10
MAX_CONTENT_LENGTH = 2000
MAX_UPLOAD_TOTAL = 24 * 1024 * 1024


def collect_import_payload(message: discord.Message | Any) -> tuple[str, list[Any], list[discord.Embed]]:
    """Collect both ordinary uploads and Discord forwarded-message snapshots."""
    content_parts: list[str] = []
    attachments: list[Any] = []
    embeds: list[discord.Embed] = []

    def add_content(value: str | None) -> None:
        clean = (value or "").strip()
        if clean and clean not in content_parts:
            content_parts.append(clean)

    add_content(getattr(message, "content", ""))
    attachments.extend(getattr(message, "attachments", []) or [])
    embeds.extend(getattr(message, "embeds", []) or [])

    for snapshot in getattr(message, "message_snapshots", []) or []:
        add_content(getattr(snapshot, "content", ""))
        attachments.extend(getattr(snapshot, "attachments", []) or [])
        embeds.extend(getattr(snapshot, "embeds", []) or [])

    return "\n\n".join(content_parts), attachments[:MAX_FILES], embeds[:MAX_EMBEDS]


def _clone_embed(embed: discord.Embed) -> discord.Embed:
    data = embed.to_dict()
    data.pop("type", None)
    data.pop("provider", None)
    data.pop("video", None)
    return discord.Embed.from_dict(data)


def _is_nsfw(channel: Any) -> bool:
    checker = getattr(channel, "is_nsfw", None)
    return bool(callable(checker) and checker())


def _with_fallback_urls(content: str, urls: list[str]) -> str:
    parts = [part for part in (content.strip(), "\n".join(urls)) if part]
    combined = "\n\n".join(parts)
    if len(combined) <= MAX_CONTENT_LENGTH:
        return combined
    return combined[: MAX_CONTENT_LENGTH - 1].rstrip() + "…"


class NSFWImportCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _prepare_files(
        self,
        attachments: list[Any],
        *,
        filesize_limit: int,
    ) -> tuple[list[discord.File], list[str]]:
        files: list[discord.File] = []
        fallback_urls: list[str] = []
        total_size = 0

        for attachment in attachments:
            size = int(getattr(attachment, "size", 0) or 0)
            url = str(getattr(attachment, "url", "") or "")
            if size > filesize_limit or total_size + size > MAX_UPLOAD_TOTAL:
                if url:
                    fallback_urls.append(url)
                continue
            try:
                file = await attachment.to_file(
                    use_cached=True,
                    spoiler=bool(attachment.is_spoiler()),
                )
            except Exception:
                logger.warning(
                    "Could not download NSFW import attachment: filename=%s",
                    getattr(attachment, "filename", "unknown"),
                    exc_info=True,
                )
                if url:
                    fallback_urls.append(url)
                continue
            files.append(file)
            total_size += size

        return files, fallback_urls

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        db = self.bot.delivery_db or self.bot.db
        if db is None:
            return

        settings = await self.bot.social_games.get_nsfw_import_settings(db, message.guild.id)
        if settings is None:
            return

        import_channel_id = int(settings["nsfw_import_channel_id"])
        target_channel_id = int(settings["nsfw_channel_id"])
        if (
            import_channel_id <= 0
            or target_channel_id <= 0
            or import_channel_id == target_channel_id
            or message.channel.id != import_channel_id
            or not _is_nsfw(message.channel)
        ):
            return

        target = self.bot.get_channel(target_channel_id)
        if target is None or not _is_nsfw(target) or not hasattr(target, "send"):
            return

        claimed = await self.bot.social_games.claim_nsfw_import(
            db,
            message.guild.id,
            message.id,
        )
        if not claimed:
            return

        try:
            content, attachments, source_embeds = collect_import_payload(message)
            filesize_limit = int(getattr(message.guild, "filesize_limit", 8 * 1024 * 1024))
            files, fallback_urls = await self._prepare_files(
                attachments,
                filesize_limit=filesize_limit,
            )
            content = _with_fallback_urls(content, fallback_urls)
            embeds = [_clone_embed(embed) for embed in source_embeds]
            if not content and not files and not embeds:
                await self.bot.social_games.release_nsfw_import_claim(
                    db,
                    message.guild.id,
                    message.id,
                )
                await message.add_reaction("❌")
                return

            sent = await target.send(
                content=content or None,
                files=files,
                embeds=embeds,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.social_games.record_nsfw_import_delivery(
                db,
                message.guild.id,
                message.id,
                sent.id,
            )
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                logger.debug("Could not add NSFW import success reaction", exc_info=True)
        except Exception:
            await self.bot.social_games.release_nsfw_import_claim(
                db,
                message.guild.id,
                message.id,
            )
            logger.exception(
                "NSFW import failed: guild_id=%s message_id=%s",
                message.guild.id,
                message.id,
            )
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                logger.debug("Could not add NSFW import failure reaction", exc_info=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(NSFWImportCog(bot))

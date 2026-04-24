from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from services.level_service import LevelService
from services.reaction_role_service import ReactionRoleService
from services.watchmode_service import WatchmodeService

logger = logging.getLogger(__name__)


class MovieBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self.db: aiosqlite.Connection | None = None

        self.watchmode = WatchmodeService(settings)
        self.levels = LevelService(settings)
        self.reaction_roles = ReactionRoleService()

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.db = await aiosqlite.connect(self.settings.db_path)
        self.db.row_factory = aiosqlite.Row

        await self.levels.init_db(self.db)
        await self.reaction_roles.init_db(self.db)
        await self.watchmode.load_genres(self.session)

        for ext in (
            "cogs.movies",
            "cogs.moderation",
            "cogs.fun",
            "cogs.levels",
            "cogs.relay",
            "cogs.reaction_roles",
        ):
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension: %s", ext)
            except Exception:
                logger.exception("Failed to load extension: %s", ext)
<<<<<<< codex/refactor-discord-bot-structure-and-functionality-6ah4o0
=======
            await self.load_extension(ext)
>>>>>>> main

        self.tree.on_error = self.on_tree_error
        await self.tree.sync()
        logger.info("Bot started, genres loaded: %s", len(self.watchmode.genre_id_to_name))

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        await self.change_presence(status=discord.Status.online, activity=discord.Game(name="Просмотр фильмов"))
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")

    async def send_mod_log(self, guild: discord.Guild, action: str, description: str, color: discord.Color) -> None:
        if not self.settings.mod_log_channel_id:
            return

        channel = guild.get_channel(self.settings.mod_log_channel_id)
        if channel is None:
            fetched = await self.fetch_channel(self.settings.mod_log_channel_id)
            if isinstance(fetched, discord.TextChannel):
                channel = fetched

        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title=f"🛡️ Модерация: {action}",
            description=description,
            color=color,
            timestamp=datetime.now(UTC),
        )
        await channel.send(embed=embed)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "У вас недостаточно прав для выполнения этой команды."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Подождите {error.retry_after:.1f} сек. перед повтором команды."
        elif isinstance(error, app_commands.TransformerError):
            text = "Похоже, один из аргументов указан неверно. Проверьте формат и попробуйте снова."
        else:
            logger.exception("App command error", exc_info=error)
            text = "Произошла ошибка при выполнении команды. Попробуйте позже."

        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from mcstatus import JavaServer

from config import Settings
from services.level_service import LevelService
from services.reputation_service import ReputationService
from services.reaction_ban_service import ReactionBanService
from services.reaction_role_service import ReactionRoleService
from services.support_ticket_service import SupportTicketService
from services.watchmode_service import WatchmodeService
from services.social_game_service import SocialGameService
from utils.command_localizations import RussianCommandNameTranslator
from utils.voice_runtime import log_voice_runtime

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
        self.reputation = ReputationService()
        self.reaction_bans = ReactionBanService()
        self.reaction_roles = ReactionRoleService()
        self.support_tickets = SupportTicketService()
        self.social_games = SocialGameService()
        self._extensions_bootstrapped = False
        self._mc_server = JavaServer("5.83.140.210", 25780)

    async def load_cogs(self) -> list[str]:
        cogs_dir = Path(__file__).resolve().parent / "cogs"

        if not cogs_dir.exists():
            logger.error("Cogs directory not found: %s", cogs_dir)
            return []

        loaded: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

        for file_path in sorted(cogs_dir.glob("*.py")):
            if file_path.name.startswith("_"):
                continue

            extension = f"cogs.{file_path.stem}"
            try:
                module_source = file_path.read_text(encoding="utf-8")
            except OSError:
                failed.append(extension)
                logger.exception("Failed to read extension file: %s", file_path)
                continue

            if "async def setup" not in module_source:
                skipped.append(extension)
                logger.info("Extension has no async setup, skip: %s", extension)
                continue

            try:
                if extension in self.extensions:
                    skipped.append(extension)
                    logger.info("Extension already loaded, skip: %s", extension)
                    continue
                await self.load_extension(extension)
            except Exception:
                failed.append(extension)
                logger.exception("Failed to load extension: %s", extension)
            else:
                loaded.append(extension)
                logger.info("Loaded extension: %s", extension)

        logger.info("Cogs loaded: %s", loaded)
        if skipped:
            logger.info("Cogs skipped: %s", skipped)
        if failed:
            logger.warning("Cogs failed to load: %s", failed)

        return loaded

    async def setup_hook(self) -> None:
        if self._extensions_bootstrapped:
            logger.warning("setup_hook called more than once; skip extension bootstrap.")
            return

        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._prepare_database_path()
        self.db = await aiosqlite.connect(self.settings.db_path)
        self.db.row_factory = aiosqlite.Row

        await self.levels.init_db(self.db)
        await self.reputation.init_rep_db(self.db)
        await self.reaction_bans.init_db(self.db)
        await self.reaction_roles.init_db(self.db)
        await self.support_tickets.init_db(self.db)
        await self.social_games.init_db(self.db)
        await self.watchmode.load_genres(self.session)
        log_voice_runtime(logger)

        loaded_cogs = await self.load_cogs()

        self.tree.on_error = self.on_tree_error
        await self.tree.set_translator(RussianCommandNameTranslator())
        await self.tree.sync()
        self._extensions_bootstrapped = True
        logger.info("Bot started, genres loaded: %s, cogs loaded: %s", len(self.watchmode.genre_id_to_name), loaded_cogs)

    def _prepare_database_path(self) -> None:
        db_path = Path(self.settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        old_default = Path("bot_data.sqlite3")
        if (
            self.settings.db_path == "data/bot.db"
            and not db_path.exists()
            and old_default.exists()
        ):
            shutil.move(str(old_default), str(db_path))
            logger.info("Migrated legacy SQLite DB from %s to %s", old_default, db_path)

    async def close(self) -> None:
        if self.minecraft_presence_loop.is_running():
            self.minecraft_presence_loop.cancel()
        if self.session and not self.session.closed:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        if not self.minecraft_presence_loop.is_running():
            self.minecraft_presence_loop.start()
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")

    @tasks.loop(seconds=60)
    async def minecraft_presence_loop(self) -> None:
        try:
            status = await self._mc_server.async_status()
            online = status.players.online
            max_players = status.players.max
            activity = discord.Game(name=f"Онлайн: {online}/{max_players}")
        except Exception:
            logger.exception("Failed to fetch Minecraft server status")
            activity = discord.Game(name="Онлайн: offline")
        await self.change_presence(status=discord.Status.online, activity=activity)

    @minecraft_presence_loop.before_loop
    async def before_minecraft_presence_loop(self) -> None:
        await self.wait_until_ready()

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

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
from utils.voice_runtime import find_ffmpeg, log_voice_runtime

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
        self._support_category_logged = False
        self._last_mc_status_error: str | None = None
        self._mc_server = JavaServer("5.83.140.210", 25780)

    async def load_cogs(self) -> tuple[list[str], list[str], list[str]]:
        cogs_dir = Path(__file__).resolve().parent / "cogs"

        if not cogs_dir.exists():
            logger.error("Cogs directory not found: %s", cogs_dir)
            return [], [], []

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
                logger.debug("Extension has no async setup, skip: %s", extension)
                continue

            try:
                if extension in self.extensions:
                    skipped.append(extension)
                    logger.debug("Extension already loaded, skip: %s", extension)
                    continue
                await self.load_extension(extension)
            except Exception:
                failed.append(extension)
                logger.exception("Failed to load extension: %s", extension)
            else:
                loaded.append(extension)
                logger.debug("Loaded extension: %s", extension)

        logger.debug("Cogs loaded: %s", loaded)
        if skipped:
            logger.debug("Cogs skipped: %s", skipped)
        if failed:
            logger.warning("Cogs failed to load: %s", failed)

        return loaded, skipped, failed

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

        loaded_cogs, skipped_cogs, failed_cogs = await self.load_cogs()

        self.tree.on_error = self.on_tree_error
        await self.tree.set_translator(RussianCommandNameTranslator())
        await self.tree.sync()
        self._extensions_bootstrapped = True
        logger.info(
            "Bot started: cogs_loaded=%s, cogs_skipped=%s, voice_ready=%s",
            len(loaded_cogs),
            len(skipped_cogs),
            bool(find_ffmpeg()),
        )
        if failed_cogs:
            logger.warning("Bot started with failed cogs: count=%s", len(failed_cogs))

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
        logger.debug("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")
        if not self._support_category_logged:
            self._support_category_logged = True
            await self.log_support_category_startup_diagnostics()

    @staticmethod
    def _missing_support_permissions(permissions: discord.Permissions) -> list[str]:
        required = {
            "view_channel": permissions.view_channel,
            "manage_channels": permissions.manage_channels,
            "manage_permissions": permissions.manage_roles,
            "send_messages": permissions.send_messages,
            "embed_links": permissions.embed_links,
            "read_message_history": permissions.read_message_history,
        }
        return [name for name, allowed in required.items() if not allowed]

    async def log_support_category_startup_diagnostics(self) -> None:
        if self.settings.support_category_error:
            logger.warning("%s", self.settings.support_category_error)
            return

        category_id = self.settings.support_category_id
        cached = self.get_channel(category_id)
        category: discord.CategoryChannel | None = cached if isinstance(cached, discord.CategoryChannel) else None
        if cached is not None and category is None:
            logger.warning("SUPPORT_CATEGORY_ID points to %s, not CategoryChannel: id=%s", type(cached).__name__, category_id)
            return

        if category is None:
            try:
                fetched = await self.fetch_channel(category_id)
            except discord.NotFound:
                logger.warning("Support category not found through cache or fetch: id=%s", category_id)
                return
            except discord.Forbidden:
                logger.warning("Support category fetch forbidden: id=%s", category_id)
                return
            except discord.HTTPException as exc:
                logger.warning("Support category fetch failed: id=%s error=%s", category_id, exc)
                return
            if not isinstance(fetched, discord.CategoryChannel):
                logger.warning("SUPPORT_CATEGORY_ID points to %s, not CategoryChannel: id=%s", type(fetched).__name__, category_id)
                return
            category = fetched

        me = category.guild.me
        if me is None and self.user is not None:
            me = category.guild.get_member(self.user.id)
        if me is None:
            logger.warning("Support category found but bot member is unavailable: id=%s guild=%s", category.id, category.guild.id)
            return

        missing = self._missing_support_permissions(category.permissions_for(me))
        if missing:
            logger.warning(
                "Support category permissions missing: id=%s guild=%s missing=%s",
                category.id,
                category.guild.id,
                ",".join(missing),
            )
            return
        logger.info("Support category ready: id=%s guild=%s name=%s", category.id, category.guild.id, category.name)

    @tasks.loop(seconds=60)
    async def minecraft_presence_loop(self) -> None:
        try:
            status = await self._mc_server.async_status()
            online = status.players.online
            max_players = status.players.max
            activity = discord.Game(name=f"Онлайн: {online}/{max_players}")
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if message != self._last_mc_status_error:
                logger.warning("Failed to fetch Minecraft server status: %s", message)
                logger.debug("Minecraft server status traceback", exc_info=True)
                self._last_mc_status_error = message
            activity = discord.Game(name="Онлайн: offline")
        try:
            await self.change_presence(status=discord.Status.online, activity=activity)
        except Exception as exc:
            logger.warning("Failed to update Minecraft presence: %s", exc)
            logger.debug("Minecraft presence traceback", exc_info=True)

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

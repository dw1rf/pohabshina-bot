from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from services.jail_service import JailService
from services.level_service import LevelService
from services.reputation_service import ReputationService
from services.reaction_ban_service import ReactionBanService
from services.reaction_role_service import ReactionRoleService
from services.social_game_service import SocialGameService
from services.economy_service import EconomyService
from services.progression_service import ProgressionService
from services.community_ops_service import CommunityOpsService
from services.automod_service import AutomodService
from utils.bot_profile import LAUNCHER_ACTIVITY, sync_profile
from utils.command_localizations import RussianCommandNameTranslator
from utils.engagement_content import EngagementContent, load_engagement_content
from utils.voice_runtime import find_ffmpeg, log_voice_runtime

logger = logging.getLogger(__name__)


class MovieBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, activity=discord.Game(name=LAUNCHER_ACTIVITY))

        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self.db: aiosqlite.Connection | None = None
        self.reputation_db: aiosqlite.Connection | None = None
        self.jail_db: aiosqlite.Connection | None = None
        # Each write-heavy domain gets its own SQLite connection.  Sharing one
        # aiosqlite connection between unrelated service locks lets one task
        # commit or collide with another task's transaction.
        self.economy_db: aiosqlite.Connection | None = None
        self.progression_db: aiosqlite.Connection | None = None
        self.community_db: aiosqlite.Connection | None = None
        self.digest_db: aiosqlite.Connection | None = None
        self.automod_db: aiosqlite.Connection | None = None
        self.giveaway_db: aiosqlite.Connection | None = None
        self.delivery_db: aiosqlite.Connection | None = None

        self.levels = LevelService(settings)
        self.jails = JailService()
        self.reputation = ReputationService()
        self.reaction_bans = ReactionBanService()
        self.reaction_roles = ReactionRoleService()
        self.social_games = SocialGameService()
        self.economy = EconomyService()
        self.progression = ProgressionService()
        self.engagement_content: EngagementContent = load_engagement_content(settings.engagement_content_path)
        self._extensions_bootstrapped = False
        self._bad_mod_log_channel_ids: set[int] = set()

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
        self._backup_database_before_migrations()
        self.db = await self._open_database_connection()

        if self.user is not None and self.application is not None:
            try:
                await sync_profile(self.db, self.user, self.application)
            except (OSError, sqlite3.Error, discord.HTTPException, discord.RateLimited):
                logger.exception("Could not update bot profile. Will retry on next startup.")

        await self.levels.init_db(self.db)
        await self.jails.init_db(self.db)
        await self.reputation.init_rep_db(self.db)
        await self.reaction_bans.init_db(self.db)
        await self.reaction_roles.init_db(self.db)
        await self.social_games.init_db(self.db)
        # Critical moderation/reputation transactions must not share a
        # connection with unrelated listeners that can commit mid-operation.
        # A shorter busy timeout keeps retry budgets bounded for interactions.
        self.reputation_db = await self._open_database_connection(busy_timeout_ms=1000)
        self.jail_db = await self._open_database_connection(busy_timeout_ms=1000)
        self.economy_db = await self._open_database_connection()
        await self.economy.init_db(self.economy_db)
        self.progression_db = await self._open_database_connection()
        await self.progression.init_db(self.progression_db)
        self.community_db = await self._open_database_connection()
        self.digest_db = await self._open_database_connection()
        self.automod_db = await self._open_database_connection()
        self.giveaway_db = await self._open_database_connection()
        self.delivery_db = await self._open_database_connection()
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

    async def _open_database_connection(self, *, busy_timeout_ms: int = 10000) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.settings.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        return db

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

    def _backup_database_before_migrations(self) -> None:
        """Create a recoverable snapshot before any pending core migration."""
        db_path = Path(self.settings.db_path)
        if not db_path.exists() or db_path.stat().st_size == 0:
            return

        pending_label = "economy"
        try:
            with closing(sqlite3.connect(db_path)) as source:
                table = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                if table:
                    economy_applied = source.execute(
                        "SELECT 1 FROM schema_migrations WHERE name = ?",
                        (EconomyService.MIGRATION_VERSION,),
                    ).fetchone()
                    gameplay_applied = source.execute(
                        "SELECT 1 FROM schema_migrations WHERE name = ?",
                        (ProgressionService.MIGRATION_VERSION,),
                    ).fetchone()
                    community_applied = source.execute(
                        "SELECT 1 FROM schema_migrations WHERE name = ?",
                        (CommunityOpsService.MIGRATION_VERSION,),
                    ).fetchone()
                    automod_applied = source.execute(
                        "SELECT 1 FROM schema_migrations WHERE name = ?",
                        (AutomodService.MIGRATION_VERSION,),
                    ).fetchone()
                    if all(
                        applied is not None
                        for applied in (economy_applied, gameplay_applied, community_applied, automod_applied)
                    ):
                        return
                    if economy_applied is not None:
                        pending_label = "gameplay" if gameplay_applied is None else "features"
        except sqlite3.Error:
            logger.exception("Could not inspect SQLite migration state; backup will still be attempted")

        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{db_path.stem}-pre-{pending_label}-{stamp}{db_path.suffix}"
        try:
            with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        except sqlite3.Error:
            logger.exception("Failed to create pre-migration database backup at %s", backup_path)
            raise
        logger.info("Created pre-migration database backup: %s", backup_path)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        connections = (
            self.automod_db,
            self.giveaway_db,
            self.delivery_db,
            self.digest_db,
            self.community_db,
            self.progression_db,
            self.economy_db,
            self.jail_db,
            self.reputation_db,
            self.db,
        )
        seen: set[int] = set()
        for connection in connections:
            if connection is not None and id(connection) not in seen:
                seen.add(id(connection))
                await connection.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.debug("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")

    async def send_mod_log(self, guild: discord.Guild, action: str, description: str, color: discord.Color) -> bool:
        if not self.settings.mod_log_channel_id:
            return False
        if self.settings.mod_log_channel_id in self._bad_mod_log_channel_ids:
            return False

        channel = guild.get_channel(self.settings.mod_log_channel_id)
        if channel is None:
            try:
                fetched = await self.fetch_channel(self.settings.mod_log_channel_id)
            except discord.NotFound as exc:
                self._bad_mod_log_channel_ids.add(self.settings.mod_log_channel_id)
                logger.warning("Failed to fetch mod log channel %s: %s", self.settings.mod_log_channel_id, exc)
                logger.debug("Mod log fetch traceback", exc_info=True)
                return False
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to fetch mod log channel %s: %s", self.settings.mod_log_channel_id, exc)
                logger.debug("Mod log fetch traceback", exc_info=True)
                return False
            if isinstance(fetched, discord.TextChannel):
                channel = fetched

        if not isinstance(channel, discord.TextChannel):
            return False

        embed = discord.Embed(
            title=f"🛡️ Модерация: {action}",
            description=description,
            color=color,
            timestamp=datetime.now(UTC),
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to send mod log: guild=%s action=%s error=%s", guild.id, action, exc)
            logger.debug("Mod log send traceback", exc_info=True)
            return False
        return True

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "У вас недостаточно прав для выполнения этой команды."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Подождите {error.retry_after:.1f} сек. перед повтором команды."
        elif isinstance(error, app_commands.TransformerError):
            text = "Похоже, один из аргументов указан неверно. Проверьте формат и попробуйте снова."
        else:
            logger.exception("App command error", exc_info=error)
            if interaction.response.is_done():
                return
            text = "Произошла ошибка при выполнении команды. Попробуйте позже."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            logger.debug("Failed to send app command error response", exc_info=True)

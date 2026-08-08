from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.automod_service import AutomodDecision, AutomodService
from utils.brand import BRAND_COLOR, BRAND_NAME

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


def _can_manage(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


def _is_staff(member: discord.Member) -> bool:
    permissions = member.guild_permissions
    return any(
        (
            permissions.administrator,
            permissions.manage_guild,
            permissions.manage_messages,
            permissions.moderate_members,
        )
    )


class AutomodCog(commands.Cog):
    automod_group = app_commands.Group(
        name="automod", description="Локальная детерминированная автомодерация"
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.service = AutomodService()
        self._classification_lock = asyncio.Lock()
        self._classification_cache: dict[tuple[int, int], AutomodDecision | None] = {}

    async def cog_load(self) -> None:
        if self.bot.automod_db is not None:
            await self.service.init_db(self.bot.automod_db)

    async def _log_incident(
        self,
        guild: discord.Guild,
        *,
        user_id: int,
        channel_id: int,
        rules: tuple[str, ...],
        action: str,
        metadata: dict[str, int],
    ) -> None:
        if self.bot.automod_db is None:
            return
        settings = await self.service.get_settings(self.bot.automod_db, guild.id)
        log_channel = guild.get_channel(int(settings["log_channel_id"]))
        if not isinstance(log_channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title="Событие автомодерации",
            color=BRAND_COLOR,
            description="Сработали локальные правила без анализа текста внешними сервисами.",
        )
        embed.add_field(name="Участник", value=f"ID {user_id}")
        embed.add_field(name="Канал", value=f"ID {channel_id}")
        embed.add_field(name="Правило", value=", ".join(rules))
        embed.add_field(name="Действие", value=action)
        embed.add_field(
            name="Счётчики",
            value=(
                f"упоминания: {metadata.get('mentions', 0)}, "
                f"ссылки: {metadata.get('links', 0)}, "
                f"burst: {metadata.get('burst_count', 0)}, "
                f"дубли: {metadata.get('duplicate_count', 0)}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Содержимое сообщения не сохраняется • {BRAND_NAME}")
        try:
            await log_channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Automod log channel unavailable: guild=%s channel=%s", guild.id, log_channel.id)

    async def _apply_message_action(
        self, message: discord.Message, member: discord.Member, mode: str, timeout_minutes: int
    ) -> str:
        if mode == "log":
            return "logged"
        bot_member = message.guild.me if message.guild is not None else None
        if bot_member is None:
            return "permission_missing"
        channel_permissions = message.channel.permissions_for(bot_member)
        if mode == "delete":
            if not channel_permissions.manage_messages:
                return "permission_missing"
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                return "permission_missing"
            return "deleted"
        if mode == "timeout":
            can_timeout = (
                bot_member.guild_permissions.moderate_members
                and member.id != message.guild.owner_id
                and bot_member.top_role > member.top_role
            )
            if not can_timeout:
                return "permission_missing"
            try:
                await member.timeout(
                    timedelta(minutes=max(1, timeout_minutes)),
                    reason="Локальная автомодерация Vulgarities Bot",
                )
            except (discord.Forbidden, discord.HTTPException):
                return "permission_missing"
            if channel_permissions.manage_messages:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("Message remained after a successful timeout: message=%s", message.id)
            return "timed_out"
        return "logged"

    async def classify_message(self, message: discord.Message) -> AutomodDecision | None:
        if (
            self.bot.automod_db is None
            or message.guild is None
            or message.webhook_id is not None
            or message.author.bot
            or not isinstance(message.author, discord.Member)
            or _is_staff(message.author)
        ):
            return None
        cache_key = (message.guild.id, message.id)
        async with self._classification_lock:
            if cache_key in self._classification_cache:
                return self._classification_cache[cache_key]
            role_ids = [role.id for role in message.author.roles]
            if await self.service.is_exempt(
                self.bot.automod_db,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role_ids=role_ids,
            ):
                decision = None
            else:
                unique_mentions = {user.id for user in message.mentions}
                unique_roles = {role.id for role in message.role_mentions}
                mention_count = len(unique_mentions) + len(unique_roles) + int(message.mention_everyone)
                decision = await self.service.evaluate_message(
                    self.bot.automod_db,
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    channel_id=message.channel.id,
                    content=message.content,
                    mention_count=mention_count,
                )
            if len(self._classification_cache) >= 5000:
                for key in list(self._classification_cache)[:1000]:
                    self._classification_cache.pop(key, None)
            self._classification_cache[cache_key] = decision
            return decision

    async def allows_progress(self, message: discord.Message) -> bool:
        """Use the exact same cached verdict before XP or analytics are written."""
        decision = await self.classify_message(message)
        return decision is None or decision.action_mode == "log"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self.bot.automod_db is None or message.guild is None or not isinstance(message.author, discord.Member):
            return
        decision = await self.classify_message(message)
        if decision is None:
            return
        settings = await self.service.get_settings(self.bot.automod_db, message.guild.id)
        action = await self._apply_message_action(
            message, message.author, decision.action_mode, int(settings["timeout_minutes"])
        )
        await self.service.record_incident(
            self.bot.automod_db,
            decision=decision,
            guild_id=message.guild.id,
            user_id=message.author.id,
            channel_id=message.channel.id,
            message_id=message.id,
            action=action,
        )
        await self._log_incident(
            message.guild,
            user_id=message.author.id,
            channel_id=message.channel.id,
            rules=decision.rules,
            action=action,
            metadata=decision.metadata,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if self.bot.automod_db is None or member.bot or _is_staff(member):
            return
        if await self.service.is_exempt(
            self.bot.automod_db,
            guild_id=member.guild.id,
            channel_id=0,
            role_ids=[role.id for role in member.roles],
        ):
            return
        decision = await self.service.record_member_join(
            self.bot.automod_db,
            guild_id=member.guild.id,
            user_id=member.id,
            account_created_at=member.created_at,
        )
        if not decision.should_quarantine:
            return
        role = member.guild.get_role(decision.quarantine_role_id)
        bot_member = member.guild.me
        can_assign = (
            role is not None
            and bot_member is not None
            and bot_member.guild_permissions.manage_roles
            and bot_member.top_role > role
            and bot_member.top_role > member.top_role
        )
        action = "quarantine_failed"
        if can_assign and role is not None:
            try:
                await member.add_roles(role, reason="Локальная защита от рейда")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not assign quarantine role: guild=%s user=%s", member.guild.id, member.id)
            else:
                action = "quarantined"
                await self.service.mark_quarantined(
                    self.bot.automod_db, guild_id=member.guild.id, user_id=member.id
                )
        incident = AutomodDecision(
            rules=("raid_join",),
            action_mode="log",
            content_hash="",
            metadata={
                "mentions": 0,
                "links": 0,
                "burst_count": decision.recent_joins,
                "duplicate_count": 0,
            },
        )
        await self.service.record_incident(
            self.bot.automod_db,
            decision=incident,
            guild_id=member.guild.id,
            user_id=member.id,
            channel_id=0,
            message_id=None,
            action=action,
        )
        await self._log_incident(
            member.guild,
            user_id=member.id,
            channel_id=0,
            rules=("raid_join",),
            action=action,
            metadata=incident.metadata,
        )

    @automod_group.command(name="setup", description="Включить автомодерацию в безопасном режиме")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        action_mode=[
            app_commands.Choice(name="Только журнал", value="log"),
            app_commands.Choice(name="Удалять", value="delete"),
            app_commands.Choice(name="Удалять и timeout", value="timeout"),
        ]
    )
    async def setup_automod(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel,
        action_mode: app_commands.Choice[str] | None = None,
        quarantine_role: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        selected_mode = action_mode.value if action_mode is not None else "log"
        await self.service.configure(
            self.bot.automod_db,
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            enabled=True,
            action_mode=selected_mode,
            log_channel_id=log_channel.id,
            quarantine_role_id=quarantine_role.id if quarantine_role else 0,
        )
        await interaction.response.send_message(
            f"Автомодерация включена. Режим: {selected_mode}. Raid guard остаётся выключенным до настройки правила.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @automod_group.command(name="status", description="Показать настройки автомодерации")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        settings = await self.service.get_settings(self.bot.automod_db, interaction.guild.id)
        text = (
            f"Статус: {'включена' if int(settings['enabled']) else 'выключена'}\n"
            f"Режим: {settings['action_mode']}\n"
            f"Burst: {int(settings['burst_enabled'])} / {int(settings['burst_count'])} за {int(settings['burst_window_seconds'])} сек.\n"
            f"Дубли: {int(settings['duplicate_enabled'])} / {int(settings['duplicate_count'])}\n"
            f"Упоминания: {int(settings['mass_mentions_enabled'])} / {int(settings['mass_mentions'])}\n"
            f"Ссылки: {int(settings['link_flood_enabled'])} / {int(settings['link_count'])}\n"
            f"Raid guard: {'включён' if int(settings['raid_enabled']) else 'выключен'}"
        )
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @automod_group.command(name="rule", description="Включить или выключить правило")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        rule=[
            app_commands.Choice(name="Burst сообщений", value="burst"),
            app_commands.Choice(name="Повторы", value="duplicate"),
            app_commands.Choice(name="Массовые упоминания", value="mass_mentions"),
            app_commands.Choice(name="Много ссылок", value="link_flood"),
            app_commands.Choice(name="Raid guard", value="raid"),
        ]
    )
    async def set_rule(
        self,
        interaction: discord.Interaction,
        rule: app_commands.Choice[str],
        enabled: bool,
        threshold: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.set_rule(
            self.bot.automod_db,
            guild_id=interaction.guild.id,
            rule=rule.value,
            enabled=enabled,
            threshold=int(threshold) if threshold is not None else None,
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Правило {rule.name}: {'включено' if enabled else 'выключено'}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @automod_group.command(name="exempt", description="Добавить или убрать исключение")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_exemption(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        remove: bool = False,
    ) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        if (channel is None) == (role is None):
            await interaction.response.send_message(
                "Укажите ровно один канал или одну роль.", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        kind = "channel" if channel is not None else "role"
        target = channel if channel is not None else role
        if target is None:
            return
        if remove:
            await self.service.remove_exemption(
                self.bot.automod_db, guild_id=interaction.guild.id, kind=kind, target_id=target.id
            )
        else:
            await self.service.set_exemption(
                self.bot.automod_db,
                guild_id=interaction.guild.id,
                kind=kind,
                target_id=target.id,
                actor_id=interaction.user.id,
            )
        await interaction.response.send_message(
            f"Исключение {kind} ID {target.id} {'удалено' if remove else 'сохранено'}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @automod_group.command(name="off", description="Немедленно выключить всю автомодерацию")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable_automod(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.configure(
            self.bot.automod_db,
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            enabled=False,
            raid_enabled=False,
        )
        await interaction.response.send_message(
            "Автомодерация и raid guard выключены. Настройки сохранены.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @automod_group.command(name="raid_status", description="Показать состояние raid guard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        state = await self.service.raid_status(self.bot.automod_db, interaction.guild.id)
        await interaction.response.send_message(
            (
                f"Raid guard: {'включён' if state['enabled'] else 'выключен'}\n"
                f"Входов в окне: {state['joins']} / {state['threshold']}\n"
                f"Окно: {state['window_seconds']} сек.\n"
                f"Карантинов: {state['quarantined']}"
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @automod_group.command(name="raid_off", description="Немедленно выключить raid guard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_off(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.automod_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.disable_raid(
            self.bot.automod_db, guild_id=interaction.guild.id, actor_id=interaction.user.id
        )
        await interaction.response.send_message(
            "Raid guard выключен. Автоматических банов бот не выполняет.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(AutomodCog(bot))

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.jail_service import JailRecord
from utils.helpers import format_dt, parse_duration, truncate_text
from utils.permissions import can_ban, can_kick, can_moderate

logger = logging.getLogger(__name__)

JAIL_CATEGORY_NAME = "🔒-тюрьма"
JAIL_ROLE_NAME = "🔒 Заключённый"
JAIL_SLOWMODE_SECONDS = 30
JAIL_APPEAL_BUTTON_ID = "moderation:jail:appeal"
JAIL_TIME_BUTTON_ID = "moderation:jail:remaining"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_remaining(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minute:02d}:{second:02d}"
    return f"{minute:02d}:{second:02d}"


def _duration_label(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400} дн."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


def _channel_slug(name: str) -> str:
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", name.strip().lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:70] or "user"


class JailAppealModal(discord.ui.Modal, title="Апелляция"):
    appeal_text = discord.ui.TextInput(
        label="Текст апелляции",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
        placeholder="Коротко объясните, почему срок нужно пересмотреть.",
    )

    def __init__(self, cog: "ModerationCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_jail_appeal(interaction, str(self.appeal_text))


class JailView(discord.ui.View):
    def __init__(self, cog: "ModerationCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📝 Написать апелляцию", style=discord.ButtonStyle.primary, custom_id=JAIL_APPEAL_BUTTON_ID)
    async def appeal(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(JailAppealModal(self.cog))

    @discord.ui.button(label="⏳ Осталось времени", style=discord.ButtonStyle.secondary, custom_id=JAIL_TIME_BUTTON_ID)
    async def remaining(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.send_jail_remaining(interaction)


class ModerationCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._jail_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self.bot.add_view(JailView(self))

    async def cog_load(self) -> None:
        self.bot.loop.create_task(self._resume_active_jails())

    async def cog_unload(self) -> None:
        for task in self._jail_tasks.values():
            task.cancel()
        self._jail_tasks.clear()

    @staticmethod
    def _guild(interaction: discord.Interaction) -> discord.Guild | None:
        return interaction.guild

    async def _safe_reply(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(text, ephemeral=ephemeral)
        except discord.HTTPException:
            logger.debug("Failed to send moderation reply", exc_info=True)

    async def _safe_mod_log(self, guild: discord.Guild, action: str, description: str, color: discord.Color) -> None:
        try:
            await self.bot.send_mod_log(guild, action, description, color)
        except Exception:
            logger.exception("Unexpected mod log failure: guild=%s action=%s", guild.id, action)

    @staticmethod
    def _can_use_jail(member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        perms = member.guild_permissions
        return perms.administrator or perms.manage_guild or perms.moderate_members

    def _validate_jail_target(self, guild: discord.Guild, moderator: discord.Member, target: discord.Member) -> str | None:
        me = guild.me
        if target.id == guild.owner_id:
            return "Нельзя посадить в тюрьму владельца сервера."
        if self.bot.user is not None and target.id == self.bot.user.id:
            return "Нельзя посадить в тюрьму самого бота."
        if target.id == moderator.id:
            return "Нельзя посадить в тюрьму самого себя."
        if target.bot:
            return "Нельзя посадить в тюрьму бота."
        if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
            return "Нельзя посадить пользователя с ролью выше или равной вашей."
        if me is None:
            return "Не удалось определить роль бота на сервере."
        if target.top_role >= me.top_role:
            return "Роль бота должна быть выше роли пользователя."
        perms = me.guild_permissions
        missing = []
        if not perms.manage_roles:
            missing.append("Manage Roles")
        if not perms.manage_channels:
            missing.append("Manage Channels")
        if not perms.move_members:
            missing.append("Move Members")
        if missing:
            return "Боту не хватает прав: " + ", ".join(missing) + "."
        return None

    async def _get_or_create_jail_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        category = discord.utils.get(guild.categories, name=JAIL_CATEGORY_NAME)
        if isinstance(category, discord.CategoryChannel):
            return category

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        return await guild.create_category(
            JAIL_CATEGORY_NAME,
            overwrites=overwrites,
            reason="Create jail category",
        )

    async def _get_or_create_jail_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)
        if isinstance(role, discord.Role):
            return role

        role = await guild.create_role(
            name=JAIL_ROLE_NAME,
            permissions=discord.Permissions.none(),
            mentionable=False,
            reason="Create jail role",
        )
        return role

    async def _lock_regular_channels_for_jail_role(
        self,
        guild: discord.Guild,
        jail_role: discord.Role,
        jail_category: discord.CategoryChannel,
    ) -> None:
        overwrite = discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            add_reactions=False,
            connect=False,
            speak=False,
            mention_everyone=False,
        )
        for channel in guild.channels:
            if channel.id == jail_category.id or getattr(channel, "category_id", None) == jail_category.id:
                continue
            try:
                await channel.set_permissions(jail_role, overwrite=overwrite, reason="Jail role channel lock")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to lock channel for jail role: guild=%s channel=%s error=%s", guild.id, channel.id, exc)

    async def _create_jail_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        role: discord.Role,
        user: discord.Member,
    ) -> discord.TextChannel:
        admin_overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=False,
                mention_everyone=False,
            ),
        }
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
            )
        for guild_role in guild.roles:
            perms = guild_role.permissions
            if perms.administrator or perms.manage_guild or perms.moderate_members:
                overwrites[guild_role] = admin_overwrite

        return await guild.create_text_channel(
            name=f"тюрьма-{_channel_slug(user.display_name)}",
            category=category,
            overwrites=overwrites,
            slowmode_delay=JAIL_SLOWMODE_SECONDS,
            reason=f"Jail channel for {user} ({user.id})",
        )

    async def _send_jail_intro(
        self,
        channel: discord.TextChannel,
        user: discord.Member,
        reason: str,
        duration: timedelta,
        expires_at: datetime,
    ) -> None:
        embed = discord.Embed(
            title="🔒 ТЫ В ИЗОЛЯТОРЕ",
            color=discord.Color.dark_gold(),
            timestamp=_utcnow(),
        )
        embed.description = (
            f"👋 Привет, {user.mention}.\n"
            f"⛔ Причина: {reason}\n"
            f"⏰ Время: {_duration_label(duration)}\n"
            f"🔓 Освобождение: {expires_at.strftime('%H:%M')}\n\n"
            "📜 Правила поведения:\n"
            "1. Не спамить. Лимит: 1 сообщение в 30 секунд.\n"
            "2. Не пинговать админов без причины.\n"
            "3. Вести себя тихо.\n\n"
            "⚠️ Нарушение правил в тюрьме = продление срока."
        )
        await channel.send(content=user.mention, embed=embed, view=JailView(self), allowed_mentions=discord.AllowedMentions(users=True))

    async def _resume_active_jails(self) -> None:
        await self.bot.wait_until_ready()
        if self.bot.db is None:
            return
        rows = await self.bot.jails.list_active(self.bot.db)
        for record in rows:
            self._schedule_jail_release(record)

    def _schedule_jail_release(self, record: JailRecord) -> None:
        key = (record.guild_id, record.user_id)
        old_task = self._jail_tasks.pop(key, None)
        if old_task is not None:
            old_task.cancel()
        task = self.bot.loop.create_task(self._release_when_due(record))
        self._jail_tasks[key] = task

    async def _release_when_due(self, record: JailRecord) -> None:
        try:
            delay = max(0.0, (_parse_iso(record.expires_at) - _utcnow()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
            await self.release_jail(record, reason="Срок тюрьмы закончился.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to release jail: guild=%s user=%s", record.guild_id, record.user_id)
        finally:
            self._jail_tasks.pop((record.guild_id, record.user_id), None)

    async def release_jail(self, record: JailRecord, *, reason: str) -> None:
        if self.bot.db is None:
            return
        guild = self.bot.get_guild(record.guild_id)
        if guild is None:
            return

        member = guild.get_member(record.user_id)
        if member is not None:
            role = guild.get_role(record.role_id)
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason=reason)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Failed to remove jail role: guild=%s user=%s error=%s", guild.id, member.id, exc)
            try:
                await member.send(reason)
            except discord.HTTPException:
                pass

        channel = guild.get_channel(record.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(record.channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                channel = None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"🔓 {reason}")
                await channel.delete(reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to close jail channel: guild=%s channel=%s error=%s", guild.id, record.channel_id, exc)

        await self.bot.jails.remove(self.bot.db, record.guild_id, record.user_id)
        await self._safe_mod_log(
            guild,
            "unjail",
            f"Пользователь: <@{record.user_id}> ({record.user_id})\nПричина: {reason}",
            discord.Color.green(),
        )

    async def handle_jail_appeal(self, interaction: discord.Interaction, text: str) -> None:
        guild = interaction.guild
        if guild is None or self.bot.db is None:
            await self._safe_reply(interaction, "Апелляция доступна только на сервере.")
            return
        record = await self.bot.jails.get_active_by_user(self.bot.db, guild.id, interaction.user.id)
        if record is None:
            await self._safe_reply(interaction, "У вас нет активного срока в тюрьме.")
            return
        message = (
            f"Апелляция из тюрьмы\n"
            f"Пользователь: {interaction.user.mention} ({interaction.user.id})\n"
            f"Канал: <#{record.channel_id}>\n"
            f"Текст: {truncate_text(text, 1200)}"
        )
        await self._safe_mod_log(guild, "jail appeal", message, discord.Color.blurple())
        await self._safe_reply(interaction, "Апелляция отправлена администрации.")

    async def send_jail_remaining(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or self.bot.db is None:
            await self._safe_reply(interaction, "Недоступно вне сервера.")
            return
        record = await self.bot.jails.get_active_by_user(self.bot.db, guild.id, interaction.user.id)
        if record is None:
            await self._safe_reply(interaction, "Активный срок не найден.")
            return
        remaining = _parse_iso(record.expires_at) - _utcnow()
        await self._safe_reply(interaction, f"⏳ Осталось времени: {_format_remaining(remaining)}")

    @app_commands.command(name="rules", description="Показать правила сервера")
    @app_commands.default_permissions(manage_guild=True)
    async def rules(self, interaction: discord.Interaction) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        first_embed = discord.Embed(
            title="📜 Правила сервера",
            description="Пожалуйста, ознакомьтесь с правилами перед общением.",
            color=discord.Color.blue(),
        )
        first_embed.add_field(
            name="1. Уважайте участников и администрацию.",
            value="Оскорбления, травля, унижение, токсичность, угрозы.\n**Наказание:** мут 1 час → мут 6 часов → бан.",
            inline=False,
        )
        first_embed.add_field(
            name="2. Не устраивайте провокации и конфликты.",
            value="Ссоры ради ссоры, разжигание конфликтов, намеренный вывод людей на агрессию.\n**Наказание:** мут 1 час → мут 1 день → бан.",
            inline=False,
        )
        first_embed.add_field(
            name="3. Спам, флуд, капс и оффтоп запрещены.",
            value="Засорение чата сообщениями, эмодзи, пингами, бессмысленным текстом.\n**Наказание:** удаление сообщений + мут 1 час → 12 часов → 1 день.",
            inline=False,
        )
        first_embed.add_field(
            name="4. Реклама и подозрительные ссылки запрещены.",
            value="Реклама серверов, каналов, услуг, ботов, а также скам и фишинг.\n**Наказание:** бан без предупреждения.",
            inline=False,
        )
        first_embed.add_field(
            name="5. Запрещён 18+, шок-контент и запрещённый материал.",
            value="Исключение: NSFW-каналы, если такой контент разрешён их тематикой.\nЗапрещённый или незаконный контент запрещён везде.\n**Наказание:** удаление контента + мут 1 день / за тяжёлые случаи — бан сразу.",
            inline=False,
        )
        first_embed.add_field(
            name="6. Не публикуйте чужие личные данные.",
            value="Сливы переписок, фото, номеров, адресов, аккаунтов без согласия.\n**Наказание:** бан без предупреждения.",
            inline=False,
        )

        second_embed = discord.Embed(
            title="📜 Правила сервера (продолжение)",
            color=discord.Color.blue(),
        )
        second_embed.add_field(
            name="7. Соблюдайте тематику каналов.",
            value="Пишите туда, где это уместно.\n**Наказание:** предупреждение → мут 3 часа → мут 12 часов.",
            inline=False,
        )
        second_embed.add_field(
            name="8. Обход наказаний запрещён.",
            value="Твинки, заход с других аккаунтов после мута/бана.\n**Наказание:** перманентный бан всех аккаунтов.",
            inline=False,
        )
        second_embed.add_field(
            name="9. Голосовые каналы.",
            value="Крики, помехи, саундпад, музыка без согласования, намеренное мешательство.\n**Наказание:** отключение от канала / мут в войсе 1 час → 12 часов → 1 день.",
            inline=False,
        )
        second_embed.add_field(
            name="10. Ники, аватары и статусы должны быть адекватными.",
            value="Оскорбительные, провокационные, NSFW или вводящие в заблуждение профили запрещены.\n**Наказание:** требование сменить → мут 12 часов / кик / бан при отказе.",
            inline=False,
        )
        second_embed.add_field(
            name="11. Решения администрации обязательны к соблюдению.",
            value="Обсуждение наказаний — только в ЛС с администрацией.\n**Наказание:** мут 12 часов → 1 день.",
            inline=False,
        )
        second_embed.add_field(
            name="Важно",
            value="Администрация вправе пропустить этапы наказаний и выдать сразу мут/бан, если нарушение серьёзное.",
            inline=False,
        )

        await interaction.response.send_message(embeds=[first_embed, second_embed])

    @app_commands.command(name="warn", description="Выдать предупреждение пользователю")
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для выдачи предупреждений.", ephemeral=True)
            return

        total = await self.bot.levels.add_warning(self.bot.db, guild.id, user.id, interaction.user.id, reason)
        msg = f"⛔ Пользователь {user.mention} получил предупреждение (3/3). Достигнут лимит предупреждений." if total >= 3 else f"⚠️ Пользователь {user.mention} получил предупреждение ({total}/3). Причина: {reason}"
        await interaction.response.send_message(msg)
        await self._safe_mod_log(guild, "warn", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nПричина: {reason}\nТекущий счёт: {total}/3", discord.Color.orange())

    @app_commands.command(name="warnings", description="Показать предупреждения пользователя")
    async def warnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        total, rows = await self.bot.levels.get_warnings(self.bot.db, guild.id, user.id)
        embed = discord.Embed(title=f"Предупреждения: {user.display_name}", description=f"Всего предупреждений: **{total}**", color=discord.Color.gold())
        if rows:
            lines = [f"• {format_dt(row['created_at'])} — <@{row['moderator_id']}>: {truncate_text(row['reason'], 120)}" for row in rows]
            embed.add_field(name="Последние причины", value="\n".join(lines[:10]), inline=False)
        else:
            embed.add_field(name="Статус", value="У пользователя пока нет предупреждений.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarns", description="Сбросить предупреждения пользователя")
    async def clearwarns(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для сброса предупреждений.", ephemeral=True)
            return
        await self.bot.levels.clear_warnings(self.bot.db, guild.id, user.id)
        await interaction.response.send_message(f"✅ Предупреждения пользователя {user.mention} сброшены.")
        await self._safe_mod_log(guild, "clearwarns", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДействие: предупреждения очищены", discord.Color.green())

    @app_commands.command(name="mute", description="Выдать тайм-аут пользователю")
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        delta = parse_duration(duration)
        if not delta:
            await interaction.response.send_message("Неверный формат duration. Используйте 30s, 10m, 1h или 2d.", ephemeral=True)
            return
        if delta > timedelta(days=28):
            await interaction.response.send_message("Максимальная длительность тайм-аута — 28 дней.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.moderate_members:
            await interaction.response.send_message("У бота нет права Moderate Members.", ephemeral=True)
            return

        try:
            await user.timeout(datetime.now(UTC) + delta, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось выдать мут: недостаточно прав или роль пользователя выше.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔇 Пользователь {user.mention} замучен на {duration}. Причина: {reason}")
        await self._safe_mod_log(guild, "mute", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДлительность: {duration}\nПричина: {reason}", discord.Color.dark_gold())

    @app_commands.command(name="unmute", description="Снять тайм-аут с пользователя")
    async def unmute(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.moderate_members:
            await interaction.response.send_message("У бота нет права Moderate Members.", ephemeral=True)
            return

        try:
            await user.timeout(None, reason=f"Unmute by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось снять мут: недостаточно прав или роль пользователя выше.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔊 Мут с пользователя {user.mention} снят.")
        await self._safe_mod_log(guild, "unmute", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}", discord.Color.green())

    @app_commands.command(name="jail", description="Посадить пользователя в тюрьму")
    @app_commands.describe(user="Пользователь", reason="Причина", duration="Время: 10m, 1h, 2d")
    @app_commands.default_permissions(moderate_members=True)
    async def jail(self, interaction: discord.Interaction, user: discord.Member, reason: str, duration: str) -> None:
        guild = self._guild(interaction)
        if guild is None or self.bot.db is None:
            await self._safe_reply(interaction, "Команда доступна только на сервере.")
            return
        if not isinstance(interaction.user, discord.Member) or not self._can_use_jail(interaction.user):
            await self._safe_reply(interaction, "У вас нет прав для команды /jail.")
            return

        delta = parse_duration(duration)
        if not delta:
            await self._safe_reply(interaction, "Неверный формат времени. Используйте 30s, 10m, 1h или 2d.")
            return

        validation_error = self._validate_jail_target(guild, interaction.user, user)
        if validation_error:
            await self._safe_reply(interaction, validation_error)
            return

        existing = await self.bot.jails.get_active_by_user(self.bot.db, guild.id, user.id)
        if existing is not None:
            await self._safe_reply(interaction, f"Пользователь уже в тюрьме: <#{existing.channel_id}>.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            category = await self._get_or_create_jail_category(guild)
            role = await self._get_or_create_jail_role(guild)
            await self._lock_regular_channels_for_jail_role(guild, role, category)
            channel = await self._create_jail_channel(guild, category, role, user)
            await user.add_roles(role, reason=f"Jail by {interaction.user}: {reason}")
            if user.voice and user.voice.channel:
                await user.move_to(None, reason=f"Jail by {interaction.user}: {reason}")
        except discord.Forbidden:
            await self._safe_reply(interaction, "Не удалось оформить тюрьму: боту не хватает прав или роль ниже нужной.")
            return
        except discord.HTTPException as exc:
            logger.warning("Failed to create jail: guild=%s user=%s error=%s", guild.id, user.id, exc)
            await self._safe_reply(interaction, "Discord API не принял создание тюрьмы. Проверьте права бота и попробуйте позже.")
            return

        started_at = _utcnow()
        expires_at = started_at + delta
        await self.bot.jails.upsert(
            self.bot.db,
            guild_id=guild.id,
            user_id=user.id,
            channel_id=channel.id,
            role_id=role.id,
            reason=reason,
            moderator_id=interaction.user.id,
            started_at=started_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        record = JailRecord(
            guild_id=guild.id,
            user_id=user.id,
            channel_id=channel.id,
            role_id=role.id,
            reason=reason,
            moderator_id=interaction.user.id,
            started_at=started_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        self._schedule_jail_release(record)
        await self._send_jail_intro(channel, user, reason, delta, expires_at)
        await self._safe_reply(interaction, f"🔒 Пользователь {user.mention} отправлен в тюрьму на {duration}. Канал: {channel.mention}")
        await self._safe_mod_log(
            guild,
            "jail",
            f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДлительность: {duration}\nПричина: {reason}\nКанал: {channel.mention}",
            discord.Color.dark_gold(),
        )

    @app_commands.command(name="unjail", description="Досрочно освободить пользователя из тюрьмы")
    @app_commands.default_permissions(moderate_members=True)
    async def unjail(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Досрочное освобождение") -> None:
        guild = self._guild(interaction)
        if guild is None or self.bot.db is None:
            await self._safe_reply(interaction, "Команда доступна только на сервере.")
            return
        if not isinstance(interaction.user, discord.Member) or not self._can_use_jail(interaction.user):
            await self._safe_reply(interaction, "У вас нет прав для команды /unjail.")
            return
        record = await self.bot.jails.get_active_by_user(self.bot.db, guild.id, user.id)
        if record is None:
            await self._safe_reply(interaction, "У пользователя нет активного срока в тюрьме.")
            return

        task = self._jail_tasks.pop((record.guild_id, record.user_id), None)
        if task is not None:
            task.cancel()
        await self.release_jail(record, reason=reason)
        await self._safe_reply(interaction, f"🔓 Пользователь {user.mention} освобождён.")

    @app_commands.command(name="ban", description="Забанить пользователя")
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_ban(interaction.user):
            await interaction.response.send_message("У вас нет прав Ban Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message("У бота нет права Ban Members.", ephemeral=True)
            return

        try:
            try:
                await guild.ban(user, reason=reason, delete_message_seconds=0)
            except TypeError:
                await guild.ban(user, reason=reason, delete_message_days=0)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось забанить пользователя: недостаточно прав.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔨 Пользователь {user.mention} забанен. Причина: {reason}")
        await self._safe_mod_log(guild, "ban", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}", discord.Color.red())

    @app_commands.command(name="unban", description="Разбанить пользователя по ID")
    async def unban(self, interaction: discord.Interaction, user_id: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_ban(interaction.user):
            await interaction.response.send_message("У вас нет прав Ban Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message("У бота нет права Ban Members.", ephemeral=True)
            return
        if not user_id.isdigit():
            await interaction.response.send_message("Нужно передать корректный числовой user_id.", ephemeral=True)
            return

        user = await self.bot.fetch_user(int(user_id))
        try:
            await guild.unban(user, reason=f"Unban by {interaction.user}")
        except discord.NotFound:
            await interaction.response.send_message("Этот пользователь не найден в списке банов.", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ Пользователь <@{user.id}> разбанен.")
        await self._safe_mod_log(guild, "unban", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})", discord.Color.green())

    @app_commands.command(name="kick", description="Исключить пользователя с сервера")
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_kick(interaction.user):
            await interaction.response.send_message("У вас нет прав Kick Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.kick_members:
            await interaction.response.send_message("У бота нет права Kick Members.", ephemeral=True)
            return

        try:
            await guild.kick(user, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось исключить пользователя: недостаточно прав.", ephemeral=True)
            return

        await interaction.response.send_message(f"👢 Пользователь {user.mention} исключён с сервера. Причина: {reason}")
        await self._safe_mod_log(guild, "kick", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}", discord.Color.orange())


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ModerationCog(bot))

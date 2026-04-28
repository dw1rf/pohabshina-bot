from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.support_ticket_service import SupportTicket
from utils.embed_format import indent_lines

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShopService:
    key: str
    title: str
    price: str
    description: str | None = None
    coming_soon: bool = False


SHOP_SERVICES: tuple[ShopService, ...] = (
    ShopService("unban", "Разбан в Discord", "100 ₽"),
    ShopService("unmute", "Размут в Discord", "50 ₽"),
    ShopService("remove_warn", "Снятие 1 предупреждения в Discord", "20 ₽"),
    ShopService("unique_role", "Уникальная роль", "100 ₽", "Индивидуальное название и градиент. Без прав управления, не поднимает в списке участников."),
    ShopService("custom_badge", "Персональный значок возле ника", "50 ₽", "Кастомный эмодзи с любым персонажем или формой."),
    ShopService("mc_private_cinema", "Приватный вход в МК-кинотеатр", "150 ₽", "Личный доступ к Minecraft-серверу, синхронизированному с Discord, показ кино в Minecraft.", coming_soon=True),
    ShopService("mc_priority_show", "Показ вашего фильма/сериала/аниме в Minecraft без очереди", "50 ₽", "Трансляция без очереди + упоминание в официальной афише.", coming_soon=True),
    ShopService("mc_op", "Права /op", "1 500 ₽", "Полный доступ к Minecraft-серверу «пахабщины»: креатив/выживание, телепортация, безлимитные показы. Выдаётся только после проверки и согласования с администрацией.", coming_soon=True),
    ShopService("discord_commands_pack", "Пак из 10 индивидуальных команд для Discord-сервера", "цена обсуждается", coming_soon=True),
)

SERVICES_BY_KEY = {service.key: service for service in SHOP_SERVICES}


class SupportPanelView(discord.ui.View):
    def __init__(self, cog: "SupportShopCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Создать обращение", style=discord.ButtonStyle.green, custom_id="support:create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.create_or_get_ticket(interaction)


class CloseTicketView(discord.ui.View):
    def __init__(self, cog: "SupportShopCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Закрыть обращение", style=discord.ButtonStyle.danger, custom_id="support:close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.close_ticket_from_button(interaction)


class ShopPanelView(discord.ui.View):
    def __init__(self, cog: "SupportShopCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        options = [
            discord.SelectOption(
                label=service.title[:100],
                value=service.key,
                description=(f"{service.price} • {'СКОРО' if service.coming_soon else 'Доступно'}")[:100],
            )
            for service in SHOP_SERVICES
        ]
        self.add_item(ShopServiceSelect(cog, options))


class ShopServiceSelect(discord.ui.Select):
    def __init__(self, cog: "SupportShopCog", options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="Выберите услугу",
            min_values=1,
            max_values=1,
            custom_id="shop:select_service",
            options=options,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        service = SERVICES_BY_KEY.get(self.values[0])
        if not service:
            await interaction.response.send_message("Не удалось определить услугу. Попробуйте снова.", ephemeral=True)
            return
        await self.cog.process_shop_selection(interaction, service)


class SupportShopCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @staticmethod
    def _is_admin(member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        perms = member.guild_permissions
        return perms.administrator or perms.manage_guild

    def _build_support_panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Техническая поддержка",
            description="Нажмите кнопку ниже, чтобы создать приватное обращение в поддержку.",
            color=discord.Color.blurple(),
        )

    def _build_shop_embed(self) -> discord.Embed:
        available = [service for service in SHOP_SERVICES if not service.coming_soon]
        coming_soon = [service for service in SHOP_SERVICES if service.coming_soon]

        available_lines: list[str] = []
        for service in available:
            available_lines.append(f"• {service.title} — {service.price}")
            if service.description:
                available_lines.append(indent_lines(service.description, 2))

        coming_soon_lines = [f"• {service.title} — {service.price}" for service in coming_soon]

        how_to_order = "\n".join(
            [
                "1. Выберите услугу через меню ниже.",
                "2. Бот создаст приватное обращение.",
                "3. Администратор выдаст реквизиты.",
                "4. После оплаты отправьте чек в тикет.",
                "5. Срок выполнения: обычно от 5 минут до 24 часов.",
            ]
        )
        important = "\n".join(
            [
                "• Оплата только на карту администраторам, без посредников.",
                "• Суббота и воскресенье — выходные дни.",
                "• Возврат невозможен после оказания услуги, кроме случая, если услуга не была выдана.",
                "• Администрация может отказать в услуге без объяснения причин.",
                "• Цены могут меняться.",
                "• /op выдаётся только после личной проверки и согласования.",
            ]
        )

        embed = discord.Embed(
            title="Услуги Discord",
            description="Выберите услугу ниже. После выбора бот создаст приватное обращение, где администрация уточнит детали и выдаст реквизиты.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Доступно", value=indent_lines("\n".join(available_lines), 2), inline=False)
        embed.add_field(name="Скоро", value=indent_lines("\n".join(coming_soon_lines), 2), inline=False)
        embed.add_field(name="Как оплатить и заказать", value=indent_lines(how_to_order, 2), inline=False)
        embed.add_field(name="Важно", value=indent_lines(important, 2), inline=False)
        return embed

    async def _find_support_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        if not self.bot.settings.support_category_id:
            return None
        category = guild.get_channel(self.bot.settings.support_category_id)
        if isinstance(category, discord.CategoryChannel):
            return category
        try:
            fetched = await self.bot.fetch_channel(self.bot.settings.support_category_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.CategoryChannel) else None

    def _admin_role(self, guild: discord.Guild) -> discord.Role | None:
        if not self.bot.settings.support_admin_role_id:
            return None
        role = guild.get_role(self.bot.settings.support_admin_role_id)
        return role

    async def _send_ticket_intro(self, channel: discord.TextChannel, user: discord.Member) -> None:
        embed = discord.Embed(
            title="Обращение создано",
            description="Опишите проблему или вопрос. Администрация ответит здесь.",
            color=discord.Color.green(),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Пользователь", value=user.mention, inline=False)
        embed.add_field(name="Дата создания", value=discord.utils.format_dt(datetime.now(UTC), style="F"), inline=False)
        await channel.send(content=f"{user.mention}", embed=embed, view=CloseTicketView(self))

    async def _safe_reply(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral)

    async def _get_open_ticket_channel(self, guild: discord.Guild, user_id: int) -> tuple[SupportTicket | None, discord.TextChannel | None]:
        if not self.bot.db:
            return None, None
        ticket = await self.bot.support_tickets.get_active_by_user(self.bot.db, guild.id, user_id)
        if not ticket:
            return None, None
        channel = guild.get_channel(ticket.channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(ticket.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.bot.support_tickets.close_ticket(self.bot.db, guild.id, ticket.channel_id, closed_by=0)
                return None, None
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if not isinstance(channel, discord.TextChannel):
            return None, None
        return ticket, channel

    async def create_or_get_ticket(self, interaction: discord.Interaction, *, announce: bool = True) -> discord.TextChannel | None:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member) or not self.bot.db:
            await self._safe_reply(interaction, "Команда доступна только на сервере.")
            return None

        existing_ticket, existing_channel = await self._get_open_ticket_channel(guild, user.id)
        if existing_ticket and existing_channel:
            if announce:
                await self._safe_reply(interaction, f"У вас уже есть открытое обращение: {existing_channel.mention}")
            return existing_channel

        category = await self._find_support_category(guild)
        if category is None:
            await self._safe_reply(
                interaction,
                "Не настроена категория поддержки. Укажите SUPPORT_CATEGORY_ID и убедитесь, что бот видит эту категорию.",
            )
            return None

        admin_role = self._admin_role(guild)
        if admin_role is None:
            await self._safe_reply(
                interaction,
                "Не настроена роль администрации поддержки. Укажите SUPPORT_ADMIN_ROLE_ID.",
            )
            return None

        me = guild.me
        if me is None:
            await self._safe_reply(interaction, "Не удалось определить бота в контексте сервера.")
            return None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            admin_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=True,
            ),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
            ),
        }

        channel_name = f"ticket-{user.id}"
        try:
            channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites, reason=f"Support ticket for {user} ({user.id})")
        except discord.Forbidden:
            logger.exception("Missing permissions while creating support channel")
            await self._safe_reply(interaction, "Не удалось создать канал: боту не хватает прав.")
            return None
        except discord.HTTPException:
            logger.exception("HTTP error while creating support channel")
            await self._safe_reply(interaction, "Ошибка Discord API при создании обращения. Попробуйте позже.")
            return None

        await self.bot.support_tickets.create_ticket(self.bot.db, guild.id, user.id, channel.id)
        await self._send_ticket_intro(channel, user)
        if announce:
            await self._safe_reply(interaction, f"Обращение создано: {channel.mention}")
        return channel

    async def close_ticket_from_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        member = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel) or not isinstance(member, discord.Member) or not self.bot.db:
            await self._safe_reply(interaction, "Закрытие обращения доступно только в тикет-канале.")
            return

        ticket = await self.bot.support_tickets.get_active_by_channel(self.bot.db, guild.id, channel.id)
        if not ticket:
            await self._safe_reply(interaction, "Это обращение уже закрыто или не найдено.")
            return

        if member.id != ticket.user_id and not self._is_admin(member):
            await self._safe_reply(interaction, "Вы не можете закрыть это обращение.")
            return

        await self._safe_reply(interaction, "Обращение закрывается…")

        await self.bot.support_tickets.close_ticket(self.bot.db, guild.id, channel.id, member.id)
        await self._log_ticket_close(guild, channel, ticket.user_id, member.id)

        try:
            await channel.delete(reason=f"Ticket closed by {member} ({member.id})")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to delete ticket channel %s", channel.id)

    async def _log_ticket_close(self, guild: discord.Guild, channel: discord.TextChannel, ticket_user_id: int, closed_by_id: int) -> None:
        log_channel_id = self.bot.settings.support_log_channel_id
        if not log_channel_id:
            return

        target = guild.get_channel(log_channel_id)
        if not isinstance(target, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(log_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Support log channel %s not found", log_channel_id)
                return
            target = fetched if isinstance(fetched, discord.TextChannel) else None

        if not isinstance(target, discord.TextChannel):
            return

        embed = discord.Embed(title="Тикет закрыт", color=discord.Color.red(), timestamp=datetime.now(UTC))
        embed.add_field(name="Автор тикета", value=f"<@{ticket_user_id}> ({ticket_user_id})", inline=False)
        embed.add_field(name="Закрыл", value=f"<@{closed_by_id}> ({closed_by_id})", inline=False)
        embed.add_field(name="Канал", value=f"{channel.name} ({channel.id})", inline=False)
        try:
            await target.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to send support close log")

    async def process_shop_selection(self, interaction: discord.Interaction, service: ShopService) -> None:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await self._safe_reply(interaction, "Эта панель работает только на сервере.")
            return
        if not self.bot.settings.shop_requests_to_support:
            await self._safe_reply(interaction, "Приём заявок из магазина временно отключён.")
            return

        ticket_channel = await self.create_or_get_ticket(interaction, announce=False)
        if not isinstance(ticket_channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title="Заявка на услугу",
            color=discord.Color.orange(),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Пользователь", value=user.mention, inline=False)
        embed.add_field(name="Услуга", value=service.title, inline=False)
        embed.add_field(name="Цена", value=service.price, inline=True)
        if service.coming_soon:
            embed.add_field(name="Статус", value="Скоро / требуется согласование", inline=True)
        else:
            embed.add_field(name="Статус", value="Доступно", inline=True)
        if service.description:
            embed.add_field(name="Описание", value=service.description, inline=False)
        embed.add_field(
            name="Инструкция для администратора",
            value="Уточните детали, выдайте реквизиты и подтвердите сроки выполнения.",
            inline=False,
        )

        try:
            await ticket_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to send service request into ticket channel %s", ticket_channel.id)
            await self._safe_reply(interaction, "Не удалось отправить заявку в тикет. Попробуйте позже.")
            return

        await self._safe_reply(interaction, f"Заявка создана: {ticket_channel.mention}")

    @app_commands.command(name="support_panel", description="Отправить панель технической поддержки")
    @app_commands.default_permissions(manage_guild=True)
    async def support_panel(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not self._is_admin(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        await interaction.response.send_message(embed=self._build_support_panel_embed(), view=SupportPanelView(self))

    @app_commands.command(name="shop_panel", description="Отправить панель магазина услуг")
    @app_commands.default_permissions(manage_guild=True)
    async def shop_panel(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not self._is_admin(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        await interaction.response.send_message(embed=self._build_shop_embed(), view=ShopPanelView(self))


async def setup(bot: MovieBot) -> None:
    cog = SupportShopCog(bot)
    await bot.add_cog(cog)
    bot.add_view(SupportPanelView(cog))
    bot.add_view(ShopPanelView(cog))
    bot.add_view(CloseTicketView(cog))

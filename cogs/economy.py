from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.leaderboard_image import (
    LeaderboardImageRow,
    make_leaderboard_file,
    make_profile_file,
    resolve_avatar_bytes,
    resolve_display_name,
)

logger = logging.getLogger(__name__)


def interaction_key(interaction: discord.Interaction, suffix: str = "") -> str:
    return f"discord:{interaction.id}{':' + suffix if suffix else ''}"


def lines_embed(title: str, lines: list[str], *, color: int = 0xC13CFF) -> discord.Embed:
    return discord.Embed(title=title, description="\n".join(lines)[:4000] or "Пока пусто.", color=color)


class EconomyCog(commands.Cog):
    economy_group = app_commands.Group(name="economy", description="Серверная экономика")
    bank_group = app_commands.Group(name="bank", description="Банк серверной экономики")
    inventory_group = app_commands.Group(name="inventory", description="Предметы пользователя")
    shop_group = app_commands.Group(name="shop", description="Магазин предметов")
    market_group = app_commands.Group(name="market", description="Рынок между участниками")
    quest_group = app_commands.Group(name="quest", description="Экономические задания")
    event_group = app_commands.Group(name="event", description="Сезонное событие сервера")
    admin_group = app_commands.Group(name="economy_admin", description="Управление серверной экономикой", default_permissions=discord.Permissions(administrator=True))

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _guild_db(self, interaction: discord.Interaction) -> tuple[discord.Guild, object] | None:
        if interaction.guild is None or self.bot.economy_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return None
        return interaction.guild, self.bot.economy_db

    async def _action(self, interaction: discord.Interaction, action: str) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.claim_action(db, guild.id, interaction.user.id, action, idempotency_key=interaction_key(interaction))
        if result.ok and self.bot.progression_db is not None:
            await self.bot.progression.record_event(
                self.bot.progression_db, guild.id, interaction.user.id, "economy_action", 1,
                f"progression:{interaction.id}:{action}", metadata={"action": action},
            )
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @economy_group.command(name="profile", description="Показать экономический профиль")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        target = user or interaction.user
        await interaction.response.defer()
        wallet = await self.bot.economy.wallet(db, guild.id, target.id)
        avatar = await resolve_avatar_bytes(self.bot, guild, target.id)
        xp = int(wallet["xp"])
        level = int(wallet["level"])
        lower = (level - 1) ** 2 * 250
        upper = level**2 * 250
        progress = (xp - lower) / max(upper - lower, 1)
        file = make_profile_file(
            target.display_name,
            "Экономический профиль",
            (("Кошелёк", str(wallet["balance"])), ("Банк", str(wallet["bank"])), ("Уровень", str(level)), ("XP", str(xp))),
            avatar=avatar,
            progress=progress,
            theme="economy",
            filename="economy_profile.png",
            description=f"Экономический профиль {target.display_name}",
        )
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    @economy_group.command(name="daily", description="Получить ежедневную награду")
    async def daily(self, interaction: discord.Interaction) -> None:
        await self._action(interaction, "daily")

    @economy_group.command(name="work", description="Поработать и получить монеты")
    async def work(self, interaction: discord.Interaction) -> None:
        await self._action(interaction, "work")

    @economy_group.command(name="crime", description="Рискнуть монетами ради крупной награды")
    async def crime(self, interaction: discord.Interaction) -> None:
        await self._action(interaction, "crime")

    @economy_group.command(name="mine", description="Отправиться за добычей")
    async def mine(self, interaction: discord.Interaction) -> None:
        await self._action(interaction, "mine")

    @economy_group.command(name="fish", description="Отправиться на рыбалку")
    async def fish(self, interaction: discord.Interaction) -> None:
        await self._action(interaction, "fish")

    @economy_group.command(name="transfer", description="Перевести монеты участнику")
    async def transfer(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.transfer(db, guild.id, interaction.user.id, user.id, int(amount), idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok, allowed_mentions=discord.AllowedMentions.none())

    @economy_group.command(name="cooldowns", description="Показать активные cooldown")
    async def cooldowns(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        values = await self.bot.economy.cooldowns(db, guild.id, interaction.user.id)
        lines = [f"**{name}** — {seconds // 60 + 1} мин." for name, seconds in values.items()]
        await interaction.response.send_message(embed=lines_embed("Cooldown", lines), ephemeral=True)

    @bank_group.command(name="deposit", description="Положить монеты в банк")
    async def deposit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.bank_move(db, guild.id, interaction.user.id, int(amount), deposit=True, idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @bank_group.command(name="withdraw", description="Снять монеты из банка")
    async def withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.bank_move(db, guild.id, interaction.user.id, int(amount), deposit=False, idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @inventory_group.command(name="view", description="Показать инвентарь")
    async def inventory_view(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        target = user or interaction.user
        rows = await self.bot.economy.inventory(db, guild.id, target.id)
        lines = [f"`#{row['item_id']}` **{row['name']}** ×{row['quantity']} — {row['description']}" for row in rows]
        await interaction.response.send_message(embed=lines_embed(f"Инвентарь — {target.display_name}", lines), ephemeral=user is None)

    @inventory_group.command(name="use", description="Использовать предмет")
    async def inventory_use(self, interaction: discord.Interaction, item_id: int) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.use_item(db, guild.id, interaction.user.id, item_id)
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @inventory_group.command(name="gift", description="Передать предмет участнику")
    async def inventory_gift(self, interaction: discord.Interaction, user: discord.Member, item_id: int, quantity: app_commands.Range[int, 1, 100]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.gift_item(db, guild.id, interaction.user.id, user.id, item_id, int(quantity))
        await interaction.response.send_message(result.message, ephemeral=not result.ok, allowed_mentions=discord.AllowedMentions.none())

    @shop_group.command(name="browse", description="Показать каталог магазина")
    async def shop_browse(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        _, db = context
        rows = await self.bot.economy.shop_items(db)
        lines = [f"`#{row['item_id']}` **{row['name']}** — {row['buy_price']} монет\n{row['description']}" for row in rows]
        await interaction.response.send_message(embed=lines_embed("Магазин Vulgarities", lines))

    @shop_group.command(name="buy", description="Купить предмет")
    async def shop_buy(self, interaction: discord.Interaction, item_id: int, quantity: app_commands.Range[int, 1, 100] = 1) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.shop_buy(db, guild.id, interaction.user.id, item_id, int(quantity), idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @shop_group.command(name="sell", description="Продать предмет магазину")
    async def shop_sell(self, interaction: discord.Interaction, item_id: int, quantity: app_commands.Range[int, 1, 100] = 1) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.shop_sell(db, guild.id, interaction.user.id, item_id, int(quantity), idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @market_group.command(name="list", description="Выставить предмет на рынок")
    async def market_create(self, interaction: discord.Interaction, item_id: int, quantity: app_commands.Range[int, 1, 100], price: app_commands.Range[int, 1, 1_000_000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.market_create(db, guild.id, interaction.user.id, item_id, int(quantity), int(price))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @market_group.command(name="search", description="Показать активные лоты")
    async def market_search(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        rows = await self.bot.economy.market_list(db, guild.id)
        lines = [f"`#{row['listing_id']}` **{row['name']}** ×{row['quantity']} — {row['price']} монет · <@{row['seller_id']}>" for row in rows]
        await interaction.response.send_message(embed=lines_embed("Рынок", lines), allowed_mentions=discord.AllowedMentions.none())

    @market_group.command(name="buy", description="Купить лот")
    async def market_buy(self, interaction: discord.Interaction, listing_id: int) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.market_buy(db, guild.id, interaction.user.id, listing_id, idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @market_group.command(name="cancel", description="Снять свой лот")
    async def market_cancel(self, interaction: discord.Interaction, listing_id: int) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.market_cancel(db, guild.id, interaction.user.id, listing_id)
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @quest_group.command(name="list", description="Показать задания")
    async def quest_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=lines_embed("Задания", ["`earn` — получить минимум 3 положительные экономические награды", "`social` — выполнить минимум 5 переводов или получить их"]), ephemeral=True)

    @quest_group.command(name="claim", description="Забрать награду за задание")
    async def quest_claim(self, interaction: discord.Interaction, quest: str) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.claim_basic_quest(db, guild.id, interaction.user.id, quest.lower())
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @event_group.command(name="view", description="Показать активное событие")
    async def event_view(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        event = await self.bot.economy.active_event(db, guild.id)
        if event is None:
            await interaction.response.send_message("Сейчас нет активного события.", ephemeral=True)
            return
        ends = datetime.fromisoformat(str(event["ends_at"]))
        await interaction.response.send_message(embed=lines_embed(str(event["name"]), [f"Завершится: {discord.utils.format_dt(ends, style='R')}", "Используйте `/event collect`, чтобы собирать жетоны."]))

    @event_group.command(name="collect", description="Собрать сезонные жетоны")
    async def event_collect(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        result = await self.bot.economy.collect_event(db, guild.id, interaction.user.id, idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=not result.ok)

    @event_group.command(name="leaderboard", description="Топ сезонного события")
    async def event_leaderboard(self, interaction: discord.Interaction) -> None:
        await self.event_top(interaction)

    async def event_top(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        rows = await self.bot.economy.event_top(db, guild.id)
        if not rows:
            await interaction.response.send_message("Пока нет активного события или данных.", ephemeral=True)
            return
        await interaction.response.defer()
        cards: list[LeaderboardImageRow] = []
        for row in rows:
            user_id = int(row["user_id"])
            cards.append(LeaderboardImageRow(
                name=await resolve_display_name(self.bot, guild, user_id),
                primary=f"Уровень события {row['level']}",
                secondary=f"{row['tokens']} жетонов",
                value=int(row["tokens"]),
                avatar=await resolve_avatar_bytes(self.bot, guild, user_id),
            ))
        file = make_leaderboard_file("ТОП СОБЫТИЯ", cards, filename="event_top.png", theme="events")
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    @admin_group.command(name="status", description="Статистика экономики")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_status(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        cursor = await db.execute("SELECT COUNT(*) AS users, COALESCE(SUM(balance+bank),0) AS supply FROM economy_wallets WHERE guild_id=?", (guild.id,))
        row = await cursor.fetchone()
        await interaction.response.send_message(f"Кошельков: {row['users']} · Денежная масса: {row['supply']}", ephemeral=True)

    @admin_group.command(name="grant", description="Выдать или отозвать монеты")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_grant(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, -1_000_000, 1_000_000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        if int(amount) == 0:
            await interaction.response.send_message("Сумма не может быть нулевой.", ephemeral=True)
            return
        result = await self.bot.economy.change_balance(db, guild.id, user.id, int(amount), reason="admin_grant", reference=str(interaction.user.id), idempotency_key=interaction_key(interaction))
        await interaction.response.send_message(result.message, ephemeral=True)

    @admin_group.command(name="item", description="Выдать или отозвать предмет")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_item(self, interaction: discord.Interaction, user: discord.Member, item_id: int, quantity: app_commands.Range[int, -1000, 1000]) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        if int(quantity) == 0:
            await interaction.response.send_message("Количество не может быть нулевым.", ephemeral=True)
            return
        async with self.bot.economy._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                changed = await self.bot.economy.add_item(db, guild.id, user.id, item_id, int(quantity))
                if not changed:
                    await db.rollback()
                    await interaction.response.send_message("Недостаточно предметов для отзыва.", ephemeral=True)
                    return
                inserted = await self.bot.economy._ledger(
                    db, guild.id, user.id, 0, 0, "admin_item",
                    reference=f"{item_id}:{int(quantity)}:by:{interaction.user.id}",
                    idempotency_key=interaction_key(interaction),
                )
                if not inserted:
                    await db.rollback()
                    await interaction.response.send_message("Эта операция уже была обработана.", ephemeral=True)
                    return
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        await interaction.response.send_message("Инвентарь обновлён, действие записано в журнал.", ephemeral=True)

    @admin_group.command(name="event_start", description="Запустить серверное событие")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_event_start(self, interaction: discord.Interaction, name: str, days: app_commands.Range[int, 1, 90] = 30) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        event_id = await self.bot.economy.start_event(db, guild.id, name, int(days))
        await self.bot.economy.change_balance(
            db, guild.id, interaction.user.id, 0, reason="admin_event_start",
            reference=str(event_id), idempotency_key=interaction_key(interaction),
        )
        await interaction.response.send_message(f"Событие #{event_id} «{name[:80]}» запущено на {days} дн.", ephemeral=True)

    @admin_group.command(name="ledger", description="Последние операции пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_ledger(self, interaction: discord.Interaction, user: discord.Member) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        cursor = await db.execute("SELECT amount, bank_amount, reason, created_at FROM economy_transactions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 20", (guild.id, user.id))
        rows = await cursor.fetchall()
        lines = [f"{row['created_at'][:16]} · `{row['reason']}` · {int(row['amount']):+d} / bank {int(row['bank_amount']):+d}" for row in rows]
        await interaction.response.send_message(embed=lines_embed(f"Журнал — {user.display_name}", lines), ephemeral=True)

    async def economy_top(self, interaction: discord.Interaction) -> None:
        context = await self._guild_db(interaction)
        if context is None:
            return
        guild, db = context
        rows = await self.bot.economy.top(db, guild.id)
        if not rows:
            await interaction.response.send_message("Пока нет данных экономики.", ephemeral=True)
            return
        await interaction.response.defer()
        cards: list[LeaderboardImageRow] = []
        for row in rows:
            user_id = int(row["user_id"])
            cards.append(LeaderboardImageRow(
                name=await resolve_display_name(self.bot, guild, user_id),
                primary=f"Уровень {row['level']}",
                secondary=f"{row['balance']} + {row['bank']} банк",
                value=int(row["wealth"]),
                avatar=await resolve_avatar_bytes(self.bot, guild, user_id),
            ))
        file = make_leaderboard_file("ТОП ЭКОНОМИКИ", cards, filename="economy_top.png", theme="economy")
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(EconomyCog(bot))

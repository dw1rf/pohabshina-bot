from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.social_game_service import utcnow_iso
from services.community_ops_service import normalize_user_text
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, resolve_avatar_bytes, resolve_display_name

logger = logging.getLogger(__name__)


class ClubCreateModal(discord.ui.Modal, title="Создать клуб"):
    name = discord.ui.TextInput(label="Название клуба", max_length=40, default="Ночной клуб")

    def __init__(self, cog: "ClubCog", owner_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.cog.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего клуба.", ephemeral=True)
            return
        await self.cog.ensure_club(interaction.guild.id, self.owner_id, normalize_user_text(str(self.name.value), max_length=40) or "Ночной клуб")
        await self.cog.refresh_menu(interaction)


class ClubRenameModal(discord.ui.Modal, title="Переименовать клуб"):
    name = discord.ui.TextInput(label="Новое название", max_length=40)

    def __init__(self, cog: "ClubCog", owner_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.cog.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего клуба.", ephemeral=True)
            return
        await self.cog.rename_club(interaction.guild.id, self.owner_id, normalize_user_text(str(self.name.value), max_length=40) or "Ночной клуб")
        await self.cog.refresh_menu(interaction)


class ClubMenuView(discord.ui.View):
    def __init__(self, cog: "ClubCog", owner_id: int, *, has_club: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        if has_club:
            self.remove_item(self.create)
        else:
            for item in (self.collect, self.upgrade, self.staff, self.daily, self.rename, self.refresh):
                self.remove_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего клуба.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🏗️ Создать клуб", style=discord.ButtonStyle.success, row=0)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.cog.has_club(interaction):
            await self.cog.refresh_menu(interaction)
            return
        await interaction.response.send_modal(ClubCreateModal(self.cog, self.owner_id))

    @discord.ui.button(label="💰 Собрать доход", style=discord.ButtonStyle.primary, row=0)
    async def collect(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.collect_income(interaction)

    @discord.ui.button(label="⬆️ Улучшить клуб", style=discord.ButtonStyle.primary, row=0)
    async def upgrade(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.upgrade_club(interaction)

    @discord.ui.button(label="👥 Нанять персонал", style=discord.ButtonStyle.primary, row=1)
    async def staff(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.hire_staff(interaction)

    @discord.ui.button(label="🎁 Ежедневный бонус", style=discord.ButtonStyle.success, row=1)
    async def daily(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.claim_daily(interaction)

    @discord.ui.button(label="✏️ Переименовать", style=discord.ButtonStyle.secondary, row=1)
    async def rename(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.has_club(interaction):
            await self.cog.refresh_menu(interaction)
            return
        await interaction.response.send_modal(ClubRenameModal(self.cog, self.owner_id))

    @discord.ui.button(label="🏆 Топ", style=discord.ButtonStyle.secondary, row=2)
    async def top(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            payload = await self.cog.top_payload(interaction)
        except Exception:
            logger.exception("Failed to generate clubs top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return
        if payload is None:
            await interaction.followup.send("Пока нет данных для топа.", ephemeral=True)
            return
        file = payload
        await interaction.edit_original_response(
            embed=None,
            view=ClubBackView(self.cog, self.owner_id),
            attachments=[file],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)

    @discord.ui.button(label="❓ Помощь", style=discord.ButtonStyle.secondary, row=2)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="❓ Помощь по клубу",
            description="Создай клуб, собирай доход раз в 4 часа, нанимай персонал и улучшай уровень. Всё сохраняется в БД.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=ClubBackView(self.cog, self.owner_id))


class ClubBackView(discord.ui.View):
    def __init__(self, cog: "ClubCog", owner_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего клуба.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.primary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)


class ClubCog(commands.Cog):
    community_group = app_commands.Group(name="community", description="Участники и банк клубного сообщества")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def club_row(self, guild_id: int, owner_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM club_profiles WHERE guild_id=? AND owner_id=?", (guild_id, owner_id))
        return await cur.fetchone()

    async def has_club(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.guild and await self.club_row(interaction.guild.id, interaction.user.id))

    async def ensure_club(self, guild_id: int, owner_id: int, name: str | None = None):
        assert self.bot.db is not None
        now = utcnow_iso()
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO club_profiles (guild_id, owner_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, owner_id, (name or "Ночной клуб")[:40], now, now),
        )
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO club_communities(guild_id, owner_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, owner_id, (name or "Ночной клуб")[:40], now, now),
        )
        cur = await self.bot.db.execute("SELECT club_id FROM club_communities WHERE guild_id=? AND owner_id=?", (guild_id, owner_id))
        community = await cur.fetchone()
        if community:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO club_members(club_id, guild_id, user_id, role, joined_at) VALUES (?, ?, ?, 'leader', ?)",
                (community["club_id"], guild_id, owner_id, now),
            )
        await self.bot.db.commit()
        return await self.club_row(guild_id, owner_id)

    async def rename_club(self, guild_id: int, owner_id: int, name: str) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute("UPDATE club_profiles SET name=?, updated_at=? WHERE guild_id=? AND owner_id=?", (name[:40], utcnow_iso(), guild_id, owner_id))
        await self.bot.db.execute("UPDATE club_communities SET name=?, updated_at=? WHERE guild_id=? AND owner_id=?", (name[:40], utcnow_iso(), guild_id, owner_id))
        await self.bot.db.commit()

    def income(self, club) -> int:
        return 25 * int(club["level"]) + 10 * int(club["staff"]) + 8 * int(club["interior_level"]) + 6 * int(club["ads_level"])

    def club_embed(self, member: discord.abc.User, club) -> discord.Embed:
        if not club:
            return discord.Embed(title="🌃 Клуб", description="У тебя пока нет клуба. Создай его кнопкой ниже.", color=discord.Color.dark_purple())
        last_collect = club["last_work_at"] or "ещё не собирался"
        embed = discord.Embed(
            title=f"🌃 Клуб: {club['name']}",
            description=(
                f"Владелец: {member.mention}\n"
                f"Уровень: {club['level']}\n"
                f"Баланс: {club['coins_earned']} coins\n"
                f"Доход: {self.income(club)} coins\n"
                f"Персонал: {club['staff']}\n"
                f"Интерьер: {club['interior_level']}\n"
                f"Реклама: {club['ads_level']}\n"
                f"Последний сбор: {last_collect}"
            ),
            color=discord.Color.dark_purple(),
        )
        return embed

    async def refresh_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        embed = self.club_embed(interaction.user, club)
        view = ClubMenuView(self, interaction.user.id, has_club=bool(club))
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view, attachments=[])
        else:
            await interaction.response.edit_message(embed=embed, view=view, attachments=[])

    async def collect_income(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        if not club:
            await self.refresh_menu(interaction)
            return
        if club["last_work_at"]:
            try:
                last = datetime.fromisoformat(club["last_work_at"])
                if datetime.now(UTC) - last < timedelta(hours=4):
                    await interaction.response.send_message("Доход можно собирать раз в 4 часа.", ephemeral=True)
                    return
            except ValueError:
                pass
        amount = self.income(club)
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned+?, xp=xp+?, last_work_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (amount, 10, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.execute("INSERT INTO club_transactions (guild_id, owner_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, amount, "work", utcnow_iso()))
        await self.bot.db.commit()
        if self.bot.progression_db is not None:
            await self.bot.progression.record_event(
                self.bot.progression_db, interaction.guild.id, interaction.user.id, "club_contribution", 1,
                f"club-income:{interaction.id}", metadata={"coins": amount},
            )
        await self.refresh_menu(interaction)

    async def upgrade_club(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        if not club:
            await self.refresh_menu(interaction)
            return
        cost = (int(club["level"]) + 1) * 100
        if int(club["coins_earned"]) < cost:
            await interaction.response.send_message(f"Нужно {cost} coins баланса клуба для улучшения.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned-?, level=level+1, interior_level=interior_level+1, ads_level=ads_level+1, updated_at=? WHERE guild_id=? AND owner_id=?", (cost, utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.execute("UPDATE club_communities SET skill_level=skill_level+1, updated_at=? WHERE guild_id=? AND owner_id=?", (utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        if self.bot.progression_db is not None:
            await self.bot.progression.record_event(
                self.bot.progression_db, interaction.guild.id, interaction.user.id, "club_contribution", 5,
                f"club-upgrade:{interaction.id}",
            )
        await self.refresh_menu(interaction)

    async def hire_staff(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        if not club:
            await self.refresh_menu(interaction)
            return
        cost = (int(club["staff"]) + 1) * 80
        if int(club["coins_earned"]) < cost:
            await interaction.response.send_message(f"Нужно {cost} coins баланса клуба для найма.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned-?, staff=staff+1, updated_at=? WHERE guild_id=? AND owner_id=?", (cost, utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def claim_daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        if not club:
            await self.refresh_menu(interaction)
            return
        today = datetime.now(UTC).date().isoformat()
        if club["last_daily_at"] and club["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневный бонус уже получен.", ephemeral=True)
            return
        amount = 50 + int(club["level"]) * 10
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned+?, last_daily_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (amount, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.execute("INSERT INTO club_transactions (guild_id, owner_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, amount, "daily", utcnow_iso()))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def top_payload(self, interaction: discord.Interaction) -> discord.File | None:
        assert interaction.guild is not None and self.bot.db is not None
        cur = await self.bot.db.execute("SELECT owner_id, name, level, coins_earned FROM club_profiles WHERE guild_id=? ORDER BY level DESC, coins_earned DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        if not rows:
            return None

        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            level = int(row["level"])
            coins = int(row["coins_earned"])
            owner_name = await resolve_display_name(self.bot, interaction.guild, int(row["owner_id"]), max_len=34)
            avatar = await resolve_avatar_bytes(self.bot, interaction.guild, int(row["owner_id"]))
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=str(row["name"]),
                    primary=f"Владелец: {owner_name}",
                    secondary=f"Уровень {level}  {coins} coins",
                    value=level * 1000 + coins,
                    avatar=avatar,
                )
            )

        filename = "clubs_top.png"
        return make_leaderboard_file("ТОП КЛУБОВ", leaderboard_rows, filename=filename, theme="clubs")

    @app_commands.command(name="club", description="Открыть меню клуба")
    async def club(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=self.club_embed(interaction.user, club), view=ClubMenuView(self, interaction.user.id, has_club=bool(club)), ephemeral=True)

    async def _member_community(self, guild_id: int, user_id: int):
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            """SELECT c.*, m.role FROM club_members m JOIN club_communities c ON c.club_id=m.club_id
               WHERE m.guild_id=? AND m.user_id=?""",
            (guild_id, user_id),
        )
        return await cursor.fetchone()

    @community_group.command(name="profile", description="Показать клубное сообщество")
    async def community_profile(self, interaction: discord.Interaction, owner: discord.Member | None = None) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if owner:
            cursor = await self.bot.db.execute("SELECT *, 'leader' AS role FROM club_communities WHERE guild_id=? AND owner_id=?", (interaction.guild.id, owner.id))
            community = await cursor.fetchone()
        else:
            community = await self._member_community(interaction.guild.id, interaction.user.id)
        if community is None:
            await interaction.response.send_message("Клубное сообщество не найдено.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT COUNT(*) AS total FROM club_members WHERE club_id=?", (community["club_id"],))
        members = int((await cursor.fetchone())["total"])
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"🌃 {community['name']}",
                description=f"Лидер: <@{community['owner_id']}>\nУчастников: **{members}**\nБанк: **{community['bank']}**\nНавык клуба: **{community['skill_level']}**",
                color=discord.Color.dark_purple(),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @community_group.command(name="apply", description="Подать заявку в клуб участника")
    async def community_apply(self, interaction: discord.Interaction, owner: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if await self._member_community(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Ты уже состоишь в клубе.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT club_id, name FROM club_communities WHERE guild_id=? AND owner_id=?", (interaction.guild.id, owner.id))
        community = await cursor.fetchone()
        if community is None:
            await interaction.response.send_message("У этого участника нет клуба.", ephemeral=True)
            return
        now = utcnow_iso()
        await self.bot.db.execute(
            """INSERT INTO club_applications(club_id, user_id, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)
               ON CONFLICT(club_id, user_id) DO UPDATE SET status='pending', updated_at=excluded.updated_at""",
            (community["club_id"], interaction.user.id, now, now),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"Заявка в **{community['name']}** отправлена.", ephemeral=True)

    @community_group.command(name="accept", description="Принять заявку в свой клуб")
    async def community_accept(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT club_id FROM club_communities WHERE guild_id=? AND owner_id=?", (interaction.guild.id, interaction.user.id))
        community = await cursor.fetchone()
        if community is None:
            await interaction.response.send_message("Только лидер клуба может принимать заявки.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT status FROM club_applications WHERE club_id=? AND user_id=?", (community["club_id"], user.id))
        application = await cursor.fetchone()
        if application is None or application["status"] != "pending":
            await interaction.response.send_message("Активная заявка не найдена.", ephemeral=True)
            return
        try:
            await self.bot.db.execute(
                "INSERT INTO club_members(club_id, guild_id, user_id, role, joined_at) VALUES (?, ?, ?, 'member', ?)",
                (community["club_id"], interaction.guild.id, user.id, utcnow_iso()),
            )
        except Exception:
            await self.bot.db.rollback()
            await interaction.response.send_message("Участник уже состоит в другом клубе.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE club_applications SET status='accepted', updated_at=? WHERE club_id=? AND user_id=?", (utcnow_iso(), community["club_id"], user.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"{user.mention} принят в клуб.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

    async def _club_bank_move(self, interaction: discord.Interaction, amount: int, *, deposit: bool) -> str:
        assert interaction.guild is not None and self.bot.economy_db is not None
        community = await self._member_community(interaction.guild.id, interaction.user.id)
        if community is None:
            return "Ты не состоишь в клубе."
        if not deposit and community["role"] != "leader":
            return "Снимать монеты может только лидер."
        async with self.bot.economy._lock:
            await self.bot.economy_db.execute("BEGIN IMMEDIATE")
            try:
                await self.bot.economy.ensure_wallet(self.bot.economy_db, interaction.guild.id, interaction.user.id)
                cursor = await self.bot.economy_db.execute("SELECT balance FROM economy_wallets WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
                wallet = await cursor.fetchone()
                if deposit and int(wallet["balance"]) < amount:
                    await self.bot.economy_db.rollback()
                    return "Недостаточно монет в кошельке."
                if not deposit and int(community["bank"]) < amount:
                    await self.bot.economy_db.rollback()
                    return "Недостаточно монет в банке клуба."
                wallet_delta = -amount if deposit else amount
                club_delta = amount if deposit else -amount
                await self.bot.economy_db.execute("UPDATE economy_wallets SET balance=balance+?, updated_at=? WHERE guild_id=? AND user_id=?", (wallet_delta, utcnow_iso(), interaction.guild.id, interaction.user.id))
                await self.bot.economy_db.execute("UPDATE club_communities SET bank=bank+?, updated_at=? WHERE club_id=?", (club_delta, utcnow_iso(), community["club_id"]))
                inserted = await self.bot.economy._ledger(
                    self.bot.economy_db, interaction.guild.id, interaction.user.id, wallet_delta, 0,
                    "club_deposit" if deposit else "club_withdraw", reference=str(community["club_id"]),
                    idempotency_key=f"club-bank:{interaction.id}",
                )
                if not inserted:
                    await self.bot.economy_db.rollback()
                    return "Эта операция уже была обработана."
                await self.bot.economy_db.commit()
            except Exception:
                await self.bot.economy_db.rollback()
                raise
        return "Монеты внесены в банк клуба." if deposit else "Монеты выведены из банка клуба."

    @community_group.command(name="deposit", description="Внести монеты в банк клуба")
    async def community_deposit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(await self._club_bank_move(interaction, int(amount), deposit=True), ephemeral=True)

    @community_group.command(name="withdraw", description="Лидеру: вывести монеты из банка клуба")
    async def community_withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(await self._club_bank_move(interaction, int(amount), deposit=False), ephemeral=True)

    @community_group.command(name="leave", description="Покинуть клуб")
    async def community_leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        community = await self._member_community(interaction.guild.id, interaction.user.id)
        if community is None:
            await interaction.response.send_message("Ты не состоишь в клубе.", ephemeral=True)
            return
        if community["role"] == "leader":
            await interaction.response.send_message("Лидер не может покинуть клуб без передачи управления.", ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM club_members WHERE club_id=? AND user_id=?", (community["club_id"], interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message("Ты покинул клуб.", ephemeral=True)

    @community_group.command(name="role", description="Лидеру: изменить роль участника клуба")
    async def community_role(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: Literal["member", "officer"],
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        community = await self._member_community(interaction.guild.id, interaction.user.id)
        if community is None or community["role"] != "leader":
            await interaction.response.send_message("Только лидер может менять роли.", ephemeral=True)
            return
        cursor = await self.bot.db.execute("SELECT role FROM club_members WHERE club_id=? AND user_id=?", (community["club_id"], user.id))
        member = await cursor.fetchone()
        if member is None or member["role"] == "leader":
            await interaction.response.send_message("Участник не найден или является лидером.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE club_members SET role=? WHERE club_id=? AND user_id=?", (role, community["club_id"], user.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Роль {user.mention} изменена на `{role}`.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

    async def _club_item_move(self, interaction: discord.Interaction, item_id: int, quantity: int, *, deposit: bool) -> str:
        assert interaction.guild is not None and self.bot.economy_db is not None
        skills = await self.bot.progression.get_club_skills(
            self.bot.progression_db, interaction.guild.id, interaction.user.id
        )
        storage_level = next(
            (int(item["level"]) for item in skills if item["skill_key"] == "storage"), 0
        )
        storage_capacity = 100 + storage_level * 100
        community = await self._member_community(interaction.guild.id, interaction.user.id)
        if community is None:
            return "Ты не состоишь в клубе."
        if not deposit and community["role"] not in {"leader", "officer"}:
            return "Брать предметы со склада могут лидер и офицеры."
        async with self.bot.economy._lock:
            await self.bot.economy_db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.bot.economy_db.execute(
                    "SELECT quantity FROM club_community_inventory WHERE club_id=? AND item_id=?",
                    (community["club_id"], item_id),
                )
                stored_row = await cursor.fetchone()
                stored = int(stored_row["quantity"] if stored_row else 0)
                if deposit:
                    total_row = await (await self.bot.economy_db.execute(
                        "SELECT COALESCE(SUM(quantity),0) AS total FROM club_community_inventory WHERE club_id=?",
                        (community["club_id"],),
                    )).fetchone()
                    if int(total_row["total"]) + quantity > storage_capacity:
                        await self.bot.economy_db.rollback()
                        return f"Склад заполнен: вместимость {storage_capacity} предметов."
                    if not await self.bot.economy.add_item(self.bot.economy_db, interaction.guild.id, interaction.user.id, item_id, -quantity):
                        await self.bot.economy_db.rollback()
                        return "Недостаточно предметов в личном инвентаре."
                    if stored_row:
                        await self.bot.economy_db.execute("UPDATE club_community_inventory SET quantity=quantity+? WHERE club_id=? AND item_id=?", (quantity, community["club_id"], item_id))
                    else:
                        await self.bot.economy_db.execute("INSERT INTO club_community_inventory(club_id, item_id, quantity) VALUES (?, ?, ?)", (community["club_id"], item_id, quantity))
                else:
                    if stored < quantity:
                        await self.bot.economy_db.rollback()
                        return "Недостаточно предметов на складе."
                    await self.bot.economy_db.execute("UPDATE club_community_inventory SET quantity=quantity-? WHERE club_id=? AND item_id=?", (quantity, community["club_id"], item_id))
                    await self.bot.economy.add_item(self.bot.economy_db, interaction.guild.id, interaction.user.id, item_id, quantity)
                inserted = await self.bot.economy._ledger(
                    self.bot.economy_db, interaction.guild.id, interaction.user.id, 0, 0,
                    "club_store_deposit" if deposit else "club_store_withdraw",
                    reference=f"{community['club_id']}:{item_id}:{quantity}",
                    idempotency_key=f"club-store:{interaction.id}",
                )
                if not inserted:
                    await self.bot.economy_db.rollback()
                    return "Эта операция уже была обработана."
                await self.bot.economy_db.commit()
            except Exception:
                await self.bot.economy_db.rollback()
                raise
        return "Предметы помещены на склад клуба." if deposit else "Предметы взяты со склада клуба."

    @community_group.command(name="store", description="Положить предметы на склад клуба")
    async def community_store(self, interaction: discord.Interaction, item_id: int, quantity: app_commands.Range[int, 1, 1000]) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(await self._club_item_move(interaction, item_id, int(quantity), deposit=True), ephemeral=True)

    @community_group.command(name="take", description="Лидеру или офицеру: взять предметы со склада")
    async def community_take(self, interaction: discord.Interaction, item_id: int, quantity: app_commands.Range[int, 1, 1000]) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(await self._club_item_move(interaction, item_id, int(quantity), deposit=False), ephemeral=True)

    @community_group.command(name="skills", description="Показать навыки клубного сообщества")
    async def community_skills(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        skills = await self.bot.progression.get_club_skills(
            self.bot.progression_db, interaction.guild.id, interaction.user.id
        )
        if not skills:
            await interaction.response.send_message("Ты не состоишь в клубном сообществе.", ephemeral=True)
            return
        labels = {
            "season_bonus": "Бонус сезонных очков",
            "pet_bonus": "Бонус питомцев",
            "storage": "Вместимость склада",
        }
        await interaction.response.send_message(
            "\n".join(f"**{labels[item['skill_key']]}:** {item['level']} / 5" for item in skills),
            ephemeral=True,
        )

    @community_group.command(name="skill_upgrade", description="Лидеру: улучшить навык клуба")
    async def community_skill_upgrade(
        self,
        interaction: discord.Interaction,
        skill: Literal["season_bonus", "pet_bonus", "storage"],
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        result = await self.bot.progression.upgrade_club_skill(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, skill
        )
        await interaction.response.send_message(
            f"Навык `{skill}` улучшен до {result['level']} уровня. В банке осталось {result['bank']} монет."
            if result else "Улучшение недоступно: нужны права лидера, монеты или достигнут максимум.",
            ephemeral=True,
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ClubCog(bot))

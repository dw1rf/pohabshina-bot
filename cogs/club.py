from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.social_game_service import utcnow_iso


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
        await self.cog.ensure_club(interaction.guild.id, self.owner_id, str(self.name.value).strip() or "Ночной клуб")
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
        await self.cog.rename_club(interaction.guild.id, self.owner_id, str(self.name.value).strip() or "Ночной клуб")
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
        embed = await self.cog.top_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=ClubBackView(self.cog, self.owner_id))

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
        await self.bot.db.commit()
        return await self.club_row(guild_id, owner_id)

    async def rename_club(self, guild_id: int, owner_id: int, name: str) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute("UPDATE club_profiles SET name=?, updated_at=? WHERE guild_id=? AND owner_id=?", (name[:40], utcnow_iso(), guild_id, owner_id))
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
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

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
        await self.bot.db.execute("UPDATE club_profiles SET level=level+1, interior_level=interior_level+1, ads_level=ads_level+1, updated_at=? WHERE guild_id=? AND owner_id=?", (utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
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
        await self.bot.db.execute("UPDATE club_profiles SET staff=staff+1, updated_at=? WHERE guild_id=? AND owner_id=?", (utcnow_iso(), interaction.guild.id, interaction.user.id))
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

    async def top_embed(self, interaction: discord.Interaction) -> discord.Embed:
        assert interaction.guild is not None and self.bot.db is not None
        cur = await self.bot.db.execute("SELECT owner_id, name, level, coins_earned FROM club_profiles WHERE guild_id=? ORDER BY level DESC, coins_earned DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        text = "\n".join(f"{i}. <@{r['owner_id']}> — {r['name']}, ур. {r['level']}, {r['coins_earned']} coins" for i, r in enumerate(rows, 1)) or "Клубов пока нет."
        return discord.Embed(title="🏆 Топ клубов", description=text, color=discord.Color.gold())

    @app_commands.command(name="club", description="Открыть меню клуба")
    async def club(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        club = await self.club_row(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=self.club_embed(interaction.user, club), view=ClubMenuView(self, interaction.user.id, has_club=bool(club)), ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ClubCog(bot))

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.social_game_service import utcnow_iso


class ClubCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _club(self, guild_id: int, owner_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM club_profiles WHERE guild_id=? AND owner_id=?", (guild_id, owner_id))
        return await cur.fetchone()

    async def _ensure_club(self, guild_id: int, owner_id: int, name: str | None = None):
        assert self.bot.db is not None
        now = utcnow_iso()
        await self.bot.db.execute("INSERT OR IGNORE INTO club_profiles (guild_id, owner_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (guild_id, owner_id, (name or "Ночной клуб")[:40], now, now))
        await self.bot.db.commit()
        return await self._club(guild_id, owner_id)

    def _income(self, club) -> int:
        return 25 * int(club["level"]) + 10 * int(club["staff"]) + 8 * int(club["interior_level"]) + 6 * int(club["ads_level"])

    @app_commands.command(name="club", description="Профиль нейтрального RP-бизнеса / ночного клуба")
    async def club(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        club = await self._ensure_club(interaction.guild.id, interaction.user.id, name)
        embed = discord.Embed(title=f"🌙 {club['name']}", description="Ироничный менеджмент ночного клуба без эксплуатационных формулировок.", color=discord.Color.dark_purple())
        embed.add_field(name="Уровень", value=str(club["level"]), inline=True)
        embed.add_field(name="Персонал", value=str(club["staff"]), inline=True)
        embed.add_field(name="Интерьер", value=str(club["interior_level"]), inline=True)
        embed.add_field(name="Реклама", value=str(club["ads_level"]), inline=True)
        embed.add_field(name="Доход за сбор", value=f"{self._income(club)} coins", inline=True)
        embed.add_field(name="Всего заработано", value=f"{club['coins_earned']} coins", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="club_work", description="Собрать доход клуба")
    async def club_work(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        club = await self._ensure_club(interaction.guild.id, interaction.user.id)
        if club["last_work_at"]:
            try:
                last = datetime.fromisoformat(club["last_work_at"])
                if datetime.now(UTC) - last < timedelta(hours=4):
                    await interaction.response.send_message("Доход можно собирать раз в 4 часа.", ephemeral=True); return
            except ValueError:
                pass
        amount = self._income(club)
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned+?, xp=xp+?, last_work_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (amount, 10, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.execute("INSERT INTO club_transactions (guild_id, owner_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, amount, "work", utcnow_iso()))
        await self.bot.db.commit(); await interaction.response.send_message(f"Клуб провёл смену и принёс {amount} coins.", ephemeral=True)

    @app_commands.command(name="club_upgrade", description="Улучшить клуб")
    @app_commands.choices(kind=[app_commands.Choice(name="интерьер", value="interior_level"), app_commands.Choice(name="реклама", value="ads_level"), app_commands.Choice(name="уровень", value="level")])
    async def club_upgrade(self, interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        club = await self._ensure_club(interaction.guild.id, interaction.user.id)
        cost = (int(club[kind.value]) + 1) * 100
        if int(club["coins_earned"]) < cost:
            await interaction.response.send_message(f"Нужно {cost} coins суммарного дохода клуба для улучшения.", ephemeral=True); return
        await self.bot.db.execute(f"UPDATE club_profiles SET {kind.value}={kind.value}+1, updated_at=? WHERE guild_id=? AND owner_id=?", (utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message(f"Улучшение применено: {kind.name}.", ephemeral=True)

    @app_commands.command(name="club_staff", description="Нанять персонал")
    async def club_staff(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        club = await self._ensure_club(interaction.guild.id, interaction.user.id)
        cost = (int(club["staff"]) + 1) * 80
        if int(club["coins_earned"]) < cost:
            await interaction.response.send_message(f"Нужно {cost} coins суммарного дохода клуба для найма.", ephemeral=True); return
        await self.bot.db.execute("UPDATE club_profiles SET staff=staff+1, updated_at=? WHERE guild_id=? AND owner_id=?", (utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message("Новый сотрудник нанят.", ephemeral=True)

    @app_commands.command(name="club_daily", description="Ежедневный бонус клуба")
    async def club_daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        club = await self._ensure_club(interaction.guild.id, interaction.user.id)
        today = datetime.now(UTC).date().isoformat()
        if club["last_daily_at"] and club["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневный бонус уже получен.", ephemeral=True); return
        amount = 50 + int(club["level"]) * 10
        await self.bot.db.execute("UPDATE club_profiles SET coins_earned=coins_earned+?, last_daily_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (amount, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.execute("INSERT INTO club_transactions (guild_id, owner_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, amount, "daily", utcnow_iso()))
        await self.bot.db.commit(); await interaction.response.send_message(f"Ежедневный бонус клуба: {amount} coins.", ephemeral=True)

    @app_commands.command(name="club_top", description="Топ клубов")
    async def club_top(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT owner_id, name, level, coins_earned FROM club_profiles WHERE guild_id=? ORDER BY level DESC, coins_earned DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        text = "\n".join(f"{i}. <@{r['owner_id']}> — {r['name']}, ур. {r['level']}, {r['coins_earned']} coins" for i, r in enumerate(rows, 1)) or "Клубов пока нет."
        await interaction.response.send_message(embed=discord.Embed(title="Топ клубов", description=text, color=discord.Color.gold()))


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ClubCog(bot))

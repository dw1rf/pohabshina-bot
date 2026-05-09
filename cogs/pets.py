from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import PET_TYPES
from services.social_game_service import utcnow_iso


def clamp(value: int) -> int:
    return max(0, min(100, value))


class PetsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _pet(self, guild_id: int, owner_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM pets WHERE guild_id = ? AND owner_id = ?", (guild_id, owner_id))
        return await cur.fetchone()

    async def _decay(self, guild_id: int, owner_id: int) -> None:
        pet = await self._pet(guild_id, owner_id)
        if not pet or self.bot.db is None:
            return
        try:
            updated = datetime.fromisoformat(pet["updated_at"])
        except ValueError:
            updated = datetime.now(UTC)
        hours = max(0, int((datetime.now(UTC) - updated).total_seconds() // 3600))
        if hours < 6:
            return
        steps = min(12, hours // 6)
        hunger = clamp(int(pet["hunger"]) - steps * 4)
        happiness = clamp(int(pet["happiness"]) - steps * 3)
        energy = clamp(int(pet["energy"]) - steps * 2)
        health = clamp(int(pet["health"]) - (5 if min(hunger, happiness, energy) < 25 else 0))
        xp = max(0, int(pet["xp"]) - (2 if health < 30 else 0))
        await self.bot.db.execute("UPDATE pets SET hunger=?, happiness=?, energy=?, health=?, xp=?, updated_at=? WHERE guild_id=? AND owner_id=?", (hunger, happiness, energy, health, xp, utcnow_iso(), guild_id, owner_id))
        await self.bot.db.commit()

    def _embed(self, member: discord.abc.User, pet) -> discord.Embed:
        mood = "отлично" if min(pet["hunger"], pet["happiness"], pet["energy"], pet["health"]) >= 70 else "нужен уход"
        embed = discord.Embed(title=f"🐾 {pet['name']} ({pet['type']})", color=discord.Color.green())
        embed.description = f"Питомец {member.mention}. Настроение: **{mood}**"
        embed.add_field(name="Уровень", value=f"{pet['level']} (XP {pet['xp']})", inline=True)
        embed.add_field(name="Сытость", value=f"{pet['hunger']}/100", inline=True)
        embed.add_field(name="Настроение", value=f"{pet['happiness']}/100", inline=True)
        embed.add_field(name="Энергия", value=f"{pet['energy']}/100", inline=True)
        embed.add_field(name="Здоровье", value=f"{pet['health']}/100", inline=True)
        embed.add_field(name="Streak", value=str(pet["streak"]), inline=True)
        embed.set_footer(text="Подсказка: используйте /pet_feed, /pet_walk, /pet_play, /pet_sleep и /pet_daily.")
        return embed

    @app_commands.command(name="pet_create", description="Создать виртуального питомца")
    async def pet_create(self, interaction: discord.Interaction, name: str, type: str = "кот") -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        if type.lower() not in PET_TYPES:
            await interaction.response.send_message(f"Тип должен быть одним из: {', '.join(PET_TYPES)}", ephemeral=True); return
        now = utcnow_iso()
        await self.bot.db.execute("INSERT OR IGNORE INTO pets (guild_id, owner_id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, name[:32], type.lower(), now, now))
        await self.bot.db.commit()
        await interaction.response.send_message("Питомец создан! Используйте /pet.", ephemeral=True)

    @app_commands.command(name="pet", description="Показать питомца")
    async def pet(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if not pet:
            await interaction.response.send_message("У вас пока нет питомца. Создайте его через /pet_create.", ephemeral=True); return
        await interaction.response.send_message(embed=self._embed(interaction.user, pet))

    async def _action(self, interaction: discord.Interaction, action: str, changes: dict[str, int], cooldown_field: str | None = None, cooldown_hours: int = 3) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if not pet:
            await interaction.response.send_message("Сначала создайте питомца через /pet_create.", ephemeral=True); return
        if cooldown_field and pet[cooldown_field]:
            try:
                last = datetime.fromisoformat(pet[cooldown_field])
                if datetime.now(UTC) - last < timedelta(hours=cooldown_hours):
                    await interaction.response.send_message("Питомец пока отдыхает после этого действия.", ephemeral=True); return
            except ValueError:
                pass
        vals = {k: clamp(int(pet[k]) + v) for k, v in changes.items() if k in {"hunger", "happiness", "energy", "health"}}
        xp = int(pet["xp"]) + changes.get("xp", 5)
        level = max(1, int(pet["level"]))
        if xp >= level * 50:
            xp -= level * 50; level += 1
        set_sql = ", ".join([f"{k} = ?" for k in vals] + ["xp = ?", "level = ?", "updated_at = ?"] + ([f"{cooldown_field} = ?"] if cooldown_field else []))
        params = list(vals.values()) + [xp, level, utcnow_iso()] + ([utcnow_iso()] if cooldown_field else []) + [interaction.guild.id, interaction.user.id]
        await self.bot.db.execute(f"UPDATE pets SET {set_sql} WHERE guild_id = ? AND owner_id = ?", tuple(params))
        await self.bot.db.execute("INSERT INTO pet_actions (guild_id, owner_id, action, created_at) VALUES (?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, action, utcnow_iso()))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Готово: {action}!", ephemeral=True)

    @app_commands.command(name="pet_feed", description="Покормить питомца")
    async def pet_feed(self, interaction: discord.Interaction) -> None: await self._action(interaction, "feed", {"hunger": 25, "health": 5}, "last_feed_at", 3)
    @app_commands.command(name="pet_walk", description="Погулять с питомцем")
    async def pet_walk(self, interaction: discord.Interaction) -> None: await self._action(interaction, "walk", {"happiness": 20, "energy": -10, "xp": 8}, "last_walk_at", 3)
    @app_commands.command(name="pet_play", description="Поиграть с питомцем")
    async def pet_play(self, interaction: discord.Interaction) -> None: await self._action(interaction, "play", {"happiness": 25, "energy": -12, "xp": 8}, "last_play_at", 2)
    @app_commands.command(name="pet_sleep", description="Уложить питомца спать")
    async def pet_sleep(self, interaction: discord.Interaction) -> None: await self._action(interaction, "sleep", {"energy": 35, "health": 5, "xp": 4})

    @app_commands.command(name="pet_name", description="Переименовать питомца")
    async def pet_name(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        await self.bot.db.execute("UPDATE pets SET name = ?, updated_at = ? WHERE guild_id = ? AND owner_id = ?", (name[:32], utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message("Имя обновлено.", ephemeral=True)

    @app_commands.command(name="pet_daily", description="Ежедневный уход за питомцем")
    async def pet_daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if not pet:
            await interaction.response.send_message("Сначала создайте питомца через /pet_create.", ephemeral=True); return
        today = datetime.now(UTC).date().isoformat()
        if pet["last_daily_at"] and pet["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневный бонус уже получен сегодня.", ephemeral=True); return
        streak = int(pet["streak"]) + 1
        await self.bot.db.execute("UPDATE pets SET hunger=?, happiness=?, energy=?, health=?, xp=xp+?, streak=?, last_daily_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (clamp(pet["hunger"]+15), clamp(pet["happiness"]+15), clamp(pet["energy"]+15), clamp(pet["health"]+10), 10 + streak, streak, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message(f"Ежедневный уход засчитан! Streak: {streak}.", ephemeral=True)

    @app_commands.command(name="pet_top", description="Топ питомцев")
    async def pet_top(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT owner_id, name, level, xp FROM pets WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        text = "\n".join(f"{i}. <@{r['owner_id']}> — {r['name']}, ур. {r['level']}" for i, r in enumerate(rows, 1)) or "Питомцев пока нет."
        await interaction.response.send_message(embed=discord.Embed(title="Топ питомцев", description=text, color=discord.Color.gold()))


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(PetsCog(bot))

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


class PetCreateModal(discord.ui.Modal, title="Создать питомца"):
    name = discord.ui.TextInput(label="Имя питомца", max_length=32, default="Мурчик")
    pet_type = discord.ui.TextInput(label=f"Тип ({', '.join(PET_TYPES)})", max_length=20, default="кот")

    def __init__(self, cog: "PetsCog", owner_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.cog.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего питомца.", ephemeral=True)
            return
        pet_type = str(self.pet_type.value).strip().lower()
        if pet_type not in PET_TYPES:
            await interaction.response.send_message(f"Тип должен быть одним из: {', '.join(PET_TYPES)}", ephemeral=True)
            return
        await self.cog.create_pet(interaction.guild.id, self.owner_id, str(self.name.value).strip()[:32] or "Питомец", pet_type)
        await self.cog.refresh_menu(interaction)


class PetRenameModal(discord.ui.Modal, title="Переименовать питомца"):
    name = discord.ui.TextInput(label="Новое имя", max_length=32)

    def __init__(self, cog: "PetsCog", owner_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.cog.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего питомца.", ephemeral=True)
            return
        await self.cog.rename_pet(interaction.guild.id, self.owner_id, str(self.name.value).strip()[:32] or "Питомец")
        await self.cog.refresh_menu(interaction)


class PetMenuView(discord.ui.View):
    def __init__(self, cog: "PetsCog", owner_id: int, *, has_pet: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        if has_pet:
            self.remove_item(self.create)
            self.remove_item(self.help)
        else:
            for item in (self.feed, self.walk, self.play, self.sleep, self.daily, self.rename, self.refresh):
                self.remove_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего питомца.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🐣 Создать питомца", style=discord.ButtonStyle.success, row=0)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.cog.has_pet(interaction):
            await self.cog.refresh_menu(interaction)
            return
        await interaction.response.send_modal(PetCreateModal(self.cog, self.owner_id))

    @discord.ui.button(label="🍖 Покормить", style=discord.ButtonStyle.primary, row=0)
    async def feed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.apply_action(interaction, "питомец покормлен", {"hunger": 25, "health": 5}, "last_feed_at", 3)

    @discord.ui.button(label="🚶 Погулять", style=discord.ButtonStyle.primary, row=0)
    async def walk(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.apply_action(interaction, "прогулка завершена", {"happiness": 20, "energy": -10, "xp": 8}, "last_walk_at", 3)

    @discord.ui.button(label="🎮 Поиграть", style=discord.ButtonStyle.primary, row=0)
    async def play(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.apply_action(interaction, "игра завершена", {"happiness": 25, "energy": -12, "xp": 8}, "last_play_at", 2)

    @discord.ui.button(label="😴 Уложить спать", style=discord.ButtonStyle.primary, row=1)
    async def sleep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.apply_action(interaction, "питомец отдохнул", {"energy": 35, "health": 5, "xp": 4})

    @discord.ui.button(label="🎁 Ежедневный бонус", style=discord.ButtonStyle.success, row=1)
    async def daily(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.daily(interaction)

    @discord.ui.button(label="✏️ Переименовать", style=discord.ButtonStyle.secondary, row=1)
    async def rename(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.has_pet(interaction):
            await self.cog.refresh_menu(interaction)
            return
        await interaction.response.send_modal(PetRenameModal(self.cog, self.owner_id))

    @discord.ui.button(label="🏆 Топ", style=discord.ButtonStyle.secondary, row=2)
    async def top(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = await self.cog.top_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=PetBackView(self.cog, self.owner_id))

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)

    @discord.ui.button(label="❓ Помощь", style=discord.ButtonStyle.secondary, row=2)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="❓ Помощь по питомцу",
            description="Создай питомца и ухаживай за ним кнопками. Параметры со временем снижаются, но питомец не умирает. Данные сохраняются в БД.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=PetBackView(self.cog, self.owner_id))


class PetBackView(discord.ui.View):
    def __init__(self, cog: "PetsCog", owner_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего питомца.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.primary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)


class PetsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _pet(self, guild_id: int, owner_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM pets WHERE guild_id = ? AND owner_id = ?", (guild_id, owner_id))
        return await cur.fetchone()

    async def has_pet(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.guild and await self._pet(interaction.guild.id, interaction.user.id))

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

    def menu_embed(self, member: discord.abc.User, pet) -> discord.Embed:
        embed = discord.Embed(title="🐾 Виртуальный питомец", color=discord.Color.green())
        if not pet:
            embed.description = "У тебя пока нет питомца. Создай его кнопкой ниже."
            return embed
        embed.description = (
            f"Имя: **{pet['name']}**\n"
            f"Тип: **{pet['type']}**\n"
            f"Уровень: **{pet['level']}**\n"
            f"Опыт: **{pet['xp']}**\n"
            f"Сытость: **{pet['hunger']}/100**\n"
            f"Настроение: **{pet['happiness']}/100**\n"
            f"Энергия: **{pet['energy']}/100**\n"
            f"Здоровье: **{pet['health']}/100**\n"
            f"Streak: **{pet['streak']}**"
        )
        embed.set_footer(text=f"Меню питомца: {member.display_name}")
        return embed

    async def refresh_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await self._reply(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        embed = self.menu_embed(interaction.user, pet)
        view = PetMenuView(self, interaction.user.id, has_pet=bool(pet))
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _reply(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    async def create_pet(self, guild_id: int, owner_id: int, name: str, pet_type: str) -> None:
        assert self.bot.db is not None
        now = utcnow_iso()
        await self.bot.db.execute("INSERT OR IGNORE INTO pets (guild_id, owner_id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (guild_id, owner_id, name, pet_type, now, now))
        await self.bot.db.commit()

    async def rename_pet(self, guild_id: int, owner_id: int, name: str) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute("UPDATE pets SET name = ?, updated_at = ? WHERE guild_id = ? AND owner_id = ?", (name, utcnow_iso(), guild_id, owner_id))
        await self.bot.db.commit()

    async def apply_action(self, interaction: discord.Interaction, label: str, changes: dict[str, int], cooldown_field: str | None = None, cooldown_hours: int = 3) -> None:
        if interaction.guild is None or self.bot.db is None:
            await self._reply(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if not pet:
            await self.refresh_menu(interaction)
            return
        if cooldown_field and pet[cooldown_field]:
            try:
                last = datetime.fromisoformat(pet[cooldown_field])
                if datetime.now(UTC) - last < timedelta(hours=cooldown_hours):
                    await interaction.response.send_message("Питомец пока отдыхает после этого действия.", ephemeral=True)
                    return
            except ValueError:
                pass
        vals = {k: clamp(int(pet[k]) + v) for k, v in changes.items() if k in {"hunger", "happiness", "energy", "health"}}
        xp = int(pet["xp"]) + changes.get("xp", 5)
        level = max(1, int(pet["level"]))
        if xp >= level * 50:
            xp -= level * 50
            level += 1
        set_sql = ", ".join([f"{k} = ?" for k in vals] + ["xp = ?", "level = ?", "updated_at = ?"] + ([f"{cooldown_field} = ?"] if cooldown_field else []))
        params = list(vals.values()) + [xp, level, utcnow_iso()] + ([utcnow_iso()] if cooldown_field else []) + [interaction.guild.id, interaction.user.id]
        await self.bot.db.execute(f"UPDATE pets SET {set_sql} WHERE guild_id = ? AND owner_id = ?", tuple(params))
        await self.bot.db.execute("INSERT INTO pet_actions (guild_id, owner_id, action, created_at) VALUES (?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, label, utcnow_iso()))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await self._reply(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if not pet:
            await self.refresh_menu(interaction)
            return
        today = datetime.now(UTC).date().isoformat()
        if pet["last_daily_at"] and pet["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневный бонус уже получен сегодня.", ephemeral=True)
            return
        streak = int(pet["streak"]) + 1
        await self.bot.db.execute("UPDATE pets SET hunger=?, happiness=?, energy=?, health=?, xp=xp+?, streak=?, last_daily_at=?, updated_at=? WHERE guild_id=? AND owner_id=?", (clamp(pet["hunger"]+15), clamp(pet["happiness"]+15), clamp(pet["energy"]+15), clamp(pet["health"]+10), 10 + streak, streak, utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def top_embed(self, interaction: discord.Interaction) -> discord.Embed:
        assert interaction.guild is not None and self.bot.db is not None
        cur = await self.bot.db.execute("SELECT owner_id, name, level, xp FROM pets WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        text = "\n".join(f"{i}. <@{r['owner_id']}> — {r['name']}, ур. {r['level']}" for i, r in enumerate(rows, 1)) or "Питомцев пока нет."
        return discord.Embed(title="🏆 Топ питомцев", description=text, color=discord.Color.gold())

    @app_commands.command(name="pet", description="Открыть меню виртуального питомца")
    async def pet(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=self.menu_embed(interaction.user, pet), view=PetMenuView(self, interaction.user.id, has_pet=bool(pet)), ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(PetsCog(bot))

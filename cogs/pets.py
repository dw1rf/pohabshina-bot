from __future__ import annotations

import logging
import random
import asyncio
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import PET_TYPES
from services.social_game_service import utcnow_iso
from services.community_ops_service import normalize_user_text
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, resolve_avatar_bytes, resolve_display_name

logger = logging.getLogger(__name__)


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
        await self.cog.create_pet(interaction.guild.id, self.owner_id, normalize_user_text(str(self.name.value), max_length=32) or "Питомец", pet_type)
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
        await self.cog.rename_pet(interaction.guild.id, self.owner_id, normalize_user_text(str(self.name.value), max_length=32) or "Питомец")
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
        await self.cog.feed_pet(interaction)

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
        await interaction.response.defer()
        try:
            payload = await self.cog.top_payload(interaction)
        except Exception:
            logger.exception("Failed to generate pets top image")
            await interaction.followup.send("Не удалось создать графический топ. Попробуйте позже.", ephemeral=True)
            return
        if payload is None:
            await interaction.followup.send("Пока нет данных для топа.", ephemeral=True)
            return
        file = payload
        await interaction.edit_original_response(
            embed=None,
            view=PetBackView(self.cog, self.owner_id),
            attachments=[file],
            allowed_mentions=discord.AllowedMentions.none(),
        )

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


class PetBattleChallengeView(discord.ui.View):
    def __init__(self, cog: "PetsCog", challenger_id: int, defender_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger_id = challenger_id
        self.defender_id = defender_id
        self.completed = False
        self._lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.defender_id:
            await interaction.response.send_message("Ответить на вызов может только выбранный соперник.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Принять бой", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        async with self._lock:
            if self.completed:
                await interaction.response.send_message("Этот вызов уже завершён.", ephemeral=True)
                return
            self.completed = True
            if interaction.guild is None:
                await interaction.response.send_message("Бой доступен только на сервере.", ephemeral=True)
                return
            result = await self.cog.resolve_pet_battle(
                interaction.guild.id, self.challenger_id, self.defender_id, interaction.id
            )
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content=result, embed=None, view=self, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

    @discord.ui.button(label="Отказаться", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        async with self._lock:
            if self.completed:
                await interaction.response.send_message("Этот вызов уже завершён.", ephemeral=True)
                return
            self.completed = True
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content="Вызов на бой отклонён.", embed=None, view=self)


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
            f"Streak: **{pet['streak']}**\n"
            f"Сила/защита/скорость: **{pet['attack']}/{pet['defense']}/{pet['speed']}**\n"
            f"Рейтинг: **{pet['rating']}** · победы: **{pet['wins']}** · поражения: **{pet['losses']}**"
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
            await interaction.edit_original_response(embed=embed, view=view, attachments=[])
        else:
            await interaction.response.edit_message(embed=embed, view=view, attachments=[])

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

    async def feed_pet(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await self._reply(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if pet is None:
            await self.refresh_menu(interaction)
            return
        if pet["last_feed_at"]:
            try:
                if datetime.now(UTC) - datetime.fromisoformat(str(pet["last_feed_at"])) < timedelta(hours=3):
                    await interaction.response.send_message("Питомец пока не голоден.", ephemeral=True)
                    return
            except ValueError:
                pass
        assert self.bot.economy_db is not None
        async with self.bot.economy._lock:
            await self.bot.economy_db.execute("BEGIN IMMEDIATE")
            try:
                await self.bot.economy.ensure_wallet(
                    self.bot.economy_db, interaction.guild.id, interaction.user.id
                )
                wallet = await (await self.bot.economy_db.execute(
                    "SELECT balance FROM economy_wallets WHERE guild_id=? AND user_id=?",
                    (interaction.guild.id, interaction.user.id),
                )).fetchone()
                current_pet = await (await self.bot.economy_db.execute(
                    "SELECT * FROM pets WHERE guild_id=? AND owner_id=?",
                    (interaction.guild.id, interaction.user.id),
                )).fetchone()
                if current_pet is None:
                    await self.bot.economy_db.rollback()
                    return
                if wallet is None or int(wallet["balance"]) < 30:
                    await self.bot.economy_db.rollback()
                    await interaction.response.send_message(
                        "Для кормления нужно 30 монет серверной экономики.", ephemeral=True
                    )
                    return
                cutoff = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
                xp = int(current_pet["xp"]) + 5
                level = int(current_pet["level"])
                if xp >= level * 50:
                    xp -= level * 50
                    level += 1
                now = utcnow_iso()
                updated = await self.bot.economy_db.execute(
                    """UPDATE pets SET hunger=?,health=?,xp=?,level=?,last_feed_at=?,updated_at=?
                       WHERE guild_id=? AND owner_id=?
                         AND (last_feed_at IS NULL OR last_feed_at<=?)""",
                    (clamp(int(current_pet["hunger"]) + 25), clamp(int(current_pet["health"]) + 5),
                     xp, level, now, now, interaction.guild.id, interaction.user.id, cutoff),
                )
                if updated.rowcount != 1:
                    await self.bot.economy_db.rollback()
                    await interaction.response.send_message("Питомец пока не голоден.", ephemeral=True)
                    return
                await self.bot.economy_db.execute(
                    "UPDATE economy_wallets SET balance=balance-30,updated_at=? WHERE guild_id=? AND user_id=?",
                    (now, interaction.guild.id, interaction.user.id),
                )
                inserted = await self.bot.economy._ledger(
                    self.bot.economy_db, interaction.guild.id, interaction.user.id, -30, 0,
                    "pet_feed", idempotency_key=f"pet-feed:{interaction.id}",
                )
                if not inserted:
                    await self.bot.economy_db.rollback()
                    return
                await self.bot.economy_db.execute(
                    "INSERT INTO pet_actions(guild_id,owner_id,action,created_at) VALUES (?,?,?,?)",
                    (interaction.guild.id, interaction.user.id, "питомец покормлен", now),
                )
                await self.bot.economy_db.commit()
            except Exception:
                await self.bot.economy_db.rollback()
                raise
        await self.refresh_menu(interaction)

    async def top_payload(self, interaction: discord.Interaction) -> discord.File | None:
        assert interaction.guild is not None and self.bot.db is not None
        cur = await self.bot.db.execute("SELECT owner_id, name, level, xp, streak FROM pets WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 10", (interaction.guild.id,))
        rows = await cur.fetchall()
        if not rows:
            return None

        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            level = int(row["level"])
            xp = int(row["xp"])
            streak = int(row["streak"] or 0)
            owner_name = await resolve_display_name(self.bot, interaction.guild, int(row["owner_id"]), max_len=34)
            avatar = await resolve_avatar_bytes(self.bot, interaction.guild, int(row["owner_id"]))
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=str(row["name"]),
                    primary=f"Владелец: {owner_name}",
                    secondary=f"Уровень {level}  XP {xp}  streak {streak}",
                    value=level * 1000 + xp,
                    avatar=avatar,
                )
            )

        filename = "pets_top.png"
        return make_leaderboard_file("ТОП ПИТОМЦЕВ", leaderboard_rows, filename=filename, theme="pets")

    @app_commands.command(name="pet", description="Открыть меню виртуального питомца")
    async def pet(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self._decay(interaction.guild.id, interaction.user.id)
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=self.menu_embed(interaction.user, pet), view=PetMenuView(self, interaction.user.id, has_pet=bool(pet)), ephemeral=True)

    @app_commands.command(name="pet_adventure", description="Отправить питомца в PvE-приключение")
    async def pet_adventure(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        pet = await self._pet(interaction.guild.id, interaction.user.id)
        if pet is None:
            await interaction.response.send_message("Сначала создай питомца через `/pet`.", ephemeral=True)
            return
        if pet["last_adventure_at"]:
            last = datetime.fromisoformat(str(pet["last_adventure_at"]))
            if datetime.now(UTC) - last < timedelta(hours=2):
                await interaction.response.send_message("Питомец ещё отдыхает после приключения.", ephemeral=True)
                return
        equipment = await self.bot.progression.get_pet_equipment_stats(
            self.bot.progression_db, interaction.guild.id, interaction.user.id
        )
        club_skills = await self.bot.progression.get_club_skills(
            self.bot.progression_db, interaction.guild.id, interaction.user.id
        )
        pet_bonus_level = next(
            (int(item["level"]) for item in club_skills if item["skill_key"] == "pet_bonus"), 0
        )
        power = (
            int(pet["attack"]) + equipment["attack"]
            + int(pet["defense"]) + equipment["defense"]
            + int(pet["speed"]) + equipment["speed"]
            + int(pet["level"]) * 3
        )
        enemy = random.randint(25, max(30, power + 15))
        won = power + random.randint(0, 20) >= enemy
        xp = 20 if won else 8
        coins = 45 if won else 12
        if pet_bonus_level:
            xp += max(1, xp * pet_bonus_level // 20)
            coins += max(1, coins * pet_bonus_level // 20)
        now = datetime.now(UTC)
        cursor = await self.bot.db.execute(
            """UPDATE pets SET xp=xp+?, energy=MAX(0, energy-20), last_adventure_at=?, updated_at=?
               WHERE guild_id=? AND owner_id=?
                 AND (last_adventure_at IS NULL OR last_adventure_at<=?)""",
            (
                xp,
                now.isoformat(),
                now.isoformat(),
                interaction.guild.id,
                interaction.user.id,
                (now - timedelta(hours=2)).isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            await self.bot.db.rollback()
            await interaction.response.send_message("РџРёС‚РѕРјРµС† РµС‰С‘ РѕС‚РґС‹С…Р°РµС‚ РїРѕСЃР»Рµ РїСЂРёРєР»СЋС‡РµРЅРёСЏ.", ephemeral=True)
            return
        await self.bot.db.execute(
            "INSERT INTO pet_battles(guild_id, attacker_id, defender_id, winner_id, rating_delta, battle_type, created_at) VALUES (?, ?, 0, ?, 0, 'pve', ?)",
            (interaction.guild.id, interaction.user.id, interaction.user.id if won else 0, utcnow_iso()),
        )
        await self.bot.db.execute(
            """INSERT OR IGNORE INTO gameplay_delivery_outbox
               (delivery_key,guild_id,user_id,coins,economy_xp,pet_xp,event_type,event_amount)
               VALUES (?,?,?,?,?,?,?,?)""",
            (f"pet-adventure:{interaction.id}", interaction.guild.id, interaction.user.id, coins, xp, xp, "pet_adventure", 1),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{'Победа' if won else 'Приключение оказалось сложным'}! Питомец получает {xp} XP, а ты — {coins} монет."
        )

    @app_commands.command(name="pet_battle", description="Вызвать питомца участника на PvP-бой")
    async def pet_battle(self, interaction: discord.Interaction, opponent: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if opponent.id == interaction.user.id or opponent.bot:
            await interaction.response.send_message("Выбери другого участника.", ephemeral=True)
            return
        attacker = await self._pet(interaction.guild.id, interaction.user.id)
        defender = await self._pet(interaction.guild.id, opponent.id)
        if attacker is None or defender is None:
            await interaction.response.send_message("У обоих участников должен быть питомец.", ephemeral=True)
            return
        if int(attacker["energy"]) < 15 or int(defender["energy"]) < 15:
            await interaction.response.send_message("Одному из питомцев не хватает энергии.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⚔️ {opponent.mention}, питомец **{attacker['name']}** вызывает **{defender['name']}** на бой. Принять?",
            view=PetBattleChallengeView(self, interaction.user.id, opponent.id),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    async def resolve_pet_battle(self, guild_id: int, attacker_id: int, defender_id: int, interaction_id: int) -> str:
        if self.bot.db is None:
            return "База данных временно недоступна."
        attacker = await self._pet(guild_id, attacker_id)
        defender = await self._pet(guild_id, defender_id)
        if attacker is None or defender is None:
            return "Один из питомцев больше недоступен."
        if int(attacker["energy"]) < 15 or int(defender["energy"]) < 15:
            return "Одному из питомцев уже не хватает энергии."
        attacker_equipment = await self.bot.progression.get_pet_equipment_stats(self.bot.progression_db, guild_id, attacker_id)
        defender_equipment = await self.bot.progression.get_pet_equipment_stats(self.bot.progression_db, guild_id, defender_id)
        def score(pet, equipment: dict[str, int]) -> int:
            return (
                (int(pet["attack"]) + equipment["attack"]) * 2
                + int(pet["defense"]) + equipment["defense"]
                + int(pet["speed"]) + equipment["speed"]
                + int(pet["level"]) * 5 + random.randint(0, 30)
            )
        attacker_wins = score(attacker, attacker_equipment) >= score(defender, defender_equipment)
        winner_id = attacker_id if attacker_wins else defender_id
        loser_id = defender_id if attacker_wins else attacker_id
        delta = 20
        await self.bot.db.execute("BEGIN IMMEDIATE")
        try:
            await self.bot.db.execute(
                "UPDATE pets SET wins=wins+1, rating=rating+?, xp=xp+15, energy=MAX(0, energy-15), updated_at=? WHERE guild_id=? AND owner_id=?",
                (delta, utcnow_iso(), guild_id, winner_id),
            )
            await self.bot.db.execute(
                "UPDATE pets SET losses=losses+1, rating=MAX(0, rating-?), xp=xp+5, energy=MAX(0, energy-15), updated_at=? WHERE guild_id=? AND owner_id=?",
                (delta, utcnow_iso(), guild_id, loser_id),
            )
            await self.bot.db.execute(
                "INSERT INTO pet_battles(guild_id, attacker_id, defender_id, winner_id, rating_delta, battle_type, created_at) VALUES (?, ?, ?, ?, ?, 'pvp', ?)",
                (guild_id, attacker_id, defender_id, winner_id, delta, utcnow_iso()),
            )
            await self.bot.db.execute(
                """INSERT OR IGNORE INTO gameplay_delivery_outbox
                   (delivery_key,guild_id,user_id,coins,economy_xp,pet_xp,event_type,event_amount)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"pet-pvp:{interaction_id}", guild_id, winner_id, 60, 20, 15, "pet_win", 1),
            )
            await self.bot.db.commit()
        except Exception:
            await self.bot.db.rollback()
            raise
        return f"⚔️ **{attacker['name']}** против **{defender['name']}**. Победитель: <@{winner_id}> (+{delta} рейтинга, 60 монет)!"


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(PetsCog(bot))

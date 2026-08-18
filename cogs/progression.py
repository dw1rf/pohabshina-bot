from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from utils.leaderboard_image import LeaderboardImageRow, make_leaderboard_file, make_profile_file
from utils.static_data import static_data_path


logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
BADGES = {"voice": "🎙️", "champion": "🏆", "idea": "💡", "season_one": "◆"}
ACCENTS = {"rose": "#D9679D", "plum": "#976784", "gold": "#D6B76E"}


def interaction_key(interaction: discord.Interaction, suffix: str) -> str:
    return f"interaction:{interaction.id}:{suffix}"


class ProgressionCog(commands.Cog):
    customize_group = app_commands.Group(name="customize", description="Персонализация графического профиля")
    season_group = app_commands.Group(name="season", description="Сезонный пропуск сервера")
    quest_group = app_commands.Group(name="serverquest", description="Общее задание сервера")
    admin_group = app_commands.Group(
        name="progress_admin",
        description="Управление локальными игровыми циклами",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    features_group = app_commands.Group(
        name="features",
        description="Состояние локальных модулей",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def _require_pet(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or self.bot.db is None:
            return False
        row = await (await self.bot.db.execute(
            "SELECT 1 FROM pets WHERE guild_id=? AND owner_id=?",
            (interaction.guild.id, interaction.user.id),
        )).fetchone()
        if row is not None:
            return True
        await interaction.response.send_message(
            "Сначала создай питомца через `/pet`.", ephemeral=True
        )
        return False

    async def cog_load(self) -> None:
        await self._seed_catalogs()
        if not self.reward_delivery_loop.is_running():
            self.reward_delivery_loop.start()

    def cog_unload(self) -> None:
        if self.reward_delivery_loop.is_running():
            self.reward_delivery_loop.cancel()

    async def _seed_catalogs(self) -> None:
        if self.bot.progression_db is None:
            return
        achievement_data = json.loads(static_data_path("achievements.json").read_text(encoding="utf-8"))
        cosmetic_data = json.loads(static_data_path("profile_cosmetics.json").read_text(encoding="utf-8"))
        pet_data = json.loads(static_data_path("pet_species.json").read_text(encoding="utf-8"))
        first_species = next(iter(pet_data["species"].values()))
        thresholds = tuple(int(stage["xp"]) for stage in first_species["stages"][1:4])
        self.bot.progression.set_pet_stage_thresholds(thresholds)
        for cosmetic in cosmetic_data["cosmetics"]:
            await self.bot.progression.register_cosmetic(
                self.bot.progression_db, str(cosmetic["type"]), str(cosmetic["key"]), str(cosmetic["name"])
            )
        for achievement in achievement_data["achievements"]:
            await self.bot.progression.create_achievement(
                self.bot.progression_db,
                str(achievement["key"]),
                str(achievement["event_type"]),
                int(achievement["threshold"]),
                reward_coins=int(achievement.get("reward_coins", 0)),
                cosmetic_type=achievement.get("cosmetic_type"),
                cosmetic_key=achievement.get("cosmetic_key"),
                hidden=bool(achievement.get("hidden", False)),
                name=str(achievement.get("name", "")),
            )
        await self.bot.progression.register_pet_equipment(self.bot.progression_db, "moon_claw", "weapon", attack=5)
        await self.bot.progression.register_pet_equipment(self.bot.progression_db, "shade_armor", "armor", defense=5)
        await self.bot.progression.register_pet_equipment(self.bot.progression_db, "swift_charm", "charm", speed=5)
        # Every member starts with a neutral accent and the regular levels background
        # once they first open the customization panel.

    async def _ensure_starter_cosmetics(self, guild_id: int, user_id: int) -> None:
        assert self.bot.progression_db is not None
        await self.bot.progression.unlock_cosmetic(self.bot.progression_db, guild_id, user_id, "background", "levels", "starter")
        await self.bot.progression.unlock_cosmetic(self.bot.progression_db, guild_id, user_id, "accent", "rose", "starter")

    @tasks.loop(seconds=30)
    async def reward_delivery_loop(self) -> None:
        if self.bot.progression_db is None or self.bot.economy_db is None:
            return
        try:
            rewards = await self.bot.progression.pending_rewards(self.bot.progression_db, limit=50)
        except Exception:
            logger.exception("Could not read the progression reward outbox")
            return
        for reward in rewards:
            try:
                key = str(reward["reward_key"])
                result = await self.bot.economy.change_balance(
                    self.bot.economy_db,
                    int(reward["guild_id"]),
                    int(reward["user_id"]),
                    int(reward["coins"]),
                    reason="progression_reward",
                    reference=str(reward["source"]),
                    idempotency_key=f"progression:{key}",
                )
                if result.ok:
                    await self.bot.progression.mark_reward_delivered(self.bot.progression_db, key)
                    continue
                cursor = await self.bot.economy_db.execute(
                    "SELECT 1 FROM economy_transactions WHERE guild_id=? AND user_id=? AND idempotency_key=?",
                    (int(reward["guild_id"]), int(reward["user_id"]), f"progression:{key}"),
                )
                if await cursor.fetchone() is not None:
                    await self.bot.progression.mark_reward_delivered(self.bot.progression_db, key)
            except Exception:
                logger.exception("Could not deliver progression reward %s", reward["reward_key"])
        if self.bot.delivery_db is None:
            return
        try:
            cursor = await self.bot.delivery_db.execute(
                "SELECT * FROM gameplay_delivery_outbox WHERE economy_delivered=0 OR progression_delivered=0 ORDER BY created_at LIMIT 50"
            )
            deliveries = await cursor.fetchall()
        except Exception:
            logger.exception("Could not read gameplay delivery outbox")
            return
        for delivery in deliveries:
            key = str(delivery["delivery_key"])
            try:
                if not int(delivery["economy_delivered"]):
                    result = await self.bot.economy.change_balance(
                        self.bot.economy_db,
                        int(delivery["guild_id"]),
                        int(delivery["user_id"]),
                        int(delivery["coins"]),
                        reason="gameplay_reward",
                        xp=int(delivery["economy_xp"]),
                        idempotency_key=f"gameplay:{key}",
                    )
                    if not result.ok:
                        continue
                    await self.bot.delivery_db.execute(
                        "UPDATE gameplay_delivery_outbox SET economy_delivered=1 WHERE delivery_key=?", (key,)
                    )
                    await self.bot.delivery_db.commit()
                if not int(delivery["progression_delivered"]):
                    if int(delivery["pet_xp"]):
                        await self.bot.progression.add_pet_xp(
                            self.bot.progression_db,
                            int(delivery["guild_id"]),
                            int(delivery["user_id"]),
                            int(delivery["pet_xp"]),
                            idempotency_key=f"gameplay-pet-xp:{key}",
                        )
                    if delivery["event_type"] and int(delivery["event_amount"]):
                        await self.bot.progression.record_event(
                            self.bot.progression_db,
                            int(delivery["guild_id"]),
                            int(delivery["user_id"]),
                            str(delivery["event_type"]),
                            int(delivery["event_amount"]),
                            f"gameplay-progress:{key}",
                        )
                    await self.bot.delivery_db.execute(
                        "UPDATE gameplay_delivery_outbox SET progression_delivered=1 WHERE delivery_key=?", (key,)
                    )
                    await self.bot.delivery_db.commit()
            except Exception:
                logger.exception("Could not deliver gameplay outbox item %s", key)

    @reward_delivery_loop.before_loop
    async def before_reward_delivery(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="achievements", description="Показать достижения участника")
    async def achievements(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        progress = await self.bot.progression.get_user_progress(self.bot.progression_db, interaction.guild.id, target.id)
        rows: list[LeaderboardImageRow] = []
        for index, (key, item) in enumerate(progress["achievements"].items(), start=1):
            if item["hidden"] and not item["unlocked"]:
                continue
            rows.append(
                LeaderboardImageRow(
                    name=str(item.get("name") or key.replace("_", " ").title()),
                    primary="Открыто" if item["unlocked"] else "В процессе",
                    secondary=f"{item['value']} / {item['threshold']}",
                    value=100 if item["unlocked"] else int(item["value"] * 100 / max(item["threshold"], 1)),
                )
            )
        await interaction.response.defer()
        file = make_leaderboard_file(
            f"Достижения · {target.display_name}", rows[:10], filename="achievements.png", theme="reputation"
        )
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="collection", description="Показать коллекцию косметики участника")
    async def collection(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        available = await self.bot.progression.list_available_cosmetics(self.bot.progression_db, interaction.guild.id, target.id)
        rows = [
            LeaderboardImageRow(
                name=str(item["name"]),
                primary=str(item["cosmetic_type"]).capitalize(),
                secondary="Выбрано" if item["equipped"] else "Открыто",
                value=1 if item["equipped"] else 0,
            )
            for item in available
        ]
        await interaction.response.defer()
        file = make_leaderboard_file(
            f"Коллекция · {target.display_name}", rows[:10], filename="collection.png", theme="economy"
        )
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    @customize_group.command(name="list", description="Показать доступное оформление")
    async def customize_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self._ensure_starter_cosmetics(interaction.guild.id, interaction.user.id)
        items = await self.bot.progression.list_available_cosmetics(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        lines = [
            f"{'✓' if item['equipped'] else '•'} `{item['cosmetic_type']}` · `{item['cosmetic_key']}` — {item['name']}"
            for item in items
        ]
        await interaction.response.send_message("\n".join(lines) or "Пока ничего не открыто.", ephemeral=True)

    @customize_group.command(name="equip", description="Выбрать открытый фон, акцент или значок")
    async def customize_equip(
        self,
        interaction: discord.Interaction,
        kind: Literal["background", "accent", "badge"],
        key: str,
    ) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self._ensure_starter_cosmetics(interaction.guild.id, interaction.user.id)
        ok = await self.bot.progression.equip_cosmetic(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, kind, key.strip().lower()
        )
        await interaction.response.send_message(
            "Оформление сохранено." if ok else "Эта косметика не открыта или не существует.", ephemeral=True
        )

    @customize_group.command(name="preview", description="Предпросмотр выбранного профиля")
    async def customize_preview(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        custom = await self.bot.progression.get_profile_customization(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        badge = BADGES.get(str(custom.get("badge_key") or ""), "")
        file = make_profile_file(
            f"{interaction.user.display_name} {badge}".strip(),
            "Предпросмотр профиля",
            (("Фон", str(custom.get("background_key") or "levels")),
             ("Акцент", ACCENTS.get(str(custom.get("accent_key") or "rose"), "#D9679D")),
             ("Значок", str(custom.get("badge_key") or "нет"))),
            theme=str(custom.get("background_key") or "levels"),
            progress=0.64,
            filename="profile_preview.png",
            description="Предпросмотр оформления профиля",
        )
        await interaction.response.send_message(file=file, ephemeral=True)

    @season_group.command(name="view", description="Показать прогресс сезонного пропуска")
    async def season_view(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        season = await self.bot.progression.get_active_season(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        if season is None:
            await interaction.response.send_message("Активного сезона сейчас нет.", ephemeral=True)
            return
        cursor = await self.bot.progression_db.execute(
            "SELECT tier,required_points,reward_coins FROM season_tiers WHERE season_id=? ORDER BY tier",
            (int(season["season_id"]),),
        )
        tiers = await cursor.fetchall()
        points = int(season["points"])
        next_tier = next((row for row in tiers if int(row["required_points"]) > points), None)
        goal = int(next_tier["required_points"]) if next_tier else max(points, 1)
        file = make_profile_file(
            interaction.user.display_name,
            str(season["name"]),
            (("Очки", str(points)), ("Уровней", str(len(tiers))),
             ("Следующая цель", str(goal)), ("До конца", str(season["ends_at"])[:10])),
            progress=min(points / max(goal, 1), 1.0),
            theme="events",
            filename="season.png",
            description=f"Сезонный прогресс {interaction.user.display_name}",
        )
        await interaction.response.send_message(file=file)

    @season_group.command(name="claim", description="Забрать награду достигнутого уровня")
    async def season_claim(self, interaction: discord.Interaction, tier: app_commands.Range[int, 1, 100]) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        reward = await self.bot.progression.claim_season_tier(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, int(tier)
        )
        await interaction.response.send_message(
            f"Награда уровня {tier} поставлена на выдачу: {reward['coins']} монет."
            if reward else "Уровень не достигнут или награда уже получена.",
            ephemeral=True,
        )

    @quest_group.command(name="view", description="Показать общее задание сервера")
    async def quest_view(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        quests = await self.bot.progression.get_active_guild_quests(
            self.bot.progression_db, interaction.guild.id, interaction.user.id
        )
        if not quests:
            await interaction.response.send_message("Активного серверного задания нет.", ephemeral=True)
            return
        quest = quests[0]
        await interaction.response.send_message(
            f"**Общая цель:** `{quest['event_type']}`\n"
            f"Прогресс сервера: **{quest['current_value']} / {quest['target_value']}**\n"
            f"Твой вклад: **{quest['user_value']}**\nСтатус: **{quest['status']}**"
        )

    @quest_group.command(name="claim", description="Забрать награду завершённого общего задания")
    async def quest_claim(self, interaction: discord.Interaction, quest_id: int) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        reward = await self.bot.progression.claim_guild_quest(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, quest_id
        )
        await interaction.response.send_message(
            f"Награда поставлена на выдачу: {reward['coins']} монет."
            if reward else "Награда недоступна, уже получена или у тебя не было вклада.",
            ephemeral=True,
        )

    @app_commands.command(name="pet_evolve", description="Показать эволюцию питомца")
    async def pet_evolve(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self._require_pet(interaction):
            return
        progress = await self.bot.progression.get_pet_progress(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            f"Эволюция питомца: стадия **{progress['stage']} / 4**, опыт **{progress['xp']} XP**."
        )

    @app_commands.command(name="pet_raid", description="Статус или атака общего босса питомцев")
    async def pet_raid(self, interaction: discord.Interaction, action: Literal["status", "attack"] = "status") -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self._require_pet(interaction):
            return
        await self.bot.progression.start_daily_boss(self.bot.progression_db, interaction.guild.id)
        if action == "attack":
            pet = await self.bot.progression.get_pet_progress(self.bot.progression_db, interaction.guild.id, interaction.user.id)
            damage = 25 + int(pet["stage"]) * 15
            result = await self.bot.progression.attack_daily_boss(
                self.bot.progression_db,
                interaction.guild.id,
                interaction.user.id,
                damage,
                interaction_key(interaction, "pet-boss"),
            )
            if result is None:
                await interaction.response.send_message("Атака пока на cooldown или босс уже побеждён.", ephemeral=True)
                return
            await self.bot.progression.record_event(
                self.bot.progression_db, interaction.guild.id, interaction.user.id, "pet_boss", 1,
                interaction_key(interaction, "pet-boss-progress"),
            )
            if result["status"] == "defeated":
                await self.bot.progression.grant_pet_equipment(
                    self.bot.progression_db, interaction.guild.id, interaction.user.id, "moon_claw", 1
                )
        boss = await self.bot.progression.get_daily_boss(self.bot.progression_db, interaction.guild.id)
        assert boss is not None
        await interaction.response.send_message(
            f"Босс питомцев: **{boss['hp']} / {boss['max_hp']} HP** · статус **{boss['status']}**"
        )

    @app_commands.command(name="pet_equipment", description="Показать экипировку питомца")
    async def pet_equipment(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self._require_pet(interaction):
            return
        items = await self.bot.progression.list_pet_equipment(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        stats = await self.bot.progression.get_pet_equipment_stats(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        lines = [
            f"{'✓' if item['equipped'] else '•'} `{item['item_key']}` · {item['slot']} · ×{item['quantity']}"
            for item in items
        ]
        lines.append(f"Бонусы: ATK +{stats['attack']} · DEF +{stats['defense']} · SPD +{stats['speed']}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="pet_equip", description="Надеть найденный предмет на питомца")
    async def pet_equip(self, interaction: discord.Interaction, item_key: str) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self._require_pet(interaction):
            return
        ok = await self.bot.progression.equip_pet_item(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, item_key.strip().lower()
        )
        await interaction.response.send_message(
            "Предмет экипирован." if ok else "Такого предмета нет в инвентаре питомца.", ephemeral=True
        )

    @app_commands.command(name="club_war", description="Показать положение своего клуба в недельной лиге")
    async def club_war(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        war = await self.bot.progression.get_club_war(self.bot.progression_db, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            f"Клубная лига: **{war['score']} очков** · клуб #{war['club_id']}"
            if war else "Активной лиги нет или ты не состоишь в клубе.",
            ephemeral=True,
        )

    @app_commands.command(name="club_war_claim", description="Лидеру: забрать награду завершённой клубной лиги")
    async def club_war_claim(self, interaction: discord.Interaction, war_id: int = 0) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        reward = await self.bot.progression.claim_club_war_reward(
            self.bot.progression_db, interaction.guild.id, interaction.user.id, war_id
        )
        await interaction.response.send_message(
            f"Клуб занял место #{reward['rank']}; в банк начислено {reward['bank_reward']} монет."
            if reward else "Награда недоступна, уже получена или нужна роль лидера.",
            ephemeral=True,
        )

    @admin_group.command(name="season_start", description="Запустить локальный сезонный пропуск")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_season_start(
        self, interaction: discord.Interaction, name: str, days: app_commands.Range[int, 1, 90] = 30
    ) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        season_id = await self.bot.progression.start_season(self.bot.progression_db, interaction.guild.id, name, days=int(days))
        for tier in range(1, 11):
            await self.bot.progression.add_season_tier(
                self.bot.progression_db,
                season_id,
                tier,
                tier * tier * 25,
                reward_coins=50 + tier * 25,
                cosmetic_key="season_one" if tier == 10 else None,
            )
        await interaction.response.send_message(f"Сезон «{name[:80]}» запущен: 10 уровней.", ephemeral=True)

    @admin_group.command(name="quest_start", description="Запустить общее серверное задание")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_quest_start(
        self,
        interaction: discord.Interaction,
        event_type: Literal["message", "economy_action", "rp_action", "pet_adventure", "pet_win", "club_contribution", "relationship_xp"],
        target: app_commands.Range[int, 1, 1_000_000],
        reward: app_commands.Range[int, 0, 100_000] = 250,
        days: app_commands.Range[int, 1, 30] = 7,
    ) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        quest_id = await self.bot.progression.create_guild_quest(
            self.bot.progression_db, interaction.guild.id, event_type, int(target), days=int(days), reward_coins=int(reward)
        )
        await interaction.response.send_message(f"Общее задание #{quest_id} запущено.", ephemeral=True)

    @admin_group.command(name="club_war_start", description="Запустить недельную клубную лигу")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_club_war_start(
        self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7
    ) -> None:
        if interaction.guild is None or self.bot.progression_db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        war_id = await self.bot.progression.start_club_war(self.bot.progression_db, interaction.guild.id, days=int(days))
        await interaction.response.send_message(f"Клубная лига #{war_id} запущена.", ephemeral=True)

    @features_group.command(name="status", description="Показать состояние локальных игровых модулей")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def features_status(self, interaction: discord.Interaction) -> None:
        if self.bot.progression_db is None:
            await interaction.response.send_message("База данных недоступна.", ephemeral=True)
            return
        pending = int((await (await self.bot.progression_db.execute(
            "SELECT COUNT(*) AS total FROM progression_reward_outbox WHERE delivered_at IS NULL"
        )).fetchone())["total"])
        await interaction.response.send_message(
            "**Локальные модули**\n"
            "✓ достижения и коллекции\n✓ персонализация профиля\n✓ сезон и общие задания\n"
            "✓ эволюции и босс питомцев\n✓ клубная лига\n"
            f"Очередь наград: **{pending}**",
            ephemeral=True,
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ProgressionCog(bot))

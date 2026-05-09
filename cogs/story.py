from __future__ import annotations

import json
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import STORY_SCENES
from services.social_game_service import utcnow_iso


class StoryChoiceView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user_id: int, choices: list[dict]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        for idx, choice in enumerate(choices[:5]):
            self.add_item(StoryButton(idx, choice))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это выбор другого игрока.", ephemeral=True)
            return False
        return True


class StoryButton(discord.ui.Button):
    def __init__(self, idx: int, choice: dict) -> None:
        super().__init__(label=str(choice.get("label", "Выбор"))[:80], style=discord.ButtonStyle.primary, custom_id=f"story:{idx}")
        self.choice = choice

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StoryChoiceView)
        await self.view.cog.apply_choice(interaction, self.choice)


class StoryCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.db:
            await self.bot.social_games.seed_story_scenes(self.bot.db, STORY_SCENES)

    async def _progress(self, guild_id: int, user_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM story_progress WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return await cur.fetchone()

    async def _scene(self, scene_id: str):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM story_scenes WHERE id=?", (scene_id,))
        return await cur.fetchone()

    async def _send_scene(self, interaction: discord.Interaction, scene_id: str, *, edit: bool = False) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        scene = await self._scene(scene_id)
        if not scene:
            await interaction.response.send_message("Сцена не найдена.", ephemeral=True); return
        settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
        if scene["nsfw"] and (not settings["story_nsfw_enabled"] or not getattr(interaction.channel, "is_nsfw", lambda: False)()):
            await interaction.response.send_message("Эта ветка недоступна: NSFW story выключен или канал не NSFW.", ephemeral=True); return
        choices = json.loads(scene["choices_json"])
        embed = discord.Embed(title=scene["title"], description=scene["text"], color=discord.Color.dark_teal())
        embed.set_footer(text="Интерактивная история. Контент настраивается через story_scenes/JSON.")
        view = StoryChoiceView(self, interaction.user.id, choices) if choices else None
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="story_start", description="Начать историю")
    async def story_start(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        await self.bot.db.execute("INSERT OR REPLACE INTO story_progress (guild_id, user_id, current_scene_id, updated_at) VALUES (?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, "tavern_001", utcnow_iso()))
        await self.bot.db.commit(); await self._send_scene(interaction, "tavern_001")

    @app_commands.command(name="story", description="Показать текущую главу")
    async def story(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        progress = await self._progress(interaction.guild.id, interaction.user.id)
        if not progress:
            await interaction.response.send_message("История ещё не начата. Используйте /story_start.", ephemeral=True); return
        await self._send_scene(interaction, progress["current_scene_id"])

    async def apply_choice(self, interaction: discord.Interaction, choice: dict) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        reward = choice.get("reward", {})
        coins = int(reward.get("coins", 0)); xp = int(reward.get("xp", 0))
        next_scene = str(choice.get("next_scene", "tavern_001"))
        await self.bot.db.execute("UPDATE story_progress SET current_scene_id=?, coins=coins+?, xp=xp+?, updated_at=? WHERE guild_id=? AND user_id=?", (next_scene, coins, xp, utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        await self._send_scene(interaction, next_scene, edit=True)

    @app_commands.command(name="story_profile", description="Показать прогресс истории")
    async def story_profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        p = await self._progress(interaction.guild.id, interaction.user.id)
        if not p:
            await interaction.response.send_message("История ещё не начата.", ephemeral=True); return
        await interaction.response.send_message(f"Сцена: `{p['current_scene_id']}`\nXP: {p['xp']}\nCoins: {p['coins']}", ephemeral=True)

    @app_commands.command(name="story_daily", description="Открыть ежедневную сцену/награду")
    async def story_daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        p = await self._progress(interaction.guild.id, interaction.user.id)
        if not p:
            await interaction.response.send_message("Сначала /story_start.", ephemeral=True); return
        today = datetime.now(UTC).date().isoformat()
        if p["last_daily_at"] and p["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневная сцена уже получена.", ephemeral=True); return
        await self.bot.db.execute("UPDATE story_progress SET coins=coins+5, xp=xp+5, last_daily_at=?, updated_at=? WHERE guild_id=? AND user_id=?", (utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message("Ежедневная сцена: дождь принёс новую подсказку. +5 coins, +5 XP.", ephemeral=True)

    @app_commands.command(name="story_reset", description="Сбросить свой прогресс истории")
    async def story_reset(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True); return
        await self.bot.db.execute("DELETE FROM story_progress WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
        await self.bot.db.commit(); await interaction.response.send_message("Прогресс истории сброшен.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(StoryCog(bot))

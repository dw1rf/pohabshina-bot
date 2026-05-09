from __future__ import annotations

import json
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import STORY_SCENES
from services.social_game_service import utcnow_iso


class StoryChoiceSelect(discord.ui.Select):
    def __init__(self, choices: list[dict]) -> None:
        super().__init__(
            placeholder="Выберите действие",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=str(choice.get("label", "Выбор"))[:100], value=str(index)) for index, choice in enumerate(choices[:25])],
        )
        self.choices = choices

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StoryMenuView)
        await self.view.cog.apply_choice(interaction, self.choices[int(self.values[0])])


class StoryChoiceButton(discord.ui.Button):
    def __init__(self, choice: dict, index: int) -> None:
        super().__init__(label=str(choice.get("label", "Выбор"))[:80], style=discord.ButtonStyle.primary, row=0)
        self.choice = choice
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, StoryMenuView)
        await self.view.cog.apply_choice(interaction, self.choice)


class StoryMenuView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user_id: int, *, started: bool, choices: list[dict] | None = None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        choices = choices or []
        if started:
            self.remove_item(self.start)
            self.remove_item(self.help)
            if len(choices) > 5:
                self.add_item(StoryChoiceSelect(choices))
            else:
                for index, choice in enumerate(choices):
                    self.add_item(StoryChoiceButton(choice, index))
        else:
            for item in (self.profile, self.refresh, self.reset):
                self.remove_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это меню не твоей истории.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="▶️ Начать историю", style=discord.ButtonStyle.success, row=0)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_story(interaction)

    @discord.ui.button(label="👤 Профиль", style=discord.ButtonStyle.secondary, row=1)
    async def profile(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_profile(interaction)

    @discord.ui.button(label="🎁 Daily", style=discord.ButtonStyle.success, row=1)
    async def daily(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.daily(interaction)

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)

    @discord.ui.button(label="🗑️ Сбросить прогресс", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(title="Сброс прогресса", description="Точно удалить прогресс истории?", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=StoryResetConfirmView(self.cog, self.user_id))

    @discord.ui.button(label="❓ Помощь", style=discord.ButtonStyle.secondary, row=2)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="❓ Помощь по истории",
            description="Нажмите «Начать историю», затем выбирайте варианты кнопками. Прогресс, XP и coins сохраняются в БД.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=StoryBackView(self.cog, self.user_id))


class StoryBackView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это меню не твоей истории.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.primary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)


class StoryResetConfirmView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это меню не твоей истории.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Да, сбросить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.reset_progress(interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_menu(interaction)


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

    async def menu_payload(self, interaction: discord.Interaction) -> tuple[discord.Embed, discord.ui.View]:
        assert interaction.guild is not None and self.bot.db is not None
        progress = await self._progress(interaction.guild.id, interaction.user.id)
        if not progress:
            embed = discord.Embed(title="📖 История", description="Ты ещё не начал сюжет.", color=discord.Color.dark_teal())
            return embed, StoryMenuView(self, interaction.user.id, started=False)
        scene = await self._scene(progress["current_scene_id"])
        if not scene:
            embed = discord.Embed(title="📖 История", description="Сцена не найдена. Можно сбросить прогресс и начать заново.", color=discord.Color.red())
            return embed, StoryResetConfirmView(self, interaction.user.id)
        choices = json.loads(scene["choices_json"] or "[]")
        embed = discord.Embed(title=f"📖 {scene['title']}", description=scene["text"], color=discord.Color.dark_teal())
        embed.set_footer(text="Выборы обновляют это сообщение. Прогресс сохраняется.")
        return embed, StoryMenuView(self, interaction.user.id, started=True, choices=choices)

    async def refresh_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        embed, view = await self.menu_payload(interaction)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def start_story(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute("INSERT OR REPLACE INTO story_progress (guild_id, user_id, current_scene_id, updated_at) VALUES (?, ?, ?, ?)", (interaction.guild.id, interaction.user.id, "tavern_001", utcnow_iso()))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def apply_choice(self, interaction: discord.Interaction, choice: dict) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        reward = choice.get("reward", {})
        coins = int(reward.get("coins", 0))
        xp = int(reward.get("xp", 0))
        next_scene = str(choice.get("next_scene", "tavern_001"))
        await self.bot.db.execute("UPDATE story_progress SET current_scene_id=?, coins=coins+?, xp=xp+?, updated_at=? WHERE guild_id=? AND user_id=?", (next_scene, coins, xp, utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    async def show_profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        progress = await self._progress(interaction.guild.id, interaction.user.id)
        if not progress:
            await interaction.response.send_message("История ещё не начата.", ephemeral=True)
            return
        embed = discord.Embed(
            title="👤 Профиль истории",
            description=f"Сцена: `{progress['current_scene_id']}`\nXP: {progress['xp']}\nCoins: {progress['coins']}",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=StoryBackView(self, interaction.user.id))

    async def daily(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        progress = await self._progress(interaction.guild.id, interaction.user.id)
        if not progress:
            await interaction.response.send_message("Сначала начните историю.", ephemeral=True)
            return
        today = datetime.now(UTC).date().isoformat()
        if progress["last_daily_at"] and progress["last_daily_at"][:10] == today:
            await interaction.response.send_message("Ежедневная сцена уже получена.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE story_progress SET coins=coins+5, xp=xp+5, last_daily_at=?, updated_at=? WHERE guild_id=? AND user_id=?", (utcnow_iso(), utcnow_iso(), interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        embed = discord.Embed(title="🎁 Ежедневная сцена", description="Дождь принёс новую подсказку. +5 coins, +5 XP.", color=discord.Color.gold())
        await interaction.response.edit_message(embed=embed, view=StoryBackView(self, interaction.user.id))

    async def reset_progress(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM story_progress WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
        await self.bot.db.commit()
        await self.refresh_menu(interaction)

    @app_commands.command(name="story", description="Открыть интерактивное меню истории")
    async def story(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        embed, view = await self.menu_payload(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(StoryCog(bot))

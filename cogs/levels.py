from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import required_messages_for_level
from utils.leaderboard_image import (
    LeaderboardImageRow,
    make_leaderboard_file,
    make_profile_file,
    resolve_avatar_bytes,
    resolve_display_name,
)

logger = logging.getLogger(__name__)
PROFILE_BADGES = {"voice": "🎙️", "champion": "🏆", "idea": "💡", "season_one": "◆"}
PROFILE_ACCENTS = {"rose": (217, 103, 157), "plum": (151, 103, 132), "gold": (214, 183, 110)}


class LevelsCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._last_level_up_gif: str | None = None

    def _get_random_level_up_text(self) -> str:
        messages = self.bot.engagement_content.list("levelup_messages")
        if not messages:
            return "Ты отлично проявляешь себя в жизни сервера."
        return random.choice(messages)

    def _get_random_level_up_gif(self) -> str | None:
        gifs = [
            gif
            for gif in self.bot.engagement_content.list("levelup_gifs")
            if gif.startswith(("http://", "https://"))
        ]
        if not gifs:
            return None
        if len(gifs) == 1:
            self._last_level_up_gif = gifs[0]
            return gifs[0]

        choices = [gif for gif in gifs if gif != self._last_level_up_gif]
        gif_url = random.choice(choices or gifs)
        self._last_level_up_gif = gif_url
        return gif_url

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.bot.db:
            return
        automod = self.bot.get_cog("AutomodCog")
        if automod is not None and not await automod.allows_progress(message):
            return
        content = (message.content or "").strip()
        if len(content) < self.bot.settings.min_message_length:
            return

        message_count, level, level_up, counted = await self.bot.levels.update_level_progress(
            self.bot.db,
            message.guild.id,
            message.author.id,
            datetime.now(UTC),
        )
        privacy = await self.bot.social_games.get_privacy_settings(
            self.bot.db, message.guild.id, message.author.id
        )
        if counted and privacy["analytics_enabled"] and self.bot.progression_db is not None:
            await self.bot.progression.record_event(
                self.bot.progression_db,
                message.guild.id,
                message.author.id,
                "message",
                1,
                f"message:{message.channel.id}:{message.id}",
            )
            if datetime.now(UTC).hour in {22, 23, 0, 1, 2, 3, 4}:
                await self.bot.progression.record_event(
                    self.bot.progression_db,
                    message.guild.id,
                    message.author.id,
                    "message_night",
                    1,
                    f"message-night:{message.channel.id}:{message.id}",
                )
            if level_up:
                await self.bot.progression.record_event(
                    self.bot.progression_db,
                    message.guild.id,
                    message.author.id,
                    "level_up",
                    1,
                    f"level-up:{message.guild.id}:{message.author.id}:{level}",
                )
        if level_up:
            await self._send_level_up_message(message, level, message_count)

    async def _send_level_up_message(self, message: discord.Message, level: int, message_count: int) -> None:
        current_required = required_messages_for_level(level)
        next_level = min(self.bot.settings.max_level, level + 1)
        next_required = required_messages_for_level(next_level)
        span = max(next_required - current_required, 1)
        progress = 1.0 if level >= self.bot.settings.max_level else (message_count - current_required) / span
        reward = 50 + level * 10
        if self.bot.economy_db is not None and message.guild is not None:
            await self.bot.economy.change_balance(
                self.bot.economy_db,
                message.guild.id,
                message.author.id,
                reward,
                reason="level_up_reward",
                xp=level * 2,
                idempotency_key=f"level-up:{message.guild.id}:{message.author.id}:{level}",
            )
        avatar = await resolve_avatar_bytes(self.bot, message.guild, message.author.id)
        customization = await self.bot.progression.get_profile_customization(
            self.bot.progression_db, message.guild.id, message.author.id
        )
        badge = PROFILE_BADGES.get(str(customization.get("badge_key") or ""), "")
        file = make_profile_file(
            f"{message.author.display_name} {badge}".strip(),
            "Новый уровень",
            (("Уровень", str(level)), ("Сообщений", str(message_count)), ("Награда", f"{reward} монет")),
            avatar=avatar,
            progress=progress,
            theme=str(customization.get("background_key") or random.choice(("levels", "neutral", "reputation", "events"))),
            accent=PROFILE_ACCENTS.get(str(customization.get("accent_key") or "")),
            filename="level_up.png",
            description=f"{message.author.display_name} достиг(ла) уровня {level}",
        )
        await message.channel.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="rank", description="Показать уровень пользователя")
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return

        target = user or interaction.user
        row = await self.bot.levels.get_rank(self.bot.db, guild.id, target.id)
        if row is None:
            await interaction.response.send_message("У этого пользователя пока нет прогресса по уровням.", ephemeral=True)
            return

        level = int(row["level"])
        message_count = int(row["message_count"])
        next_level = min(self.bot.settings.max_level, level + 1)
        if level >= self.bot.settings.max_level:
            progress_text = "Достигнут максимальный уровень."
        else:
            current_req = required_messages_for_level(level)
            next_req = required_messages_for_level(next_level)
            progress_text = f"{message_count - current_req}/{next_req - current_req} сообщений"

        await interaction.response.defer()
        rank_position = await self.bot.levels.get_rank_position(self.bot.db, guild.id, target.id)
        avatar = await resolve_avatar_bytes(self.bot, guild, target.id)
        customization = await self.bot.progression.get_profile_customization(self.bot.progression_db, guild.id, target.id)
        badge = PROFILE_BADGES.get(str(customization.get("badge_key") or ""), "")
        if level >= self.bot.settings.max_level:
            progress = 1.0
        else:
            current_req = required_messages_for_level(level)
            next_req = required_messages_for_level(next_level)
            progress = (message_count - current_req) / max(next_req - current_req, 1)
        file = make_profile_file(
            f"{target.display_name} {badge}".strip(),
            "Ранг участника",
            (("Уровень", str(level)), ("Сообщений", str(message_count)), ("Место", f"#{rank_position}"), ("До уровня", progress_text)),
            avatar=avatar,
            progress=progress,
            theme=str(customization.get("background_key") or "levels"),
            accent=PROFILE_ACCENTS.get(str(customization.get("accent_key") or "")),
            filename="rank.png",
            description=f"Ранг {target.display_name}: уровень {level}, место {rank_position}",
        )
        await interaction.followup.send(file=file, allowed_mentions=discord.AllowedMentions.none())

    async def top(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        rows = await self.bot.levels.get_top(self.bot.db, guild.id, limit=10)
        if not rows:
            await interaction.response.send_message("Пока нет данных для топа.", ephemeral=True)
            return

        await interaction.response.defer()
        leaderboard_rows: list[LeaderboardImageRow] = []
        for row in rows:
            level = int(row["level"])
            message_count = int(row["message_count"])
            name = await resolve_display_name(self.bot, guild, int(row["user_id"]))
            avatar = await resolve_avatar_bytes(self.bot, guild, int(row["user_id"]))
            leaderboard_rows.append(
                LeaderboardImageRow(
                    name=name,
                    primary=f"Уровень {level}",
                    secondary=f"{message_count} сообщений",
                    value=message_count,
                    avatar=avatar,
                )
            )

        try:
            filename = "levels_top.png"
            file = make_leaderboard_file("ТОП УРОВНЕЙ", leaderboard_rows, filename=filename, theme="levels")
        except Exception:
            logger.exception("Failed to generate levels top image")
            await interaction.followup.send(
                "Не удалось создать графический топ. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(LevelsCog(bot))

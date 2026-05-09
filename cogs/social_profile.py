from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.social_game_service import utcnow_iso

logger = logging.getLogger(__name__)

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{3,}")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF]")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?\d[\s()\-]?){8,}")
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
STOP_WORDS = {"это", "как", "что", "или", "для", "про", "все", "тебя", "меня", "есть", "the", "and", "you"}
DISCLAIMER = "Развлекательная аналитика на основе сообщений сервера. Не является фактом."


def top_json(raw: str, limit: int = 8) -> list[tuple[str, int]]:
    try:
        return Counter(json.loads(raw or "{}")).most_common(limit)
    except (json.JSONDecodeError, TypeError):
        return []


def load_counter(raw: str) -> Counter[str]:
    try:
        return Counter(json.loads(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return Counter()


def style_from(row) -> str:
    count = max(1, int(row["message_count"]))
    total_length = int(row["total_length"] if "total_length" in row.keys() else 0)
    avg = float(row["avg_length"] if "avg_length" in row.keys() and row["avg_length"] else total_length / count)
    emoji_rate = int(row["emoji_count"]) / count
    q_rate = int(row["question_count"]) / count
    parts = ["короткие сообщения" if avg < 35 else "развёрнутые сообщения" if avg > 120 else "средняя длина сообщений"]
    if emoji_rate > 0.4:
        parts.append("много эмодзи")
    if q_rate > 0.25:
        parts.append("часто задаёт вопросы")
    if not parts:
        parts.append("спокойный стиль")
    return ", ".join(parts)


def sanitize_sample(content: str) -> str | None:
    text = content.strip().replace("\n", " ")
    if not text or INVITE_RE.search(text):
        return None
    text = URL_RE.sub("[ссылка]", text)
    text = EMAIL_RE.sub("[скрыто]", text)
    text = PHONE_RE.sub("[скрыто]", text)
    text = LONG_NUMBER_RE.sub("[скрыто]", text)
    return text[:250] if text else None


class CloneAskModal(discord.ui.Modal, title="Цифровой двойник"):
    question = discord.ui.TextInput(label="Вопрос", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, cog: "SocialProfileCog", owner_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего профиля.", ephemeral=True)
            return
        await self.cog.answer_clone(interaction, str(self.question.value))


class ProfileMenuView(discord.ui.View):
    def __init__(self, cog: "SocialProfileCog", owner_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего профиля.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.primary, row=0)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_profile_menu(interaction)

    @discord.ui.button(label="📈 Эволюция", style=discord.ButtonStyle.secondary, row=0)
    async def evolution(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_evolution(interaction)

    @discord.ui.button(label="🕸️ Связи", style=discord.ButtonStyle.secondary, row=0)
    async def graph(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_social_graph(interaction)

    @discord.ui.button(label="🧠 Цифровой двойник", style=discord.ButtonStyle.secondary, row=1)
    async def clone(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CloneAskModal(self.cog, self.owner_id))

    @discord.ui.button(label="🗑️ Удалить мои данные", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(title="Удалить данные профиля", description="Точно удалить данные профиля?", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=ProfileDeleteConfirmView(self.cog, self.owner_id))

    @discord.ui.button(label="❓ Что хранится?", style=discord.ButtonStyle.secondary, row=1)
    async def privacy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=self.cog.privacy_embed(), view=ProfileBackView(self.cog, self.owner_id))


class ProfileBackView(discord.ui.View):
    def __init__(self, cog: "SocialProfileCog", owner_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего профиля.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.primary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_profile_menu(interaction)


class ProfileDeleteConfirmView(discord.ui.View):
    def __init__(self, cog: "SocialProfileCog", owner_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это меню не твоего профиля.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Да, удалить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or self.cog.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await self.cog.bot.social_games.forget_profile_data(self.cog.bot.db, interaction.guild.id, interaction.user.id)
        embed = discord.Embed(title="Данные удалены", description="Данные профиля удалены, аналитика отключена.", color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.refresh_profile_menu(interaction)


class SocialProfileCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.db is None:
            return
        try:
            settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, message.guild.id)
            if not settings["profile_analytics_enabled"]:
                return
            privacy = await self.bot.social_games.get_privacy_settings(self.bot.db, message.guild.id, message.author.id)
            if not privacy["analytics_enabled"]:
                return
            await self._aggregate_message(message, bool(privacy["store_message_samples"]))
        except (aiosqlite.Error, discord.HTTPException):
            logger.exception("Failed to aggregate profile activity for guild=%s user=%s", message.guild.id, message.author.id)
        except Exception:
            logger.exception("Unexpected profile aggregation error for guild=%s user=%s", message.guild.id, message.author.id)

    async def _aggregate_message(self, message: discord.Message, store_sample: bool) -> None:
        assert self.bot.db is not None and message.guild is not None
        content = message.content or ""
        now = utcnow_iso()
        words = [w.lower() for w in WORD_RE.findall(content) if w.lower() not in STOP_WORDS][:50]
        word_counts = Counter(words)
        channel_counts = Counter({str(message.channel.id): 1})
        day_counts = Counter({datetime.now(UTC).date().isoformat(): 1})
        emoji_count = len(EMOJI_RE.findall(content))
        question_count = content.count("?")
        mentions = [m for m in message.mentions if not m.bot]
        mention_counts = Counter({str(member.id): 1 for member in mentions})
        reply_target_id: int | None = None
        if message.reference and isinstance(message.reference.resolved, discord.Message) and not message.reference.resolved.author.bot:
            reply_target_id = message.reference.resolved.author.id
        reply_counts = Counter({str(reply_target_id): 1}) if reply_target_id else Counter()

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO user_activity_aggregates (guild_id, user_id, updated_at) VALUES (?, ?, ?)",
            (message.guild.id, message.author.id, now),
        )
        cur = await self.bot.db.execute(
            "SELECT * FROM user_activity_aggregates WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id),
        )
        row = await cur.fetchone()
        words_total = load_counter(row["words_json"]); words_total.update(word_counts)
        channels_total = load_counter(row["channels_json"]); channels_total.update(channel_counts)
        days_total = load_counter(row["activity_days_json"] if "activity_days_json" in row.keys() else "{}"); days_total.update(day_counts)
        mentions_total = load_counter(row["mentions_json"] if "mentions_json" in row.keys() else "{}"); mentions_total.update(mention_counts)
        replies_total = load_counter(row["reply_targets_json"] if "reply_targets_json" in row.keys() else "{}"); replies_total.update(reply_counts)
        old_count = int(row["message_count"])
        new_count = old_count + 1
        total_length = int(row["total_length"]) + len(content)
        avg_length = total_length / max(1, new_count)
        sample = sanitize_sample(content) if store_sample else None
        sample_short = sample or row["sample_short"] or ""
        await self.bot.db.execute(
            """
            UPDATE user_activity_aggregates
            SET message_count=?, total_length=?, avg_length=?, emoji_count=emoji_count+?, question_count=question_count+?,
                mention_count=mention_count+?, reply_count=reply_count+?, words_json=?, channels_json=?, activity_days_json=?,
                mentions_json=?, reply_targets_json=?, sample_short=?, sample_recent=?, last_seen_at=?, updated_at=?
            WHERE guild_id=? AND user_id=?
            """,
            (
                new_count, total_length, avg_length, emoji_count, question_count, len(mentions), 1 if reply_target_id else 0,
                json.dumps(dict(words_total.most_common(100)), ensure_ascii=False),
                json.dumps(dict(channels_total.most_common(30)), ensure_ascii=False),
                json.dumps(dict(days_total.most_common(90)), ensure_ascii=False),
                json.dumps(dict(mentions_total.most_common(50)), ensure_ascii=False),
                json.dumps(dict(replies_total.most_common(50)), ensure_ascii=False),
                sample_short, sample or "", now, now, message.guild.id, message.author.id,
            ),
        )
        if sample:
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO user_message_samples (guild_id, user_id, channel_id, message_id, content_sample, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (message.guild.id, message.author.id, message.channel.id, message.id, sample, now),
            )
            await self.bot.db.execute(
                """
                DELETE FROM user_message_samples
                WHERE guild_id=? AND user_id=? AND message_id NOT IN (
                    SELECT message_id FROM user_message_samples WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT 100
                )
                """,
                (message.guild.id, message.author.id, message.guild.id, message.author.id),
            )
        week = self.bot.social_games.week_start(datetime.now(UTC))
        await self.bot.db.execute("INSERT OR IGNORE INTO user_weekly_style_stats (guild_id, user_id, week_start) VALUES (?, ?, ?)", (message.guild.id, message.author.id, week))
        await self.bot.db.execute("UPDATE user_weekly_style_stats SET message_count=message_count+1, avg_length=((avg_length*(message_count-1))+?)/message_count, emoji_count=emoji_count+?, question_count=question_count+?, words_json=?, sample=? WHERE guild_id=? AND user_id=? AND week_start=?", (len(content), emoji_count, question_count, json.dumps(dict(word_counts), ensure_ascii=False), sample or "", message.guild.id, message.author.id, week))
        for mentioned in mentions:
            if not await self._is_opted_out(message.guild.id, mentioned.id):
                await self._edge(message.guild.id, message.author.id, mentioned.id, mention=True)
        if reply_target_id and not await self._is_opted_out(message.guild.id, reply_target_id):
            await self._edge(message.guild.id, message.author.id, reply_target_id, reply=True)
        await self.bot.db.commit()

    async def _edge(self, guild_id: int, user_id: int, target_id: int, *, mention: bool = False, reply: bool = False) -> None:
        assert self.bot.db is not None
        for source, target in ((user_id, target_id), (target_id, user_id)):
            await self.bot.db.execute("INSERT OR IGNORE INTO user_social_edges (guild_id, user_id, other_user_id, updated_at) VALUES (?, ?, ?, ?)", (guild_id, source, target, utcnow_iso()))
            if mention:
                await self.bot.db.execute("UPDATE user_social_edges SET mention_count=mention_count+1, updated_at=? WHERE guild_id=? AND user_id=? AND other_user_id=?", (utcnow_iso(), guild_id, source, target))
            if reply:
                await self.bot.db.execute("UPDATE user_social_edges SET reply_count=reply_count+1, updated_at=? WHERE guild_id=? AND user_id=? AND other_user_id=?", (utcnow_iso(), guild_id, source, target))
            await self.bot.db.execute("INSERT OR IGNORE INTO social_edges (guild_id, user_id, target_user_id, updated_at) VALUES (?, ?, ?, ?)", (guild_id, source, target, utcnow_iso()))
            await self.bot.db.execute(
                "UPDATE social_edges SET weight=weight+?, replies_count=replies_count+?, mentions_count=mentions_count+?, updated_at=? WHERE guild_id=? AND user_id=? AND target_user_id=?",
                ((2 if reply else 1), 1 if reply else 0, 1 if mention else 0, utcnow_iso(), guild_id, source, target),
            )

    async def _is_opted_out(self, guild_id: int, user_id: int) -> bool:
        assert self.bot.db is not None
        privacy = await self.bot.social_games.get_privacy_settings(self.bot.db, guild_id, user_id)
        return not bool(privacy["analytics_enabled"])

    async def _activity_row(self, guild_id: int, user_id: int):
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM user_activity_aggregates WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return await cur.fetchone()

    def _not_enough_embed(self, member: discord.Member | discord.User) -> discord.Embed:
        embed = discord.Embed(
            title="Недостаточно данных",
            description="Недостаточно данных для профиля. Нужно больше сообщений на сервере.",
            color=discord.Color.light_grey(),
        )
        embed.set_footer(text=DISCLAIMER)
        return embed

    async def _profile_embed(self, guild: discord.Guild, member: discord.Member | discord.User) -> discord.Embed | None:
        row = await self._activity_row(guild.id, member.id)
        if not row or row["message_count"] < 3:
            return None
        msg_count = int(row["message_count"])
        activity = "высокая" if msg_count > 300 else "средняя" if msg_count > 50 else "низкая"
        words = ", ".join(w for w, _ in top_json(row["words_json"], 5)) or "пока не выявлены"
        sample = row["sample_short"] or "пример не сохраняется"
        style = style_from(row)
        embed = discord.Embed(
            title=f"👤 {member.display_name} | 📊 Активность: {activity}",
            description=(
                f"🎭 Стиль общения: {style}; пример: {sample}\n"
                f"🧠 Ключевые интересы: {words}\n"
                "🔍 Поведенческий паттерн: оценка строится по вопросам, эмодзи, ответам и упоминаниям без чувствительных выводов.\n"
                "💡 Как взаимодействовать: пишите уважительно, уточняйте контекст и не воспринимайте профиль как диагноз."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=DISCLAIMER)
        return embed

    def privacy_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Что хранит бот",
            description=(
                "Аналитика профиля включена по умолчанию как развлекательная функция. "
                "Бот хранит агрегаты: количество сообщений, среднюю длину, эмодзи, вопросы, частотные слова, активные каналы и связи/ответы/упоминания. "
                "Samples ограничены 100 короткими очищенными фразами на пользователя и сохраняются только если включён store_message_samples. "
                "Вложения, файлы, DM и сообщения ботов не анализируются. /forget_me удаляет данные и отключает дальнейший сбор."
            ),
            color=discord.Color.blurple(),
        )

    async def refresh_profile_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            embed = discord.Embed(title="Профиль отключён", description="Аналитика профиля отключена. /forget_me уже удаляет данные и оставляет opt-out.", color=discord.Color.light_grey())
        else:
            embed = await self._profile_embed(interaction.guild, interaction.user) or self._not_enough_embed(interaction.user)
        view = ProfileMenuView(self, interaction.user.id)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def show_evolution(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        cur = await self.bot.db.execute("SELECT * FROM user_weekly_style_stats WHERE guild_id=? AND user_id=? ORDER BY week_start ASC LIMIT 4", (interaction.guild.id, interaction.user.id))
        rows = await cur.fetchall()
        if len(rows) < 2 or sum(r["message_count"] for r in rows) < 10:
            await interaction.response.edit_message(embed=discord.Embed(title="📈 Эволюция", description="Недостаточно данных", color=discord.Color.light_grey()), view=ProfileBackView(self, interaction.user.id))
            return
        first, last = rows[0], rows[-1]
        before = "коротко" if first["avg_length"] < 40 else "развёрнуто"
        after = "коротко" if last["avg_length"] < 40 else "развёрнуто"
        progress = int(((last["avg_length"] - first["avg_length"]) / max(1, first["avg_length"])) * 100)
        embed = discord.Embed(
            title="📈 Эволюция стиля",
            description=f"{first['week_start']} → {last['week_start']}\nБыло: {before}\nСтало: {after}\nПрогресс по средней длине: {progress:+d}%\n\n{DISCLAIMER}",
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=ProfileBackView(self, interaction.user.id))

    async def show_social_graph(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        text = await self._edges_text(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(title="🕸️ Связи", description=text or "Недостаточно данных", color=discord.Color.dark_teal())
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.edit_message(embed=embed, view=ProfileBackView(self, interaction.user.id))

    async def _edges_text(self, guild_id: int, user_id: int) -> str | None:
        assert self.bot.db is not None
        cur = await self.bot.db.execute(
            """
            SELECT e.other_user_id, e.reply_count, e.mention_count, e.shared_channel_count
            FROM user_social_edges e
            LEFT JOIN user_privacy_settings p ON p.guild_id = e.guild_id AND p.user_id = e.other_user_id
            WHERE e.guild_id=? AND e.user_id=? AND COALESCE(p.analytics_enabled, 1) = 1
            ORDER BY (e.reply_count*2 + e.mention_count + e.shared_channel_count) DESC
            LIMIT 5
            """,
            (guild_id, user_id),
        )
        rows = await cur.fetchall()
        if not rows:
            return None
        max_score = max(1, max(r["reply_count"]*2 + r["mention_count"] + r["shared_channel_count"] for r in rows))
        return "\n".join(f"<@{r['other_user_id']}> — {int((r['reply_count']*2 + r['mention_count'] + r['shared_channel_count'])/max_score*100)}%: ответы/упоминания" for r in rows)

    async def answer_clone(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if any(bad in question.lower() for bad in ("пароль", "секс", "убей", "адрес", "токен")):
            await interaction.response.send_message("Двойник не отвечает на слишком личные, опасные или графичные запросы.", ephemeral=True)
            return
        row = await self._activity_row(interaction.guild.id, interaction.user.id)
        if not row or row["message_count"] < 10:
            await interaction.response.send_message("Недостаточно данных", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Симуляция стиля: вероятно, ответ был бы в стиле: **{style_from(row)}**. Это развлекательная симуляция, а не реальный ответ пользователя.",
            ephemeral=True,
        )

    @app_commands.command(name="profile", description="Показать свой развлекательный профиль")
    async def profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Аналитика профиля отключена.", ephemeral=True)
            return
        embed = await self._profile_embed(interaction.guild, interaction.user) or self._not_enough_embed(interaction.user)
        await interaction.response.send_message(embed=embed, view=ProfileMenuView(self, interaction.user.id), ephemeral=True)

    @app_commands.command(name="profile_user", description="Показать развлекательный профиль пользователя")
    async def profile_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("Профили ботов не анализируются.", ephemeral=True)
            return
        if await self._is_opted_out(interaction.guild.id, user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True)
            return
        embed = await self._profile_embed(interaction.guild, user)
        if not embed:
            await interaction.response.send_message("Недостаточно данных для профиля. Нужно больше сообщений на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="privacy", description="Показать, что бот хранит и зачем")
    async def privacy(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.privacy_embed(), ephemeral=True)

    @app_commands.command(name="forget_me", description="Удалить данные профиля и отключить аналитику")
    async def forget_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        await self.bot.social_games.forget_profile_data(self.bot.db, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("Данные профиля удалены, аналитика отключена.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(SocialProfileCog(bot))

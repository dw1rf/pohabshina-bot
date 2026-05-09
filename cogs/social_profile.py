from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.social_game_service import utcnow_iso

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{3,}")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF]")
STOP_WORDS = {"это", "как", "что", "или", "для", "про", "все", "тебя", "меня", "есть", "the", "and", "you"}
DISCLAIMER = "Развлекательная аналитика на основе активности сервера. Не является фактом."


def top_json(raw: str, limit: int = 8) -> list[tuple[str, int]]:
    try:
        return Counter(json.loads(raw or "{}")).most_common(limit)
    except (json.JSONDecodeError, TypeError):
        return []


def style_from(row) -> str:
    count = max(1, int(row["message_count"]))
    avg = int(row["total_length"]) / count
    emoji_rate = int(row["emoji_count"]) / count
    q_rate = int(row["question_count"]) / count
    parts = []
    parts.append("короткие сообщения" if avg < 35 else "развёрнутые сообщения" if avg > 120 else "средняя длина сообщений")
    if emoji_rate > 0.4: parts.append("много эмодзи")
    if q_rate > 0.25: parts.append("часто задаёт вопросы")
    return ", ".join(parts)


class SocialProfileCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._buffer: list[discord.Message] = []

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.db is None:
            return
        settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, message.guild.id)
        if not settings["profile_analytics_enabled"]:
            return
        privacy = await self.bot.social_games.get_privacy_settings(self.bot.db, message.guild.id, message.author.id)
        if not privacy["analytics_enabled"]:
            return
        await self._aggregate_message(message, bool(privacy["store_message_samples"]))

    async def _aggregate_message(self, message: discord.Message, store_sample: bool) -> None:
        assert self.bot.db is not None and message.guild is not None
        content = message.content or ""
        words = [w.lower() for w in WORD_RE.findall(content) if w.lower() not in STOP_WORDS][:50]
        word_counts = Counter(words)
        channel_counts = Counter({str(message.channel.id): 1})
        emoji_count = len(EMOJI_RE.findall(content))
        question_count = content.count("?")
        mentions = [m for m in message.mentions if not m.bot]
        mention_count = len(mentions)
        reply_count = 1 if message.reference and message.reference.resolved else 0
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO user_activity_aggregates (guild_id, user_id, updated_at) VALUES (?, ?, ?)",
            (message.guild.id, message.author.id, utcnow_iso()),
        )
        cur = await self.bot.db.execute("SELECT words_json, channels_json, message_count, total_length, sample_short FROM user_activity_aggregates WHERE guild_id=? AND user_id=?", (message.guild.id, message.author.id))
        row = await cur.fetchone()
        words_total = Counter(json.loads(row["words_json"] or "{}")); words_total.update(word_counts)
        channels_total = Counter(json.loads(row["channels_json"] or "{}")); channels_total.update(channel_counts)
        sample = content[:120] if store_sample and len(content) <= 160 else (row["sample_short"] or "")
        await self.bot.db.execute(
            """
            UPDATE user_activity_aggregates
            SET message_count=message_count+1, total_length=total_length+?, emoji_count=emoji_count+?, question_count=question_count+?,
                mention_count=mention_count+?, reply_count=reply_count+?, words_json=?, channels_json=?, sample_short=?, sample_recent=?, updated_at=?
            WHERE guild_id=? AND user_id=?
            """,
            (len(content), emoji_count, question_count, mention_count, reply_count, json.dumps(dict(words_total.most_common(100)), ensure_ascii=False), json.dumps(dict(channels_total.most_common(30))), sample, content[:120] if store_sample else "", utcnow_iso(), message.guild.id, message.author.id),
        )
        week = self.bot.social_games.week_start(datetime.now(UTC))
        await self.bot.db.execute("INSERT OR IGNORE INTO user_weekly_style_stats (guild_id, user_id, week_start) VALUES (?, ?, ?)", (message.guild.id, message.author.id, week))
        await self.bot.db.execute("UPDATE user_weekly_style_stats SET message_count=message_count+1, avg_length=((avg_length*(message_count-1))+?)/message_count, emoji_count=emoji_count+?, question_count=question_count+?, words_json=?, sample=? WHERE guild_id=? AND user_id=? AND week_start=?", (len(content), emoji_count, question_count, json.dumps(dict(word_counts), ensure_ascii=False), sample, message.guild.id, message.author.id, week))
        for mentioned in mentions:
            if not await self._is_opted_out(message.guild.id, mentioned.id):
                await self._edge(message.guild.id, message.author.id, mentioned.id, "mention_count")
        if message.reference and isinstance(message.reference.resolved, discord.Message) and not message.reference.resolved.author.bot:
            if not await self._is_opted_out(message.guild.id, message.reference.resolved.author.id):
                await self._edge(message.guild.id, message.author.id, message.reference.resolved.author.id, "reply_count")
        await self.bot.db.commit()

    async def _edge(self, guild_id: int, a: int, b: int, field: str) -> None:
        assert self.bot.db is not None
        for user_id, other_id in ((a, b), (b, a)):
            await self.bot.db.execute("INSERT OR IGNORE INTO user_social_edges (guild_id, user_id, other_user_id, updated_at) VALUES (?, ?, ?, ?)", (guild_id, user_id, other_id, utcnow_iso()))
            await self.bot.db.execute(f"UPDATE user_social_edges SET {field}={field}+1, updated_at=? WHERE guild_id=? AND user_id=? AND other_user_id=?", (utcnow_iso(), guild_id, user_id, other_id))

    async def _is_opted_out(self, guild_id: int, user_id: int) -> bool:
        assert self.bot.db is not None
        privacy = await self.bot.social_games.get_privacy_settings(self.bot.db, guild_id, user_id)
        return not bool(privacy["analytics_enabled"])

    def _not_enough_embed(self, member: discord.Member | discord.User) -> discord.Embed:
        embed = discord.Embed(
            title="Недостаточно данных",
            description=f"Недостаточно данных для профиля {member.mention}. Нужно больше активности на сервере.",
            color=discord.Color.light_grey(),
        )
        embed.set_footer(text=DISCLAIMER)
        return embed

    async def _profile_embed(self, guild: discord.Guild, member: discord.Member | discord.User) -> discord.Embed | None:
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT * FROM user_activity_aggregates WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        row = await cur.fetchone()
        if not row or row["message_count"] < 5:
            return None
        msg_count = int(row["message_count"])
        activity = "высокая" if msg_count > 300 else "средняя" if msg_count > 50 else "низкая"
        words = ", ".join(w for w, _ in top_json(row["words_json"], 5)) or "пока не выявлены"
        channels = ", ".join(f"<#{cid}>" for cid, _ in top_json(row["channels_json"], 3)) or "нет данных"
        style = style_from(row)
        sample = row["sample_short"] or "не сохраняется"
        embed = discord.Embed(
            title=f"Профиль участника: {member.display_name}",
            description=(
                f"Активность: {activity}\n"
                f"Стиль общения: {style}. Типичная фраза: {sample}\n"
                f"Ключевые интересы: {words}\n"
                "Поведенческий паттерн: оценка строится только по агрегатам длины сообщений, вопросов, эмодзи, ответов и упоминаний.\n"
                "Как взаимодействовать: пишите уважительно, уточняйте контекст и не воспринимайте профиль как факт.\n"
                f"Частые каналы: {channels}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=DISCLAIMER)
        return embed

    @app_commands.command(name="privacy", description="Показать, что бот хранит и зачем")
    async def privacy(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Аналитика профиля включена по умолчанию как развлекательная функция сервера. "
            "Бот хранит агрегаты, а не полную историю сообщений: счётчики, среднюю длину, эмодзи, вопросы, частотные слова, активные каналы и связи/ответы/упоминания. "
            "Короткие samples не сохраняются, если отдельно не включить store_message_samples. "
            "Отключить сбор можно через /profile_opt_out, удалить данные — через /forget_me.",
            ephemeral=True,
        )

    @app_commands.command(name="profile_opt_in", description="Снова включить аналитику профиля")
    async def profile_opt_in(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_profile_privacy(
            self.bot.db,
            interaction.guild.id,
            interaction.user.id,
            analytics_enabled=True,
            public_profile=True,
            matchmaking_enabled=True,
        )
        await interaction.response.send_message("Аналитика профиля снова включена.", ephemeral=True)

    @app_commands.command(name="profile_opt_out", description="Выключить анализ своих сообщений")
    async def profile_opt_out(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_profile_privacy(
            self.bot.db,
            interaction.guild.id,
            interaction.user.id,
            analytics_enabled=False,
            public_profile=False,
            matchmaking_enabled=False,
        )
        await interaction.response.send_message("Аналитика профиля отключена. Новые сообщения не будут учитываться.", ephemeral=True)

    @app_commands.command(name="profile_public", description="Устаревшая настройка публичности профиля")
    async def profile_public(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "public_profile", int(enabled))
        await interaction.response.send_message("Публичный профиль теперь доступен по умолчанию, если аналитика не отключена через /profile_opt_out.", ephemeral=True)

    @app_commands.command(name="forget_me", description="Удалить все сохранённые данные профиля")
    async def forget_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.forget_profile_data(self.bot.db, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("Данные профиля удалены, аналитика отключена.", ephemeral=True)

    @app_commands.command(name="profile", description="Показать свой развлекательный профиль")
    async def profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Аналитика профиля отключена. Включить снова можно через /profile_opt_in.", ephemeral=True); return
        embed = await self._profile_embed(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed or self._not_enough_embed(interaction.user), ephemeral=True)

    @app_commands.command(name="profile_user", description="Показать публичный развлекательный профиль пользователя")
    async def profile_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if user.bot:
            await interaction.response.send_message("Профили ботов не анализируются.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        embed = await self._profile_embed(interaction.guild, user)
        await interaction.response.send_message(embed=embed or self._not_enough_embed(user), ephemeral=True)

    @app_commands.command(name="evolution", description="Показать изменение своего стиля по неделям")
    async def evolution(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT * FROM user_weekly_style_stats WHERE guild_id=? AND user_id=? ORDER BY week_start ASC LIMIT 4", (interaction.guild.id, interaction.user.id))
        rows = await cur.fetchall()
        if len(rows) < 2 or sum(r["message_count"] for r in rows) < 10:
            await interaction.response.send_message("Недостаточно данных", ephemeral=True); return
        first, last = rows[0], rows[-1]
        before = "коротко" if first["avg_length"] < 40 else "развёрнуто"
        after = "коротко" if last["avg_length"] < 40 else "развёрнуто"
        progress = int(((last["avg_length"] - first["avg_length"]) / max(1, first["avg_length"])) * 100)
        await interaction.response.send_message(f"Твоя эволюция: {first['week_start']} → {last['week_start']}\n\nСтиль:\nБыло: {before}, эмодзи {first['emoji_count']}\nСтало: {after}, эмодзи {last['emoji_count']}\n\nТемы: смещение по топ-словам агрегируется без хранения полных сообщений.\n\nПрогресс: {progress:+d}% к средней длине диалогов\n\n{DISCLAIMER}", ephemeral=True)

    async def _edges_text(self, guild_id: int, user_id: int) -> str | None:
        assert self.bot.db is not None
        cur = await self.bot.db.execute(
            """
            SELECT e.other_user_id, e.reply_count, e.mention_count, e.shared_channel_count
            FROM user_social_edges e
            LEFT JOIN user_privacy_settings p
                ON p.guild_id = e.guild_id AND p.user_id = e.other_user_id
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

    @app_commands.command(name="social_graph", description="Показать свои связи")
    async def social_graph(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        text = await self._edges_text(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"Топ связей:\n{text}\n\n{DISCLAIMER}" if text else "Недостаточно данных", ephemeral=True)

    @app_commands.command(name="social_graph_user", description="Показать связи пользователя")
    async def social_graph_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if user.bot:
            await interaction.response.send_message("Профили ботов не анализируются.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        text = await self._edges_text(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Топ связей {user.mention}:\n{text}\n\n{DISCLAIMER}" if text else "Недостаточно данных", ephemeral=True)

    async def _send_compatibility(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if user.bot or user.id == interaction.user.id: await interaction.response.send_message("Выберите другого участника.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id) or await self._is_opted_out(interaction.guild.id, user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT reply_count, mention_count FROM user_social_edges WHERE guild_id=? AND user_id=? AND other_user_id=?", (interaction.guild.id, interaction.user.id, user.id))
        edge = await cur.fetchone()
        if not edge or (edge["reply_count"] + edge["mention_count"]) <= 0:
            await interaction.response.send_message("Недостаточно данных", ephemeral=True); return
        score = min(100, 35 + edge["reply_count"]*7 + edge["mention_count"]*4)
        await interaction.response.send_message(f"Совместимость с {user.mention}: **{score}%**. Это развлекательная метрика по агрегатам, не психологический факт.", ephemeral=True)

    @app_commands.command(name="compatibility", description="Развлекательная совместимость двух пользователей")
    async def compatibility(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await self._send_compatibility(interaction, user)

    @app_commands.command(name="match_me", description="Подобрать пользователей для общения")
    async def match_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
        if not settings["matchmaking_enabled"]: await interaction.response.send_message("Эта функция временно отключена на сервере.", ephemeral=True); return
        if await self._is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Пользователь отключил аналитику профиля.", ephemeral=True); return
        cur = await self.bot.db.execute(
            """
            SELECT a.user_id
            FROM user_activity_aggregates a
            LEFT JOIN user_privacy_settings p
                ON p.guild_id = a.guild_id AND p.user_id = a.user_id
            WHERE a.guild_id=? AND a.user_id<>?
              AND COALESCE(p.analytics_enabled, 1) = 1
              AND COALESCE(p.matchmaking_enabled, 1) = 1
            ORDER BY a.message_count DESC
            LIMIT 5
            """,
            (interaction.guild.id, interaction.user.id),
        )
        rows = await cur.fetchall()
        text = "\n".join(f"• <@{r['user_id']}> — общие темы и похожая активность" for r in rows)
        await interaction.response.send_message(f"Вам может быть интересно пообщаться с:\n{text}\n\nПредложение можно отправить вручную, без спама." if text else "Недостаточно данных", ephemeral=True)

    @app_commands.command(name="match", description="Проверить развлекательную совместимость для знакомств")
    async def match(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await self._send_compatibility(interaction, user)

    @app_commands.command(name="clone_opt_in", description="Включить шуточного цифрового двойника")
    async def clone_opt_in(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "clone_opt_in", 1)
        await interaction.response.send_message("Цифровой двойник включён. Он использует только агрегированный стиль.", ephemeral=True)

    @app_commands.command(name="clone_opt_out", description="Выключить цифрового двойника")
    async def clone_opt_out(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "clone_opt_in", 0)
        await interaction.response.send_message("Цифровой двойник выключен.", ephemeral=True)

    @app_commands.command(name="clone_settings", description="Настройки цифрового двойника")
    async def clone_settings(self, interaction: discord.Interaction, public: bool = False) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "clone_public", int(public))
        await interaction.response.send_message(f"Публичные вопросы к двойнику: {public}.", ephemeral=True)

    @app_commands.command(name="clone_ask", description="Спросить своего шуточного цифрового двойника")
    async def clone_ask(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if any(bad in question.lower() for bad in ("пароль", "секс", "убей", "адрес")):
            await interaction.response.send_message("Двойник не отвечает на слишком личные, опасные или графичные запросы.", ephemeral=True); return
        privacy = await self.bot.social_games.get_privacy_settings(self.bot.db, interaction.guild.id, interaction.user.id)
        if not privacy["clone_opt_in"]: await interaction.response.send_message("Сначала включите /clone_opt_in.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT * FROM user_activity_aggregates WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
        row = await cur.fetchone()
        if not row or row["message_count"] < 10: await interaction.response.send_message("Пока не хватает данных для шуточной симуляции.", ephemeral=True); return
        await interaction.response.send_message(f"Симуляция стиля: вероятно, ты бы ответил в стиле: **{style_from(row)}**. Это развлекательная реконструкция, не реальный ответ пользователя.", ephemeral=True)

    @app_commands.command(name="server_insights", description="Общая статистика сервера для админов")
    @app_commands.default_permissions(manage_guild=True)
    async def server_insights(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT COUNT(*) AS users, SUM(message_count) AS messages FROM user_activity_aggregates WHERE guild_id=?", (interaction.guild.id,))
        row = await cur.fetchone(); await interaction.response.send_message(f"Обезличенная статистика: пользователей с агрегатами {row['users'] or 0}, агрегированных сообщений {row['messages'] or 0}.", ephemeral=True)




async def setup(bot: MovieBot) -> None:
    await bot.add_cog(SocialProfileCog(bot))

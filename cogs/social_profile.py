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
DISCLAIMER = "Развлекательная аналитика, не является фактом."


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
        privacy = await self.bot.social_games.ensure_privacy(self.bot.db, message.guild.id, message.author.id)
        if not privacy["profile_opt_in"]:
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
            await self._edge(message.guild.id, message.author.id, mentioned.id, "mention_count")
        if message.reference and isinstance(message.reference.resolved, discord.Message) and not message.reference.resolved.author.bot:
            await self._edge(message.guild.id, message.author.id, message.reference.resolved.author.id, "reply_count")
        await self.bot.db.commit()

    async def _edge(self, guild_id: int, a: int, b: int, field: str) -> None:
        assert self.bot.db is not None
        for user_id, other_id in ((a, b), (b, a)):
            await self.bot.db.execute("INSERT OR IGNORE INTO user_social_edges (guild_id, user_id, other_user_id, updated_at) VALUES (?, ?, ?, ?)", (guild_id, user_id, other_id, utcnow_iso()))
            await self.bot.db.execute(f"UPDATE user_social_edges SET {field}={field}+1, updated_at=? WHERE guild_id=? AND user_id=? AND other_user_id=?", (utcnow_iso(), guild_id, user_id, other_id))

    async def _can_view(self, guild_id: int, viewer_id: int, target_id: int) -> bool:
        if viewer_id == target_id:
            return True
        assert self.bot.db is not None
        p = await self.bot.social_games.ensure_privacy(self.bot.db, guild_id, target_id)
        return bool(p["profile_public"])

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
        embed = discord.Embed(title=f"{member.display_name} | Активность: {activity}", color=discord.Color.blurple())
        embed.add_field(name="Стиль общения", value=f"{style_from(row)}. Типичная фраза: {row['sample_short'] or 'не сохраняется'}", inline=False)
        embed.add_field(name="Ключевые интересы", value=words, inline=False)
        embed.add_field(name="Поведенческий паттерн", value="Оценка строится только по агрегатам: длина сообщений, вопросы, эмодзи, ответы и упоминания.", inline=False)
        embed.add_field(name="Как взаимодействовать", value="Пишите уважительно, уточняйте контекст и не воспринимайте профиль как факт.", inline=False)
        embed.add_field(name="Частые каналы", value=channels, inline=False)
        embed.set_footer(text=DISCLAIMER)
        return embed

    @app_commands.command(name="privacy", description="Показать, что бот хранит и зачем")
    async def privacy(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Бот хранит только opt-in агрегаты: счётчики сообщений, частотные слова, каналы, эмодзи, ответы/упоминания, недельные сводки и игровые данные. Полные сообщения не сохраняются, если пользователь отдельно не включил samples. /forget_me удаляет ваши данные.", ephemeral=True)

    @app_commands.command(name="profile_opt_in", description="Включить анализ своих сообщений")
    async def profile_opt_in(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "profile_opt_in", 1)
        await interaction.response.send_message("Анализ включён. Бот будет хранить агрегаты, а не полные сообщения.", ephemeral=True)

    @app_commands.command(name="profile_opt_out", description="Выключить анализ своих сообщений")
    async def profile_opt_out(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "profile_opt_in", 0)
        await interaction.response.send_message("Анализ выключен. Ранее сохранённые данные можно удалить через /forget_me.", ephemeral=True)

    @app_commands.command(name="profile_public", description="Разрешить или запретить публичный профиль")
    async def profile_public(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.set_privacy_flag(self.bot.db, interaction.guild.id, interaction.user.id, "profile_public", int(enabled))
        await interaction.response.send_message(f"Публичный профиль: {enabled}.", ephemeral=True)

    @app_commands.command(name="forget_me", description="Удалить все сохранённые данные пользователя")
    async def forget_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await self.bot.social_games.forget_user(self.bot.db, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("Ваши данные удалены.", ephemeral=True)

    @app_commands.command(name="profile", description="Показать свой развлекательный профиль")
    async def profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        embed = await self._profile_embed(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, content=None if embed else "Недостаточно данных. Включите /profile_opt_in и пообщайтесь на сервере.", ephemeral=True)

    @app_commands.command(name="profile_user", description="Показать профиль пользователя, если он разрешил публичный просмотр")
    async def profile_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if not await self._can_view(interaction.guild.id, interaction.user.id, user.id):
            await interaction.response.send_message("Пользователь не разрешил публичный просмотр профиля.", ephemeral=True); return
        embed = await self._profile_embed(interaction.guild, user)
        await interaction.response.send_message(embed=embed, content=None if embed else "Недостаточно данных.", ephemeral=True)

    @app_commands.command(name="evolution", description="Показать изменение своего стиля по неделям")
    async def evolution(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT * FROM user_weekly_style_stats WHERE guild_id=? AND user_id=? ORDER BY week_start ASC LIMIT 4", (interaction.guild.id, interaction.user.id))
        rows = await cur.fetchall()
        if len(rows) < 2 or sum(r["message_count"] for r in rows) < 10:
            await interaction.response.send_message("Недостаточно данных для эволюции.", ephemeral=True); return
        first, last = rows[0], rows[-1]
        before = "коротко" if first["avg_length"] < 40 else "развёрнуто"
        after = "коротко" if last["avg_length"] < 40 else "развёрнуто"
        progress = int(((last["avg_length"] - first["avg_length"]) / max(1, first["avg_length"])) * 100)
        await interaction.response.send_message(f"Твоя эволюция: {first['week_start']} → {last['week_start']}\n\nСтиль:\nБыло: {before}, эмодзи {first['emoji_count']}\nСтало: {after}, эмодзи {last['emoji_count']}\n\nТемы: смещение по топ-словам агрегируется без хранения полных сообщений.\n\nПрогресс: {progress:+d}% к средней длине диалогов\n\n{DISCLAIMER}", ephemeral=True)

    async def _edges_text(self, guild_id: int, user_id: int) -> str:
        assert self.bot.db is not None
        cur = await self.bot.db.execute("SELECT other_user_id, reply_count, mention_count, shared_channel_count FROM user_social_edges WHERE guild_id=? AND user_id=? ORDER BY (reply_count*2 + mention_count + shared_channel_count) DESC LIMIT 5", (guild_id, user_id))
        rows = await cur.fetchall()
        if not rows: return "Связей пока не найдено."
        max_score = max(1, max(r["reply_count"]*2 + r["mention_count"] + r["shared_channel_count"] for r in rows))
        return "\n".join(f"<@{r['other_user_id']}> — {int((r['reply_count']*2 + r['mention_count'] + r['shared_channel_count'])/max_score*100)}%: ответы/упоминания" for r in rows)

    @app_commands.command(name="social_graph", description="Показать свои связи")
    async def social_graph(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        await interaction.response.send_message(f"Топ связей:\n{await self._edges_text(interaction.guild.id, interaction.user.id)}\n\n{DISCLAIMER}", ephemeral=True)

    @app_commands.command(name="social_graph_user", description="Показать связи пользователя при публичном consent")
    async def social_graph_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or not await self._can_view(interaction.guild.id, interaction.user.id, user.id):
            await interaction.response.send_message("Нет публичного разрешения.", ephemeral=True); return
        await interaction.response.send_message(f"Топ связей {user.mention}:\n{await self._edges_text(interaction.guild.id, user.id)}\n\n{DISCLAIMER}", ephemeral=True)

    async def _send_compatibility(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        if user.bot or user.id == interaction.user.id: await interaction.response.send_message("Выберите другого участника.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT reply_count, mention_count FROM user_social_edges WHERE guild_id=? AND user_id=? AND other_user_id=?", (interaction.guild.id, interaction.user.id, user.id))
        edge = await cur.fetchone(); score = min(100, 35 + (edge["reply_count"]*7 + edge["mention_count"]*4 if edge else 0))
        await interaction.response.send_message(f"Совместимость с {user.mention}: **{score}%**. Это развлекательная метрика по агрегатам, не психологический факт.", ephemeral=True)

    @app_commands.command(name="compatibility", description="Развлекательная совместимость двух пользователей")
    async def compatibility(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await self._send_compatibility(interaction, user)

    @app_commands.command(name="match_me", description="Подобрать пользователей для общения")
    async def match_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None: await interaction.response.send_message("Только на сервере.", ephemeral=True); return
        settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
        if not settings["matchmaking_enabled"]: await interaction.response.send_message("Matchmaking выключен на сервере.", ephemeral=True); return
        cur = await self.bot.db.execute("SELECT user_id FROM user_privacy_settings WHERE guild_id=? AND profile_public=1 AND profile_opt_in=1 AND user_id<>? LIMIT 5", (interaction.guild.id, interaction.user.id))
        rows = await cur.fetchall(); text = "\n".join(f"• <@{r['user_id']}> — общие темы и похожая активность" for r in rows) or "Пока нет участников с opt-in."
        await interaction.response.send_message(f"Вам может быть интересно пообщаться с:\n{text}\n\nПредложение можно отправить вручную, без спама.", ephemeral=True)

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
        privacy = await self.bot.social_games.ensure_privacy(self.bot.db, interaction.guild.id, interaction.user.id)
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
        row = await cur.fetchone(); await interaction.response.send_message(f"Обезличенная статистика: opt-in пользователей {row['users'] or 0}, агрегированных сообщений {row['messages'] or 0}.", ephemeral=True)




async def setup(bot: MovieBot) -> None:
    await bot.add_cog(SocialProfileCog(bot))

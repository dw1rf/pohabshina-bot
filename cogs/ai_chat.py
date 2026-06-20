from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from services.ai_persona_service import AIPersonaService

logger = logging.getLogger(__name__)

AI_HTTP_TIMEOUT_SECONDS = 45
MAX_DISCORD_RESPONSE_LENGTH = 1800
GROQ_LIMIT_MESSAGE = "Groq задушил меня лимитами. Мой цифровой череп перегрелся, попробуй позже."
DAILY_LIMIT_MESSAGE = "На сегодня мой мозг высох. Лимит AI-запросов сожран."
RELATIONS = {"favorite", "neutral", "rival", "ignored", "cursed"}
MOODS = {"neutral", "playful", "annoyed", "creepy", "sleepy", "friday_chaos", "horny_chaos"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
REACTION_BY_MOOD = {
    "question": "🤨",
    "joke": "💀",
    "aggressive": "👁️",
    "horny": "🫦",
    "sad": "🕯️",
    "chaotic": "🧨",
    "neutral": "👀",
}


@dataclass(slots=True)
class AIRuntimeConfig:
    persona: str
    random_reply_chance: float
    global_cooldown_seconds: int
    user_cooldown_seconds: int
    daily_limit: int
    context_limit_chars: int
    memory_days: int
    aliases: tuple[str, ...]
    enable_memory: bool
    enable_random_replies: bool
    enable_reactions: bool
    enable_image_describe: bool
    max_prompt_length: int
    max_output_tokens: int
    temperature: float


class AIRateLimited(Exception):
    pass


class AfishaConfirmView(discord.ui.View):
    def __init__(self, author_id: int, target_channel: discord.TextChannel, text: str) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.target_channel = target_channel
        self.text = text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Эта кнопка не для твоих пальцев.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Опубликовать", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.target_channel.send(self.text, allowed_mentions=discord.AllowedMentions.none())
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Афиша опубликована.", view=self)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Публикация отменена.", view=self)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on", "y", "да"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(_env(name, str(default)) or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(_env(name, str(default)) or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_aliases() -> tuple[str, ...]:
    raw = _env("AI_BOT_ALIASES", "мурка,бот,пахабщина,пахаб")
    aliases = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return aliases or ("бот",)


def _clean_prompt(prompt: str) -> str:
    return " ".join(str(prompt or "").replace("\r", " ").replace("\n", " ").split())


def _can_manage_ai(user: discord.Member | discord.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    permissions = user.guild_permissions
    return permissions.administrator or permissions.manage_guild


def _parse_period_days(period: str, default: int = 30) -> int:
    match = re.fullmatch(r"\s*(\d{1,3})\s*d?\s*", str(period or ""), re.IGNORECASE)
    if not match:
        return default
    return max(1, min(365, int(match.group(1))))


async def _post_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, str]:
    timeout = aiohttp.ClientTimeout(total=AI_HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            text = await response.text()
            data: dict[str, Any] | None = None
            if text:
                try:
                    parsed = await response.json(content_type=None)
                    if isinstance(parsed, dict):
                        data = parsed
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
            return response.status, data, text[:1000]


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for candidate in payload.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


def _extract_openai_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if output_text:
        return str(output_text).strip()

    parts: list[str] = []
    for output in payload.get("output", []) or []:
        for content in output.get("content", []) or []:
            text = content.get("text")
            if text:
                parts.append(str(text))
    if parts:
        return "\n".join(parts).strip()

    choices = payload.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


async def _generate_gemini_response(system_prompt: str, user_prompt: str, config: AIRuntimeConfig) -> str:
    api_key = _env("GEMINI_API_KEY")
    if not api_key:
        logger.error("AI_PROVIDER=gemini, but GEMINI_API_KEY is not configured")
        return "Gemini API не настроен: добавьте GEMINI_API_KEY в .env."

    model = _env("GEMINI_MODEL", "gemini-2.5-pro") or "gemini-2.5-pro"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": config.max_output_tokens,
            "temperature": config.temperature,
        },
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    try:
        status, data, raw_text = await _post_json(url, headers=headers, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Gemini request failed")
        return "AI сейчас недоступен: Gemini не отвечает."

    if status == 429:
        raise AIRateLimited
    if status in {401, 403}:
        logger.error("Gemini auth error %s: %s", status, raw_text)
        return "Gemini API отклонил ключ. Проверьте GEMINI_API_KEY."
    if status >= 400:
        logger.error("Gemini API error %s: %s", status, raw_text)
        return "Gemini API вернул ошибку. Попробуйте позже."
    if not data:
        logger.error("Gemini returned an empty or invalid JSON response: %s", raw_text)
        return "Gemini вернул пустой ответ."

    return _extract_gemini_text(data) or "Gemini не вернул текстовый ответ."


async def _generate_ollama_response(system_prompt: str, user_prompt: str, config: AIRuntimeConfig) -> str:
    base_url = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"
    model = _env("OLLAMA_MODEL", "gemma3") or "gemma3"
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": config.temperature,
            "num_predict": config.max_output_tokens,
        },
    }

    try:
        status, data, raw_text = await _post_json(url, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Ollama request failed")
        return "AI сейчас недоступен: Ollama не отвечает."

    if status == 429:
        raise AIRateLimited
    if status == 404:
        logger.error("Ollama model or endpoint was not found: %s", raw_text)
        return "Ollama не нашла модель или endpoint. Проверьте OLLAMA_MODEL и OLLAMA_BASE_URL."
    if status >= 400:
        logger.error("Ollama API error %s: %s", status, raw_text)
        return "Ollama вернула ошибку. Попробуйте позже."
    if not data:
        return "Ollama вернула пустой ответ."

    message = data.get("message") or {}
    return str(message.get("content") or "").strip() or "Ollama не вернула текстовый ответ."


async def _generate_groq_response(system_prompt: str, user_prompt: str, config: AIRuntimeConfig) -> str:
    api_key = _env("GROQ_API_KEY")
    if not api_key:
        logger.error("AI_PROVIDER=groq, but GROQ_API_KEY is not configured")
        return "Groq API не настроен: добавьте GROQ_API_KEY в .env."

    model = _env("GROQ_MODEL", "openai/gpt-oss-120b") or "openai/gpt-oss-120b"
    url = "https://api.groq.com/openai/v1/responses"
    payload = {
        "model": model,
        "instructions": system_prompt,
        "input": user_prompt,
        "temperature": config.temperature,
        "max_output_tokens": config.max_output_tokens,
        "reasoning": {"effort": "low"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        status, data, raw_text = await _post_json(url, headers=headers, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Groq request failed")
        return "AI сейчас недоступен: Groq не отвечает."

    if status == 429:
        logger.warning("Groq rate limit response: %s", raw_text)
        raise AIRateLimited
    if status in {401, 403}:
        logger.error("Groq auth error %s: %s", status, raw_text)
        return "Groq API отклонил ключ. Проверьте GROQ_API_KEY."
    if status >= 400:
        logger.error("Groq API error %s: %s", status, raw_text)
        return "Groq API вернул ошибку. Попробуйте позже."
    if not data:
        return "Groq вернул пустой ответ."

    return _extract_openai_response_text(data) or "Groq не вернул текстовый ответ."


async def generate_ai_response(system_prompt: str, user_prompt: str, config: AIRuntimeConfig) -> str:
    provider = (_env("AI_PROVIDER", "groq") or "groq").lower()
    if provider == "gemini":
        return await _generate_gemini_response(system_prompt, user_prompt, config)
    if provider == "groq":
        return await _generate_groq_response(system_prompt, user_prompt, config)
    if provider == "ollama":
        return await _generate_ollama_response(system_prompt, user_prompt, config)

    logger.error("Unknown AI_PROVIDER configured: %s", provider)
    return "AI_PROVIDER неизвестен. Используйте gemini, groq или ollama."


class AIChatCog(commands.Cog):
    ai_group = app_commands.Group(name="ai", description="AI-NPC сервера")
    relation_group = app_commands.Group(name="ai_relation", description="Отношения AI-NPC к пользователям")
    config_group = app_commands.Group(name="ai_config", description="Настройки AI-NPC")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.persona = AIPersonaService()
        self._global_reply_cooldowns: dict[int, float] = {}
        self._user_reply_cooldowns: dict[tuple[int, int], float] = {}
        self._reaction_cooldowns: dict[tuple[int, int], float] = {}

    def runtime_config(self) -> AIRuntimeConfig:
        return AIRuntimeConfig(
            persona=_env("AI_PERSONA", "pohab_npc"),
            random_reply_chance=_env_float("AI_RANDOM_REPLY_CHANCE", 0.04, minimum=0.0, maximum=0.25),
            global_cooldown_seconds=_env_int("AI_GLOBAL_COOLDOWN_SECONDS", 60, minimum=0),
            user_cooldown_seconds=_env_int("AI_USER_COOLDOWN_SECONDS", 120, minimum=0),
            daily_limit=_env_int("AI_DAILY_LIMIT", 700, minimum=1),
            context_limit_chars=_env_int("AI_CONTEXT_LIMIT_CHARS", 1500, minimum=300),
            memory_days=_env_int("AI_MEMORY_DAYS", 30, minimum=1),
            aliases=_env_aliases(),
            enable_memory=_env_bool("AI_ENABLE_MEMORY", True),
            enable_random_replies=_env_bool("AI_ENABLE_RANDOM_REPLIES", True),
            enable_reactions=_env_bool("AI_ENABLE_REACTIONS", True),
            enable_image_describe=_env_bool("AI_ENABLE_IMAGE_DESCRIBE", False),
            max_prompt_length=_env_int("AI_MAX_PROMPT_LENGTH", 1800, minimum=200),
            max_output_tokens=_env_int("AI_MAX_OUTPUT_TOKENS", 500, minimum=64),
            temperature=_env_float("AI_TEMPERATURE", 0.75, minimum=0.0, maximum=2.0),
        )

    async def init_db(self) -> None:
        if self.bot.db is None:
            logger.error("AI chat cog loaded without database connection")
            return
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS ai_message_memory (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_clean TEXT NOT NULL,
                mood TEXT NOT NULL DEFAULT 'neutral',
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ai_memory_user
            ON ai_message_memory (guild_id, user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_ai_memory_channel
            ON ai_message_memory (guild_id, channel_id, created_at);

            CREATE TABLE IF NOT EXISTS ai_user_relations (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                relation TEXT NOT NULL DEFAULT 'neutral',
                affection INTEGER NOT NULL DEFAULT 0,
                irritation INTEGER NOT NULL DEFAULT 0,
                nickname TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ai_guild_mood (
                guild_id INTEGER PRIMARY KEY,
                mood TEXT NOT NULL DEFAULT 'neutral',
                irritation INTEGER NOT NULL DEFAULT 0,
                chaos INTEGER NOT NULL DEFAULT 0,
                last_random_reply_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ai_usage_daily (
                guild_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                requests_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, date)
            );

            CREATE TABLE IF NOT EXISTS ai_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                random_replies_enabled INTEGER,
                reactions_enabled INTEGER,
                updated_at TEXT NOT NULL
            );
            """
        )
        await self.bot.db.commit()
        await self.cleanup_old_memory()

    async def _is_ai_channel_enabled(self, guild_id: int, channel_id: int) -> bool:
        if self.bot.db is None:
            return False
        cursor = await self.bot.db.execute(
            "SELECT enabled FROM ai_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        row = await cursor.fetchone()
        return bool(row and int(row["enabled"]))

    async def _set_ai_channel(self, guild_id: int, channel_id: int, enabled: bool) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute(
            """
            INSERT INTO ai_channels (guild_id, channel_id, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET enabled = excluded.enabled
            """,
            (guild_id, channel_id, int(enabled)),
        )
        await self.bot.db.commit()

    async def _get_setting_bool(self, guild_id: int, column: str, default: bool) -> bool:
        if self.bot.db is None:
            return default
        cursor = await self.bot.db.execute(
            f"SELECT {column} FROM ai_guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row or row[column] is None:
            return default
        return bool(int(row[column]))

    async def _set_setting_bool(self, guild_id: int, column: str, enabled: bool) -> None:
        assert self.bot.db is not None
        now = datetime.now(UTC).isoformat()
        await self.bot.db.execute(
            """
            INSERT INTO ai_guild_settings (guild_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (guild_id, now),
        )
        await self.bot.db.execute(
            f"UPDATE ai_guild_settings SET {column} = ?, updated_at = ? WHERE guild_id = ?",
            (int(enabled), now, guild_id),
        )
        await self.bot.db.commit()

    async def cleanup_old_memory(self) -> None:
        if self.bot.db is None:
            return
        config = self.runtime_config()
        cutoff = (datetime.now(UTC) - timedelta(days=config.memory_days)).isoformat()
        await self.bot.db.execute("DELETE FROM ai_message_memory WHERE created_at < ?", (cutoff,))
        await self.bot.db.commit()

    async def save_message_memory(self, message: discord.Message, mood: str) -> None:
        if self.bot.db is None or message.guild is None:
            return
        config = self.runtime_config()
        if not config.enable_memory or message.author.bot:
            return

        cleaned = self.persona.clean_memory_text(message.clean_content)
        if len(cleaned) < 2:
            return

        await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO ai_message_memory
            (guild_id, channel_id, user_id, message_id, content_clean, mood, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.guild.id,
                message.channel.id,
                message.author.id,
                message.id,
                cleaned,
                mood,
                datetime.now(UTC).isoformat(),
            ),
        )
        await self.bot.db.commit()

    async def get_relation(self, guild_id: int, user_id: int) -> dict[str, Any]:
        if self.bot.db is None:
            return {"relation": "neutral", "affection": 0, "irritation": 0, "nickname": None}
        cursor = await self.bot.db.execute(
            """
            SELECT relation, affection, irritation, nickname
            FROM ai_user_relations
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "relation": row["relation"],
                "affection": int(row["affection"]),
                "irritation": int(row["irritation"]),
                "nickname": row["nickname"],
            }
        now = datetime.now(UTC).isoformat()
        await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO ai_user_relations (guild_id, user_id, relation, updated_at)
            VALUES (?, ?, 'neutral', ?)
            """,
            (guild_id, user_id, now),
        )
        await self.bot.db.commit()
        return {"relation": "neutral", "affection": 0, "irritation": 0, "nickname": None}

    async def set_relation(self, guild_id: int, user_id: int, relation: str, nickname: str | None = None) -> None:
        assert self.bot.db is not None
        now = datetime.now(UTC).isoformat()
        await self.bot.db.execute(
            """
            INSERT INTO ai_user_relations (guild_id, user_id, relation, affection, irritation, nickname, updated_at)
            VALUES (?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                relation = excluded.relation,
                nickname = excluded.nickname,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, relation, nickname, now),
        )
        await self.bot.db.commit()

    async def reset_relation(self, guild_id: int, user_id: int) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute(
            "DELETE FROM ai_user_relations WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.bot.db.commit()

    async def adjust_relation_from_message(self, message: discord.Message, mood: str, directly_addressed: bool) -> None:
        if self.bot.db is None or message.guild is None:
            return
        current = await self.get_relation(message.guild.id, message.author.id)
        relation = current["relation"]
        if relation in {"ignored", "cursed"}:
            return

        content = message.clean_content.lower()
        affection_delta = 1 if directly_addressed and mood in {"question", "joke", "neutral"} else 0
        irritation_delta = 1 if directly_addressed and (mood in {"aggressive", "chaotic"} or "бот" in content and "туп" in content) else 0
        if not affection_delta and not irritation_delta:
            return

        affection = int(current["affection"]) + affection_delta
        irritation = int(current["irritation"]) + irritation_delta
        if relation == "neutral" and affection >= 10:
            relation = "favorite"
        if relation == "neutral" and irritation >= 10:
            relation = "rival"

        await self.bot.db.execute(
            """
            UPDATE ai_user_relations
            SET relation = ?, affection = ?, irritation = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (relation, affection, irritation, datetime.now(UTC).isoformat(), message.guild.id, message.author.id),
        )
        await self.bot.db.commit()

    async def get_guild_mood(self, guild_id: int) -> dict[str, Any]:
        if self.bot.db is None:
            return {"mood": "neutral", "irritation": 0, "chaos": 0}
        cursor = await self.bot.db.execute(
            "SELECT mood, irritation, chaos, last_random_reply_at FROM ai_guild_mood WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "mood": row["mood"],
                "irritation": int(row["irritation"]),
                "chaos": int(row["chaos"]),
                "last_random_reply_at": row["last_random_reply_at"],
            }
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO ai_guild_mood (guild_id, mood) VALUES (?, 'neutral')",
            (guild_id,),
        )
        await self.bot.db.commit()
        return {"mood": "neutral", "irritation": 0, "chaos": 0, "last_random_reply_at": None}

    async def set_guild_mood(self, guild_id: int, mood: str) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute(
            """
            INSERT INTO ai_guild_mood (guild_id, mood, irritation, chaos)
            VALUES (?, ?, 0, 0)
            ON CONFLICT(guild_id) DO UPDATE SET mood = excluded.mood
            """,
            (guild_id, mood),
        )
        await self.bot.db.commit()

    async def update_guild_mood_from_message(self, guild_id: int, mood: str) -> None:
        if self.bot.db is None:
            return
        current = await self.get_guild_mood(guild_id)
        chaos = max(0, min(25, int(current["chaos"]) + (1 if mood in {"chaotic", "aggressive"} else -1)))
        irritation = max(0, min(25, int(current["irritation"]) + (1 if mood == "aggressive" else -1)))

        now = datetime.now()
        server_mood = str(current["mood"])
        if now.weekday() == 4:
            server_mood = "friday_chaos"
        elif now.hour in {0, 1, 2, 3, 4, 5}:
            server_mood = "creepy" if chaos >= 5 else "sleepy"
        elif mood == "horny" and chaos >= 4:
            server_mood = "horny_chaos"
        elif mood == "joke":
            server_mood = "playful"
        elif irritation >= 8:
            server_mood = "annoyed"
        elif chaos >= 10:
            server_mood = "creepy"
        elif server_mood not in {"friday_chaos", "sleepy", "creepy"}:
            server_mood = "neutral"

        await self.bot.db.execute(
            """
            INSERT INTO ai_guild_mood (guild_id, mood, irritation, chaos)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                mood = excluded.mood,
                irritation = excluded.irritation,
                chaos = excluded.chaos
            """,
            (guild_id, server_mood, irritation, chaos),
        )
        await self.bot.db.commit()

    async def build_context(self, guild_id: int, channel_id: int, user_id: int, relation: dict[str, Any]) -> str:
        if self.bot.db is None:
            return ""
        config = self.runtime_config()
        parts: list[str] = []

        cursor = await self.bot.db.execute(
            """
            SELECT user_id, content_clean
            FROM ai_message_memory
            WHERE guild_id = ? AND channel_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (guild_id, channel_id),
        )
        channel_rows = list(reversed(await cursor.fetchall()))
        if channel_rows:
            lines = [f"User{row['user_id']}: {row['content_clean']}" for row in channel_rows]
            parts.append("[Память канала]\n" + "\n".join(lines))

        cursor = await self.bot.db.execute(
            """
            SELECT content_clean
            FROM ai_message_memory
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (guild_id, user_id),
        )
        user_rows = list(reversed(await cursor.fetchall()))
        if user_rows:
            lines = [f"- {row['content_clean']}" for row in user_rows]
            parts.append("[Память пользователя]\n" + "\n".join(lines))

        guild_mood = await self.get_guild_mood(guild_id)
        parts.append(
            "[Отношение]\n"
            f"relation: {relation['relation']}\n"
            f"affection: {relation['affection']}\n"
            f"irritation: {relation['irritation']}\n"
            f"nickname: {relation['nickname'] or 'не задан'}\n"
            f"guild_mood: {guild_mood['mood']}"
        )

        context = "\n\n".join(parts)
        if len(context) <= config.context_limit_chars:
            return context
        return context[-config.context_limit_chars :]

    async def can_use_ai(self, guild_id: int, user_id: int) -> tuple[bool, str | None]:
        if self.bot.db is None:
            return False, "База данных не готова, мой архив пока закрыт."
        config = self.runtime_config()
        today = datetime.now(UTC).date().isoformat()
        cursor = await self.bot.db.execute(
            "SELECT requests_count FROM ai_usage_daily WHERE guild_id = ? AND date = ?",
            (guild_id, today),
        )
        row = await cursor.fetchone()
        if row and int(row["requests_count"]) >= config.daily_limit:
            return False, DAILY_LIMIT_MESSAGE

        now = time.monotonic()
        global_last = self._global_reply_cooldowns.get(guild_id, 0.0)
        if now - global_last < config.global_cooldown_seconds:
            return False, None
        user_key = (guild_id, user_id)
        user_last = self._user_reply_cooldowns.get(user_key, 0.0)
        if now - user_last < config.user_cooldown_seconds:
            return False, None
        return True, None

    async def increment_ai_usage(self, guild_id: int, user_id: int) -> None:
        if self.bot.db is None:
            return
        today = datetime.now(UTC).date().isoformat()
        await self.bot.db.execute(
            """
            INSERT INTO ai_usage_daily (guild_id, date, requests_count)
            VALUES (?, ?, 1)
            ON CONFLICT(guild_id, date) DO UPDATE SET requests_count = requests_count + 1
            """,
            (guild_id, today),
        )
        await self.bot.db.commit()
        now = time.monotonic()
        self._global_reply_cooldowns[guild_id] = now
        self._user_reply_cooldowns[(guild_id, user_id)] = now

    async def daily_usage(self, guild_id: int) -> int:
        if self.bot.db is None:
            return 0
        today = datetime.now(UTC).date().isoformat()
        cursor = await self.bot.db.execute(
            "SELECT requests_count FROM ai_usage_daily WHERE guild_id = ? AND date = ?",
            (guild_id, today),
        )
        row = await cursor.fetchone()
        return int(row["requests_count"]) if row else 0

    async def _send_ai_response(self, interaction: discord.Interaction, prompt: str, *, ephemeral: bool) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        config = self.runtime_config()
        clean_prompt = _clean_prompt(prompt)
        if not clean_prompt:
            await interaction.response.send_message("Напишите вопрос для AI.", ephemeral=True)
            return
        if len(clean_prompt) > config.max_prompt_length:
            await interaction.response.send_message(
                f"Prompt слишком длинный. Максимум: {config.max_prompt_length} символов.",
                ephemeral=True,
            )
            return

        can_use, reason = await self.can_use_ai(interaction.guild.id, interaction.user.id)
        if not can_use:
            await interaction.response.send_message(reason or "Мой цифровой череп на cooldown. Попробуй позже.", ephemeral=True)
            return

        relation = await self.get_relation(interaction.guild.id, interaction.user.id)
        guild_mood = await self.get_guild_mood(interaction.guild.id)
        system_prompt = self.persona.build_system_prompt(
            interaction.user.display_name,
            relation["relation"],
            guild_mood["mood"],
            relation["nickname"],
        )
        context = await self.build_context(interaction.guild.id, interaction.channel_id or 0, interaction.user.id, relation)
        user_prompt = f"{context}\n\n[Новое сообщение]\n{interaction.user.display_name}: {clean_prompt}".strip()

        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        try:
            answer = await generate_ai_response(system_prompt, user_prompt, config)
        except AIRateLimited:
            answer = GROQ_LIMIT_MESSAGE
        else:
            await self.increment_ai_usage(interaction.guild.id, interaction.user.id)

        answer = self.persona.sanitize_ai_output(answer)
        await interaction.followup.send(answer, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())

    @ai_group.command(name="ask", description="Задать вопрос AI-NPC")
    @app_commands.describe(prompt="Текст вопроса")
    async def ai_ask(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._send_ai_response(interaction, prompt, ephemeral=False)

    @ai_group.command(name="private", description="Задать вопрос AI-NPC приватно")
    @app_commands.describe(prompt="Текст вопроса")
    async def ai_private(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._send_ai_response(interaction, prompt, ephemeral=True)

    @ai_group.command(name="channel_enable", description="Включить AI-NPC в текущем канале")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ai_channel_enable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        await self._set_ai_channel(interaction.guild.id, interaction.channel_id, True)
        await interaction.response.send_message("AI-NPC включён в этом канале.", ephemeral=True)

    @ai_group.command(name="channel_disable", description="Выключить AI-NPC в текущем канале")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ai_channel_disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        await self._set_ai_channel(interaction.guild.id, interaction.channel_id, False)
        await interaction.response.send_message("AI-NPC выключен в этом канале.", ephemeral=True)

    @ai_group.command(name="history", description="Поиск по сохранённой AI-памяти")
    @app_commands.describe(query="Что искать", period="Период в днях, например 30d")
    @app_commands.guild_only()
    async def ai_history(self, interaction: discord.Interaction, query: str, period: str = "30d") -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        clean_query = self.persona.clean_memory_text(query)
        if len(clean_query) < 2:
            await interaction.response.send_message("Слишком короткий запрос для архива.", ephemeral=True)
            return
        days = _parse_period_days(period)
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cursor = await self.bot.db.execute(
            """
            SELECT user_id, channel_id, content_clean, created_at
            FROM ai_message_memory
            WHERE guild_id = ? AND created_at >= ? AND content_clean LIKE ?
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (interaction.guild.id, cutoff, f"%{clean_query}%"),
        )
        rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("В моём грязном архиве по этому поводу пусто.", ephemeral=True)
            return

        lines = []
        for row in rows:
            created = str(row["created_at"]).split("T", 1)[0]
            lines.append(f"`{created}` <#{row['channel_id']}> User{row['user_id']}: {row['content_clean'][:160]}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @ai_group.command(name="creepy", description="Отправить локальную криповую картинку из assets/creepy")
    @app_commands.guild_only()
    async def ai_creepy(self, interaction: discord.Interaction) -> None:
        creepy_dir = Path("assets") / "creepy"
        files = [path for path in creepy_dir.glob("*") if path.is_file()] if creepy_dir.exists() else []
        if not files:
            await interaction.response.send_message("В подвале логов пусто: `assets/creepy` не содержит картинок.", ephemeral=True)
            return
        captions = [
            "Я нашёл это в подвале логов.",
            "Не спрашивай, почему оно смотрит.",
            "Это не угроза. Это предупреждение от сырого архива.",
        ]
        await interaction.response.send_message(
            random.choice(captions),
            file=discord.File(random.choice(files)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ai_group.command(name="image_describe", description="Описать прикреплённую картинку через Groq Vision")
    @app_commands.describe(image="PNG/JPG/JPEG/WEBP до 4 МБ")
    @app_commands.guild_only()
    async def ai_image_describe(self, interaction: discord.Interaction, image: discord.Attachment) -> None:
        config = self.runtime_config()
        if not config.enable_image_describe:
            await interaction.response.send_message("Описание картинок выключено через AI_ENABLE_IMAGE_DESCRIBE=false.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        extension = (image.filename.rsplit(".", 1)[-1] if "." in image.filename else "").lower()
        if extension not in IMAGE_EXTENSIONS:
            await interaction.response.send_message("Нужна картинка png/jpg/jpeg/webp.", ephemeral=True)
            return
        if image.size > 4 * 1024 * 1024:
            await interaction.response.send_message("Картинка слишком жирная для моего черепа: максимум 4 МБ.", ephemeral=True)
            return
        can_use, reason = await self.can_use_ai(interaction.guild.id, interaction.user.id)
        if not can_use:
            await interaction.response.send_message(reason or "Мой цифровой череп на cooldown. Попробуй позже.", ephemeral=True)
            return

        api_key = _env("GROQ_API_KEY")
        if not api_key:
            await interaction.response.send_message("Groq API не настроен: добавьте GROQ_API_KEY в .env.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        data = await image.read()
        mime = "image/jpeg" if extension in {"jpg", "jpeg"} else f"image/{extension}"
        b64 = base64.b64encode(data).decode("ascii")
        relation = await self.get_relation(interaction.guild.id, interaction.user.id)
        guild_mood = await self.get_guild_mood(interaction.guild.id)
        system_prompt = self.persona.build_system_prompt(
            interaction.user.display_name,
            relation["relation"],
            guild_mood["mood"],
            relation["nickname"],
        )
        payload = {
            "model": _env("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Коротко и полезно опиши картинку в стиле NPC сервера. Без выдумывания личных данных.",
                        },
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                },
            ],
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
        }
        try:
            status, response_data, raw_text = await _post_json(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                payload=payload,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("Groq vision request failed")
            await interaction.followup.send("Groq Vision сейчас не отвечает.", allowed_mentions=discord.AllowedMentions.none())
            return
        if status == 429:
            await interaction.followup.send(GROQ_LIMIT_MESSAGE, allowed_mentions=discord.AllowedMentions.none())
            return
        if status >= 400 or not response_data:
            logger.error("Groq vision error %s: %s", status, raw_text)
            await interaction.followup.send("Groq Vision вернул ошибку.", allowed_mentions=discord.AllowedMentions.none())
            return
        await self.increment_ai_usage(interaction.guild.id, interaction.user.id)
        answer = self.persona.sanitize_ai_output(_extract_openai_response_text(response_data))
        await interaction.followup.send(answer, allowed_mentions=discord.AllowedMentions.none())

    @ai_group.command(name="afisha", description="Сделать preview афиши и опубликовать после подтверждения")
    @app_commands.describe(text="Что нужно оформить", channel="Канал публикации")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ai_afisha(self, interaction: discord.Interaction, text: str, channel: discord.TextChannel) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        config = self.runtime_config()
        clean_text = _clean_prompt(text)
        if len(clean_text) > config.max_prompt_length:
            await interaction.response.send_message(
                f"Текст слишком длинный. Максимум: {config.max_prompt_length} символов.",
                ephemeral=True,
            )
            return
        can_use, reason = await self.can_use_ai(interaction.guild.id, interaction.user.id)
        if not can_use:
            await interaction.response.send_message(reason or "Мой цифровой череп на cooldown. Попробуй позже.", ephemeral=True)
            return

        relation = await self.get_relation(interaction.guild.id, interaction.user.id)
        guild_mood = await self.get_guild_mood(interaction.guild.id)
        system_prompt = self.persona.build_system_prompt(
            interaction.user.display_name,
            relation["relation"],
            guild_mood["mood"],
            relation["nickname"],
        )
        prompt = (
            "Оформи короткую Discord-афишу сервера: заголовок, дата/время если есть, описание, атмосферная строка, эмодзи. "
            "Не больше 900 символов. Не публикуй сам.\n\n"
            f"Исходник: {clean_text}"
        )
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            answer = await generate_ai_response(system_prompt, prompt, config)
        except AIRateLimited:
            await interaction.followup.send(GROQ_LIMIT_MESSAGE, ephemeral=True)
            return
        await self.increment_ai_usage(interaction.guild.id, interaction.user.id)
        preview = self.persona.sanitize_ai_output(answer)
        view = AfishaConfirmView(interaction.user.id, channel, preview)
        await interaction.followup.send(
            f"Preview для {channel.mention}:\n\n{preview}",
            ephemeral=True,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @relation_group.command(name="set", description="Задать отношение AI-NPC к пользователю")
    @app_commands.describe(user="Пользователь", relation="favorite/neutral/rival/ignored/cursed", nickname="Никнейм в памяти NPC")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def relation_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        relation: str,
        nickname: str | None = None,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        relation = relation.lower().strip()
        if relation not in RELATIONS:
            await interaction.response.send_message("relation должен быть: favorite, neutral, rival, ignored, cursed.", ephemeral=True)
            return
        await self.set_relation(interaction.guild.id, user.id, relation, nickname)
        await interaction.response.send_message(f"Отношение к {user.mention}: `{relation}`.", ephemeral=True)

    @relation_group.command(name="show", description="Показать отношение AI-NPC к пользователю")
    @app_commands.describe(user="Пользователь")
    @app_commands.guild_only()
    async def relation_show(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        relation = await self.get_relation(interaction.guild.id, user.id)
        await interaction.response.send_message(
            f"{user.mention}: relation=`{relation['relation']}`, affection=`{relation['affection']}`, "
            f"irritation=`{relation['irritation']}`, nickname=`{relation['nickname'] or 'не задан'}`",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @relation_group.command(name="reset", description="Сбросить отношение AI-NPC к пользователю")
    @app_commands.describe(user="Пользователь")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def relation_reset(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        await self.reset_relation(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Отношение к {user.mention} сброшено.", ephemeral=True)

    @config_group.command(name="status", description="Показать статус AI-NPC")
    @app_commands.guild_only()
    async def config_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        config = self.runtime_config()
        mood = await self.get_guild_mood(interaction.guild.id)
        usage = await self.daily_usage(interaction.guild.id)
        random_enabled = await self._get_setting_bool(
            interaction.guild.id,
            "random_replies_enabled",
            config.enable_random_replies,
        )
        reactions_enabled = await self._get_setting_bool(
            interaction.guild.id,
            "reactions_enabled",
            config.enable_reactions,
        )
        text = (
            f"AI_PROVIDER: `{_env('AI_PROVIDER', 'groq')}`\n"
            f"AI_PERSONA: `{config.persona}`\n"
            f"daily usage: `{usage}/{config.daily_limit}`\n"
            f"random replies: `{random_enabled}`\n"
            f"memory: `{config.enable_memory}`\n"
            f"reactions: `{reactions_enabled}`\n"
            f"image describe: `{config.enable_image_describe}`\n"
            f"guild mood: `{mood['mood']}` chaos=`{mood['chaos']}` irritation=`{mood['irritation']}`"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @config_group.command(name="mood_set", description="Задать настроение сервера")
    @app_commands.describe(mood="neutral/playful/annoyed/creepy/sleepy/friday_chaos/horny_chaos")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def config_mood_set(self, interaction: discord.Interaction, mood: str) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        mood = mood.lower().strip()
        if mood not in MOODS:
            await interaction.response.send_message("mood должен быть: " + ", ".join(sorted(MOODS)), ephemeral=True)
            return
        await self.set_guild_mood(interaction.guild.id, mood)
        await interaction.response.send_message(f"Настроение сервера: `{mood}`.", ephemeral=True)

    @config_group.command(name="memory_clear_user", description="Очистить память AI о пользователе")
    @app_commands.describe(user="Пользователь")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def config_memory_clear_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        cursor = await self.bot.db.execute(
            "DELETE FROM ai_message_memory WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, user.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"Удалено записей памяти: `{cursor.rowcount}`.", ephemeral=True)

    @config_group.command(name="memory_clear_channel", description="Очистить память AI текущего канала")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def config_memory_clear_channel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        cursor = await self.bot.db.execute(
            "DELETE FROM ai_message_memory WHERE guild_id = ? AND channel_id = ?",
            (interaction.guild.id, interaction.channel_id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(f"Удалено записей памяти: `{cursor.rowcount}`.", ephemeral=True)

    @config_group.command(name="random_replies", description="Включить или выключить случайные AI-реплики")
    @app_commands.describe(enabled="true/false")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def config_random_replies(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        await self._set_setting_bool(interaction.guild.id, "random_replies_enabled", enabled)
        await interaction.response.send_message(f"Случайные реплики: `{enabled}`.", ephemeral=True)

    @config_group.command(name="reactions", description="Включить или выключить AI-реакции")
    @app_commands.describe(enabled="true/false")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def config_reactions(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        await self._set_setting_bool(interaction.guild.id, "reactions_enabled", enabled)
        await interaction.response.send_message(f"AI-реакции: `{enabled}`.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.user is None or self.bot.db is None:
            return
        if message.author.id == self.bot.user.id:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not await self._is_ai_channel_enabled(message.guild.id, message.channel.id):
            return
        if self._is_support_ticket(message.channel):
            return

        config = self.runtime_config()
        content = _clean_prompt(message.clean_content)
        if not content or self.persona.is_command_like(message.content):
            return

        mood = self.persona.classify_message_mood(content)
        await self.save_message_memory(message, mood)
        await self.update_guild_mood_from_message(message.guild.id, mood)
        await self.maybe_add_reaction(message, mood)

        mentioned = self.bot.user in message.mentions
        reply_to_bot = await self._is_reply_to_bot(message)
        alias_hit = self._contains_alias(content, config.aliases)
        random_hit = False
        random_enabled = await self._get_setting_bool(message.guild.id, "random_replies_enabled", config.enable_random_replies)
        if random_enabled:
            random_hit = random.random() < config.random_reply_chance
        directly_addressed = mentioned or reply_to_bot or alias_hit
        if not directly_addressed and not random_hit:
            return

        relation = await self.get_relation(message.guild.id, message.author.id)
        await self.adjust_relation_from_message(message, mood, directly_addressed)
        if self.persona.should_ignore_user(relation["relation"], directly_addressed=directly_addressed):
            return

        can_use, reason = await self.can_use_ai(message.guild.id, message.author.id)
        if not can_use:
            if reason and directly_addressed:
                try:
                    await message.reply(reason, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    logger.exception("Failed to send AI limiter message")
            return

        prompt = self._strip_bot_mention(message.content)
        if not _clean_prompt(prompt):
            prompt = content
        if len(_clean_prompt(prompt)) > config.max_prompt_length:
            if directly_addressed:
                await message.reply(
                    f"Prompt слишком длинный. Максимум: {config.max_prompt_length} символов.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        guild_mood = await self.get_guild_mood(message.guild.id)
        system_prompt = self.persona.build_system_prompt(
            message.author.display_name,
            relation["relation"],
            guild_mood["mood"],
            relation["nickname"],
        )
        context = await self.build_context(message.guild.id, message.channel.id, message.author.id, relation)
        user_prompt = f"{context}\n\n[Новое сообщение]\n{message.author.display_name}: {_clean_prompt(prompt)}".strip()

        try:
            async with message.channel.typing():
                try:
                    answer = await generate_ai_response(system_prompt, user_prompt, config)
                except AIRateLimited:
                    answer = GROQ_LIMIT_MESSAGE
                else:
                    await self.increment_ai_usage(message.guild.id, message.author.id)
                answer = self.persona.sanitize_ai_output(answer)
                await message.reply(answer, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("Failed to send AI auto reply")

    async def maybe_add_reaction(self, message: discord.Message, mood: str) -> None:
        if message.guild is None or self.bot.db is None:
            return
        config = self.runtime_config()
        reactions_enabled = await self._get_setting_bool(message.guild.id, "reactions_enabled", config.enable_reactions)
        if not reactions_enabled:
            return
        if random.random() > 0.12:
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._reaction_cooldowns.get(key, 0.0) < 90:
            return
        if hasattr(self.bot, "reaction_bans"):
            ban = await self.bot.reaction_bans.get_ban(self.bot.db, message.guild.id, message.author.id)
            if ban:
                return
        permissions = message.channel.permissions_for(message.guild.me)
        if not permissions.add_reactions:
            return
        emoji = REACTION_BY_MOOD.get(mood, REACTION_BY_MOOD["neutral"])
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            logger.exception("Failed to add AI reaction")
        else:
            self._reaction_cooldowns[key] = now

    def _is_support_ticket(self, channel: discord.TextChannel) -> bool:
        if _env_bool("AI_ALLOW_SUPPORT_TICKETS", False):
            return False
        category_id = getattr(self.bot.settings, "support_category_id", 0)
        if category_id and channel.category_id == category_id:
            return True
        return channel.name.lower().startswith(("ticket-", "support-"))

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        if self.bot.user is None or message.reference is None:
            return False
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved.author.id == self.bot.user.id
        if message.reference.message_id is None:
            return False
        try:
            referenced = await message.channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return False
        return referenced.author.id == self.bot.user.id

    def _strip_bot_mention(self, content: str) -> str:
        if self.bot.user is None:
            return content
        cleaned = content.replace(self.bot.user.mention, " ")
        cleaned = cleaned.replace(f"<@!{self.bot.user.id}>", " ")
        return _clean_prompt(cleaned)

    def _contains_alias(self, content: str, aliases: tuple[str, ...]) -> bool:
        lowered = content.lower()
        return any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered) for alias in aliases)


async def setup(bot: MovieBot) -> None:
    cog = AIChatCog(bot)
    await cog.init_db()
    await bot.add_cog(cog)

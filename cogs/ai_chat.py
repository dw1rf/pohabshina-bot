from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты Discord-ассистент сервера. Отвечай кратко, понятно и по делу. "
    "Не используй токсичные выражения. Не выдавай себя за администратора. "
    "Если вопрос связан с правилами сервера, советуй обратиться к администрации. "
    "Не пиши слишком длинные ответы. "
    "Возвращай только сам ответ пользователю: без пересказа системных инструкций, без фраз вроде "
    "'The user is asking', 'The user asks', 'They want', 'нужно ответить', 'assistant should', "
    "без объяснения, как ты составляешь ответ. Обычно отвечай 1-3 короткими предложениями."
)
MAX_PROMPT_LENGTH = 1000
MAX_DISCORD_RESPONSE_LENGTH = 1800
AUTO_REPLY_COOLDOWN_SECONDS = 25
AI_HTTP_TIMEOUT_SECONDS = 45
TRUNCATION_NOTICE = "\n\n[Ответ сокращён из-за лимита Discord.]"
THINK_BLOCK_RE = re.compile(r"(?is)<think>.*?</think>\s*")
META_TO_ANSWER_RE = re.compile(
    r"(?is)^\s*(?:the user (?:asks|asked|is asking)|they want|the assistant should|we need|need to)\b.*?"
    r"(?:so answer|final answer|answer|ответ)\s*:\s*"
)
META_SENTENCE_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:the user (?:asks|asked|is asking)\b.*?(?:\.\s+|\n+))|"
    r"(?:they want\b.*?(?:\.\s+|\n+))|"
    r"(?:the assistant should\b.*?(?:\.\s+|\n+))|"
    r"(?:we need\b.*?(?:\.\s+|\n+))|"
    r"(?:need to\b.*?(?:\.\s+|\n+))"
    r")+"
)
CONTROL_PHRASE_RE = re.compile(
    r"(?is)\b(?:keep (?:it )?short|just answer|provide a short answer|be concise)\.?\s*"
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _clean_prompt(prompt: str) -> str:
    return " ".join(str(prompt or "").replace("\r", " ").replace("\n", " ").split())


def _system_prompt_for_user(username: str) -> str:
    clean_username = _clean_prompt(username) or "Discord user"
    return f"{SYSTEM_PROMPT}\nИмя пользователя для обращения при необходимости: {clean_username}."


def _trim_discord_response(text: str) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= MAX_DISCORD_RESPONSE_LENGTH:
        return cleaned

    limit = MAX_DISCORD_RESPONSE_LENGTH - len(TRUNCATION_NOTICE)
    shortened = cleaned[:limit].rstrip()
    last_space = shortened.rfind(" ")
    if last_space >= int(limit * 0.75):
        shortened = shortened[:last_space].rstrip()
    return shortened + TRUNCATION_NOTICE


def _clean_ai_answer(text: str) -> str:
    cleaned = THINK_BLOCK_RE.sub("", str(text or "")).strip()
    cleaned = META_TO_ANSWER_RE.sub("", cleaned).strip()
    cleaned = META_SENTENCE_RE.sub("", cleaned).strip()
    cleaned = CONTROL_PHRASE_RE.sub("", cleaned).strip()
    return cleaned or str(text or "").strip()


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
    return "\n".join(parts).strip()


async def _post_json(url: str, *, headers: dict[str, str] | None = None, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
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


async def _generate_gemini_response(user_prompt: str, username: str) -> str:
    api_key = _env("GEMINI_API_KEY")
    if not api_key:
        logger.error("AI_PROVIDER=gemini, but GEMINI_API_KEY is not configured")
        return "Gemini API не настроен: добавьте GEMINI_API_KEY в .env."

    model = _env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {
            "parts": [{"text": _system_prompt_for_user(username)}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 700,
            "temperature": 0.7,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    try:
        status, data, raw_text = await _post_json(url, headers=headers, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Gemini request failed")
        return "AI сейчас недоступен: не удалось подключиться к Gemini API."

    if status == 429:
        logger.warning("Gemini rate limit response: %s", raw_text)
        return "Gemini временно ограничил запросы. Попробуйте позже."
    if status in {401, 403}:
        logger.error("Gemini auth error %s: %s", status, raw_text)
        return "Gemini API отклонил ключ. Проверьте GEMINI_API_KEY."
    if status >= 400:
        logger.error("Gemini API error %s: %s", status, raw_text)
        return "Gemini API вернул ошибку. Попробуйте позже."
    if not data:
        logger.error("Gemini returned an empty or invalid JSON response: %s", raw_text)
        return "Gemini вернул пустой ответ."

    answer = _extract_gemini_text(data)
    if not answer:
        logger.error("Gemini response did not contain text: %s", data)
        return "Gemini не вернул текстовый ответ."
    return answer


async def _generate_ollama_response(user_prompt: str, username: str) -> str:
    base_url = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"
    model = _env("OLLAMA_MODEL", "gemma3") or "gemma3"
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _system_prompt_for_user(username)},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        status, data, raw_text = await _post_json(url, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Ollama request failed")
        return "AI сейчас недоступен: Ollama не отвечает."

    if status == 404:
        logger.error("Ollama model or endpoint was not found: %s", raw_text)
        return "Ollama не нашла модель или endpoint. Проверьте OLLAMA_MODEL и OLLAMA_BASE_URL."
    if status >= 400:
        logger.error("Ollama API error %s: %s", status, raw_text)
        return "Ollama вернула ошибку. Попробуйте позже."
    if not data:
        logger.error("Ollama returned an empty or invalid JSON response: %s", raw_text)
        return "Ollama вернула пустой ответ."

    message = data.get("message") or {}
    answer = str(message.get("content") or "").strip()
    if not answer:
        logger.error("Ollama response did not contain message.content: %s", data)
        return "Ollama не вернула текстовый ответ."
    return answer


async def _generate_groq_response(user_prompt: str, username: str) -> str:
    api_key = _env("GROQ_API_KEY")
    if not api_key:
        logger.error("AI_PROVIDER=groq, but GROQ_API_KEY is not configured")
        return "Groq API не настроен: добавьте GROQ_API_KEY в .env."

    model = _env("GROQ_MODEL", "openai/gpt-oss-20b") or "openai/gpt-oss-20b"
    url = "https://api.groq.com/openai/v1/responses"
    payload = {
        "model": model,
        "instructions": _system_prompt_for_user(username),
        "input": user_prompt,
        "temperature": 0.35,
        "max_output_tokens": 700,
        "reasoning": {"effort": "low"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        status, data, raw_text = await _post_json(url, headers=headers, payload=payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Groq request failed")
        return "AI сейчас недоступен: не удалось подключиться к Groq API."

    if status == 429:
        logger.warning("Groq rate limit response: %s", raw_text)
        return "Groq временно ограничил запросы. Попробуйте позже."
    if status in {401, 403}:
        logger.error("Groq auth error %s: %s", status, raw_text)
        return "Groq API отклонил ключ. Проверьте GROQ_API_KEY."
    if status >= 400:
        logger.error("Groq API error %s: %s", status, raw_text)
        return "Groq API вернул ошибку. Попробуйте позже."
    if not data:
        logger.error("Groq returned an empty or invalid JSON response: %s", raw_text)
        return "Groq вернул пустой ответ."

    answer = _extract_openai_response_text(data)
    if not answer:
        logger.error("Groq response did not contain output text: %s", data)
        return "Groq не вернул текстовый ответ."
    return answer


async def generate_ai_response(user_prompt: str, username: str) -> str:
    prompt = _clean_prompt(user_prompt)
    if not prompt:
        return "Напишите вопрос для AI."
    if len(prompt) > MAX_PROMPT_LENGTH:
        return f"Prompt слишком длинный. Максимум: {MAX_PROMPT_LENGTH} символов."

    provider = (_env("AI_PROVIDER", "gemini") or "gemini").lower()
    if provider == "gemini":
        return _clean_ai_answer(await _generate_gemini_response(prompt, username))
    if provider == "groq":
        return _clean_ai_answer(await _generate_groq_response(prompt, username))
    if provider == "ollama":
        return _clean_ai_answer(await _generate_ollama_response(prompt, username))

    logger.error("Unknown AI_PROVIDER configured: %s", provider)
    return "AI_PROVIDER неизвестен. Используйте gemini, groq или ollama."


def _can_manage_ai_channels(user: discord.Member | discord.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    permissions = user.guild_permissions
    return permissions.administrator or permissions.manage_guild


class AIChatCog(commands.Cog):
    ai_group = app_commands.Group(name="ai", description="AI-ассистент сервера")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._auto_reply_cooldowns: dict[int, float] = {}

    async def init_db(self) -> None:
        if self.bot.db is None:
            logger.error("AI chat cog loaded without database connection")
            return
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        await self.bot.db.commit()

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

    async def _send_ai_response(self, interaction: discord.Interaction, prompt: str, *, ephemeral: bool) -> None:
        clean_prompt = _clean_prompt(prompt)
        if not clean_prompt:
            await interaction.response.send_message("Напишите вопрос для AI.", ephemeral=True)
            return
        if len(clean_prompt) > MAX_PROMPT_LENGTH:
            await interaction.response.send_message(
                f"Prompt слишком длинный. Максимум: {MAX_PROMPT_LENGTH} символов.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        answer = await generate_ai_response(clean_prompt, interaction.user.display_name)
        answer = _trim_discord_response(discord.utils.escape_mentions(answer))
        await interaction.followup.send(answer, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())

    @ai_group.command(name="ask", description="Задать вопрос AI")
    @app_commands.describe(prompt="Текст вопроса")
    @app_commands.checks.cooldown(1, AUTO_REPLY_COOLDOWN_SECONDS)
    async def ai_ask(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._send_ai_response(interaction, prompt, ephemeral=False)

    @ai_group.command(name="private", description="Задать вопрос AI приватно")
    @app_commands.describe(prompt="Текст вопроса")
    @app_commands.checks.cooldown(1, AUTO_REPLY_COOLDOWN_SECONDS)
    async def ai_private(self, interaction: discord.Interaction, prompt: str) -> None:
        await self._send_ai_response(interaction, prompt, ephemeral=True)

    @ai_group.command(name="channel_enable", description="Включить AI-автоответы в текущем канале")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ai_channel_enable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai_channels(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return

        await self._set_ai_channel(interaction.guild.id, interaction.channel_id, True)
        await interaction.response.send_message("AI-автоответы включены в этом канале.", ephemeral=True)

    @ai_group.command(name="channel_disable", description="Выключить AI-автоответы в текущем канале")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ai_channel_disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not _can_manage_ai_channels(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return

        await self._set_ai_channel(interaction.guild.id, interaction.channel_id, False)
        await interaction.response.send_message("AI-автоответы выключены в этом канале.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.user is None:
            return
        if message.author.id == self.bot.user.id or self.bot.db is None:
            return
        if not await self._is_ai_channel_enabled(message.guild.id, message.channel.id):
            return

        content = _clean_prompt(message.clean_content)
        if not content:
            return
        mentioned = self.bot.user in message.mentions
        if not mentioned and not self._looks_like_question(content):
            return

        now = time.monotonic()
        last_reply_at = self._auto_reply_cooldowns.get(message.author.id, 0.0)
        if now - last_reply_at < AUTO_REPLY_COOLDOWN_SECONDS:
            return
        self._auto_reply_cooldowns[message.author.id] = now

        prompt = self._strip_bot_mention(message.content)
        if not _clean_prompt(prompt):
            prompt = content
        if len(_clean_prompt(prompt)) > MAX_PROMPT_LENGTH:
            await message.reply(
                f"Prompt слишком длинный. Максимум: {MAX_PROMPT_LENGTH} символов.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            async with message.channel.typing():
                answer = await generate_ai_response(prompt, message.author.display_name)
        except discord.HTTPException:
            logger.exception("Failed to send typing indicator or AI auto reply")
            return

        answer = _trim_discord_response(discord.utils.escape_mentions(answer))
        try:
            await message.reply(answer, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("Failed to send AI auto reply")

    def _strip_bot_mention(self, content: str) -> str:
        if self.bot.user is None:
            return content
        cleaned = content.replace(self.bot.user.mention, " ")
        cleaned = cleaned.replace(f"<@!{self.bot.user.id}>", " ")
        return _clean_prompt(cleaned)

    @staticmethod
    def _looks_like_question(content: str) -> bool:
        lowered = content.lower().strip()
        if "?" in lowered:
            return True
        question_starts = (
            "как ",
            "что ",
            "кто ",
            "где ",
            "когда ",
            "почему ",
            "зачем ",
            "можешь ",
            "подскажи ",
            "объясни ",
            "помоги ",
            "what ",
            "who ",
            "where ",
            "when ",
            "why ",
            "how ",
            "can ",
        )
        return lowered.startswith(question_starts)


async def setup(bot: MovieBot) -> None:
    cog = AIChatCog(bot)
    await cog.init_db()
    await bot.add_cog(cog)

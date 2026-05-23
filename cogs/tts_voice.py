from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.voice_runtime import FFMPEG_MISSING_MESSAGE, find_ffmpeg, require_ffmpeg

try:
    import edge_tts
except ImportError:  # pragma: no cover - handled at runtime for clearer Discord errors.
    edge_tts = None

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 10 * 60
MESSAGE_COOLDOWN_SECONDS = 1.5
MAX_QUEUE_SIZE = 10
MAX_TTS_PART_LENGTH = 200
DEFAULT_VOICE = "ru-RU-SvetlanaNeural"
COMMAND_PREFIXES = ("/", "!", ".", "?", "+", "-")

VOICE_OPTIONS: dict[str, tuple[str, str, str]] = {
    "ru-RU-DmitryNeural": ("Дмитрий", "мужской", "🧔"),
    "ru-RU-SvetlanaNeural": ("Светлана", "женский", "👩"),
    "ru-RU-DariyaNeural": ("Дарья", "женский", "👩‍🦰"),
}

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"<(?:@!?|@&|#)\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_~]+:\d+>")
MARKDOWN_RE = re.compile(r"[*_`~>#\[\]()]")
SPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class TTSItem:
    text: str
    display_text: str
    author_name: str
    source_message_id: int | None = None
    send_file: bool = True


@dataclass(slots=True)
class TTSSession:
    guild_id: int
    owner_id: int
    text_channel_id: int
    voice_channel_id: int
    selected_voice: str = DEFAULT_VOICE
    queue: asyncio.Queue[TTSItem] = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE_SIZE))
    worker_task: asyncio.Task[None] | None = None
    last_message_at: float = 0.0
    panel_message_id: int | None = None
    tmp_files: set[Path] = field(default_factory=set)
    active: bool = True


def _voice_label(voice: str) -> str:
    name, gender, _emoji = VOICE_OPTIONS.get(voice, VOICE_OPTIONS[DEFAULT_VOICE])
    return f"{name} ({gender})"


def _compact(text: str, limit: int = 180) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _strip_unicode_symbols(text: str) -> str:
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in {"So", "Sk", "Cs"}:
            cleaned.append(" ")
        else:
            cleaned.append(char)
    return "".join(cleaned)


def clean_tts_text(text: str) -> str:
    text = CUSTOM_EMOJI_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    text = MARKDOWN_RE.sub(" ", text)
    text = _strip_unicode_symbols(text)
    return SPACE_RE.sub(" ", text).strip()


def looks_like_command(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.startswith(COMMAND_PREFIXES)


def split_tts_text(text: str) -> list[str]:
    words = text.split()
    parts: list[str] = []
    current = ""
    for word in words:
        if len(word) > MAX_TTS_PART_LENGTH:
            if current:
                parts.append(current)
                current = ""
            parts.extend(word[index : index + MAX_TTS_PART_LENGTH] for index in range(0, len(word), MAX_TTS_PART_LENGTH))
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) <= MAX_TTS_PART_LENGTH:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = word
    if current:
        parts.append(current)
    return parts


class TTSVoiceSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoiceCog", session: TTSSession | None, *, disabled: bool = False) -> None:
        self.cog = cog
        options = [
            discord.SelectOption(
                label=name,
                description=f"{gender} голос",
                value=voice,
                emoji=emoji,
                default=session is not None and session.selected_voice == voice,
            )
            for voice, (name, gender, emoji) in VOICE_OPTIONS.items()
        ]
        super().__init__(
            placeholder="Выбери голос озвучки",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tts_voice:select_voice",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_voice_select(interaction, self.values[0])


class TTSVoiceView(discord.ui.View):
    def __init__(self, cog: "TTSVoiceCog", session: TTSSession | None, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.add_item(TTSVoiceSelect(cog, session, disabled=disabled))


class TTSVoiceCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.sessions: dict[int, TTSSession] = {}
        self._ffmpeg_executable: str | None = None

    def cog_unload(self) -> None:
        for guild_id in list(self.sessions):
            self.bot.loop.create_task(self.shutdown_session(guild_id, reason="cog unload"))

    async def send_interaction_message(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ) -> discord.Message | None:
        kwargs: dict[str, Any] = {
            "ephemeral": ephemeral,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view

        if interaction.response.is_done():
            return await interaction.followup.send(wait=True, **kwargs)
        await interaction.response.send_message(**kwargs)
        try:
            return await interaction.original_response()
        except discord.HTTPException:
            return None

    def ffmpeg_executable(self) -> str | None:
        if self._ffmpeg_executable:
            return self._ffmpeg_executable
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            logger.error("%s TTS playback is unavailable.", FFMPEG_MISSING_MESSAGE)
            return None
        self._ffmpeg_executable = ffmpeg_path
        return self._ffmpeg_executable

    def dependency_error(self) -> str | None:
        if edge_tts is None:
            return "edge-tts не установлен. Добавь зависимость edge-tts и перезапусти бота."
        if self.ffmpeg_executable() is None:
            return FFMPEG_MISSING_MESSAGE
        try:
            import nacl  # noqa: F401
        except ImportError:
            return "Голосовой модуль Discord не установлен. Проверь discord.py[voice] и PyNaCl."
        return None

    def build_embed(self, session: TTSSession, *, enabled: bool = True) -> discord.Embed:
        color = discord.Color.green() if enabled else discord.Color.dark_grey()
        embed = discord.Embed(
            title="🎙️ Озвучка чата",
            description=(
                "Один раз включи — бот читает только твои сообщения.\n"
                "Пока озвучка включена, только ты можешь пользоваться ботом в этом голосе.\n\n"
                "• Не читает эмодзи, стикеры, спам и команды\n"
                "• Быстрые сообщения встают в очередь\n"
                "• Длинный текст — до 200 символов за часть\n"
                "• Бот не выйдет, пока ты в голосе с включённой озвучкой"
            ),
            color=color,
        )
        status = f"✅ Включена - голос: {_voice_label(session.selected_voice)}" if enabled else "⛔ Выключена"
        voice_channel = self.bot.get_channel(session.voice_channel_id)
        voice_name = f"🎮 {voice_channel.name}" if isinstance(voice_channel, discord.VoiceChannel) else "🎮 голосовой канал"
        embed.add_field(name="Статус", value=status, inline=False)
        embed.add_field(name="Голосовой канал", value=voice_name, inline=False)
        embed.set_footer(text="Команды: /tts_say — разовая фраза / /tts — это меню")
        return embed

    async def update_panel(self, session: TTSSession, *, enabled: bool = True) -> None:
        if session.panel_message_id is None:
            return
        channel = self.bot.get_channel(session.text_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.HTTPException:
                logger.exception("Failed to fetch TTS panel channel: guild=%s", session.guild_id)
                return
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(session.panel_message_id)
            await message.edit(embed=self.build_embed(session, enabled=enabled), view=TTSVoiceView(self, session if enabled else None, disabled=not enabled))
        except discord.NotFound:
            logger.warning("TTS panel message is gone: guild=%s message=%s", session.guild_id, session.panel_message_id)
        except discord.HTTPException:
            logger.exception("Failed to update TTS panel: guild=%s", session.guild_id)

    async def ensure_voice_client(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return None
        user_voice = interaction.user.voice
        if user_voice is None or user_voice.channel is None:
            await self.send_interaction_message(interaction, "Зайди в голосовой канал, чтобы включить озвучку.", ephemeral=True)
            return None

        error = self.dependency_error()
        if error:
            await self.send_interaction_message(interaction, error, ephemeral=True)
            return None

        channel = user_voice.channel
        me = interaction.guild.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not permissions.connect or not permissions.speak:
                await self.send_interaction_message(interaction, "У бота нет прав зайти или говорить в этом голосовом канале.", ephemeral=True)
                return None

        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient):
            if voice_client.is_playing() or voice_client.is_paused():
                await self.send_interaction_message(interaction, "Бот уже занят в голосовом канале.", ephemeral=True)
                return None
            if voice_client.channel != channel:
                await voice_client.move_to(channel)
            return voice_client

        try:
            return await channel.connect(timeout=15, reconnect=True)
        except (TimeoutError, discord.ClientException, discord.HTTPException):
            logger.exception("Failed to connect to voice channel: guild=%s channel=%s", interaction.guild.id, channel.id)
            await self.send_interaction_message(interaction, "Не удалось подключиться к голосовому каналу.", ephemeral=True)
            return None

    async def synthesize_to_file(self, session: TTSSession | None, text: str, voice: str) -> Path:
        if edge_tts is None:
            raise RuntimeError("edge-tts is not installed")
        handle = tempfile.NamedTemporaryFile(prefix="discord_tts_", suffix=".mp3", delete=False)
        temp_path = Path(handle.name)
        handle.close()
        if session is not None:
            session.tmp_files.add(temp_path)
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(temp_path))
            return temp_path
        except Exception:
            temp_path.unlink(missing_ok=True)
            if session is not None:
                session.tmp_files.discard(temp_path)
            raise

    async def play_file(self, guild: discord.Guild, file_path: Path) -> bool:
        try:
            ffmpeg_path = require_ffmpeg()
        except RuntimeError as exc:
            logger.error("Cannot play TTS file: %s", exc)
            return False
        self._ffmpeg_executable = ffmpeg_path
        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            return False
        if voice_client.is_playing() or voice_client.is_paused():
            logger.warning("Voice client is already playing while TTS tried to start: guild=%s", guild.id)
            return False

        finished = self.bot.loop.create_future()

        def after_play(error: Exception | None) -> None:
            if error:
                logger.error("TTS playback error: guild=%s error=%s", guild.id, error)
            self.bot.loop.call_soon_threadsafe(finished.set_result, error is None)

        try:
            source = discord.FFmpegPCMAudio(str(file_path), executable=ffmpeg_path)
            voice_client.play(source, after=after_play)
            return bool(await finished)
        except Exception:
            logger.exception("Failed to play TTS file: guild=%s file=%s", guild.id, file_path)
            return False

    async def send_tts_file(self, session: TTSSession, item: TTSItem, file_path: Path) -> None:
        if not item.send_file:
            return
        channel = self.bot.get_channel(session.text_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.HTTPException:
                logger.exception("Failed to fetch TTS text channel: guild=%s", session.guild_id)
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await channel.send(
                content=f"🔊 **{discord.utils.escape_markdown(item.author_name)}:** {_compact(item.display_text)}",
                file=discord.File(file_path, filename="tts.mp3"),
                allowed_mentions=discord.AllowedMentions.none(),
                reference=discord.MessageReference(
                    message_id=item.source_message_id,
                    channel_id=session.text_channel_id,
                    guild_id=session.guild_id,
                    fail_if_not_exists=False,
                )
                if item.source_message_id
                else None,
                mention_author=False,
            )
        except discord.HTTPException:
            logger.exception("Failed to send TTS mp3 file: guild=%s", session.guild_id)

    async def tts_worker(self, session: TTSSession) -> None:
        logger.info("TTS worker started: guild=%s owner=%s", session.guild_id, session.owner_id)
        try:
            while session.active:
                try:
                    item = await asyncio.wait_for(session.queue.get(), timeout=IDLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.info("TTS session idle timeout: guild=%s", session.guild_id)
                    await self.shutdown_session(session.guild_id, reason="idle timeout")
                    return

                guild = self.bot.get_guild(session.guild_id)
                if guild is None:
                    await self.shutdown_session(session.guild_id, reason="guild missing")
                    return
                voice_client = guild.voice_client
                if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
                    await self.shutdown_session(session.guild_id, reason="voice disconnected")
                    return

                file_path: Path | None = None
                try:
                    file_path = await self.synthesize_to_file(session, item.text, session.selected_voice)
                    await self.send_tts_file(session, item, file_path)
                    await self.play_file(guild, file_path)
                except Exception:
                    logger.exception("Failed to process TTS item: guild=%s", session.guild_id)
                finally:
                    if file_path is not None:
                        file_path.unlink(missing_ok=True)
                        session.tmp_files.discard(file_path)
                    session.queue.task_done()
        finally:
            logger.info("TTS worker stopped: guild=%s", session.guild_id)

    async def enqueue_text(
        self,
        session: TTSSession,
        text: str,
        author_name: str,
        *,
        source_message_id: int | None = None,
        send_file: bool = True,
    ) -> int:
        cleaned = clean_tts_text(text)
        if not cleaned:
            return 0
        parts = split_tts_text(cleaned)
        queued = 0
        for part in parts:
            if session.queue.full():
                logger.warning("TTS queue overflow: guild=%s owner=%s", session.guild_id, session.owner_id)
                break
            session.queue.put_nowait(
                TTSItem(
                    text=part,
                    display_text=part,
                    author_name=author_name,
                    source_message_id=source_message_id,
                    send_file=send_file,
                )
            )
            queued += 1
        return queued

    async def shutdown_session(self, guild_id: int, *, reason: str) -> None:
        session = self.sessions.pop(guild_id, None)
        if session is None:
            return
        logger.info("TTS shutdown: guild=%s owner=%s reason=%s", guild_id, session.owner_id, reason)
        session.active = False

        while not session.queue.empty():
            try:
                session.queue.get_nowait()
                session.queue.task_done()
            except asyncio.QueueEmpty:
                break

        current_task = asyncio.current_task()
        if session.worker_task is not None and session.worker_task is not current_task:
            session.worker_task.cancel()

        guild = self.bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None
        if isinstance(voice_client, discord.VoiceClient):
            try:
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                if voice_client.channel and voice_client.channel.id == session.voice_channel_id:
                    await voice_client.disconnect(force=False)
            except (discord.ClientException, discord.HTTPException):
                logger.exception("Failed to disconnect TTS voice client: guild=%s", guild_id)

        for temp_path in list(session.tmp_files):
            temp_path.unlink(missing_ok=True)
            session.tmp_files.discard(temp_path)

        await self.update_panel(session, enabled=False)

    async def handle_voice_select(self, interaction: discord.Interaction, voice: str) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        session = self.sessions.get(interaction.guild.id)
        if session is None:
            await self.send_interaction_message(interaction, "Озвучка уже выключена.", ephemeral=True)
            return
        if interaction.user.id != session.owner_id:
            await self.send_interaction_message(interaction, "Только владелец озвучки может менять голос.", ephemeral=True)
            return
        if voice not in VOICE_OPTIONS:
            await self.send_interaction_message(interaction, "Неизвестный голос.", ephemeral=True)
            return
        session.selected_voice = voice
        await self.update_panel(session, enabled=True)
        await self.send_interaction_message(interaction, f"Голос изменён: {_voice_label(voice)}.", ephemeral=True)

    @app_commands.command(name="tts", description="Включить озвучку своих сообщений в голосовом канале")
    @app_commands.guild_only()
    async def tts(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Команда доступна только в текстовом канале сервера.", ephemeral=True)
            return

        existing = self.sessions.get(interaction.guild.id)
        if existing is not None:
            owner = interaction.guild.get_member(existing.owner_id)
            owner_text = owner.mention if owner else f"<@{existing.owner_id}>"
            await interaction.response.send_message(
                f"Озвучка уже включена пользователем {owner_text}. Дождись окончания или попроси его выключить.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        user_voice = interaction.user.voice if isinstance(interaction.user, discord.Member) else None
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message("Зайди в голосовой канал, чтобы включить озвучку.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        voice_client = await self.ensure_voice_client(interaction)
        if voice_client is None:
            return

        session = TTSSession(
            guild_id=interaction.guild.id,
            owner_id=interaction.user.id,
            text_channel_id=interaction.channel.id,
            voice_channel_id=user_voice.channel.id,
        )
        self.sessions[interaction.guild.id] = session
        session.worker_task = self.bot.loop.create_task(self.tts_worker(session))

        message = await interaction.followup.send(embed=self.build_embed(session), view=TTSVoiceView(self, session), wait=True)
        session.panel_message_id = message.id
        logger.info(
            "TTS session started: guild=%s owner=%s text_channel=%s voice_channel=%s",
            session.guild_id,
            session.owner_id,
            session.text_channel_id,
            session.voice_channel_id,
        )

    @app_commands.command(name="tts_say", description="Озвучить одну фразу через TTS")
    @app_commands.describe(text="Текст для озвучки")
    @app_commands.guild_only()
    async def tts_say(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if looks_like_command(text):
            await interaction.response.send_message("Это похоже на команду, озвучка пропущена.", ephemeral=True)
            return

        existing = self.sessions.get(interaction.guild.id)
        if existing is not None:
            if interaction.user.id != existing.owner_id:
                owner = interaction.guild.get_member(existing.owner_id)
                owner_text = owner.mention if owner else f"<@{existing.owner_id}>"
                await interaction.response.send_message(
                    f"Озвучка уже включена пользователем {owner_text}. Дождись окончания или попроси его выключить.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            queued = await self.enqueue_text(existing, text, interaction.user.display_name, send_file=True)
            await interaction.response.send_message(
                "Фраза добавлена в очередь." if queued else "После очистки текста нечего озвучивать.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        voice_client = await self.ensure_voice_client(interaction)
        if voice_client is None:
            return
        cleaned = clean_tts_text(text)
        parts = split_tts_text(cleaned)
        if not parts:
            await interaction.followup.send("После очистки текста нечего озвучивать.", ephemeral=True)
            return

        temp_files: list[Path] = []
        try:
            for part in parts[:MAX_QUEUE_SIZE]:
                file_path = await self.synthesize_to_file(None, part, DEFAULT_VOICE)
                temp_files.append(file_path)
                if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                    try:
                        await interaction.channel.send(
                            content=f"🔊 **{discord.utils.escape_markdown(interaction.user.display_name)}:** {_compact(part)}",
                            file=discord.File(file_path, filename="tts.mp3"),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        logger.exception("Failed to send one-shot TTS mp3: guild=%s", interaction.guild.id)
                await self.play_file(interaction.guild, file_path)
            await interaction.followup.send("Фраза озвучена.", ephemeral=True)
        except Exception:
            logger.exception("One-shot TTS failed: guild=%s", interaction.guild.id)
            await interaction.followup.send("Не удалось озвучить фразу. Ошибка записана в лог.", ephemeral=True)
        finally:
            for file_path in temp_files:
                file_path.unlink(missing_ok=True)
            active = self.sessions.get(interaction.guild.id)
            if active is None and isinstance(voice_client, discord.VoiceClient):
                try:
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop()
                    await voice_client.disconnect(force=False)
                except (discord.ClientException, discord.HTTPException):
                    logger.exception("Failed to disconnect one-shot TTS voice client: guild=%s", interaction.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        session = self.sessions.get(message.guild.id)
        if session is None or not session.active:
            return
        if message.channel.id != session.text_channel_id or message.author.id != session.owner_id:
            return
        if message.attachments or message.stickers:
            return
        if looks_like_command(message.content):
            return

        now = time.monotonic()
        if now - session.last_message_at < MESSAGE_COOLDOWN_SECONDS:
            logger.info("TTS message skipped by cooldown: guild=%s owner=%s", session.guild_id, session.owner_id)
            return
        session.last_message_at = now

        queued = await self.enqueue_text(
            session,
            message.content,
            message.author.display_name,
            source_message_id=message.id,
            send_file=True,
        )
        if queued:
            logger.info("TTS message queued: guild=%s owner=%s parts=%s", session.guild_id, session.owner_id, queued)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild_id = member.guild.id
        session = self.sessions.get(guild_id)
        if session is None:
            return

        bot_user = self.bot.user
        if bot_user is not None and member.id == bot_user.id:
            if before.channel and before.channel.id == session.voice_channel_id and (
                after.channel is None or after.channel.id != session.voice_channel_id
            ):
                await self.shutdown_session(guild_id, reason="bot disconnected or moved")
            return

        if member.id == session.owner_id:
            if after.channel is None or after.channel.id != session.voice_channel_id:
                await self.shutdown_session(guild_id, reason="owner left or moved")
                return

        voice_channel = member.guild.get_channel(session.voice_channel_id)
        if isinstance(voice_channel, discord.VoiceChannel):
            non_bot_members = [voice_member for voice_member in voice_channel.members if not voice_member.bot]
            if not non_bot_members:
                await self.shutdown_session(guild_id, reason="voice channel empty")


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(TTSVoiceCog(bot))

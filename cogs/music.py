from __future__ import annotations

import asyncio
import logging
import random
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

try:
    import yt_dlp
except ImportError:  # pragma: no cover - handled at runtime for clearer Discord errors.
    yt_dlp = None

logger = logging.getLogger(__name__)

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",
}
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
URL_PREFIXES = ("http://", "https://")


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by_id: int
    requested_by_name: str
    duration: int | None = None
    uploader: str | None = None
    thumbnail: str | None = None


@dataclass(slots=True)
class MusicGuildSettings:
    volume: float = 0.5
    loop: bool = False
    shuffle: bool = False
    stopped: bool = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "неизвестно"
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes}:{rest:02d}"


def _short(text: str | None, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _is_http_url(query: str) -> bool:
    lowered = query.strip().lower()
    return lowered.startswith(URL_PREFIXES)


def _is_safe_url(query: str) -> bool:
    stripped = query.strip()
    lowered = stripped.lower()
    if len(stripped) > 2000:
        return False
    if not lowered.startswith(URL_PREFIXES):
        return False
    if lowered.startswith(("file://", "ftp://")):
        return False
    return "../" not in lowered and "..\\" not in lowered


def _can_manage_music(user: discord.Member | discord.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    return perms.administrator or perms.manage_guild


class MusicSearchModal(discord.ui.Modal, title="Найти / включить музыку"):
    query = discord.ui.TextInput(
        label="Название песни или URL",
        placeholder="Например: crystal castles vanished или https://youtube.com/...",
        max_length=300,
    )

    def __init__(self, cog: "MusicCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_query_interaction(interaction, str(self.query), from_panel=True)


class MusicVolumeModal(discord.ui.Modal, title="Громкость"):
    volume = discord.ui.TextInput(label="Громкость 0-100", placeholder="65", max_length=3)

    def __init__(self, cog: "MusicCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_volume_submit(interaction, str(self.volume))


class MusicSearchSelect(discord.ui.Select):
    def __init__(self, cog: "MusicCog", tracks: list[Track]) -> None:
        self.cog = cog
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=_short(track.title, 100) or "Без названия",
                description=_short(f"{track.uploader or 'неизвестный автор'} • {_format_duration(track.duration)}", 100),
                value=str(index),
                emoji="🎵",
            )
            for index, track in enumerate(tracks[:5])
        ]
        super().__init__(placeholder="Выбери трек из музыкальной помойки", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        index = int(self.values[0])
        track = self.tracks[index]
        logger.info("Music search result selected: guild=%s title=%s", interaction.guild_id, track.title)
        await self.cog.enqueue_or_play_interaction(interaction, track)


class MusicSearchSelectView(discord.ui.View):
    def __init__(self, cog: "MusicCog", tracks: list[Track]) -> None:
        super().__init__(timeout=120)
        self.add_item(MusicSearchSelect(cog, tracks))


class QueueControlView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Очистить очередь", emoji="🧹", style=discord.ButtonStyle.danger)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.ensure_can_control(interaction):
            return
        self.cog.queues[self.guild_id] = []
        await self.cog.update_panel(self.guild_id)
        await self.cog.send_interaction_message(interaction, "🧹 Очередь вычищена до скрипа.", ephemeral=True)

    @discord.ui.button(label="Перемешать", emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.ensure_can_control(interaction):
            return
        random.shuffle(self.cog.queues.setdefault(self.guild_id, []))
        await self.cog.update_panel(self.guild_id)
        await self.cog.send_interaction_message(interaction, "🔀 Очередь перемешана.", ephemeral=True)


class MusicPanelView(discord.ui.View):
    def __init__(self, cog: "MusicCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Найти / Включить", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="music:search", row=0)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicSearchModal(self.cog))

    @discord.ui.button(label="Пауза / Продолжить", emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause_resume", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.pause_resume_action(interaction)

    @discord.ui.button(label="Скип", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.skip_action(interaction)

    @discord.ui.button(label="Стоп", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.stop_action(interaction)

    @discord.ui.button(label="Выйти", emoji="👋", style=discord.ButtonStyle.danger, custom_id="music:leave", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.leave_action(interaction)

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=1)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.queue_action(interaction)

    @discord.ui.button(label="Громкость", emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music:volume", row=1)
    async def volume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicVolumeModal(self.cog))

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.loop_action(interaction)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.shuffle_action(interaction)

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="music:refresh", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None:
            await self.cog.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        await self.cog.update_panel(interaction.guild.id)
        await self.cog.send_interaction_message(interaction, "🔄 Обновил пульт.", ephemeral=True)


class MusicCog(commands.Cog):
    music_group = app_commands.Group(name="music", description="Управление музыкальным пультом")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.queues: dict[int, list[Track]] = {}
        self.now_playing: dict[int, Track] = {}
        self.settings: dict[int, MusicGuildSettings] = {}
        self.panel_messages: dict[int, tuple[int, int]] = {}
        self.bot.add_view(MusicPanelView(self))

    async def init_db(self) -> None:
        if self.bot.db is None:
            logger.error("Music cog loaded without database connection")
            return
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS music_settings (
                guild_id INTEGER PRIMARY KEY,
                volume REAL NOT NULL DEFAULT 0.5,
                loop INTEGER NOT NULL DEFAULT 0,
                shuffle INTEGER NOT NULL DEFAULT 0,
                panel_channel_id INTEGER,
                panel_message_id INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.commit()
        await self.load_music_settings()

    async def load_music_settings(self) -> None:
        if self.bot.db is None:
            return
        cursor = await self.bot.db.execute(
            "SELECT guild_id, volume, loop, shuffle, panel_channel_id, panel_message_id FROM music_settings"
        )
        for row in await cursor.fetchall():
            guild_id = int(row["guild_id"])
            self.settings[guild_id] = MusicGuildSettings(
                volume=max(0.0, min(1.0, float(row["volume"]))),
                loop=bool(row["loop"]),
                shuffle=bool(row["shuffle"]),
            )
            if row["panel_channel_id"] and row["panel_message_id"]:
                self.panel_messages[guild_id] = (int(row["panel_channel_id"]), int(row["panel_message_id"]))

    async def get_settings(self, guild_id: int) -> MusicGuildSettings:
        if guild_id in self.settings:
            return self.settings[guild_id]
        settings = MusicGuildSettings()
        self.settings[guild_id] = settings
        if self.bot.db is not None:
            await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO music_settings (guild_id, volume, loop, shuffle, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, settings.volume, int(settings.loop), int(settings.shuffle), _now_iso()),
            )
            await self.bot.db.commit()
        return settings

    async def save_settings(self, guild_id: int) -> None:
        if self.bot.db is None:
            return
        settings = await self.get_settings(guild_id)
        panel_channel_id, panel_message_id = self.panel_messages.get(guild_id, (None, None))
        await self.bot.db.execute(
            """
            INSERT INTO music_settings (guild_id, volume, loop, shuffle, panel_channel_id, panel_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                volume = excluded.volume,
                loop = excluded.loop,
                shuffle = excluded.shuffle,
                panel_channel_id = excluded.panel_channel_id,
                panel_message_id = excluded.panel_message_id,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                settings.volume,
                int(settings.loop),
                int(settings.shuffle),
                panel_channel_id,
                panel_message_id,
                _now_iso(),
            ),
        )
        await self.bot.db.commit()

    def voice_dependency_error(self) -> str | None:
        if shutil.which("ffmpeg") is None:
            return "FFmpeg не найден. Без него я немой кусок железа. Нужен Docker image/egg с ffmpeg."
        try:
            import nacl  # noqa: F401
        except ImportError:
            return "Голосовой модуль Discord не установлен. Проверь discord.py[voice] и PyNaCl."
        if yt_dlp is None:
            return "yt-dlp не установлен. Без него я не достану аудио из музыкальной помойки."
        return None

    async def send_interaction_message(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ) -> None:
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
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return None
        dependency_error = self.voice_dependency_error()
        if dependency_error:
            await self.send_interaction_message(interaction, dependency_error, ephemeral=True)
            return None

        user_voice = interaction.user.voice
        if user_voice is None or user_voice.channel is None:
            await self.send_interaction_message(interaction, "Сначала зайди в голосовой канал, кожаный. Я не буду петь в пустоту.", ephemeral=True)
            return None

        channel = user_voice.channel
        me = interaction.guild.me
        permissions = channel.permissions_for(me)
        if not permissions.connect or not permissions.speak:
            await self.send_interaction_message(interaction, "У меня нет прав зайти или говорить в этом голосовом канале.", ephemeral=True)
            return None

        voice_client = interaction.guild.voice_client
        try:
            if isinstance(voice_client, discord.VoiceClient):
                if voice_client.channel != channel:
                    logger.info("Moving voice client: guild=%s channel=%s", interaction.guild.id, channel.id)
                    await voice_client.move_to(channel)
                return voice_client

            logger.info("Connecting voice client: guild=%s channel=%s", interaction.guild.id, channel.id)
            connected = await channel.connect()
            if isinstance(connected, discord.VoiceClient):
                return connected
        except (discord.Forbidden, discord.ClientException, discord.HTTPException):
            logger.exception("Discord voice connection failed")
            await self.send_interaction_message(interaction, "У меня нет прав зайти или говорить в этом голосовом канале.", ephemeral=True)
        return None

    async def ensure_can_control(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return False
        if _can_manage_music(interaction.user):
            return True
        voice_client = interaction.guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or voice_client.channel is None:
            await self.send_interaction_message(interaction, "Сейчас ничего не играет. Даже демоны молчат.", ephemeral=True)
            return False
        user_voice = interaction.user.voice
        if user_voice and user_voice.channel == voice_client.channel:
            return True
        await self.send_interaction_message(interaction, "Ты даже не в моём голосовом канале. Командовать издалека не выйдет.", ephemeral=True)
        return False

    async def extract_tracks(self, query: str, requested_by: discord.Member | discord.User, *, search: bool) -> tuple[list[Track], bool]:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        search_query = f"ytsearch5:{query}" if search else query
        logger.info("yt-dlp extract: search=%s query=%s", search, query)
        loop = asyncio.get_running_loop()

        def extract() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
                return ytdl.extract_info(search_query, download=False)

        data = await loop.run_in_executor(None, extract)
        playlist_detected = False
        entries = data.get("entries") if isinstance(data, dict) else None
        if entries:
            playlist_detected = not search
            items = [entry for entry in entries if entry][:5]
        elif isinstance(data, dict):
            items = [data]
        else:
            items = []

        tracks: list[Track] = []
        for item in items:
            stream_url = item.get("url")
            webpage_url = item.get("webpage_url") or item.get("original_url") or item.get("url")
            title = item.get("title") or "Без названия"
            if not stream_url or not webpage_url:
                continue
            tracks.append(
                Track(
                    title=str(title),
                    webpage_url=str(webpage_url),
                    stream_url=str(stream_url),
                    requested_by_id=requested_by.id,
                    requested_by_name=getattr(requested_by, "display_name", requested_by.name),
                    duration=item.get("duration"),
                    uploader=item.get("uploader") or item.get("channel"),
                    thumbnail=item.get("thumbnail"),
                )
            )
        return tracks, playlist_detected

    async def handle_query_interaction(self, interaction: discord.Interaction, query: str, *, from_panel: bool = False) -> None:
        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            await self.send_interaction_message(interaction, "Скорми мне название или URL, не воздух.", ephemeral=True)
            return
        if _is_http_url(clean_query) and not _is_safe_url(clean_query):
            await self.send_interaction_message(interaction, "Ссылка тухлая или небезопасная. Жру только http/https без странных трюков.", ephemeral=True)
            return
        if not _is_http_url(clean_query) and ("://" in clean_query or "../" in clean_query or "..\\" in clean_query):
            await self.send_interaction_message(interaction, "Такой путь я не ем. Дай нормальный URL или название трека.", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=not _is_http_url(clean_query))

        try:
            tracks, playlist_detected = await self.extract_tracks(clean_query, interaction.user, search=not _is_http_url(clean_query))
        except Exception:
            logger.exception("yt-dlp failed")
            await interaction.followup.send("Не смог достать аудио. Ссылка тухлая или сервис сопротивляется.", ephemeral=True)
            return

        if not tracks:
            await interaction.followup.send("Я порылся в музыкальной помойке и ничего не нашёл.", ephemeral=True)
            return
        if _is_http_url(clean_query):
            if playlist_detected:
                await interaction.followup.send("Плейлисты пока не жру целиком. Включаю первый трек.", ephemeral=True)
            await self.enqueue_or_play_interaction(interaction, tracks[0])
            return

        embed = discord.Embed(
            title="🔎 Результаты поиска",
            description="Выбери трек из списка ниже.",
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, view=MusicSearchSelectView(self, tracks), ephemeral=True)

    async def enqueue_or_play_interaction(self, interaction: discord.Interaction, track: Track) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        voice_client = await self.ensure_voice(interaction)
        if voice_client is None:
            return

        queue = self.queues.setdefault(interaction.guild.id, [])
        if voice_client.is_playing() or voice_client.is_paused() or interaction.guild.id in self.now_playing:
            queue.append(track)
            logger.info("Track queued: guild=%s title=%s", interaction.guild.id, track.title)
            await self.update_panel(interaction.guild.id)
            await self.send_interaction_message(
                interaction,
                embed=self.simple_embed("➕ Добавлено в очередь", f"**{track.title}**", track),
                ephemeral=False,
            )
            return

        await self.start_track(interaction.guild.id, voice_client, track)
        await self.send_interaction_message(
            interaction,
            embed=self.simple_embed("▶️ Играю", f"**{track.title}**", track),
            ephemeral=False,
        )

    async def start_track(self, guild_id: int, voice_client: discord.VoiceClient, track: Track) -> None:
        dependency_error = self.voice_dependency_error()
        if dependency_error:
            logger.error("Cannot start track: %s", dependency_error)
            return
        settings = await self.get_settings(guild_id)
        settings.stopped = False
        self.now_playing[guild_id] = track

        try:
            source = discord.FFmpegPCMAudio(
                track.stream_url,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
            volume_source = discord.PCMVolumeTransformer(source, volume=settings.volume)
        except Exception:
            logger.exception("FFmpeg source creation failed")
            self.now_playing.pop(guild_id, None)
            return

        def after_play(error: Exception | None) -> None:
            if error:
                logger.error("Music playback error", exc_info=(type(error), error, error.__traceback__))
            asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self.bot.loop)

        logger.info("Starting track: guild=%s title=%s", guild_id, track.title)
        try:
            voice_client.play(volume_source, after=after_play)
        except Exception:
            logger.exception("FFmpeg playback failed")
            self.now_playing.pop(guild_id, None)
            return
        await self.update_panel(guild_id)

    async def play_next(self, guild_id: int) -> None:
        settings = await self.get_settings(guild_id)
        if settings.stopped:
            await self.update_panel(guild_id)
            return

        guild = self.bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            self.now_playing.pop(guild_id, None)
            await self.update_panel(guild_id)
            return

        next_track: Track | None = None
        if settings.loop and guild_id in self.now_playing:
            next_track = self.now_playing[guild_id]
        else:
            queue = self.queues.setdefault(guild_id, [])
            if queue:
                if settings.shuffle:
                    next_track = queue.pop(random.randrange(len(queue)))
                else:
                    next_track = queue.pop(0)

        if next_track is None:
            self.now_playing.pop(guild_id, None)
            await self.update_panel(guild_id)
            return
        await self.start_track(guild_id, voice_client, next_track)

    def simple_embed(self, title: str, description: str, track: Track | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=discord.Color.green())
        if track:
            embed.add_field(name="Заказал", value=track.requested_by_name, inline=True)
            embed.add_field(name="Длительность", value=_format_duration(track.duration), inline=True)
            if track.webpage_url:
                embed.add_field(name="Ссылка", value=f"[Открыть трек]({track.webpage_url})", inline=False)
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
        return embed

    async def build_panel_embed(self, guild_id: int) -> discord.Embed:
        settings = await self.get_settings(guild_id)
        track = self.now_playing.get(guild_id)
        queue_len = len(self.queues.get(guild_id, []))
        embed = discord.Embed(title="🎶 Музыкальный пульт Пахабщины", color=discord.Color.purple())
        if track:
            embed.description = f"▶️ Сейчас играет:\n**{track.title}**"
            embed.add_field(name="Заказал", value=track.requested_by_name, inline=True)
            embed.add_field(name="Автор", value=track.uploader or "неизвестно", inline=True)
            embed.add_field(name="Длительность", value=_format_duration(track.duration), inline=True)
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
        else:
            embed.description = "Сейчас ничего не играет. Нажми 🔎 и скорми мне песню."

        guild = self.bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None
        status = "ничего не играет"
        if isinstance(voice_client, discord.VoiceClient):
            if voice_client.is_paused():
                status = "пауза"
            elif voice_client.is_playing():
                status = "играет"

        embed.add_field(name="Статус", value=status, inline=True)
        embed.add_field(name="Громкость", value=f"{int(settings.volume * 100)}%", inline=True)
        embed.add_field(name="Loop", value="ON" if settings.loop else "OFF", inline=True)
        embed.add_field(name="Shuffle", value="ON" if settings.shuffle else "OFF", inline=True)
        embed.add_field(name="Очередь", value=f"{queue_len} треков", inline=True)
        embed.set_footer(text="Вставь URL или название через кнопку 🔎")
        return embed

    async def update_panel(self, guild_id: int) -> None:
        panel = self.panel_messages.get(guild_id)
        if not panel:
            return
        channel_id, message_id = panel
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                logger.exception("Failed to fetch music panel channel")
                return
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=await self.build_panel_embed(guild_id), view=MusicPanelView(self))
        except discord.HTTPException:
            logger.exception("Failed to update music panel")

    async def pause_resume_action(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_can_control(interaction):
            return
        voice_client = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(voice_client, discord.VoiceClient):
            await self.send_interaction_message(interaction, "Сейчас ничего не играет. Даже демоны молчат.", ephemeral=True)
            return
        if voice_client.is_playing():
            voice_client.pause()
            await self.update_panel(interaction.guild.id)
            await self.send_interaction_message(interaction, "⏸️ Поставил на паузу.", ephemeral=True)
        elif voice_client.is_paused():
            voice_client.resume()
            await self.update_panel(interaction.guild.id)
            await self.send_interaction_message(interaction, "▶️ Продолжаю.", ephemeral=True)
        else:
            await self.send_interaction_message(interaction, "Сейчас ничего не играет. Даже демоны молчат.", ephemeral=True)

    async def skip_action(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_can_control(interaction):
            return
        voice_client = interaction.guild.voice_client if interaction.guild else None
        if not isinstance(voice_client, discord.VoiceClient) or not (voice_client.is_playing() or voice_client.is_paused()):
            await self.send_interaction_message(interaction, "Скипать нечего. Музыкальная пустота смотрит в ответ.", ephemeral=True)
            return
        logger.info("Track skipped: guild=%s user=%s", interaction.guild.id, interaction.user.id)
        voice_client.stop()
        await self.send_interaction_message(interaction, "⏭️ Скипнул. Следующий грешник из очереди пошёл.", ephemeral=True)

    async def stop_action(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_can_control(interaction):
            return
        guild_id = interaction.guild.id
        settings = await self.get_settings(guild_id)
        settings.stopped = True
        self.queues[guild_id] = []
        self.now_playing.pop(guild_id, None)
        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
        logger.info("Music stopped: guild=%s user=%s", guild_id, interaction.user.id)
        await self.update_panel(guild_id)
        await self.send_interaction_message(interaction, "⏹️ Остановил музыку и сжёг очередь.", ephemeral=True)

    async def leave_action(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_can_control(interaction):
            return
        guild_id = interaction.guild.id
        settings = await self.get_settings(guild_id)
        settings.stopped = True
        self.queues[guild_id] = []
        self.now_playing.pop(guild_id, None)
        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient):
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            try:
                await voice_client.disconnect(force=False)
            except discord.HTTPException:
                logger.exception("Voice disconnect failed")
        logger.info("Music leave: guild=%s user=%s", guild_id, interaction.user.id)
        await self.update_panel(guild_id)
        await self.send_interaction_message(interaction, "👋 Ушёл из голосового. Не скучайте слишком громко.", ephemeral=True)

    async def queue_action(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        queue = self.queues.get(interaction.guild.id, [])
        if not queue:
            embed = discord.Embed(title="📜 Очередь", description="Очередь пустая. Музыкальная помойка молчит.", color=discord.Color.dark_grey())
            await self.send_interaction_message(interaction, embed=embed, view=QueueControlView(self, interaction.guild.id), ephemeral=True)
            return
        lines = [f"{index}. **{_short(track.title, 70)}** — {track.requested_by_name}" for index, track in enumerate(queue[:10], 1)]
        if len(queue) > 10:
            lines.append(f"И ещё {len(queue) - 10} треков...")
        embed = discord.Embed(title="📜 Очередь", description="\n".join(lines), color=discord.Color.blurple())
        await self.send_interaction_message(interaction, embed=embed, view=QueueControlView(self, interaction.guild.id), ephemeral=True)

    async def handle_volume_submit(self, interaction: discord.Interaction, raw_value: str) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self.ensure_can_control(interaction):
            return
        try:
            value = int(str(raw_value).strip())
        except ValueError:
            await self.send_interaction_message(interaction, "Громкость должна быть числом от 0 до 100.", ephemeral=True)
            return
        if not 0 <= value <= 100:
            await self.send_interaction_message(interaction, "Громкость должна быть числом от 0 до 100.", ephemeral=True)
            return

        settings = await self.get_settings(interaction.guild.id)
        settings.volume = value / 100
        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and isinstance(voice_client.source, discord.PCMVolumeTransformer):
            voice_client.source.volume = settings.volume
        await self.save_settings(interaction.guild.id)
        await self.update_panel(interaction.guild.id)
        await self.send_interaction_message(interaction, f"🔊 Громкость поставил на {value}%. Уши берегите сами.", ephemeral=True)

    async def loop_action(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self.ensure_can_control(interaction):
            return
        settings = await self.get_settings(interaction.guild.id)
        settings.loop = not settings.loop
        await self.save_settings(interaction.guild.id)
        await self.update_panel(interaction.guild.id)
        text = "🔁 Loop включён. Этот трек теперь в цифровом аду." if settings.loop else "🔁 Loop выключен."
        await self.send_interaction_message(interaction, text, ephemeral=True)

    async def shuffle_action(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        if not await self.ensure_can_control(interaction):
            return
        settings = await self.get_settings(interaction.guild.id)
        settings.shuffle = not settings.shuffle
        await self.save_settings(interaction.guild.id)
        await self.update_panel(interaction.guild.id)
        text = "🔀 Shuffle включён. Очередь пошла по кривой дорожке." if settings.shuffle else "🔀 Shuffle выключен."
        await self.send_interaction_message(interaction, text, ephemeral=True)

    @app_commands.command(name="music_panel", description="Отправить музыкальный пульт Пахабщины")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def music_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Команда доступна только в текстовом канале сервера.", ephemeral=True)
            return
        if not _can_manage_music(interaction.user):
            await interaction.response.send_message("Нужны права Administrator или Manage Server.", ephemeral=True)
            return
        logger.info("Creating music panel: guild=%s channel=%s", interaction.guild.id, interaction.channel.id)
        embed = await self.build_panel_embed(interaction.guild.id)
        await interaction.response.send_message(embed=embed, view=MusicPanelView(self))
        message = await interaction.original_response()
        self.panel_messages[interaction.guild.id] = (message.channel.id, message.id)
        await self.save_settings(interaction.guild.id)

    @app_commands.command(name="play", description="Включить музыку по URL или поисковому запросу")
    @app_commands.describe(query="URL или название песни")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await self.handle_query_interaction(interaction, query)

    @music_group.command(name="skip", description="Пропустить текущий трек")
    async def skip_command(self, interaction: discord.Interaction) -> None:
        await self.skip_action(interaction)

    @music_group.command(name="stop", description="Остановить музыку и очистить очередь")
    async def stop_command(self, interaction: discord.Interaction) -> None:
        await self.stop_action(interaction)

    @music_group.command(name="leave", description="Отключить бота от голосового канала")
    async def leave_command(self, interaction: discord.Interaction) -> None:
        await self.leave_action(interaction)

    @music_group.command(name="queue", description="Показать музыкальную очередь")
    async def queue_command(self, interaction: discord.Interaction) -> None:
        await self.queue_action(interaction)

    @music_group.command(name="nowplaying", description="Показать текущий трек")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        track = self.now_playing.get(interaction.guild.id)
        if not track:
            await interaction.response.send_message("Сейчас ничего не играет. Даже демоны молчат.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.simple_embed("▶️ Сейчас играет", f"**{track.title}**", track))

    @music_group.command(name="volume", description="Поставить громкость музыки")
    @app_commands.describe(value="Громкость 0-100")
    async def volume_command(self, interaction: discord.Interaction, value: int) -> None:
        await self.handle_volume_submit(interaction, str(value))


async def setup(bot: MovieBot) -> None:
    cog = MusicCog(bot)
    await cog.init_db()
    await bot.add_cog(cog)

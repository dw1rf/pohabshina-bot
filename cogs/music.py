from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.voice_runtime import FFMPEG_MISSING_MESSAGE, find_ffmpeg, log_binary_version, require_ffmpeg

try:
    import yt_dlp
except ImportError:  # pragma: no cover - handled at runtime for clearer Discord errors.
    yt_dlp = None

logger = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
MAX_QUEUE_PAGE_VALUE = 950
MAX_PLAYLIST_TRACKS = 100
URL_PREFIXES = ("http://", "https://")

YTDL_BASE_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "ignoreerrors": True,
}
YTDLP_COOKIE_FILE_ENV = "YTDLP_COOKIE_FILE"


def _ytdl_options(**overrides: Any) -> dict[str, Any]:
    options = {**YTDL_BASE_OPTIONS, **overrides}
    cookie_file = os.getenv(YTDLP_COOKIE_FILE_ENV, "").strip()
    if cookie_file:
        if os.path.isfile(cookie_file):
            options["cookiefile"] = cookie_file
        else:
            logger.warning("%s is set but file does not exist: %s", YTDLP_COOKIE_FILE_ENV, cookie_file)
    return options


class MusicUserError(Exception):
    """A user-facing music error with a safe Russian message."""


class ExternalPlaylistNotConfigured(MusicUserError):
    pass


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int | None
    requester_id: int
    requester_name: str
    thumbnail: str | None = None


@dataclass(slots=True)
class MusicPlayer:
    guild_id: int
    voice_client: discord.VoiceClient | None = None
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    is_paused: bool = False
    shuffle_enabled: bool = False
    autoplay_enabled: bool = False
    audio_player_task: asyncio.Task[None] | None = None
    stopped: bool = False


def _short(text: str | None, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "неизвестно"
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes}:{rest:02d}"


def _is_http_url(query: str) -> bool:
    return query.strip().lower().startswith(URL_PREFIXES)


def _is_safe_url(query: str) -> bool:
    value = query.strip()
    lowered = value.lower()
    if len(value) > 2000:
        return False
    if not lowered.startswith(URL_PREFIXES):
        return False
    return "../" not in lowered and "..\\" not in lowered


def _host_matches(host: str, *suffixes: str) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def _is_spotify_or_apple(query: str) -> bool:
    try:
        host = urlparse(query).netloc.lower()
    except ValueError:
        return False
    return _host_matches(host, "spotify.com", "music.apple.com")


def _track_from_info(info: dict[str, Any], requester: discord.Member | discord.User) -> Track | None:
    title = info.get("title") or "Без названия"
    webpage_url = info.get("webpage_url") or info.get("original_url")
    if not webpage_url and info.get("id"):
        webpage_url = f"https://www.youtube.com/watch?v={info['id']}"
    stream_url = info.get("url")
    if not webpage_url or not stream_url:
        return None
    requester_name = getattr(requester, "display_name", None) or getattr(requester, "name", None) or "Пользователь"
    return Track(
        title=str(title),
        webpage_url=str(webpage_url),
        stream_url=str(stream_url),
        duration=info.get("duration"),
        requester_id=requester.id,
        requester_name=requester_name,
        thumbnail=info.get("thumbnail"),
    )


class MusicCog(commands.Cog):
    music_group = app_commands.Group(name="music", description="Музыка: YouTube, очередь и управление голосом")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self._ffmpeg_executable: str | None = None
        self._ffmpeg_logged = False
        ffmpeg_path = self.ffmpeg_executable()
        if ffmpeg_path:
            logger.info("Music cog found FFmpeg: %s", ffmpeg_path)
            self.log_ffmpeg_details(ffmpeg_path)
        else:
            logger.error("%s Music playback commands will return a clear user error.", FFMPEG_MISSING_MESSAGE)

    def cog_unload(self) -> None:
        for guild_id in list(self.players):
            self.bot.loop.create_task(self.shutdown_player(guild_id, disconnect=True))

    def get_player(self, guild_id: int) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(guild_id=guild_id)
            self.players[guild_id] = player
        return player

    def ffmpeg_executable(self) -> str | None:
        if self._ffmpeg_executable:
            return self._ffmpeg_executable
        ffmpeg_path = find_ffmpeg()
        if ffmpeg_path:
            self._ffmpeg_executable = ffmpeg_path
            return ffmpeg_path
        logger.error("%s find_ffmpeg() returned nothing", FFMPEG_MISSING_MESSAGE)
        return None

    def log_ffmpeg_details(self, ffmpeg_executable: str) -> None:
        if self._ffmpeg_logged:
            return
        self._ffmpeg_logged = True
        log_binary_version(logger, "ffmpeg", ffmpeg_executable, "-version")

    def dependency_error(self) -> str | None:
        if self.ffmpeg_executable() is None:
            logger.error("%s", FFMPEG_MISSING_MESSAGE)
            return "FFmpeg не найден. Попросите администратора установить системный ffmpeg и перезапустить бота."
        try:
            import nacl  # noqa: F401
        except ImportError:
            return "Голосовой модуль Discord не установлен. Проверьте discord.py[voice] и PyNaCl."
        if yt_dlp is None:
            return "yt-dlp не установлен. Без него я не могу искать и включать музыку."
        return None

    async def send_interaction_message(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        ephemeral: bool = True,
    ) -> None:
        kwargs: dict[str, Any] = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def send_context_message(
        self,
        ctx: commands.Context[MovieBot],
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> None:
        await ctx.reply(content=content, embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    def _tts_session_active(self, guild_id: int) -> bool:
        tts_cog = self.bot.get_cog("TTSVoiceCog")
        sessions = getattr(tts_cog, "sessions", None)
        return isinstance(sessions, dict) and guild_id in sessions

    async def ensure_voice(
        self,
        guild: discord.Guild | None,
        user: discord.Member | discord.User,
        *,
        send_error: Any,
    ) -> discord.VoiceClient | None:
        if guild is None or not isinstance(user, discord.Member):
            await send_error("Команда доступна только на сервере.")
            return None

        dependency_error = self.dependency_error()
        if dependency_error:
            await send_error(dependency_error)
            return None

        user_voice = user.voice
        if user_voice is None or user_voice.channel is None:
            await send_error("Зайди в голосовой канал.")
            return None

        if self._tts_session_active(guild.id):
            await send_error("Сейчас активна озвучка TTS. Сначала выключите TTS или дождитесь окончания.")
            return None

        channel = user_voice.channel
        me = guild.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not permissions.connect or not permissions.speak:
                await send_error("У меня нет прав зайти или говорить в этом голосовом канале.")
                return None

        player = self.get_player(guild.id)
        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient):
            player.voice_client = voice_client
            if voice_client.channel != channel:
                await send_error("Бот уже подключён к другому голосовому каналу на этом сервере.")
                return None
            if (voice_client.is_playing() or voice_client.is_paused()) and player.current is None:
                await send_error("Бот уже занят другим голосовым модулем.")
                return None
            return voice_client

        try:
            logger.info("Music connecting: guild=%s channel=%s", guild.id, channel.id)
            connected = await channel.connect(timeout=15, reconnect=True)
        except (TimeoutError, discord.Forbidden, discord.ClientException, discord.HTTPException):
            logger.exception("Music voice connection failed: guild=%s channel=%s", guild.id, channel.id)
            await send_error("Не удалось подключиться к голосовому каналу.")
            return None

        if isinstance(connected, discord.VoiceClient):
            player.voice_client = connected
            return connected
        await send_error("Не удалось создать голосовое подключение.")
        return None

    async def ensure_can_control(
        self,
        guild: discord.Guild | None,
        user: discord.Member | discord.User,
        *,
        send_error: Any,
    ) -> MusicPlayer | None:
        if guild is None or not isinstance(user, discord.Member):
            await send_error("Команда доступна только на сервере.")
            return None
        player = self.get_player(guild.id)
        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or voice_client.channel is None:
            await send_error("Сейчас ничего не играет.")
            return None
        user_voice = user.voice
        if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
            player.voice_client = voice_client
            return player
        if user_voice and user_voice.channel == voice_client.channel:
            player.voice_client = voice_client
            return player
        await send_error("Ты должен быть в том же голосовом канале, что и бот.")
        return None

    async def extract_tracks(
        self,
        query: str,
        requester: discord.Member | discord.User,
        *,
        allow_playlist: bool,
    ) -> tuple[list[Track], bool]:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        clean_query = " ".join(query.split())
        if _is_spotify_or_apple(clean_query):
            raise ExternalPlaylistNotConfigured("Поддержка плейлистов Spotify/Apple пока не настроена.")

        is_url = _is_http_url(clean_query)
        ytdl_query = clean_query if is_url else f"ytsearch1:{clean_query}"
        options = _ytdl_options(
            noplaylist=not allow_playlist,
            playlistend=MAX_PLAYLIST_TRACKS,
        )
        logger.info("yt-dlp extract: query=%s playlist=%s", clean_query, allow_playlist)

        def extract() -> dict[str, Any] | None:
            with yt_dlp.YoutubeDL(options) as ytdl:
                return ytdl.extract_info(ytdl_query, download=False)

        data = await asyncio.to_thread(extract)
        if not isinstance(data, dict):
            return [], False

        entries = data.get("entries")
        playlist_detected = bool(entries) and is_url
        raw_items: list[dict[str, Any]] = []
        if entries:
            raw_items.extend(entry for entry in entries if isinstance(entry, dict))
        else:
            raw_items.append(data)

        tracks: list[Track] = []
        for item in raw_items[:MAX_PLAYLIST_TRACKS]:
            track = _track_from_info(item, requester)
            if track is not None:
                tracks.append(track)
        return tracks, playlist_detected

    async def refresh_stream_url(self, track: Track) -> Track:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        options = _ytdl_options(noplaylist=True)

        def extract() -> dict[str, Any] | None:
            with yt_dlp.YoutubeDL(options) as ytdl:
                return ytdl.extract_info(track.webpage_url, download=False)

        data = await asyncio.to_thread(extract)
        if not isinstance(data, dict):
            raise RuntimeError("yt-dlp returned no track info")
        refreshed = _track_from_info(
            data,
            discord.Object(id=track.requester_id),  # type: ignore[arg-type]
        )
        if refreshed is None:
            raise RuntimeError("yt-dlp returned no playable stream")
        refreshed.requester_id = track.requester_id
        refreshed.requester_name = track.requester_name
        return refreshed

    async def add_autoplay_track(self, player: MusicPlayer, previous: Track) -> bool:
        query = f"{previous.title} music mix"
        try:
            tracks, _ = await self.extract_tracks(
                query,
                discord.Object(id=previous.requester_id),  # type: ignore[arg-type]
                allow_playlist=False,
            )
        except Exception:
            logger.exception("Autoplay search failed: guild=%s title=%s", player.guild_id, previous.title)
            return False
        if not tracks:
            logger.warning("Autoplay found no related track: guild=%s title=%s", player.guild_id, previous.title)
            return False
        track = tracks[0]
        track.requester_id = previous.requester_id
        track.requester_name = "Автоплей"
        player.queue.append(track)
        logger.info("Autoplay queued track: guild=%s title=%s", player.guild_id, track.title)
        return True

    async def add_tracks(
        self,
        guild: discord.Guild,
        voice_client: discord.VoiceClient,
        tracks: list[Track],
        *,
        mode: str,
    ) -> tuple[str, Track | None, int]:
        player = self.get_player(guild.id)
        player.voice_client = voice_client
        if not tracks:
            raise MusicUserError("Ничего не найдено.")

        async with player.lock:
            player.stopped = False
            first_track = tracks[0]
            if mode == "now":
                player.queue = tracks + player.queue
                logger.info("Track queued as current: guild=%s title=%s count=%s", guild.id, first_track.title, len(tracks))
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                    return "now", first_track, len(tracks)
                started = await self.start_next_locked(player)
                return "started" if started else "queued", first_track, len(tracks)

            if mode == "next":
                player.queue = tracks + player.queue
                logger.info("Track queued next: guild=%s title=%s count=%s", guild.id, first_track.title, len(tracks))
            else:
                player.queue.extend(tracks)
                logger.info("Track queued: guild=%s title=%s count=%s", guild.id, first_track.title, len(tracks))

            if player.current is None and not voice_client.is_playing() and not voice_client.is_paused():
                started = await self.start_next_locked(player)
                return "started" if started else "queued", first_track, len(tracks)
            return mode, first_track, len(tracks)

    async def start_next_locked(self, player: MusicPlayer) -> bool:
        voice_client = player.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            player.current = None
            return False
        while player.queue:
            index = random.randrange(len(player.queue)) if player.shuffle_enabled else 0
            track = player.queue.pop(index)
            if await self.start_track_locked(player, track):
                return True
            logger.warning("Skipping unplayable queued track: guild=%s title=%s", player.guild_id, track.title)
        player.current = None
        return False

    async def start_track_locked(self, player: MusicPlayer, track: Track) -> bool:
        voice_client = player.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            player.current = None
            return False

        try:
            ffmpeg_executable = require_ffmpeg()
            self._ffmpeg_executable = ffmpeg_executable
            self.log_ffmpeg_details(ffmpeg_executable)
            try:
                track = await self.refresh_stream_url(track)
            except Exception:
                logger.exception("yt-dlp failed to refresh stream URL: guild=%s title=%s", player.guild_id, track.title)
                return False
            source = discord.FFmpegPCMAudio(
                track.stream_url,
                executable=ffmpeg_executable,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
        except RuntimeError as exc:
            logger.error("FFmpeg missing for music playback: %s", exc)
            player.current = None
            return False
        except Exception:
            logger.exception("FFmpeg source creation failed: guild=%s title=%s", player.guild_id, track.title)
            player.current = None
            return False

        def after_play(error: Exception | None) -> None:
            if error:
                logger.error("Music playback error: guild=%s error=%s", player.guild_id, error)
            self.bot.loop.call_soon_threadsafe(
                lambda: self.bot.loop.create_task(self.after_track(player.guild_id, error))
            )

        try:
            player.current = track
            player.is_paused = False
            logger.info("Track started: guild=%s title=%s url=%s", player.guild_id, track.title, track.webpage_url)
            voice_client.play(source, after=after_play)
            return True
        except Exception:
            logger.exception("FFmpeg playback failed: guild=%s title=%s", player.guild_id, track.title)
            player.current = None
            return False

    async def after_track(self, guild_id: int, error: Exception | None) -> None:
        player = self.get_player(guild_id)
        async with player.lock:
            finished = player.current
            player.is_paused = False
            if player.stopped:
                player.current = None
                return
            if player.queue:
                await self.start_next_locked(player)
                return
            player.current = None
            if player.autoplay_enabled and finished is not None:
                added = await self.add_autoplay_track(player, finished)
                if added:
                    await self.start_next_locked(player)
                else:
                    player.autoplay_enabled = False
                    logger.warning("Autoplay disabled after failed lookup: guild=%s", guild_id)

    async def shutdown_player(self, guild_id: int, *, disconnect: bool) -> None:
        player = self.get_player(guild_id)
        async with player.lock:
            player.stopped = True
            player.queue.clear()
            player.current = None
            player.is_paused = False
            voice_client = player.voice_client
            if isinstance(voice_client, discord.VoiceClient):
                try:
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop()
                    if disconnect and voice_client.is_connected():
                        await voice_client.disconnect(force=False)
                        logger.info("Music disconnected: guild=%s", guild_id)
                except (discord.ClientException, discord.HTTPException):
                    logger.exception("Music disconnect failed: guild=%s", guild_id)
            if disconnect:
                self.players.pop(guild_id, None)

    def build_track_embed(self, title: str, track: Track, *, count: int = 1) -> discord.Embed:
        description = f"**[{_short(track.title, 180)}]({track.webpage_url})**"
        if count > 1:
            description += f"\nДобавлено треков: **{count}**"
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="Длительность", value=_format_duration(track.duration), inline=True)
        embed.add_field(name="Заказал", value=_short(track.requester_name, 80), inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    def build_current_embed(self, player: MusicPlayer) -> discord.Embed:
        track = player.current
        if track is None:
            return discord.Embed(title="Сейчас играет", description="Сейчас ничего не играет.", color=discord.Color.dark_grey())
        status = "пауза" if player.is_paused else "играет"
        embed = discord.Embed(
            title="Сейчас играет",
            description=f"**[{_short(track.title, 180)}]({track.webpage_url})**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Статус", value=status, inline=True)
        embed.add_field(name="Длительность", value=_format_duration(track.duration), inline=True)
        embed.add_field(name="Заказал", value=_short(track.requester_name, 80), inline=True)
        embed.add_field(name="Очередь", value=f"{len(player.queue)} треков", inline=True)
        embed.add_field(name="Shuffle", value="включён" if player.shuffle_enabled else "выключен", inline=True)
        embed.add_field(name="Автоплей", value="включён" if player.autoplay_enabled else "выключен", inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    def build_queue_embed(self, player: MusicPlayer, *, page: int) -> discord.Embed:
        total = len(player.queue)
        embed = discord.Embed(title="Очередь", color=discord.Color.blurple())
        if player.current:
            embed.add_field(
                name="Сейчас",
                value=f"**[{_short(player.current.title, 120)}]({player.current.webpage_url})**",
                inline=False,
            )
        if not player.queue:
            embed.description = "Очередь пустая."
            return embed

        page = max(1, page)
        start = (page - 1) * 10
        end = min(start + 10, total)
        if start >= total:
            page = 1
            start = 0
            end = min(10, total)

        lines: list[str] = []
        for index, track in enumerate(player.queue[start:end], start + 1):
            line = f"{index}. [{_short(track.title, 72)}]({track.webpage_url}) - {_short(track.requester_name, 32)}"
            candidate = "\n".join([*lines, line])
            if len(candidate) > MAX_QUEUE_PAGE_VALUE:
                break
            lines.append(line)

        value = "\n".join(lines) if lines else "На этой странице нет треков."
        embed.add_field(name=f"Треки {start + 1}-{end} из {total}", value=value[:1024], inline=False)
        pages = max(1, (total + 9) // 10)
        embed.set_footer(text=f"Страница {page}/{pages}. Используй page, чтобы открыть другую страницу.")
        return embed

    async def handle_add(
        self,
        guild: discord.Guild | None,
        user: discord.Member | discord.User,
        query: str,
        *,
        mode: str,
        send_error: Any,
        send_success: Any,
    ) -> None:
        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            await send_error("Укажи YouTube-ссылку или название трека.")
            return
        if _is_http_url(clean_query) and not _is_safe_url(clean_query):
            await send_error("Ссылка выглядит небезопасно. Принимаю только обычные http/https URL.")
            return
        if not _is_http_url(clean_query) and ("://" in clean_query or "../" in clean_query or "..\\" in clean_query):
            await send_error("Укажи обычную ссылку или название трека.")
            return

        voice_client = await self.ensure_voice(guild, user, send_error=send_error)
        if voice_client is None or guild is None:
            return

        try:
            tracks, playlist_detected = await self.extract_tracks(clean_query, user, allow_playlist=_is_http_url(clean_query))
        except ExternalPlaylistNotConfigured as exc:
            logger.info("External playlist rejected: guild=%s query=%s", guild.id, clean_query)
            await send_error(str(exc))
            return
        except Exception:
            logger.exception("yt-dlp failed: guild=%s query=%s", guild.id, clean_query)
            await send_error("Не смог найти или разобрать трек. Проверь ссылку/название и попробуй ещё раз.")
            return

        if playlist_detected:
            logger.info("YouTube playlist extracted: guild=%s count=%s", guild.id, len(tracks))
        try:
            result, first_track, count = await self.add_tracks(guild, voice_client, tracks, mode=mode)
        except MusicUserError as exc:
            await send_error(str(exc))
            return
        if first_track is None:
            await send_error("Ничего не найдено.")
            return

        if result == "started":
            embed_title = "Включаю"
        elif mode == "now":
            embed_title = "Включаю следующим"
        elif mode == "next":
            embed_title = "Добавлено следующим"
        else:
            embed_title = "Добавлено в очередь"
        await send_success(embed=self.build_track_embed(embed_title, first_track, count=count))

    async def handle_pause(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        async with player.lock:
            voice_client = guild.voice_client
            if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_playing():
                await send_error("Сейчас нечего ставить на паузу.")
                return ""
            voice_client.pause()
            player.is_paused = True
            logger.info("Music paused: guild=%s user=%s", guild.id, user.id)
            return "Пауза."

    async def handle_resume(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        async with player.lock:
            voice_client = guild.voice_client
            if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_paused():
                await send_error("Сейчас нет трека на паузе.")
                return ""
            voice_client.resume()
            player.is_paused = False
            logger.info("Music resumed: guild=%s user=%s", guild.id, user.id)
            return "Продолжаю."

    async def handle_skip(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        async with player.lock:
            voice_client = guild.voice_client
            if not isinstance(voice_client, discord.VoiceClient) or not (voice_client.is_playing() or voice_client.is_paused()):
                await send_error("Сейчас нечего пропускать.")
                return ""
            logger.info("Music skipped: guild=%s user=%s title=%s", guild.id, user.id, player.current.title if player.current else None)
            voice_client.stop()
            return "Пропускаю текущий трек."

    async def handle_stop(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        async with player.lock:
            player.stopped = True
            player.queue.clear()
            player.current = None
            player.is_paused = False
            voice_client = guild.voice_client
            if isinstance(voice_client, discord.VoiceClient) and (voice_client.is_playing() or voice_client.is_paused()):
                voice_client.stop()
            logger.info("Music stopped: guild=%s user=%s", guild.id, user.id)
            return "Музыка остановлена, очередь очищена."

    async def handle_clear(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        async with player.lock:
            count = len(player.queue)
            player.queue.clear()
            logger.info("Music queue cleared: guild=%s user=%s count=%s", guild.id, user.id, count)
            return f"Очередь очищена. Убрано треков: {count}."

    async def handle_leave(self, guild: discord.Guild | None, user: discord.Member | discord.User, *, send_error: Any) -> str:
        player = await self.ensure_can_control(guild, user, send_error=send_error)
        if player is None or guild is None:
            return ""
        await self.shutdown_player(guild.id, disconnect=True)
        logger.info("Music leave requested: guild=%s user=%s", guild.id, user.id)
        return "Вышел из голосового канала."

    async def handle_shuffle(
        self,
        guild: discord.Guild | None,
        user: discord.Member | discord.User,
        enabled: bool,
        *,
        send_error: Any,
    ) -> str:
        if guild is None:
            await send_error("Команда доступна только на сервере.")
            return ""
        player = self.get_player(guild.id)
        player.shuffle_enabled = enabled
        logger.info("Music shuffle changed: guild=%s user=%s enabled=%s", guild.id, user.id, enabled)
        return "Перемешивание включено." if enabled else "Перемешивание выключено."

    async def handle_autoplay(
        self,
        guild: discord.Guild | None,
        user: discord.Member | discord.User,
        enabled: bool,
        *,
        send_error: Any,
    ) -> str:
        if guild is None:
            await send_error("Команда доступна только на сервере.")
            return ""
        player = self.get_player(guild.id)
        if enabled and player.current is None and not player.queue:
            await send_error("Для автоплея нужен текущий трек или хотя бы один трек в очереди.")
            return ""
        player.autoplay_enabled = enabled
        logger.info("Music autoplay changed: guild=%s user=%s enabled=%s", guild.id, user.id, enabled)
        return "Автоплей включён." if enabled else "Автоплей выключен."

    @music_group.command(name="play", description="Добавить YouTube-ссылку или найденный по названию трек в очередь")
    @app_commands.describe(query="YouTube URL, YouTube playlist или название трека")
    @app_commands.guild_only()
    async def slash_play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        await self.handle_add(
            interaction.guild,
            interaction.user,
            query,
            mode="end",
            send_error=lambda text: interaction.followup.send(text, ephemeral=True),
            send_success=lambda **kwargs: interaction.followup.send(**kwargs),
        )

    @music_group.command(name="next", description="Добавить трек или плейлист следующим в очереди")
    @app_commands.describe(query="YouTube URL, YouTube playlist или название трека")
    @app_commands.guild_only()
    async def slash_next(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        await self.handle_add(
            interaction.guild,
            interaction.user,
            query,
            mode="next",
            send_error=lambda text: interaction.followup.send(text, ephemeral=True),
            send_success=lambda **kwargs: interaction.followup.send(**kwargs),
        )

    @music_group.command(name="now", description="Включить трек сразу, пропустив текущий")
    @app_commands.describe(query="YouTube URL, YouTube playlist или название трека")
    @app_commands.guild_only()
    async def slash_now(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        await self.handle_add(
            interaction.guild,
            interaction.user,
            query,
            mode="now",
            send_error=lambda text: interaction.followup.send(text, ephemeral=True),
            send_success=lambda **kwargs: interaction.followup.send(**kwargs),
        )

    @music_group.command(name="pause", description="Поставить музыку на паузу")
    @app_commands.guild_only()
    async def slash_pause(self, interaction: discord.Interaction) -> None:
        text = await self.handle_pause(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="resume", description="Продолжить музыку после паузы")
    @app_commands.guild_only()
    async def slash_resume(self, interaction: discord.Interaction) -> None:
        text = await self.handle_resume(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="skip", description="Пропустить текущий трек")
    @app_commands.guild_only()
    async def slash_skip(self, interaction: discord.Interaction) -> None:
        text = await self.handle_skip(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="stop", description="Остановить музыку и очистить очередь")
    @app_commands.guild_only()
    async def slash_stop(self, interaction: discord.Interaction) -> None:
        text = await self.handle_stop(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="leave", description="Отключить бота от голосового канала")
    @app_commands.guild_only()
    async def slash_leave(self, interaction: discord.Interaction) -> None:
        text = await self.handle_leave(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="queue", description="Показать очередь")
    @app_commands.describe(page="Номер страницы очереди")
    @app_commands.guild_only()
    async def slash_queue(self, interaction: discord.Interaction, page: int = 1) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        player = self.get_player(interaction.guild.id)
        await interaction.response.send_message(embed=self.build_queue_embed(player, page=page), ephemeral=True)

    @music_group.command(name="current", description="Показать текущий трек")
    @app_commands.guild_only()
    async def slash_current(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.build_current_embed(self.get_player(interaction.guild.id)))

    @music_group.command(name="clear", description="Очистить очередь, не отключая бота")
    @app_commands.guild_only()
    async def slash_clear(self, interaction: discord.Interaction) -> None:
        text = await self.handle_clear(interaction.guild, interaction.user, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="shuffle", description="Включить или выключить перемешивание очереди")
    @app_commands.describe(enabled="Включить перемешивание")
    @app_commands.guild_only()
    async def slash_shuffle(self, interaction: discord.Interaction, enabled: bool) -> None:
        text = await self.handle_shuffle(interaction.guild, interaction.user, enabled, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @music_group.command(name="autoplay", description="Включить или выключить автодобавление похожих треков")
    @app_commands.describe(enabled="Включить автоплей")
    @app_commands.guild_only()
    async def slash_autoplay(self, interaction: discord.Interaction, enabled: bool) -> None:
        text = await self.handle_autoplay(interaction.guild, interaction.user, enabled, send_error=lambda message: self.send_interaction_message(interaction, message))
        if text:
            await self.send_interaction_message(interaction, text)

    @commands.command(name="play", aliases=["играть"])
    async def text_play(self, ctx: commands.Context[MovieBot], *, query: str) -> None:
        await self.handle_add(
            ctx.guild,
            ctx.author,
            query,
            mode="end",
            send_error=lambda text: self.send_context_message(ctx, text),
            send_success=lambda **kwargs: self.send_context_message(ctx, **kwargs),
        )

    @commands.command(name="playnext", aliases=["next", "следующим"])
    async def text_next(self, ctx: commands.Context[MovieBot], *, query: str) -> None:
        await self.handle_add(
            ctx.guild,
            ctx.author,
            query,
            mode="next",
            send_error=lambda text: self.send_context_message(ctx, text),
            send_success=lambda **kwargs: self.send_context_message(ctx, **kwargs),
        )

    @commands.command(name="playnow", aliases=["now", "сейчас"])
    async def text_now(self, ctx: commands.Context[MovieBot], *, query: str) -> None:
        await self.handle_add(
            ctx.guild,
            ctx.author,
            query,
            mode="now",
            send_error=lambda text: self.send_context_message(ctx, text),
            send_success=lambda **kwargs: self.send_context_message(ctx, **kwargs),
        )

    @commands.command(name="pause", aliases=["пауза"])
    async def text_pause(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_pause(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="resume", aliases=["продолжить"])
    async def text_resume(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_resume(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="skip", aliases=["пропустить"])
    async def text_skip(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_skip(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="stop", aliases=["стоп"])
    async def text_stop(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_stop(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="leave", aliases=["выйти"])
    async def text_leave(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_leave(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="queue", aliases=["очередь"])
    async def text_queue(self, ctx: commands.Context[MovieBot], page: int = 1) -> None:
        if ctx.guild is None:
            await self.send_context_message(ctx, "Команда доступна только на сервере.")
            return
        await self.send_context_message(ctx, embed=self.build_queue_embed(self.get_player(ctx.guild.id), page=page))

    @commands.command(name="current", aliases=["nowplaying", "трек", "сейчас_играет"])
    async def text_current(self, ctx: commands.Context[MovieBot]) -> None:
        if ctx.guild is None:
            await self.send_context_message(ctx, "Команда доступна только на сервере.")
            return
        await self.send_context_message(ctx, embed=self.build_current_embed(self.get_player(ctx.guild.id)))

    @commands.command(name="clear", aliases=["очистить"])
    async def text_clear(self, ctx: commands.Context[MovieBot]) -> None:
        text = await self.handle_clear(ctx.guild, ctx.author, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="shuffle", aliases=["перемешать"])
    async def text_shuffle(self, ctx: commands.Context[MovieBot], enabled: str | None = None) -> None:
        if enabled is None:
            if ctx.guild is None:
                await self.send_context_message(ctx, "Команда доступна только на сервере.")
                return
            value = not self.get_player(ctx.guild.id).shuffle_enabled
        else:
            lowered = enabled.lower()
            if lowered not in {"on", "off", "true", "false", "1", "0", "да", "нет", "вкл", "выкл"}:
                await self.send_context_message(ctx, "Укажи on/off или да/нет.")
                return
            value = lowered in {"on", "true", "1", "да", "вкл"}
        text = await self.handle_shuffle(ctx.guild, ctx.author, value, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.command(name="autoplay", aliases=["автоплей"])
    async def text_autoplay(self, ctx: commands.Context[MovieBot], enabled: str | None = None) -> None:
        if enabled is None:
            if ctx.guild is None:
                await self.send_context_message(ctx, "Команда доступна только на сервере.")
                return
            value = not self.get_player(ctx.guild.id).autoplay_enabled
        else:
            lowered = enabled.lower()
            if lowered not in {"on", "off", "true", "false", "1", "0", "да", "нет", "вкл", "выкл"}:
                await self.send_context_message(ctx, "Укажи on/off или да/нет.")
                return
            value = lowered in {"on", "true", "1", "да", "вкл"}
        text = await self.handle_autoplay(ctx.guild, ctx.author, value, send_error=lambda message: self.send_context_message(ctx, message))
        if text:
            await self.send_context_message(ctx, text)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        bot_user = self.bot.user
        if bot_user is None or member.id != bot_user.id:
            return
        if before.channel is not None and after.channel is None:
            guild_id = member.guild.id
            player = self.players.get(guild_id)
            if player is not None:
                async with player.lock:
                    player.voice_client = None
                    player.queue.clear()
                    player.current = None
                    player.is_paused = False
                    player.stopped = True
                logger.info("Music voice disconnected: guild=%s", guild_id)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(MusicCog(bot))

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


SPOTIFY_CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"
SPOTIFY_CLIENT_SECRET_ENV = "SPOTIFY_CLIENT_SECRET"
YANDEX_MUSIC_TOKEN_ENV = "YANDEX_MUSIC_TOKEN"


class ResolverUserError(Exception):
    """A safe, user-facing resolver error."""


class ResolverNotConfigured(ResolverUserError):
    pass


@dataclass(slots=True)
class ResolvedTrack:
    title: str
    artist: str | None
    duration: int | None
    source: str
    original_url: str
    thumbnail: str | None = None

    @property
    def search_query(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title} official audio"
        return f"{self.title} official audio"


@dataclass(slots=True)
class ResolvedMusicQuery:
    tracks: list[ResolvedTrack]
    source: str
    playlist_detected: bool = False
    skipped: int = 0
    limit: int | None = None


def _host_matches(host: str, *suffixes: str) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def is_spotify_query(query: str) -> bool:
    value = query.strip().lower()
    if value.startswith(("spotify:track:", "spotify:album:", "spotify:playlist:")):
        return True
    try:
        host = urlparse(value).netloc.lower()
    except ValueError:
        return False
    return _host_matches(host, "open.spotify.com")


def is_yandex_music_query(query: str) -> bool:
    try:
        parsed = urlparse(query.strip().lower())
    except ValueError:
        return False
    return _host_matches(parsed.netloc, "music.yandex.ru", "m.music.yandex.ru")


def is_external_music_query(query: str) -> bool:
    return is_spotify_query(query) or is_yandex_music_query(query)


def resolve_external_music_query(query: str, *, max_tracks: int) -> ResolvedMusicQuery:
    if is_spotify_query(query):
        return _resolve_spotify(query, max_tracks=max_tracks)
    if is_yandex_music_query(query):
        return _resolve_yandex_music(query, max_tracks=max_tracks)
    raise ResolverUserError("Неподдерживаемый музыкальный источник.")


def _parse_spotify(query: str) -> tuple[str, str]:
    value = query.strip()
    if value.startswith("spotify:"):
        parts = value.split(":")
        if len(parts) >= 3 and parts[1] in {"track", "album", "playlist"} and parts[2]:
            return parts[1], parts[2]
    parsed = urlparse(value)
    pieces = [piece for piece in parsed.path.split("/") if piece]
    if len(pieces) >= 2 and pieces[0] in {"track", "album", "playlist"}:
        return pieces[0], pieces[1]
    raise ResolverUserError("Не понял ссылку Spotify. Пришли track, album или playlist.")


def _spotify_client() -> Any:
    client_id = os.getenv(SPOTIFY_CLIENT_ID_ENV, "").strip()
    client_secret = os.getenv(SPOTIFY_CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        raise ResolverNotConfigured("Spotify пока не настроен. Добавьте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET.")
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
    except ImportError as exc:
        raise ResolverNotConfigured("Spotify resolver требует пакет spotipy. Установите зависимости из requirements.txt.") from exc
    auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    return spotipy.Spotify(client_credentials_manager=auth, requests_timeout=15, retries=2)


def _spotify_track_from_payload(payload: dict[str, Any], *, original_url: str) -> ResolvedTrack:
    artists = ", ".join(artist.get("name", "") for artist in payload.get("artists", []) if artist.get("name"))
    images = payload.get("album", {}).get("images", []) if isinstance(payload.get("album"), dict) else []
    thumbnail = images[0].get("url") if images and isinstance(images[0], dict) else None
    return ResolvedTrack(
        title=str(payload.get("name") or "Spotify track"),
        artist=artists or None,
        duration=int(payload["duration_ms"] / 1000) if payload.get("duration_ms") else None,
        source="spotify",
        original_url=original_url,
        thumbnail=thumbnail,
    )


def _resolve_spotify(query: str, *, max_tracks: int) -> ResolvedMusicQuery:
    kind, spotify_id = _parse_spotify(query)
    sp = _spotify_client()
    tracks: list[ResolvedTrack] = []
    skipped = 0

    if kind == "track":
        payload = sp.track(spotify_id)
        tracks.append(_spotify_track_from_payload(payload, original_url=query))
        return ResolvedMusicQuery(tracks=tracks, source="spotify")

    if kind == "album":
        album = sp.album(spotify_id)
        album_images = album.get("images", [])
        thumbnail = album_images[0].get("url") if album_images and isinstance(album_images[0], dict) else None
        for item in album.get("tracks", {}).get("items", [])[:max_tracks]:
            full_item = dict(item)
            full_item["album"] = {"images": album_images}
            track = _spotify_track_from_payload(full_item, original_url=query)
            track.thumbnail = track.thumbnail or thumbnail
            tracks.append(track)
        total = int(album.get("total_tracks") or len(tracks))
        skipped = max(0, total - len(tracks))
        return ResolvedMusicQuery(tracks=tracks, source="spotify", playlist_detected=True, skipped=skipped, limit=max_tracks)

    offset = 0
    while len(tracks) < max_tracks:
        page = sp.playlist_items(spotify_id, limit=min(100, max_tracks - len(tracks)), offset=offset)
        items = page.get("items", [])
        if not items:
            break
        for entry in items:
            payload = entry.get("track")
            if not isinstance(payload, dict) or payload.get("is_local"):
                skipped += 1
                continue
            tracks.append(_spotify_track_from_payload(payload, original_url=query))
            if len(tracks) >= max_tracks:
                break
        if not page.get("next") or len(tracks) >= max_tracks:
            total = int(page.get("total") or len(tracks))
            skipped += max(0, total - offset - len(items))
            break
        offset += len(items)
    return ResolvedMusicQuery(tracks=tracks, source="spotify", playlist_detected=True, skipped=skipped, limit=max_tracks)


YANDEX_TRACK_RE = re.compile(r"/album/(?P<album_id>\d+)/track/(?P<track_id>\d+)")
YANDEX_ALBUM_RE = re.compile(r"/album/(?P<album_id>\d+)(?:/)?$")
YANDEX_USER_PLAYLIST_RE = re.compile(r"/users/(?P<user>[^/]+)/playlists/(?P<playlist_id>\d+)")
YANDEX_PLAYLIST_RE = re.compile(r"/playlist/(?P<playlist_id>\d+)")


def _yandex_client() -> Any:
    token = os.getenv(YANDEX_MUSIC_TOKEN_ENV, "").strip()
    if not token:
        raise ResolverNotConfigured("Яндекс Музыка пока не настроена. Добавьте YANDEX_MUSIC_TOKEN.")
    try:
        from yandex_music import Client
    except ImportError as exc:
        raise ResolverNotConfigured("Yandex Music resolver требует пакет yandex-music. Установите зависимости из requirements.txt.") from exc
    return Client(token).init()


def _yandex_track_to_resolved(track: Any, *, original_url: str) -> ResolvedTrack:
    title = str(getattr(track, "title", None) or "Yandex Music track")
    artists = getattr(track, "artists", None) or []
    artist_names = ", ".join(str(getattr(artist, "name", "")) for artist in artists if getattr(artist, "name", ""))
    duration_ms = getattr(track, "duration_ms", None)
    thumbnail = None
    try:
        thumbnail = track.get_cover_url("200x200")
    except Exception:
        thumbnail = None
    return ResolvedTrack(
        title=title,
        artist=artist_names or None,
        duration=int(duration_ms / 1000) if duration_ms else None,
        source="yandex",
        original_url=original_url,
        thumbnail=thumbnail,
    )


def _resolve_yandex_music(query: str, *, max_tracks: int) -> ResolvedMusicQuery:
    parsed = urlparse(query.strip())
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        raise ResolverUserError("Это главная страница Яндекс Музыки. Пришли ссылку на конкретный трек, альбом или плейлист.")

    client = _yandex_client()
    track_match = YANDEX_TRACK_RE.search(path)
    if track_match:
        track_id = track_match.group("track_id")
        tracks = client.tracks([track_id])
        if not tracks:
            return ResolvedMusicQuery(tracks=[], source="yandex", skipped=1)
        return ResolvedMusicQuery(tracks=[_yandex_track_to_resolved(tracks[0], original_url=query)], source="yandex")

    album_match = YANDEX_ALBUM_RE.search(path)
    if album_match:
        album = client.albums_with_tracks(album_match.group("album_id"))
        resolved: list[ResolvedTrack] = []
        for volume in getattr(album, "volumes", []) or []:
            for track in volume:
                if len(resolved) >= max_tracks:
                    break
                resolved.append(_yandex_track_to_resolved(track, original_url=query))
            if len(resolved) >= max_tracks:
                break
        total = sum(len(volume) for volume in (getattr(album, "volumes", []) or []))
        return ResolvedMusicQuery(tracks=resolved, source="yandex", playlist_detected=True, skipped=max(0, total - len(resolved)), limit=max_tracks)

    playlist_match = YANDEX_USER_PLAYLIST_RE.search(path)
    if playlist_match:
        playlist = client.users_playlists(playlist_match.group("playlist_id"), playlist_match.group("user"))
    else:
        playlist_match = YANDEX_PLAYLIST_RE.search(path)
        if not playlist_match:
            raise ResolverUserError("Яндекс Музыка пока поддерживает только ссылки на трек, альбом или плейлист.")
        playlist = client.users_playlists(playlist_match.group("playlist_id"))

    resolved = []
    tracks = getattr(playlist, "tracks", []) or []
    for item in tracks[:max_tracks]:
        track = getattr(item, "track", item)
        resolved.append(_yandex_track_to_resolved(track, original_url=query))
    skipped = max(0, len(tracks) - len(resolved))
    return ResolvedMusicQuery(tracks=resolved, source="yandex", playlist_detected=True, skipped=skipped, limit=max_tracks)

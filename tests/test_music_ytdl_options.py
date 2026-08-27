from __future__ import annotations

import shlex
from types import SimpleNamespace

from cogs import music
from cogs.music import (
    _describe_cookie_file,
    _ffmpeg_before_options,
    _is_ytdl_cookie_error,
    _resolve_cookie_file,
    _track_from_info,
    _youtube_radio_url_as_single_track,
    _ytdl_cookie_state,
    _ytdl_options,
)


def test_ytdl_options_uses_configured_cookie_file(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(cookie_file))

    options = _ytdl_options()

    assert options["cookiefile"] == str(cookie_file)
    assert _ytdl_cookie_state(options) == "enabled"


def test_ytdl_options_resolves_cookie_file_from_project_root(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "app"
    cookie_file = project_root / "cookies" / "youtube-cookies.txt"
    cookie_file.parent.mkdir(parents=True)
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    other_cwd = tmp_path / "not-project-root"
    other_cwd.mkdir()

    monkeypatch.setattr(music, "PROJECT_ROOT", project_root)
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("YTDLP_COOKIE_FILE", "cookies/youtube-cookies.txt")

    assert _resolve_cookie_file("cookies/youtube-cookies.txt") == str(cookie_file)
    assert _ytdl_options()["cookiefile"] == str(cookie_file)


def test_ytdl_options_ignores_missing_cookie_file(monkeypatch) -> None:
    monkeypatch.setenv("YTDLP_COOKIE_FILE", "/missing/youtube-cookies.txt")

    options = _ytdl_options()

    assert "cookiefile" not in options
    assert _ytdl_cookie_state(options) == "disabled"


def test_describe_cookie_file_does_not_return_full_path() -> None:
    assert _describe_cookie_file("/app/secrets/youtube-cookies.txt") == "youtube-cookies.txt"


def test_cookie_load_error_detection_uses_yt_dlp_error_shape() -> None:
    error = RuntimeError("failed to load cookies")

    assert _is_ytdl_cookie_error(error)


def test_youtube_radio_url_is_forced_to_single_track() -> None:
    url = "https://www.youtube.com/watch?v=XALLZHKnS_U&list=RDXALLZHKnS_U&start_radio=1"

    normalized, forced_single = _youtube_radio_url_as_single_track(url)

    assert forced_single is True
    assert normalized == "https://www.youtube.com/watch?v=XALLZHKnS_U"


def test_regular_youtube_playlist_is_not_forced_to_single_track() -> None:
    url = "https://www.youtube.com/watch?v=abc123&list=PL1234567890"

    normalized, forced_single = _youtube_radio_url_as_single_track(url)

    assert forced_single is False
    assert normalized == url


def test_track_preserves_ytdlp_http_headers_for_ffmpeg() -> None:
    track = _track_from_info(
        {
            "title": "Track",
            "webpage_url": "https://www.youtube.com/watch?v=test",
            "url": "https://media.example.invalid/audio.webm",
            "http_headers": {
                "User-Agent": "yt-dlp-agent",
                "Accept-Language": "ru-RU",
            },
        },
        SimpleNamespace(id=7, display_name="Requester"),
    )

    assert track is not None
    assert track.http_headers == {
        "User-Agent": "yt-dlp-agent",
        "Accept-Language": "ru-RU",
    }


def test_ffmpeg_options_forward_sanitized_ytdlp_headers() -> None:
    options = _ffmpeg_before_options(
        {
            "User-Agent": "yt-dlp agent",
            "Referer": "https://www.youtube.com/\r\n-injected 1",
            "Bad Header": "ignored",
        }
    )
    args = shlex.split(options)

    header_block = args[args.index("-headers") + 1]
    assert "User-Agent: yt-dlp agent\r\n" in header_block
    assert "Referer: https://www.youtube.com/ -injected 1\r\n" in header_block
    assert "Bad Header" not in header_block


def test_music_rejects_discord_voice_stack_without_dave_support(monkeypatch) -> None:
    cog = object.__new__(music.MusicCog)
    cog._ffmpeg_executable = "ffmpeg"
    monkeypatch.setattr(
        music.discord,
        "version_info",
        SimpleNamespace(major=2, minor=6, micro=4),
    )

    error = cog.dependency_error()

    assert error is not None
    assert "discord.py 2.7.1+" in error

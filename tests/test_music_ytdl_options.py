from __future__ import annotations

from cogs import music
from cogs.music import (
    _describe_cookie_file,
    _is_ytdl_cookie_error,
    _resolve_cookie_file,
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

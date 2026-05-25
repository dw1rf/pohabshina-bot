from __future__ import annotations

from cogs.music import _describe_cookie_file, _ytdl_cookie_state, _ytdl_options


def test_ytdl_options_uses_configured_cookie_file(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(cookie_file))

    options = _ytdl_options()

    assert options["cookiefile"] == str(cookie_file)
    assert _ytdl_cookie_state(options) == "enabled"


def test_ytdl_options_ignores_missing_cookie_file(monkeypatch) -> None:
    monkeypatch.setenv("YTDLP_COOKIE_FILE", "/missing/youtube-cookies.txt")

    options = _ytdl_options()

    assert "cookiefile" not in options
    assert _ytdl_cookie_state(options) == "disabled"


def test_describe_cookie_file_does_not_return_full_path() -> None:
    assert _describe_cookie_file("/app/secrets/youtube-cookies.txt") == "youtube-cookies.txt"

from __future__ import annotations

from cogs.music import _ytdl_options


def test_ytdl_options_uses_configured_cookie_file(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "youtube-cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(cookie_file))

    assert _ytdl_options()["cookiefile"] == str(cookie_file)


def test_ytdl_options_ignores_missing_cookie_file(monkeypatch) -> None:
    monkeypatch.setenv("YTDLP_COOKIE_FILE", "/missing/youtube-cookies.txt")

    assert "cookiefile" not in _ytdl_options()

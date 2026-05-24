from __future__ import annotations

from utils import voice_runtime


def test_find_ffmpeg_uses_configured_executable(monkeypatch) -> None:
    monkeypatch.setenv("FFMPEG_EXECUTABLE", "/custom/bin/ffmpeg")
    monkeypatch.setattr(voice_runtime, "_is_executable_file", lambda path: path == "/custom/bin/ffmpeg")
    monkeypatch.setattr(voice_runtime.shutil, "which", lambda name: None)

    assert voice_runtime.find_ffmpeg() == "/custom/bin/ffmpeg"


def test_find_ffmpeg_falls_back_to_standard_linux_path(monkeypatch) -> None:
    monkeypatch.delenv("FFMPEG_EXECUTABLE", raising=False)
    monkeypatch.setattr(voice_runtime, "FFMPEG_CANDIDATES", ("/usr/bin/ffmpeg",))
    monkeypatch.setattr(voice_runtime, "_is_executable_file", lambda path: path == "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_runtime.shutil, "which", lambda name: None)

    assert voice_runtime.find_ffmpeg() == "/usr/bin/ffmpeg"


def test_find_binary_uses_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(voice_runtime.shutil, "which", lambda name: f"/bin/{name}" if name == "deno" else None)

    assert voice_runtime.find_binary("deno") == "/bin/deno"

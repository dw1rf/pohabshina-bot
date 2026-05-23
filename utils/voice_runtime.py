from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys

FFMPEG_MISSING_MESSAGE = "FFmpeg не найден в контейнере. Установите системный ffmpeg в Dockerfile/egg."


def find_binary(name: str) -> str | None:
    return shutil.which(name)


def find_ffmpeg() -> str | None:
    return find_binary("ffmpeg")


def require_ffmpeg() -> str:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        raise RuntimeError(FFMPEG_MISSING_MESSAGE)
    return ffmpeg_path


def first_version_line(executable: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return f"version check failed: {exc}"

    lines = (completed.stdout or completed.stderr or "").splitlines()
    if not lines:
        return f"version check returned code {completed.returncode} without output"
    return lines[0]


def log_binary_version(logger: logging.Logger, name: str, executable: str, *args: str) -> None:
    logger.info("%s found: path=%s version=%s", name, executable, first_version_line(executable, *args))


def log_voice_runtime(logger: logging.Logger) -> None:
    logger.info("Voice runtime: python=%s platform=%s", sys.version.split()[0], platform.platform())
    logger.info("Voice runtime: PATH=%s", os.environ.get("PATH", ""))

    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        log_binary_version(logger, "ffmpeg", ffmpeg_path, "-version")
    else:
        logger.error(
            "%s PATH=%s. Rebuild the Docker image/egg with apt package ffmpeg.",
            FFMPEG_MISSING_MESSAGE,
            os.environ.get("PATH", ""),
        )

    ffprobe_path = find_binary("ffprobe")
    if ffprobe_path:
        log_binary_version(logger, "ffprobe", ffprobe_path, "-version")
    else:
        logger.warning("ffprobe not found in PATH. The Debian apt package ffmpeg normally installs it together with ffmpeg.")

    deno_path = find_binary("deno")
    if deno_path:
        log_binary_version(logger, "deno", deno_path, "--version")
    else:
        logger.warning("deno not found in PATH. yt-dlp may warn about missing JavaScript runtime for YouTube.")

    try:
        import nacl  # noqa: F401
    except ImportError:
        logger.error("PyNaCl is not installed; Discord voice support is unavailable.")
    else:
        logger.info("PyNaCl is installed; Discord voice support can load.")

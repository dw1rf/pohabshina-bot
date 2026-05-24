from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

FFMPEG_MISSING_MESSAGE = "FFmpeg не найден в контейнере. Установите системный ffmpeg в Dockerfile/egg."

FFMPEG_EXECUTABLE_ENV = "FFMPEG_EXECUTABLE"
FFPROBE_EXECUTABLE_ENV = "FFPROBE_EXECUTABLE"
FFMPEG_CANDIDATES = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg")
FFPROBE_CANDIDATES = ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/bin/ffprobe")


def _is_executable_file(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.X_OK)


def find_binary(name: str, *, env_var: str | None = None, candidates: tuple[str, ...] = ()) -> str | None:
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            if _is_executable_file(configured):
                return configured
            configured_path = shutil.which(configured)
            if configured_path:
                return configured_path

    found = shutil.which(name)
    if found:
        return found

    for candidate in candidates:
        if _is_executable_file(candidate):
            return candidate
    return None


def find_ffmpeg() -> str | None:
    return find_binary("ffmpeg", env_var=FFMPEG_EXECUTABLE_ENV, candidates=FFMPEG_CANDIDATES)


def find_ffprobe() -> str | None:
    return find_binary("ffprobe", env_var=FFPROBE_EXECUTABLE_ENV, candidates=FFPROBE_CANDIDATES)


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

    ffprobe_path = find_ffprobe()
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

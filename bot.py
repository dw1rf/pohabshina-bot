from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot_client import MovieBot
from config import load_settings


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _scan_for_merge_artifacts() -> list[str]:
    repo_root = Path(__file__).resolve().parent
    suspicious_tokens = ("<<<<<<<", "=======", ">>>>>>>", "codex/")
    paths_to_scan = (
        repo_root / "bot.py",
        repo_root / "bot_client.py",
        * (repo_root / "cogs").glob("*.py"),
        * (repo_root / "services").glob("*.py"),
        * (repo_root / "utils").glob("*.py"),
    )

    invalid_files: list[str] = []
    for path in paths_to_scan:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(token in content for token in suspicious_tokens):
            invalid_files.append(str(path.relative_to(repo_root)))
    return invalid_files


async def main() -> None:
    configure_logging()
    settings = load_settings()
    invalid_files = _scan_for_merge_artifacts()

    if invalid_files:
        files = ", ".join(invalid_files)
        raise RuntimeError(
            "Обнаружены следы merge-конфликта в файлах: "
            f"{files}. Удалите конфликтные маркеры и перезапустите бота."
        )

    if not settings.discord_token:
        raise RuntimeError("Не задан DISCORD_TOKEN")
    if not settings.watchmode_api_key:
        raise RuntimeError("Не задан WATCHMODE_API_KEY")

    bot = MovieBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())

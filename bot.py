from __future__ import annotations

import asyncio
import ast
import logging
from pathlib import Path

from config import load_settings

MERGE_TOKENS: tuple[str, ...] = ("<" * 7, "=" * 7, ">" * 7, "codex" + "/refactor")


def _project_python_files() -> list[Path]:
    root = Path(__file__).resolve().parent
    return [
        root / "bot.py",
        root / "bot_client.py",
        *list((root / "cogs").glob("*.py")),
        *list((root / "services").glob("*.py")),
        *list((root / "utils").glob("*.py")),
    ]


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _find_merge_artifacts() -> list[str]:
    root = Path(__file__).resolve().parent
    broken_files: list[str] = []

    for file_path in _project_python_files():
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(token in content for token in MERGE_TOKENS):
            broken_files.append(str(file_path.relative_to(root)))

    return broken_files


def _find_syntax_errors() -> list[str]:
    root = Path(__file__).resolve().parent
    broken_files: list[str] = []

    for file_path in _project_python_files():
        try:
            content = file_path.read_text(encoding="utf-8")
            ast.parse(content, filename=str(file_path))
        except OSError:
            continue
        except (SyntaxError, IndentationError) as exc:
            relative = str(file_path.relative_to(root))
            line_part = f":{exc.lineno}" if exc.lineno else ""
            broken_files.append(f"{relative}{line_part} ({exc.msg})")

    return broken_files


async def main() -> None:
    configure_logging()
    settings = load_settings()

    broken_files = _find_merge_artifacts()
    if broken_files:
        files = ", ".join(broken_files)
        raise RuntimeError(
            "Обнаружены следы merge-конфликта в файлах: "
            f"{files}. Удалите конфликтные маркеры и перезапустите бота."
        )

    syntax_errors = _find_syntax_errors()
    if syntax_errors:
        details = "; ".join(syntax_errors)
        logging.getLogger(__name__).warning(
            "Обнаружены синтаксические ошибки в Python-файлах проекта: %s. "
            "Бот попробует продолжить запуск, но часть cogs может не загрузиться.",
            details,
        )

    if not settings.discord_token:
        raise RuntimeError("Не задан DISCORD_TOKEN")
    if not settings.watchmode_api_key:
        raise RuntimeError("Не задан WATCHMODE_API_KEY")

    from bot_client import MovieBot

    bot = MovieBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())

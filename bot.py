from __future__ import annotations

import asyncio
import logging

from bot_client import MovieBot
from config import load_settings


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def validate_required_settings(discord_token: str, watchmode_api_key: str) -> None:
    if not discord_token:
        raise RuntimeError("Не задан DISCORD_TOKEN")
    if not watchmode_api_key:
        raise RuntimeError("Не задан WATCHMODE_API_KEY")


async def main() -> None:
    configure_logging()
    settings = load_settings()
    validate_required_settings(settings.discord_token, settings.watchmode_api_key)

    bot = MovieBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())

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


async def main() -> None:
    configure_logging()
    settings = load_settings()

    if not settings.discord_token:
        raise RuntimeError("Не задан DISCORD_TOKEN")
    if not settings.watchmode_api_key:
        raise RuntimeError("Не задан WATCHMODE_API_KEY")

    bot = MovieBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
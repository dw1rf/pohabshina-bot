from __future__ import annotations

import hashlib
import logging

import aiosqlite
import discord

from utils.brand import PROJECT_ROOT

LAUNCHER_NAME = "BlockEra Launcher"
LAUNCHER_DOMAIN = "blockera.space"
LAUNCHER_URL = f"https://{LAUNCHER_DOMAIN}"
LAUNCHER_ACTIVITY = f"{LAUNCHER_NAME} · {LAUNCHER_DOMAIN}"
PROFILE_DIR = PROJECT_ROOT / "assets" / "profile"

logger = logging.getLogger(__name__)


async def sync_profile(db: aiosqlite.Connection, user: discord.ClientUser, app: discord.AppInfo) -> None:
    """Apply packaged artwork once per revision and keep the existing app description."""
    avatar = (PROFILE_DIR / "avatar.png").read_bytes()
    banner = (PROFILE_DIR / "banner.png").read_bytes()
    revision = hashlib.sha256(avatar + banner).hexdigest()
    await db.execute(
        "CREATE TABLE IF NOT EXISTS bot_profile_art (bot_id INTEGER PRIMARY KEY, revision TEXT NOT NULL)"
    )
    async with db.execute("SELECT revision FROM bot_profile_art WHERE bot_id = ?", (user.id,)) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] != revision:
        await user.edit(avatar=avatar, banner=banner)
        await db.execute("INSERT OR REPLACE INTO bot_profile_art VALUES (?, ?)", (user.id, revision))
        await db.commit()

    if LAUNCHER_URL not in app.description:
        description = (app.description.rstrip() + "\n" + LAUNCHER_NAME + " · " + LAUNCHER_URL).lstrip("\n")
        if len(description) <= 400:
            await app.edit(description=description)
        else:
            logger.warning("App description is full. Launcher link is available in /helpme.")

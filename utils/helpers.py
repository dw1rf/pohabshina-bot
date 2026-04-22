from __future__ import annotations

import io
import math
import re
from datetime import UTC, datetime, timedelta

import discord

DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("/", " ").split())


def truncate_text(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def format_dt(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return ts


def parse_duration(duration_text: str) -> timedelta | None:
    match = DURATION_RE.fullmatch(duration_text.strip().lower())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = value * unit_seconds[unit]
    return timedelta(seconds=seconds) if seconds > 0 else None


def required_messages_for_level(level: int) -> int:
    return 0 if level <= 0 else 10 * level * level


def calculate_level(message_count: int, max_level: int) -> int:
    if message_count <= 0:
        return 0
    raw_level = int(math.sqrt(message_count / 10))
    return max(0, min(max_level, raw_level))


async def attachment_to_file(attachment: discord.Attachment) -> discord.File:
    data = await attachment.read(use_cached=True)
    return discord.File(io.BytesIO(data), filename=attachment.filename, spoiler=attachment.is_spoiler())

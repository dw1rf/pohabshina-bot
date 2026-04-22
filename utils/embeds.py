from __future__ import annotations

import discord


def parse_color(color: str | None) -> discord.Color:
    if not color:
        return discord.Color.blurple()
    raw = color.strip().lstrip("#")
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return discord.Color.blurple()

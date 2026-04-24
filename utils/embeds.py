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
 codex/refactor-discord-bot-structure-and-add-features-h7krw8


def parse_color_strict(color: str | None) -> discord.Color | None:
    if not color:
        return None
    raw = color.strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return None

 main

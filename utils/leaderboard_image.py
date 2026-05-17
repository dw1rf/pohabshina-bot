from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

import discord
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<[@#][!&]?\d+>|<@&\d+>")
MARKDOWN_CHARS = set("*`~|<>@")
DEFAULT_NAME = "Без имени"


@dataclass(slots=True, frozen=True)
class LeaderboardImageRow:
    name: str
    value: int | float
    primary: str = ""
    secondary: str = ""


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _is_emoji_codepoint(codepoint: int) -> bool:
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
        or codepoint == 0x200D
    )


def safe_text(text: str, max_len: int = 32) -> str:
    cleaned = MENTION_RE.sub(" ", str(text or ""))
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")

    chars: list[str] = []
    for char in cleaned:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs", "So"}:
            continue
        if _is_emoji_codepoint(ord(char)):
            continue
        if char == "\ufffd" or char in MARKDOWN_CHARS:
            continue
        chars.append(char)

    result = " ".join("".join(chars).split())
    if not result:
        return DEFAULT_NAME

    if max_len > 3 and len(result) > max_len:
        result = result[: max_len - 3].rstrip() + "..."
    elif max_len > 0 and len(result) > max_len:
        result = result[:max_len].rstrip()
    return result or DEFAULT_NAME


def sanitize_leaderboard_name(name: str, max_len: int = 32) -> str:
    return safe_text(name, max_len=max_len)


async def resolve_display_name(
    bot: discord.Client,
    guild: discord.Guild | None,
    user_id: int,
    *,
    max_len: int = 32,
) -> str:
    member = guild.get_member(user_id) if guild is not None else None
    if member is not None:
        return sanitize_leaderboard_name(member.display_name, max_len=max_len)

    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.debug("Could not fetch leaderboard user %s", user_id, exc_info=True)
            user = None

    if user is not None:
        return sanitize_leaderboard_name(user.display_name, max_len=max_len)
    return f"Пользователь {str(user_id)[-4:]}"


def _text_length(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except UnicodeEncodeError:
        return float(draw.textlength(text.encode("ascii", "ignore").decode("ascii"), font=font))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if _text_length(draw, text, font) <= max_width:
        return text

    suffix = "..."
    while text and _text_length(draw, text + suffix, font) > max_width:
        text = text[:-1].rstrip()
    return text + suffix if text else suffix


def _draw_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    *,
    fill: tuple[int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    try:
        draw.text(position, text, fill=fill, font=font)
    except UnicodeEncodeError:
        fallback = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii") or "No name"
        draw.text(position, fallback, fill=fill, font=font)


def draw_leaderboard_image(
    title: str,
    rows: Sequence[LeaderboardImageRow],
    *,
    width: int = 1000,
) -> io.BytesIO:
    safe_rows = list(rows)
    row_height = 124
    row_gap = 14
    top_padding = 128
    bottom_padding = 42
    height = top_padding + max(len(safe_rows), 1) * (row_height + row_gap) + bottom_padding - row_gap

    image = Image.new("RGB", (width, height), (18, 20, 28))
    draw = ImageDraw.Draw(image)

    title_font = load_font(40, bold=True)
    name_font = load_font(27, bold=True)
    primary_font = load_font(20)
    secondary_font = load_font(18)
    small_font = load_font(16, bold=True)

    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=28, fill=(27, 30, 42), outline=(52, 57, 78), width=2)
    draw.rounded_rectangle((48, 50, 128, 58), radius=4, fill=(118, 94, 255))
    _draw_text(draw, (48, 70), safe_text(title, max_len=54).upper(), fill=(245, 247, 255), font=title_font)

    values = [max(float(row.value), 0.0) for row in safe_rows]
    max_value = max(values, default=1.0) or 1.0
    place_colors = {
        1: (255, 210, 92),
        2: (200, 210, 226),
        3: (221, 151, 94),
    }

    y = top_padding
    for index, row in enumerate(safe_rows, start=1):
        card_color = (36, 40, 56) if index % 2 else (32, 36, 51)
        border_color = place_colors.get(index, (58, 64, 86))
        draw.rounded_rectangle((48, y, width - 48, y + row_height), radius=22, fill=card_color, outline=border_color, width=2)

        rank_color = place_colors.get(index, (120, 132, 166))
        draw.ellipse((72, y + 30, 122, y + 80), fill=rank_color)
        rank_text = f"#{index}"
        rank_x = 97 - int(_text_length(draw, rank_text, small_font) // 2)
        _draw_text(draw, (rank_x, y + 46), rank_text, fill=(18, 20, 28), font=small_font)

        name = _fit_text(draw, sanitize_leaderboard_name(row.name, max_len=46), name_font, width - 250)
        primary = _fit_text(draw, safe_text(row.primary, max_len=74), primary_font, width - 250) if row.primary else ""
        secondary = _fit_text(draw, safe_text(row.secondary, max_len=78), secondary_font, width - 250) if row.secondary else ""

        _draw_text(draw, (150, y + 24), name, fill=(246, 247, 252), font=name_font)
        _draw_text(draw, (150, y + 62), primary, fill=(203, 210, 228), font=primary_font)
        if secondary:
            _draw_text(draw, (150, y + 88), secondary, fill=(162, 171, 198), font=secondary_font)

        value = max(float(row.value), 0.0)
        percent = min(max(value / max_value, 0.0), 1.0)
        bar_x, bar_y, bar_w, bar_h = 660, y + 88, 220, 12
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=6, fill=(61, 67, 91))
        filled = int(bar_w * percent)
        if filled > 0:
            draw.rounded_rectangle((bar_x, bar_y, bar_x + filled, bar_y + bar_h), radius=6, fill=(118, 94, 255))
        percent_text = f"{int(percent * 100)}%"
        _draw_text(draw, (895, y + 82), percent_text, fill=(185, 193, 216), font=small_font)

        y += row_height + row_gap

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def make_leaderboard_file(
    title: str,
    rows: Sequence[LeaderboardImageRow],
    *,
    filename: str = "leaderboard.png",
) -> discord.File:
    image = draw_leaderboard_image(title, rows)
    return discord.File(image, filename=filename)

from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import discord
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<[@#][!&]?\d+>|<@&\d+>")
DEFAULT_NAME = "Без имени"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
WINDOWS_FONT_DIR = Path("C:/Windows/Fonts")
COLOR_EMOJI_SIZE = 109
ELLIPSIS = "..."


@dataclass(slots=True, frozen=True)
class LeaderboardImageRow:
    name: str
    value: int | float
    primary: str = ""
    secondary: str = ""


@dataclass(slots=True, frozen=True)
class LoadedFont:
    font: ImageFont.ImageFont
    label: str
    path: Path | None = None
    is_color_emoji: bool = False
    emoji_scale: float = 1.0


class FontStack:
    def __init__(self, fonts: Sequence[LoadedFont]) -> None:
        self.fonts = list(fonts) or [LoadedFont(ImageFont.load_default(), "default")]

    @property
    def primary(self) -> ImageFont.ImageFont:
        return self.fonts[0].font

    def supports_text(self, text: str) -> bool:
        return all(self._font_for_cluster(cluster) is not None for cluster in _text_clusters(text))

    def text_length(self, text: str) -> float:
        total = 0.0
        for loaded_font, run in self._runs(text):
            total += self._run_length(loaded_font, run)
        return total

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        position: tuple[int, int],
        text: str,
        *,
        fill: tuple[int, int, int] | tuple[int, int, int, int],
    ) -> None:
        x, y = position
        for loaded_font, run in self._runs(text):
            if loaded_font.is_color_emoji:
                x += self._draw_color_emoji(draw, (x, y), run, loaded_font)
            else:
                try:
                    draw.text((x, y), run, fill=fill, font=loaded_font.font, embedded_color=True)
                except TypeError:
                    draw.text((x, y), run, fill=fill, font=loaded_font.font)
                x += self._run_length(loaded_font, run)

    def _runs(self, text: str) -> list[tuple[LoadedFont, str]]:
        runs: list[tuple[LoadedFont, str]] = []
        for cluster in _text_clusters(text):
            loaded_font = self._font_for_cluster(cluster) or self.fonts[0]
            if runs and runs[-1][0] == loaded_font:
                runs[-1] = (loaded_font, runs[-1][1] + cluster)
            else:
                runs.append((loaded_font, cluster))
        return runs

    def _font_for_cluster(self, cluster: str) -> LoadedFont | None:
        for loaded_font in self.fonts:
            if _font_supports_cluster(loaded_font, cluster):
                return loaded_font
        return None

    @staticmethod
    def _run_length(loaded_font: LoadedFont, text: str) -> float:
        if not text:
            return 0.0
        if loaded_font.is_color_emoji:
            return sum(_color_emoji_width(loaded_font, cluster) for cluster in _text_clusters(text))
        try:
            return float(loaded_font.font.getlength(text))
        except (AttributeError, UnicodeEncodeError):
            return float(loaded_font.font.getbbox(text)[2])

    @staticmethod
    def _draw_color_emoji(
        draw: ImageDraw.ImageDraw,
        position: tuple[float, int],
        text: str,
        loaded_font: LoadedFont,
    ) -> float:
        x, y = position
        total_width = 0.0
        for cluster in _text_clusters(text):
            total_width += _draw_color_emoji_cluster(draw, (x + total_width, y), cluster, loaded_font)
        return total_width


def _font_candidates(kind: str, bold: bool) -> list[Path]:
    noto_regular = LOCAL_FONT_DIR / "NotoSans-Regular.ttf"
    noto_bold = LOCAL_FONT_DIR / "NotoSans-Bold.ttf"
    noto_ui = noto_bold if bold else noto_regular

    if kind == "name":
        return [
            LOCAL_FONT_DIR / "NotoSansSymbols2-Regular.ttf",
            LOCAL_FONT_DIR / "NotoSansMath-Regular.ttf",
            noto_ui,
            LOCAL_FONT_DIR / "NotoSansCJKjp-Regular.otf",
            WINDOWS_FONT_DIR / "seguiemj.ttf",
            WINDOWS_FONT_DIR / "seguisym.ttf",
            LOCAL_FONT_DIR / "NotoColorEmoji.ttf",
            WINDOWS_FONT_DIR / "arialuni.ttf",
            WINDOWS_FONT_DIR / ("arialbd.ttf" if bold else "arial.ttf"),
            WINDOWS_FONT_DIR / "cambria.ttc",
            Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]

    return [
        noto_ui,
        LOCAL_FONT_DIR / "NotoSansMath-Regular.ttf",
        WINDOWS_FONT_DIR / ("arialbd.ttf" if bold else "arial.ttf"),
        WINDOWS_FONT_DIR / "seguisym.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]


@lru_cache(maxsize=96)
def load_font_stack(size: int, bold: bool = False, kind: str = "ui") -> FontStack:
    fonts: list[LoadedFont] = []
    seen: set[str] = set()
    for path in _font_candidates(kind, bold):
        normalized = str(path).lower()
        if normalized in seen or not path.exists():
            continue
        seen.add(normalized)
        is_color_emoji = path.name.lower() == "notocoloremoji.ttf"
        try:
            if is_color_emoji:
                font = ImageFont.truetype(str(path), COLOR_EMOJI_SIZE)
                emoji_scale = size / COLOR_EMOJI_SIZE
            else:
                font = ImageFont.truetype(str(path), size=size)
                emoji_scale = 1.0
        except OSError:
            continue
        fonts.append(
            LoadedFont(
                font=font,
                label=path.stem,
                path=path,
                is_color_emoji=is_color_emoji,
                emoji_scale=emoji_scale,
            )
        )
    if not fonts:
        logger.warning("No TrueType fonts were available for leaderboard rendering")
    return FontStack(fonts)


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    return load_font_stack(size, bold=bold).primary


def _is_emoji_codepoint(codepoint: int) -> bool:
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
        or codepoint == 0x200D
    )


def _is_joining_codepoint(codepoint: int) -> bool:
    return (
        codepoint == 0x200D
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or unicodedata.category(chr(codepoint)).startswith("M")
    )


def _text_clusters(text: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    force_join_next = False
    for char in text:
        codepoint = ord(char)
        if not current:
            current = char
        elif force_join_next or _is_joining_codepoint(codepoint):
            current += char
            force_join_next = False
        else:
            clusters.append(current)
            current = char

        if codepoint == 0x200D:
            force_join_next = True

    if current:
        clusters.append(current)
    return clusters


def _clean_single_line_text(text: str, *, default: str, max_len: int = 0) -> str:
    cleaned = MENTION_RE.sub(" ", str(text or ""))
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(char for char in cleaned if unicodedata.category(char) not in {"Cc", "Cs"} and char != "\ufffd")
    result = " ".join(cleaned.split())
    if not result:
        return default
    if max_len > 0:
        result = _truncate_clusters(result, max_len)
    return result or default


def _truncate_clusters(text: str, max_len: int) -> str:
    clusters = _text_clusters(text)
    if len(clusters) <= max_len:
        return text
    if max_len <= len(ELLIPSIS):
        return "".join(clusters[:max_len]).rstrip()
    return "".join(clusters[: max_len - len(ELLIPSIS)]).rstrip() + ELLIPSIS


def safe_text(text: str, max_len: int = 32) -> str:
    return _clean_single_line_text(text, default=DEFAULT_NAME, max_len=max_len)


def sanitize_leaderboard_name(name: str, max_len: int = 0) -> str:
    return _clean_single_line_text(name, default=DEFAULT_NAME)


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


@lru_cache(maxsize=2048)
def _missing_signature(font_id: int, font: ImageFont.ImageFont) -> tuple[tuple[int, int], tuple[int, int, int, int] | None, bytes]:
    return _mask_signature(font, "\U0010FFFF")


def _mask_signature(font: ImageFont.ImageFont, text: str) -> tuple[tuple[int, int], tuple[int, int, int, int] | None, bytes]:
    try:
        mask = font.getmask(text, mode="L")
    except (OSError, UnicodeEncodeError, ValueError):
        return (0, 0), None, b""
    return mask.size, mask.getbbox(), bytes(mask)


def _font_supports_cluster(loaded_font: LoadedFont, cluster: str) -> bool:
    if not cluster:
        return True
    if loaded_font.is_color_emoji:
        return any(_is_emoji_codepoint(ord(char)) for char in cluster) and all(
            _is_emoji_codepoint(ord(char)) or _is_joining_codepoint(ord(char)) for char in cluster
        )
    return all(_font_supports_char(loaded_font.font, char) for char in cluster)


def _font_supports_char(font: ImageFont.ImageFont, char: str) -> bool:
    if char.isspace() or _is_joining_codepoint(ord(char)):
        return True
    signature = _mask_signature(font, char)
    if signature[2] == b"" and signature[1] is None:
        return False
    return signature != _missing_signature(id(font), font)


def _color_emoji_width(loaded_font: LoadedFont, cluster: str) -> float:
    try:
        return float(loaded_font.font.getlength(cluster)) * loaded_font.emoji_scale
    except (AttributeError, UnicodeEncodeError):
        bbox = loaded_font.font.getbbox(cluster)
        return float(bbox[2] - bbox[0]) * loaded_font.emoji_scale


def _draw_color_emoji_cluster(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, int],
    cluster: str,
    loaded_font: LoadedFont,
) -> float:
    width = max(1, int(_color_emoji_width(loaded_font, cluster) / loaded_font.emoji_scale) + 8)
    height = COLOR_EMOJI_SIZE + 24
    emoji_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    emoji_draw = ImageDraw.Draw(emoji_image)
    try:
        emoji_draw.text((4, 4), cluster, font=loaded_font.font, embedded_color=True)
    except TypeError:
        emoji_draw.text((4, 4), cluster, font=loaded_font.font)

    bbox = emoji_image.getbbox()
    if bbox is None:
        return _color_emoji_width(loaded_font, cluster)

    emoji_image = emoji_image.crop(bbox)
    target_width = max(1, int(emoji_image.width * loaded_font.emoji_scale))
    target_height = max(1, int(emoji_image.height * loaded_font.emoji_scale))
    emoji_image = emoji_image.resize((target_width, target_height), Image.Resampling.LANCZOS)

    baseline_offset = max(0, int((loaded_font.emoji_scale * COLOR_EMOJI_SIZE - target_height) * 0.5))
    target_image = getattr(draw, "_image", None)
    paste_position = (int(position[0]), position[1] + baseline_offset)
    if isinstance(target_image, Image.Image):
        if target_image.mode == "RGBA":
            target_image.alpha_composite(emoji_image, paste_position)
        else:
            target_image.paste(emoji_image, paste_position, emoji_image)
    else:
        draw.bitmap(paste_position, emoji_image)
    return float(target_width)


def _text_length(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont | FontStack) -> float:
    if isinstance(font, FontStack):
        return font.text_length(text)
    try:
        return float(draw.textlength(text, font=font))
    except UnicodeEncodeError:
        return float(font.getlength(text))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont | FontStack, max_width: int) -> str:
    if _text_length(draw, text, font) <= max_width:
        return text

    clusters = _text_clusters(text)
    while clusters and _text_length(draw, "".join(clusters) + ELLIPSIS, font) > max_width:
        clusters.pop()
    return ("".join(clusters).rstrip() + ELLIPSIS) if clusters else ELLIPSIS


def _draw_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    *,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    font: ImageFont.ImageFont | FontStack,
) -> None:
    if isinstance(font, FontStack):
        font.draw(draw, position, text, fill=fill)
        return
    try:
        draw.text(position, text, fill=fill, font=font, embedded_color=True)
    except TypeError:
        draw.text(position, text, fill=fill, font=font)


def _rounded_layer(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int] | None = None,
    width: int = 1,
    blur: int = 0,
) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return layer


def _draw_glow(image: Image.Image, box: tuple[int, int, int, int], color: tuple[int, int, int, int], blur: int) -> None:
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(box, fill=color)
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(blur)))


def _draw_background(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    for y in range(height):
        blend = y / max(height - 1, 1)
        r = int(23 + 34 * blend)
        g = int(23 + 34 * blend)
        b = int(24 + 34 * blend)
        draw.line((0, y, width, y), fill=(r, g, b, 255))

    draw.rectangle((0, 0, width, height // 2), fill=(0, 0, 0, 45))
    _draw_glow(image, (width // 2 - 380, 80, width // 2 + 380, 520), (126, 88, 255, 48), 92)
    _draw_glow(image, (width // 2 - 250, 0, width // 2 + 520, 390), (60, 120, 255, 34), 94)


def _draw_lens_disc(image: Image.Image, center: tuple[int, int], radius: int) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    cx, cy = center
    box = (cx - radius, cy - radius, cx + radius, cy + radius)

    draw.ellipse(box, fill=(31, 30, 48, 112), outline=(224, 225, 255, 105), width=3)
    draw.ellipse((cx - radius + 34, cy - radius + 18, cx + radius - 44, cy + radius - 22), outline=(124, 92, 255, 105), width=4)
    draw.arc((cx - radius + 18, cy - radius + 8, cx + radius - 8, cy + radius + 16), 183, 342, fill=(225, 221, 255, 165), width=7)
    draw.arc((cx - radius + 68, cy - radius + 44, cx + radius + 54, cy + radius + 12), 197, 13, fill=(99, 130, 255, 116), width=5)
    draw.arc((cx - radius - 20, cy - radius - 6, cx + radius - 76, cy + radius - 12), 332, 74, fill=(203, 116, 255, 95), width=6)

    for offset, color in [
        (-72, (95, 100, 255, 78)),
        (-46, (86, 255, 220, 52)),
        (-20, (219, 113, 255, 66)),
        (16, (255, 255, 255, 42)),
    ]:
        draw.rounded_rectangle((cx - radius + 36, cy + offset, cx + radius - 34, cy + offset + 11), radius=5, fill=color)

    draw.polygon(
        [
            (cx + 22, cy - radius),
            (cx + 126, cy - radius),
            (cx - 34, cy + radius),
            (cx - 138, cy + radius),
        ],
        fill=(255, 255, 255, 44),
    )
    draw.line((cx - radius + 24, cy + 94, cx + radius - 16, cy + 30), fill=(255, 255, 255, 26), width=2)
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.25)))


def _draw_corner_ticks(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    size = 6
    inset = 18
    for x, y in [
        (x1 + inset, y1 + inset),
        (x2 - inset - size, y1 + inset),
        (x1 + inset, y2 - inset - size),
        (x2 - inset - size, y2 - inset - size),
    ]:
        draw.rectangle((x, y, x + size, y + size), fill=(248, 248, 255, 230))


def _draw_rank_marker(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    rank: int,
    *,
    font: ImageFont.ImageFont | FontStack,
    color: tuple[int, int, int, int],
) -> None:
    cx, cy = center
    rank_text = str(rank)
    text_width = _text_length(draw, rank_text, font)
    if rank <= 3:
        draw.arc((cx - 27, cy - 22, cx - 4, cy + 22), 92, 268, fill=color, width=2)
        draw.arc((cx + 4, cy - 22, cx + 27, cy + 22), -88, 88, fill=color, width=2)
        for step in range(4):
            draw.line((cx - 25 + step * 3, cy - 6 + step * 8, cx - 18 + step * 3, cy - 10 + step * 8), fill=color, width=2)
            draw.line((cx + 25 - step * 3, cy - 6 + step * 8, cx + 18 - step * 3, cy - 10 + step * 8), fill=color, width=2)
    _draw_text(draw, (int(cx - text_width / 2), cy - 13), rank_text, fill=color, font=font)


def _draw_avatar_orb(draw: ImageDraw.ImageDraw, center: tuple[int, int], rank: int) -> None:
    cx, cy = center
    palettes = {
        1: ((13, 12, 27), (245, 194, 89), (102, 84, 255)),
        2: ((11, 13, 32), (126, 163, 255), (124, 80, 255)),
        3: ((14, 17, 34), (70, 209, 255), (118, 72, 255)),
    }
    base, ring, core = palettes.get(rank, ((12, 12, 18), (128, 111, 185), (88, 67, 180)))
    draw.ellipse((cx - 23, cy - 23, cx + 23, cy + 23), fill=(*base, 255), outline=(*ring, 185), width=2)
    draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(*core, 170), outline=(235, 239, 255, 120), width=1)
    draw.arc((cx - 17, cy - 17, cx + 17, cy + 17), 205, 520, fill=(*ring, 210), width=3)


def _draw_progress(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    percent: float,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=5, fill=(34, 38, 54, 255), outline=(75, 66, 120, 170), width=1)
    filled = int((x2 - x1) * min(max(percent, 0.0), 1.0))
    if filled <= 0:
        return
    fill_box = (x1, y1, x1 + filled, y2)
    draw.rounded_rectangle(fill_box, radius=5, fill=(139, 92, 246, 255))
    if filled > 14:
        draw.rounded_rectangle((x1, y1, x1 + max(6, int(filled * 0.45)), y2), radius=5, fill=(194, 132, 255, 185))


def draw_leaderboard_image(
    title: str,
    rows: Sequence[LeaderboardImageRow],
    *,
    width: int = 1400,
) -> io.BytesIO:
    safe_rows = list(rows)
    row_height = 57
    panel_x = 38
    panel_y = 92
    panel_w = width - panel_x * 2
    top_area = 170
    table_header_height = 76
    bottom_padding = 42
    body_height = max(len(safe_rows), 1) * row_height
    height = panel_y * 2 + top_area + table_header_height + body_height + bottom_padding

    image = Image.new("RGBA", (width, height), (30, 30, 31, 255))
    _draw_background(image)
    draw = ImageDraw.Draw(image, "RGBA")

    title_font = load_font_stack(48, kind="ui")
    micro_font = load_font_stack(14, bold=True, kind="ui")
    header_font = load_font_stack(17, bold=True, kind="ui")
    name_font = load_font_stack(25, bold=True, kind="name")
    small_font = load_font_stack(15, bold=True, kind="ui")
    meta_font = load_font_stack(20, kind="ui")
    score_font = load_font_stack(20, bold=True, kind="ui")

    panel_box = (panel_x, panel_y, panel_x + panel_w, height - panel_y)
    image.alpha_composite(_rounded_layer(image.size, (panel_x + 8, panel_y + 14, panel_x + panel_w + 8, height - panel_y + 14), radius=18, fill=(0, 0, 0, 125), blur=22))
    draw.rounded_rectangle(panel_box, radius=18, fill=(0, 0, 0, 246), outline=(42, 42, 48, 255), width=2)

    _draw_lens_disc(image, (width // 2, panel_y + 38), 238)
    draw.rectangle((panel_x + 1, panel_y + 144, panel_x + panel_w - 1, panel_y + top_area + 38), fill=(0, 0, 0, 102))

    title_text = "LEADERBOARD"
    title_width = _text_length(draw, title_text, title_font)
    _draw_text(draw, (int(width / 2 - title_width / 2), panel_y + 76), title_text, fill=(248, 249, 255, 238), font=title_font)
    brand = _clean_single_line_text(title, default="O.XY", max_len=18)
    brand_width = _text_length(draw, brand, micro_font)
    _draw_text(draw, (int(width / 2 - brand_width / 2), panel_y + 38), brand, fill=(240, 240, 255, 206), font=micro_font)

    table_y = panel_y + top_area
    table_x = panel_x + 32
    table_r = panel_x + panel_w - 32
    table_bottom = height - panel_y - 38
    table_box = (table_x, table_y, table_r, table_bottom)
    image.alpha_composite(_rounded_layer(image.size, (table_x, table_y + 6, table_r, table_bottom + 6), radius=12, fill=(0, 0, 0, 125), blur=14))
    draw.rounded_rectangle(table_box, radius=12, fill=(17, 17, 19, 214), outline=(31, 31, 36, 238), width=1)
    draw.rounded_rectangle((table_x + 2, table_y + 2, table_r - 2, table_y + 54), radius=10, fill=(36, 34, 42, 94))
    _draw_corner_ticks(draw, table_box)

    table_w = table_r - table_x
    columns = {
        "rank": table_x + 38,
        "avatar": table_x + 190,
        "name": table_x + 238,
        "content": table_x + int(table_w * 0.55),
        "reward": table_x + int(table_w * 0.70),
        "score": table_x + int(table_w * 0.84),
    }
    headers = [
        ("//RANK", columns["rank"]),
        ("//CHAMPIONS", table_x + 170),
        ("//CONTENT", columns["content"]),
        ("//REWARD", columns["reward"]),
        ("//SCORE", columns["score"]),
    ]
    for label, x in headers:
        _draw_text(draw, (x, table_y + 56), label, fill=(161, 159, 166, 225), font=header_font)

    for x in [columns["content"] - 28, columns["reward"] - 28, columns["score"] - 28]:
        draw.line((x, table_y + 2, x, table_bottom - 2), fill=(255, 255, 255, 11), width=1)

    values = [max(float(row.value), 0.0) for row in safe_rows]
    max_value = max(values, default=1.0) or 1.0
    place_styles = {
        1: {
            "fill": (86, 78, 17, 142),
            "rank": (238, 199, 87, 255),
            "line": (232, 189, 76, 116),
        },
        2: {
            "fill": (111, 108, 122, 118),
            "rank": (214, 217, 229, 255),
            "line": (186, 190, 207, 96),
        },
        3: {
            "fill": (98, 44, 18, 132),
            "rank": (215, 149, 82, 255),
            "line": (222, 120, 58, 100),
        },
    }

    body_y = table_y + table_header_height
    if not safe_rows:
        empty_text = "No leaderboard data yet"
        _draw_text(draw, (table_x + 42, body_y + 24), empty_text, fill=(196, 190, 202, 255), font=meta_font)

    for index, row in enumerate(safe_rows, start=1):
        y = body_y + (index - 1) * row_height
        row_box = (table_x + 18, y, table_r - 18, y + row_height)
        style = place_styles.get(index)
        if style:
            draw.rectangle(row_box, fill=style["fill"])
            draw.rectangle((row_box[0], row_box[1], row_box[2], row_box[1] + 1), fill=style["line"])
        else:
            base_fill = (28, 28, 30, 170) if index % 2 else (7, 7, 8, 178)
            draw.rectangle(row_box, fill=base_fill)
        draw.line((row_box[0], row_box[3], row_box[2], row_box[3]), fill=(0, 0, 0, 128), width=2)

        rank_color = style["rank"] if style else (156, 156, 162, 255)
        _draw_rank_marker(draw, (columns["rank"] + 34, y + row_height // 2), index, font=score_font, color=rank_color)
        if index <= 3:
            _draw_avatar_orb(draw, (columns["avatar"], y + row_height // 2), index)

        name_max_width = columns["content"] - columns["name"] - 46
        name = _fit_text(draw, sanitize_leaderboard_name(row.name), name_font, name_max_width)
        name_x = columns["name"]
        if index <= 3:
            _draw_text(draw, (name_x, y + 17), "[", fill=(220, 220, 226, 175), font=name_font)
            _draw_text(draw, (name_x + 24, y + 17), name, fill=(250, 249, 255, 255), font=name_font)
            name_end = name_x + 38 + int(_text_length(draw, name, name_font))
            _draw_text(draw, (min(name_end, columns["content"] - 34), y + 17), "]", fill=(220, 220, 226, 175), font=name_font)
        else:
            _draw_text(draw, (name_x, y + 17), name, fill=(238, 238, 242, 245), font=name_font)

        primary = _fit_text(draw, safe_text(row.primary, max_len=44), meta_font, columns["reward"] - columns["content"] - 28) if row.primary else "-"
        secondary = _fit_text(draw, safe_text(row.secondary, max_len=44), meta_font, columns["score"] - columns["reward"] - 62) if row.secondary else "-"
        _draw_text(draw, (columns["content"], y + 18), primary, fill=(236, 236, 240, 238), font=meta_font)
        _draw_text(draw, (columns["reward"], y + 18), secondary, fill=(197, 242, 190, 250), font=meta_font)
        draw.ellipse((columns["reward"] + int(_text_length(draw, secondary, meta_font)) + 13, y + 22, columns["reward"] + int(_text_length(draw, secondary, meta_font)) + 27, y + 36), outline=(224, 236, 226, 220), width=2)

        value = max(float(row.value), 0.0)
        percent = min(max(value / max_value, 0.0), 1.0)
        score_text = f"{value:.2f}" if value % 1 else f"{int(value):.2f}"
        score_text = _fit_text(draw, score_text, score_font, table_r - columns["score"] - 38)
        _draw_text(draw, (columns["score"], y + 17), score_text, fill=(244, 244, 248, 252), font=score_font)
        progress_x1 = columns["score"]
        progress_x2 = table_r - 32
        progress_y = y + row_height - 7
        draw.line((progress_x1, progress_y, progress_x2, progress_y), fill=(255, 255, 255, 18), width=2)
        draw.line((progress_x1, progress_y, progress_x1 + int((progress_x2 - progress_x1) * percent), progress_y), fill=(142, 96, 244, 78), width=2)

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG")
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

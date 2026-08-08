from __future__ import annotations

import io
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import discord
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from utils.brand import BRAND_ACCENT, BRAND_NAME, BRAND_PURPLE, theme_path

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<[@#][!&]?\d+>|<@&\d+>")
DEFAULT_NAME = "Без имени"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
WINDOWS_FONT_DIR = Path("C:/Windows/Fonts")
COLOR_EMOJI_SIZE = 109
ELLIPSIS = "..."
AVATAR_CACHE_TTL_SECONDS = 15 * 60
AVATAR_CACHE_MAX_ITEMS = 512
_AVATAR_CACHE: dict[tuple[int, str], tuple[float, bytes | None]] = {}


@dataclass(slots=True, frozen=True)
class LeaderboardImageRow:
    name: str
    value: int | float
    primary: str = ""
    secondary: str = ""
    avatar: bytes | None = None


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
    dm_sans = LOCAL_FONT_DIR / "DMSans-Variable.ttf"
    noto_regular = LOCAL_FONT_DIR / "NotoSans-Regular.ttf"
    noto_bold = LOCAL_FONT_DIR / "NotoSans-Bold.ttf"
    noto_ui = noto_bold if bold else noto_regular

    if kind == "name":
        return [
            dm_sans,
            noto_ui,
            LOCAL_FONT_DIR / "NotoSansSymbols2-Regular.ttf",
            LOCAL_FONT_DIR / "NotoSansMath-Regular.ttf",
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
        dm_sans,
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
                if path.name == "DMSans-Variable.ttf" and hasattr(font, "set_variation_by_axes"):
                    font.set_variation_by_axes([min(max(size, 9), 40), 700 if bold else 400])
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
    return _clean_single_line_text(name, default=DEFAULT_NAME, max_len=max_len)


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


async def resolve_avatar_bytes(
    bot: discord.Client,
    guild: discord.Guild | None,
    user_id: int,
) -> bytes | None:
    member = guild.get_member(user_id) if guild is not None else None
    user = member or bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    avatar = user.display_avatar.with_size(128)
    cache_key = (user_id, str(getattr(avatar, "key", avatar.url)))
    cached = _AVATAR_CACHE.get(cache_key)
    now = time.monotonic()
    cache_ttl = 60 if cached is not None and cached[1] is None else AVATAR_CACHE_TTL_SECONDS
    if cached is not None and now - cached[0] < cache_ttl:
        return cached[1]
    try:
        payload = await avatar.read()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, OSError):
        logger.debug("Could not load avatar for leaderboard user %s", user_id, exc_info=True)
        payload = None
    _AVATAR_CACHE[cache_key] = (now, payload)
    while len(_AVATAR_CACHE) > AVATAR_CACHE_MAX_ITEMS:
        _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
    return payload


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


def _themed_canvas(size: tuple[int, int], theme: str) -> Image.Image:
    path = theme_path(theme)
    if path is None:
        image = Image.new("RGBA", size, (13, 11, 18, 255))
    else:
        with Image.open(path) as source:
            image = ImageOps.fit(source.convert("RGBA"), size, method=Image.Resampling.LANCZOS)
    shade = Image.new("RGBA", size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade, "RGBA")
    width, height = size
    for x in range(width):
        position = x / max(width - 1, 1)
        strength = 212 if position < 0.55 else int(212 - 172 * ((position - 0.55) / 0.45))
        shade_draw.line((x, 0, x, height), fill=(5, 6, 9, max(36, strength)))
    for y in range(height):
        edge = min(y / max(height * 0.18, 1), (height - y) / max(height * 0.2, 1), 1.0)
        alpha = int(56 * (1.0 - max(edge, 0.0)))
        if alpha:
            shade_draw.line((0, y, width, y), fill=(0, 0, 0, alpha))
    image.alpha_composite(shade)
    return image


def _avatar_circle(data: bytes | None, size: int, *, fallback: str = "V") -> Image.Image:
    if data:
        try:
            with Image.open(io.BytesIO(data)) as source:
                avatar = ImageOps.fit(source.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            avatar = Image.new("RGBA", (size, size), (*BRAND_PURPLE, 255))
    else:
        avatar = Image.new("RGBA", (size, size), (42, 38, 47, 255))
        draw = ImageDraw.Draw(avatar)
        font = load_font_stack(max(18, size // 2), bold=True, kind="name")
        initial = safe_text(fallback, 1).upper()
        width = _text_length(draw, initial, font)
        _draw_text(draw, (int((size - width) / 2), size // 5), initial, fill=(255, 255, 255, 255), font=font)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    avatar.putalpha(mask)
    return avatar


def draw_leaderboard_image(
    title: str,
    rows: Sequence[LeaderboardImageRow],
    *,
    width: int = 1600,
    height: int = 1200,
    theme: str = "neutral",
) -> io.BytesIO:
    safe_rows = list(rows)[:10]
    image = _themed_canvas((width, height), theme)
    draw = ImageDraw.Draw(image, "RGBA")
    title_font = load_font_stack(56, bold=True)
    eyebrow_font = load_font_stack(19, bold=True)
    name_font = load_font_stack(27, bold=True, kind="name")
    meta_font = load_font_stack(19)
    score_font = load_font_stack(26, bold=True)
    rank_font = load_font_stack(22, bold=True)

    _draw_text(draw, (72, 50), BRAND_NAME.upper(), fill=(213, 199, 208, 235), font=eyebrow_font)
    title_text = safe_text(title, 42).capitalize()
    _draw_text(draw, (72, 88), title_text, fill=(250, 247, 249, 255), font=title_font)
    header_y = 188
    _draw_text(draw, (202, header_y), "УЧАСТНИК", fill=(164, 155, 165, 255), font=eyebrow_font)
    _draw_text(draw, (680, header_y), "СТАТИСТИКА", fill=(164, 155, 165, 255), font=eyebrow_font)
    _draw_text(draw, (1072, header_y), "ИТОГ", fill=(164, 155, 165, 255), font=eyebrow_font)
    draw.line((72, 224, 1168, 224), fill=(255, 255, 255, 34), width=1)

    row_height = 88
    first_y = 234
    place_colors = {1: (229, 177, 92), 2: (179, 187, 199), 3: (190, 130, 99)}
    if not safe_rows:
        _draw_text(draw, (72, 286), "Пока нет данных", fill=(211, 203, 210, 255), font=name_font)

    for index, row in enumerate(safe_rows, start=1):
        y = first_y + (index - 1) * row_height
        accent = place_colors.get(index, (145, 133, 142))
        fill = (20, 18, 23, 142) if index % 2 else (28, 25, 30, 142)
        if index <= 3:
            fill = (31, 27, 31, 172)
        draw.rounded_rectangle((72, y, 1168, y + 76), radius=10, fill=fill)
        if index <= 3:
            draw.rounded_rectangle((72, y + 14, 76, y + 62), radius=2, fill=(*accent, 235))
        rank = str(index)
        rank_w = _text_length(draw, rank, rank_font)
        _draw_text(draw, (110 - int(rank_w / 2), y + 25), rank, fill=(*accent, 255), font=rank_font)
        avatar = _avatar_circle(row.avatar, 52, fallback=row.name)
        image.alpha_composite(avatar, (140, y + 12))

        name = _fit_text(draw, sanitize_leaderboard_name(row.name), name_font, 420)
        _draw_text(draw, (210, y + 21), name, fill=(248, 245, 247, 255), font=name_font)
        primary = _fit_text(draw, safe_text(row.primary, 48) if row.primary else "—", meta_font, 320)
        secondary = _fit_text(draw, safe_text(row.secondary, 48) if row.secondary else "—", meta_font, 320)
        _draw_text(draw, (680, y + 13), primary, fill=(235, 230, 233, 255), font=meta_font)
        _draw_text(draw, (680, y + 42), secondary, fill=(165, 155, 165, 255), font=meta_font)
        value = max(float(row.value), 0.0)
        score = f"{int(value):,}".replace(",", " ") if value.is_integer() else f"{value:.1f}"
        score = _fit_text(draw, score, score_font, 150)
        score_width = _text_length(draw, score, score_font)
        _draw_text(draw, (1142 - int(score_width), y + 21), score, fill=(250, 247, 249, 255), font=score_font)

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True, compress_level=7)
    output.seek(0)
    return output


def draw_profile_card(
    name: str,
    headline: str,
    stats: Sequence[tuple[str, str]],
    *,
    avatar: bytes | None = None,
    progress: float = 0.0,
    theme: str = "neutral",
    accent: tuple[int, int, int] | None = None,
    size: tuple[int, int] = (1200, 675),
) -> io.BytesIO:
    image = _themed_canvas(size, theme)
    draw = ImageDraw.Draw(image, "RGBA")
    title_font = load_font_stack(48, bold=True)
    name_font = load_font_stack(36, bold=True, kind="name")
    label_font = load_font_stack(17, bold=True)
    value_font = load_font_stack(26, bold=True)
    _draw_text(draw, (72, 58), BRAND_NAME.upper(), fill=(204, 193, 200, 240), font=label_font)
    headline_text = safe_text(headline, 34).capitalize()
    _draw_text(draw, (72, 94), headline_text, fill=(250, 247, 249, 255), font=title_font)
    draw.line((72, 170, 766, 170), fill=(255, 255, 255, 38), width=1)
    avatar_image = _avatar_circle(avatar, 132, fallback=name)
    image.alpha_composite(avatar_image, (72, 214))
    fitted_name = _fit_text(draw, sanitize_leaderboard_name(name), name_font, 520)
    _draw_text(draw, (232, 216), fitted_name, fill=(250, 247, 249, 255), font=name_font)
    stat_items = list(stats)[:4]
    for index, (label, value) in enumerate(stat_items):
        x = 232 + (index % 3) * 188
        y = 282 + (index // 3) * 92
        _draw_text(draw, (x, y), safe_text(label.upper(), 22), fill=(168, 157, 166, 255), font=label_font)
        _draw_text(draw, (x, y + 29), safe_text(value, 26), fill=(246, 242, 245, 255), font=value_font)
    _draw_text(draw, (72, 478), "ПРОГРЕСС", fill=(168, 157, 166, 255), font=label_font)
    percent_text = f"{int(min(max(progress, 0.0), 1.0) * 100)}%"
    percent_width = _text_length(draw, percent_text, label_font)
    _draw_text(draw, (766 - int(percent_width), 478), percent_text, fill=(220, 211, 217, 255), font=label_font)
    draw.rounded_rectangle((72, 515, 766, 525), radius=5, fill=(255, 255, 255, 42))
    fill_width = int(694 * min(max(progress, 0.0), 1.0))
    if fill_width:
        progress_accent = accent or BRAND_ACCENT
        draw.rounded_rectangle((72, 515, 72 + fill_width, 525), radius=5, fill=(*progress_accent, 255))
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True, compress_level=7)
    output.seek(0)
    return output


def make_leaderboard_file(
    title: str,
    rows: Sequence[LeaderboardImageRow],
    *,
    filename: str = "leaderboard.png",
    theme: str = "neutral",
) -> discord.File:
    image = draw_leaderboard_image(title, rows, theme=theme)
    return discord.File(image, filename=filename, description=f"{title} — {BRAND_NAME}")


def make_profile_file(
    name: str,
    headline: str,
    stats: Sequence[tuple[str, str]],
    *,
    avatar: bytes | None = None,
    progress: float = 0.0,
    theme: str = "neutral",
    accent: tuple[int, int, int] | None = None,
    filename: str = "card.png",
    description: str | None = None,
) -> discord.File:
    image = draw_profile_card(name, headline, stats, avatar=avatar, progress=progress, theme=theme, accent=accent)
    return discord.File(image, filename=filename, description=description or f"{headline}: {name}")

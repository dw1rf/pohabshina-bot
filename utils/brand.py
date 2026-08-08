from __future__ import annotations

import os
from pathlib import Path

BRAND_NAME = os.getenv("BOT_DISPLAY_NAME", "Vulgarities Bot").strip() or "Vulgarities Bot"
BRAND_TAGLINE = "Dark social entertainment for Discord"
BRAND_COLOR = 0xD9679D
BRAND_ACCENT = (217, 103, 157)
BRAND_PURPLE = (151, 103, 132)
BRAND_GRAPHITE = (9, 10, 14)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THEME_ROOT = PROJECT_ROOT / "assets" / "themes" / "dark_anime_glam"
THEME_MANIFEST = THEME_ROOT / "manifest.json"

THEME_FILES = {
    "levels": THEME_ROOT / "levels.png",
    "economy": THEME_ROOT / "economy.png",
    "reputation": THEME_ROOT / "reputation.png",
    "pets": THEME_ROOT / "pets.png",
    "clubs": THEME_ROOT / "clubs.png",
    "relationships": THEME_ROOT / "relationships.png",
    "events": THEME_ROOT / "events.png",
    "neutral": THEME_ROOT / "neutral.png",
}


def theme_path(theme: str) -> Path | None:
    candidate = THEME_FILES.get(theme) or THEME_FILES["neutral"]
    if candidate.exists():
        return candidate
    fallback = THEME_FILES["neutral"]
    return fallback if fallback.exists() else None

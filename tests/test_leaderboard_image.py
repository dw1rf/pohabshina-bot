from __future__ import annotations

from utils.leaderboard_image import (
    LeaderboardImageRow,
    draw_leaderboard_image,
    load_font_stack,
    sanitize_leaderboard_name,
)


UNICODE_NAMES = [
    "𝕯𝖆𝖗𝖐𝕻𝖑𝖆𝖞𝖊𝖗",
    "𝓙𝓾𝓼𝓽𝓕𝓾𝓷",
    "Ｌｅｌｏｕｃｈ",
    "ᴅɪᴇɢᴏ",
    "NightmareGirl",
    "Игрок_Алекс",
    "⚡Player⚡",
    "Player🔥",
]


def test_leaderboard_names_preserve_decorative_unicode() -> None:
    for name in UNICODE_NAMES:
        assert sanitize_leaderboard_name(name) == name


def test_leaderboard_name_font_stack_supports_unicode_names() -> None:
    font_stack = load_font_stack(27, bold=True, kind="name")

    unsupported = [name for name in UNICODE_NAMES if not font_stack.supports_text(name)]

    assert unsupported == []


def test_leaderboard_image_renders_unicode_names() -> None:
    rows = [
        LeaderboardImageRow(
            name=name,
            primary=f"Tier {index}",
            secondary=f"Reward {index}",
            value=(len(UNICODE_NAMES) - index + 1) * 100,
        )
        for index, name in enumerate(UNICODE_NAMES, start=1)
    ]

    image = draw_leaderboard_image("Unicode Test", rows)

    assert image.getbuffer().nbytes > 10_000

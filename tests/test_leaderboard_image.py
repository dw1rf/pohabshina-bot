from __future__ import annotations

from utils.leaderboard_image import (
    LeaderboardImageRow,
    draw_leaderboard_image,
    draw_reputation_card,
    load_font_stack,
    sanitize_leaderboard_name,
    draw_profile_card,
)
from PIL import Image
import io
import asyncio
from utils.leaderboard_image import resolve_avatar_bytes


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


def test_dm_sans_is_the_primary_interface_font() -> None:
    font_stack = load_font_stack(27, bold=True)

    assert font_stack.fonts[0].path is not None
    assert font_stack.fonts[0].path.name == "DMSans-Variable.ttf"
    assert font_stack.supports_text("Vulgarities Bot · Уровень 42")


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


def test_branded_leaderboard_dimensions_and_discord_limit() -> None:
    for count in (0, 1, 10):
        rows = [
            LeaderboardImageRow(
                name=f"Очень длинное имя игрока №{index} 🔥✨ с хвостом",
                primary=f"Уровень {index}",
                secondary="Кириллица и emoji работают",
                value=100 - index,
            )
            for index in range(count)
        ]
        payload = draw_leaderboard_image("ТОП УЧАСТНИКОВ", rows)
        with Image.open(io.BytesIO(payload.getvalue())) as image:
            assert image.size == (1600, 1200)
        assert payload.getbuffer().nbytes < 8 * 1024 * 1024


def test_rank_and_level_up_card_dimensions() -> None:
    payload = draw_profile_card(
        "Игрок_Алекс 🔥",
        "Новый уровень 42",
        (("Место", "#1"), ("Опыт", "1200 / 1500")),
        progress=0.8,
        theme="levels",
    )
    with Image.open(io.BytesIO(payload.getvalue())) as image:
        assert image.size == (1200, 675)
    assert payload.getbuffer().nbytes < 8 * 1024 * 1024


def test_profile_card_accepts_unlocked_accent_color() -> None:
    payload = draw_profile_card(
        "Александра",
        "Ранг участника",
        (("Уровень", "42"),),
        progress=0.75,
        theme="levels",
        accent=(214, 183, 110),
    )

    with Image.open(io.BytesIO(payload.getvalue())) as image:
        assert image.getpixel((200, 520))[0] > image.getpixel((200, 520))[2]


def test_reputation_cards_support_both_signs_and_negative_totals() -> None:
    cases = ((1, 12), (-1, 11), (-1, -7))
    for change, total in cases:
        payload = draw_reputation_card(
            "Очень длинное имя участника с хвостом 🔥✨" * 2,
            change,
            total,
            avatar=None,
        )
        with Image.open(io.BytesIO(payload.getvalue())) as image:
            assert image.size == (1000, 460)
        assert payload.getbuffer().nbytes < 8 * 1024 * 1024


def test_reputation_card_rejects_zero_change() -> None:
    import pytest

    with pytest.raises(ValueError, match="cannot be zero"):
        draw_reputation_card("Игрок", 0, 10)


def test_avatar_download_is_cached_and_has_safe_fallback() -> None:
    class Avatar:
        def __init__(self, key: str, *, fails: bool = False) -> None:
            self.key = key
            self.url = f"https://example.invalid/{key}.png"
            self.fails = fails
            self.reads = 0

        def with_size(self, _: int):
            return self

        async def read(self) -> bytes:
            self.reads += 1
            if self.fails:
                raise OSError("offline")
            return b"avatar"

    class User:
        def __init__(self, avatar: Avatar) -> None:
            self.display_avatar = avatar

    class Bot:
        def __init__(self, user: User) -> None:
            self.user = user

        def get_user(self, _: int):
            return self.user

    async def scenario() -> None:
        avatar = Avatar("cache-success")
        bot = Bot(User(avatar))
        assert await resolve_avatar_bytes(bot, None, 987001) == b"avatar"
        assert await resolve_avatar_bytes(bot, None, 987001) == b"avatar"
        assert avatar.reads == 1

        missing = Avatar("cache-fallback", fails=True)
        missing_bot = Bot(User(missing))
        assert await resolve_avatar_bytes(missing_bot, None, 987002) is None
        assert await resolve_avatar_bytes(missing_bot, None, 987002) is None
        assert missing.reads == 1

    asyncio.run(scenario())

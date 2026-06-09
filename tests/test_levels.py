from __future__ import annotations

from types import SimpleNamespace

from cogs.levels import LevelsCog
from utils.engagement_content import EngagementContent


def test_level_up_gif_does_not_repeat_consecutively() -> None:
    content = EngagementContent(
        {
            "levelup_messages": ["text"],
            "levelup_gifs": ["https://example.com/1.gif", "https://example.com/2.gif"],
        }
    )
    cog = LevelsCog(SimpleNamespace(engagement_content=content))

    previous = cog._get_random_level_up_gif()
    for _ in range(10):
        current = cog._get_random_level_up_gif()
        assert current != previous
        previous = current


def test_level_up_gif_allows_empty_list() -> None:
    content = EngagementContent({"levelup_messages": ["text"], "levelup_gifs": []})
    cog = LevelsCog(SimpleNamespace(engagement_content=content))

    assert cog._get_random_level_up_gif() is None

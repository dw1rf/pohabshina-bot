from __future__ import annotations

import asyncio

from cogs.suggestions import SuggestionView, SuggestionsCog
from cogs.weekly_digest import WeeklyDigestCog


def test_suggestion_buttons_have_restart_safe_custom_ids() -> None:
    async def scenario() -> None:
        view = SuggestionView(object(), 42)
        assert view.timeout is None
        assert [item.custom_id for item in view.children] == [
            "vulgarities:suggestion:up:42",
            "vulgarities:suggestion:down:42",
        ]

    asyncio.run(scenario())


def test_admin_commands_keep_runtime_permission_checks() -> None:
    assert SuggestionsCog.setup_channel.checks
    assert SuggestionsCog.decide.checks
    assert SuggestionsCog.disable.checks
    assert WeeklyDigestCog.setup_digest.checks
    assert WeeklyDigestCog.disable_digest.checks


def test_weekly_digest_loop_runs_every_fifteen_minutes() -> None:
    assert WeeklyDigestCog.delivery_loop.minutes == 15

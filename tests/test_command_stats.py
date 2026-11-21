import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import discord
import pytest
from discord import app_commands

from cogs.command_stats import CommandStatsCog
from services.social_game_service import SocialGameService


def test_recording_and_admin_report(tmp_path):
    async def scenario():
        bot = SimpleNamespace(settings=SimpleNamespace(db_path=tmp_path / "stats.db"))
        cog = CommandStatsCog(bot)
        privacy = SocialGameService()
        async with aiosqlite.connect(bot.settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            await privacy.init_db(db)
        await cog.cog_load()
        try:
            first = SimpleNamespace(id=5, bot=False, mention="<@5>")
            second = SimpleNamespace(id=6, bot=False)
            command = SimpleNamespace(qualified_name="music play")
            interaction = SimpleNamespace(guild_id=1, user=first, response=SimpleNamespace(defer=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()))
            await asyncio.gather(*(cog.on_app_command_completion(interaction, command) for _ in range(3)))
            await cog.on_app_command_completion(SimpleNamespace(guild_id=1, user=second), command)
            await cog.on_app_command_completion(SimpleNamespace(guild_id=2, user=second), command)
            await cog.on_app_command_completion(SimpleNamespace(guild_id=None, user=first), command)
            await cog.on_app_command_completion(interaction, cog.command_stats)
            prefix = SimpleNamespace(guild=SimpleNamespace(id=1), command=SimpleNamespace(qualified_name="play"),
                                     author=first, interaction=None)
            await cog.on_command_completion(prefix)
            prefix.interaction = interaction
            await cog.on_command_completion(prefix)
            today = datetime.now(UTC).date()
            await cog.db.execute("INSERT INTO command_usage_daily VALUES(?,?,?,?,?)",
                                 (1, (today - timedelta(days=32)).isoformat(), 5, "/music play", 40))

            async def report(**filters):
                await cog.command_stats.callback(cog, interaction, **filters)
                kwargs = interaction.followup.send.await_args.kwargs
                assert kwargs["ephemeral"] and kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
                return kwargs["embed"].description

            assert "`/music play` — 4 · участников: 2" in await report()
            assert "`!play` — 1" in await report(member=first)
            assert (await report(command="music play")).splitlines() == ["<@5> — 3", "<@6> — 1"]
            assert await report(command="/music play", member=first, days=90) == "<@5> — 43"
            await cog.command_stats.callback(cog, interaction, page=2)
            assert "страницы" in interaction.followup.send.await_args.args[0]
            assert await (await cog.db.execute("SELECT COUNT(*) FROM command_usage_daily")).fetchone() == (5,)
        finally:
            await cog.cog_unload()
        await cog.cog_load()
        try:
            assert (await (await cog.db.execute("SELECT SUM(uses) FROM command_usage_daily")).fetchone())[0] == 46
            async with aiosqlite.connect(bot.settings.db_path) as db:
                db.row_factory = aiosqlite.Row
                await privacy.forget_profile_data(db, 1, first.id)
            await cog.on_app_command_completion(interaction, command)
            assert await report(command="/music play") == "<@6> — 1"
        finally:
            await cog.cog_unload()

    asyncio.run(scenario())


def test_report_requires_admin_at_runtime():
    cmd = CommandStatsCog.command_stats
    assert cmd.guild_only and cmd.default_permissions.administrator
    with pytest.raises(app_commands.MissingPermissions):
        cmd.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))

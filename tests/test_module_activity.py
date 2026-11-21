import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.command_stats import CommandStatsCog
from cogs.giveaways import GiveawaysCog
from cogs.reaction_roles import ReactionRolesCog
from cogs.story import StoryMenuView
from services.social_game_service import SocialGameService


def test_module_report_combines_historical_commands_and_interactions(tmp_path):
    async def scenario():
        path = tmp_path / "usage.db"
        privacy = SocialGameService()
        async with aiosqlite.connect(path) as setup:
            setup.row_factory = aiosqlite.Row
            await privacy.init_db(setup)
        modules = ["giveaways", "reputation", "starboard", "story", "nsfw_import", "command_stats"]
        bot = SimpleNamespace(settings=SimpleNamespace(db_path=path), extensions=["cogs." + name for name in modules],
                              tree=SimpleNamespace(walk_commands=lambda guild=None: [] if guild else [GiveawaysCog.create]),
                              walk_commands=lambda: [])
        stats = CommandStatsCog(bot)
        member = SimpleNamespace(id=10, mention="<@10>")
        interaction = SimpleNamespace(guild_id=1, response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))

        async def report(**filters):
            await stats.command_stats.callback(stats, interaction, **filters)
            result = interaction.followup.send.await_args
            assert result.kwargs["ephemeral"]
            return result.kwargs["embed"].description if "embed" in result.kwargs else result.args[0]

        await stats.cog_load()
        try:
            day = datetime.now(UTC).date()
            await stats.db.execute("INSERT INTO command_usage_daily VALUES(1,?,10,?,2)",
                                   (day.isoformat(), "/" + GiveawaysCog.create.qualified_name))
            await stats.on_module_activity(1, 10, "giveaways", "component")
            await stats.on_module_activity(1, 20, "giveaways", "component")
            await stats.on_module_activity(1, 20, "starboard", "reaction")
            await stats.on_module_activity(1, 10, "reputation", "reputation")
            await stats.on_module_activity(2, 30, "story", "component")
            await stats.db.execute("INSERT INTO module_interactions_daily VALUES(1,?,40,'story','component',99)",
                                   ((day - timedelta(days=35)).isoformat(),))
            text = await report(view="modules")
            assert "**giveaways** — 4 · участников: 2 (команды: 2, кнопки/меню: 2)" in text
            assert "**starboard** — 1" in text and "**reputation** — 1" in text
            assert "**giveaways** — 3 · участников: 1" in await report(view="modules", member=member)
            assert await report(view="inactive") == "**story** — 0 · участников: 0"
            assert "**story** — 99" in await report(view="modules", days=90)
            assert "страницы" in await report(view="modules", page=2)
            assert "выберите вид commands" in await report(view="modules", command="abc")

            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await privacy.forget_profile_data(db, 1, 10)
            await stats.on_module_activity(1, 10, "giveaways", "component")
            assert "**giveaways** — 1 · участников: 1 (кнопки/меню: 1)" in await report(view="modules")
            await stats.db.execute("INSERT INTO module_interactions_daily VALUES(1,?,50,'story','component',1)",
                                   ((day - timedelta(days=90)).isoformat(),))
        finally:
            await stats.cog_unload()
        stats = CommandStatsCog(bot)
        await stats.cog_load()
        try:
            async with stats.db.execute("SELECT COUNT(*) FROM module_interactions_daily WHERE user_id=50") as cursor:
                assert await cursor.fetchone() == (0,)
            assert "**giveaways** — 1" in await report(view="modules")
        finally:
            await stats.cog_unload()

    asyncio.run(scenario())


def test_view_counts_authorized_decorated_buttons_and_dynamic_selects():
    async def scenario():
        cog = SimpleNamespace(start_story=AsyncMock(), apply_choice=AsyncMock())
        interaction = SimpleNamespace(client=Mock(), guild_id=4, user=SimpleNamespace(id=6, bot=False),
                                      response=SimpleNamespace(send_message=AsyncMock()), data={})
        menu = StoryMenuView(cog, 6, started=False, choices=[])
        await menu._scheduled_task(menu.start, interaction)
        cog.start_story.assert_awaited_once()
        interaction.client.dispatch.assert_called_once_with("module_activity", 4, 6, "story", "component")
        interaction.client.dispatch.reset_mock()
        interaction.user.id = 7
        await menu._scheduled_task(menu.start, interaction)
        interaction.client.dispatch.assert_not_called()
        interaction.user.id = 6
        choices = [{"label": str(i)} for i in range(6)]
        menu = StoryMenuView(cog, 6, started=True, choices=choices)
        select = next(item for item in menu.children if isinstance(item, discord.ui.Select))
        interaction.data = {"values": ["2"]}
        await menu._scheduled_task(select, interaction)
        cog.apply_choice.assert_awaited_once_with(interaction, choices[2])
        interaction.client.dispatch.assert_called_once_with("module_activity", 4, 6, "story", "component")

    asyncio.run(scenario())


def test_reaction_roles_counts_only_human_role_grants():
    async def scenario():
        member = SimpleNamespace(id=10, bot=False, add_roles=AsyncMock(), remove_roles=AsyncMock())
        guild = SimpleNamespace(id=1, get_member=lambda _: member, get_role=lambda _: object())
        bot = SimpleNamespace(db=object(), user=SimpleNamespace(id=100), get_guild=lambda _: guild, dispatch=Mock(),
                              reaction_roles=SimpleNamespace(find_binding=AsyncMock(return_value=SimpleNamespace(role_id=3))))
        cog = object.__new__(ReactionRolesCog)
        cog.bot = bot
        cog._can_manage_role = Mock(return_value=True)
        payload = SimpleNamespace(guild_id=1, user_id=10, message_id=8, emoji="⭐")
        await cog.on_raw_reaction_add(payload)
        await cog.on_raw_reaction_remove(payload)
        member.bot = True
        await cog.on_raw_reaction_add(payload)
        member.bot = False
        bot.reaction_roles.find_binding.return_value = None
        await cog.on_raw_reaction_add(payload)
        bot.dispatch.assert_called_once_with("module_activity", 1, 10, "reaction_roles", "reaction")

    asyncio.run(scenario())

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from bot_client import MovieBot
from cogs.music import MusicCog, MusicControlView, Track
from cogs.tts_voice import TTSVoiceCog


def test_panel_layout_and_controls():
    async def scenario():
        cog = MusicCog(Mock())
        player = cog.get_player(1)
        guild = SimpleNamespace(id=1)
        empty = cog.build_panel_embed(guild)
        assert "Добавить" in empty.description and not empty.fields
        view = MusicControlView(cog, player)
        assert view.is_persistent()
        rows = view.to_components()
        assert [len(row["components"]) for row in rows] == [3, 1]
        add, pause, skip = rows[0]["components"]
        assert not add["disabled"] and pause["disabled"] and skip["disabled"]
        keys = {option["value"] for option in rows[1]["components"][0]["options"]}
        assert keys == {"next", "current", "queue", "shuffle", "autoplay", "refresh", "clear", "stop", "leave"}
        legacy = MusicControlView(cog, register_legacy=True)
        assert {item.custom_id for item in legacy.children} >= {"music_panel:" + key for key in keys}
        song = Track("Night Drive", "https://example.com/track", "audio", 192, 3, "Alex")
        player.current = song
        player.queue = [song] * 3
        player.is_paused = True
        embed = cog.build_panel_embed(guild)
        assert embed.title == song.title and embed.url == song.webpage_url
        assert "На паузе" in embed.author.name and "3:12" in embed.description
        assert embed.fields[0].name == "В очереди · 3" and len(embed.fields[0].value.splitlines()) == 2
        view = MusicControlView(cog, player)
        assert view.to_components()[0]["components"][1]["label"] == "Продолжить"
        user = SimpleNamespace(id=5, bot=False)
        interaction = SimpleNamespace(guild=guild, guild_id=1, message=None, user=user, client=Mock(),
                                      response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
                                      data={"values": ["queue"]})
        await view._scheduled_task(view.menu, interaction)
        assert interaction.response.send_message.await_args.kwargs["embed"].title == "Очередь"
        interaction.client.dispatch.assert_called_once_with("module_activity", 1, 5, "music", "component")

    asyncio.run(scenario())


def test_live_panel_updates_only_after_content_changes():
    async def scenario():
        guild = SimpleNamespace(id=42)
        cog = MusicCog(Mock(get_guild=Mock(return_value=guild)))
        message = Mock(id=123, edit=AsyncMock())
        message.channel.get_partial_message.return_value = message
        with patch.object(cog.refresh_panels, "start"):
            cog.watch_panel(guild.id, message, cog.build_panel_embed(guild))
            await cog.refresh_panels()
            message.edit.assert_not_awaited()
            cog.get_player(guild.id).current = Track("Track", "https://example.com", "url", 20, 2, "Alex")
            await cog.refresh_panels()
            assert message.edit.await_args.kwargs["embed"].title == "Track"
            await cog.refresh_panels()
            message.edit.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["music", "tts"])
def test_voice_joins_self_deafened_with_audio_output(mode):
    async def scenario():
        channel = Mock(connect=AsyncMock(return_value=Mock(spec=discord.VoiceClient)))
        user = Mock(spec=discord.Member)
        user.voice = SimpleNamespace(channel=channel)
        guild = SimpleNamespace(id=1, voice_client=None, me=None)
        bot = Mock()
        if mode == "music":
            cog = MusicCog(bot)
            cog.dependency_error = Mock(return_value=None)
            assert await cog.ensure_voice(guild, user, send_error=AsyncMock())
        else:
            cog = TTSVoiceCog(bot)
            cog.dependency_error = Mock(return_value=None)
            assert await cog.ensure_voice_client(SimpleNamespace(guild=guild, user=user))
        channel.connect.assert_awaited_once_with(timeout=15, reconnect=True, self_deaf=True)

    asyncio.run(scenario())


def test_self_deafen_is_restored_only_for_current_bot_channel():
    async def scenario():
        bot = SimpleNamespace(user=SimpleNamespace(id=1))
        channel = Mock()
        change = AsyncMock()
        state = SimpleNamespace(channel=channel, self_deaf=False, self_mute=False)
        member = SimpleNamespace(id=1, voice=state, guild=SimpleNamespace(change_voice_state=change))
        await MovieBot.on_voice_state_update(bot, member, None, state)
        change.assert_awaited_once_with(channel=channel, self_deaf=True, self_mute=False)
        state.self_deaf = True
        await MovieBot.on_voice_state_update(bot, member, None, state)
        state.self_deaf = False
        await MovieBot.on_voice_state_update(bot, member, None, SimpleNamespace(channel=Mock(), self_deaf=False))
        member.id = 2
        await MovieBot.on_voice_state_update(bot, member, None, state)
        assert change.await_count == 1

    asyncio.run(scenario())

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from cogs.music import MusicCog, Track


@pytest.mark.parametrize("mode", ["end", "next", "now"])
@pytest.mark.parametrize("include_playable", [False, True])
def test_playback_feedback_matches_queue(mode, include_playable):
    async def scenario():
        cog = MusicCog(Mock())
        voice = Mock(spec=discord.VoiceClient)
        voice.is_playing.return_value = voice.is_paused.return_value = False
        voice.is_connected.return_value = True
        cog.ensure_voice = AsyncMock(return_value=voice)
        cog.get_player(1).voice_client = voice
        broken = Track("broken", "https://example.com/a", "a", 5, 1, "listener")
        good = Track("playable", "https://example.com/b", "b", 5, 1, "listener")
        cog.extract_tracks = AsyncMock(return_value=([broken, good] if include_playable else [broken], False))

        async def start(player, track):
            if track is broken:
                return False
            player.current = track
            return True

        cog.start_track_locked = start
        success = AsyncMock()
        error = AsyncMock()
        await cog.handle_add(SimpleNamespace(id=1, voice_client=voice), Mock(), "song", mode=mode, send_error=error, send_success=success)
        assert not cog.get_player(1).queue
        if include_playable:
            error.assert_not_awaited()
            success.assert_awaited_once()
            assert "playable" in str(success.await_args.kwargs["embed"].to_dict())
        else:
            success.assert_not_awaited()
            error.assert_awaited_once()
            assert "воспроизвести" in error.await_args.args[0]

    asyncio.run(scenario())

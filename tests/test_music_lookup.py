import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from cogs import music
from cogs.music_resolvers import ResolvedMusicQuery, ResolvedTrack


@pytest.mark.parametrize("provider", ["search", "youtube", "spotify", "yandex"])
def test_lookups_before_first_track_and_while_advancing(monkeypatch, provider):
    async def scenario():
        voice = Mock(spec=discord.VoiceClient)
        voice.is_connected.return_value = True
        voice.is_playing.return_value = voice.is_paused.return_value = False
        guild = SimpleNamespace(id=7, voice_client=voice)
        user = SimpleNamespace(id=4, display_name="listener")
        cog = music.MusicCog(Mock())
        player = cog.get_player(guild.id)
        player.voice_client = voice
        cog.ensure_voice = AsyncMock(return_value=voice)
        monkeypatch.setattr(music, "require_ffmpeg", lambda: "ffmpeg")
        monkeypatch.setattr(cog, "log_ffmpeg_details", Mock())
        audio = Mock()
        monkeypatch.setattr(discord, "FFmpegPCMAudio", audio)
        urls = {
            "youtube": "https://www.youtube.com/playlist?list=PLtest",
            "spotify": "https://open.spotify.com/playlist/test",
            "yandex": "https://music.yandex.ru/users/test/playlists/123",
            "search": "artist song",
        }
        query = urls[provider]
        flat = [{"_type": "url", "title": f"song {i}", "url": f"https://www.youtube.com/watch?v={i}"}
                for i in range(50)]
        metadata = [ResolvedTrack(row["title"], "artist", 10, provider, query) for row in flat]
        lookup = Mock(return_value=ResolvedMusicQuery(metadata, provider, playlist_detected=True))
        monkeypatch.setattr(music, "resolve_external_music_query", lookup)
        calls = []

        def factory(options):
            def extract(link, download=False):
                calls.append(link)
                if link == query and provider == "youtube":
                    assert options["extract_flat"] == "in_playlist"
                    return {"entries": flat}
                song = {"title": "result", "webpage_url": "https://www.youtube.com/watch?v=result",
                        "url": "https://media.example/audio", "http_headers": {"User-Agent": "test"}}
                return {"entries": [song]} if link.startswith("ytsearch1:") else song
            worker = MagicMock()
            worker.__enter__.return_value.extract_info.side_effect = extract
            return worker

        monkeypatch.setattr(music.yt_dlp, "YoutubeDL", factory)
        success, error = AsyncMock(), AsyncMock()
        await cog.handle_add(guild, user, query, mode="end", send_error=error, send_success=success)
        error.assert_not_awaited()
        success.assert_awaited_once()
        voice.play.assert_called_once()
        assert audio.call_args.args == ("https://media.example/audio",)
        assert player.current.requester_name == "listener"
        if provider == "search":
            assert calls == ["ytsearch1:artist song"]
            assert not player.queue
            # Long-queued links are refreshed from the original video page.
            player.current.resolved_at -= music.STREAM_REUSE_SECONDS + 1
            await cog.refresh_stream_url(player.current)
            assert calls[-1] == "https://www.youtube.com/watch?v=result"
        else:
            assert len(player.queue) == 49
            if provider == "youtube":
                assert calls == [query, flat[0]["url"]]
            else:
                lookup.assert_called_once()
                assert calls == ["ytsearch1:artist - song 0 official audio"]
                assert player.current.title == "song 0"
            assert all(not track.stream_url for track in player.queue)
            await cog.after_track(player, player.current)
            assert len(player.queue) == 48 and voice.play.call_count == 2
            assert calls[-1] == (flat[1]["url"] if provider == "youtube" else "ytsearch1:artist - song 1 official audio")

    asyncio.run(scenario())


def test_delayed_callbacks_and_cancelled_lookups_do_not_change_new_queue():
    async def scenario():
        voice = Mock(spec=discord.VoiceClient)
        voice.is_connected.return_value = True
        guild = SimpleNamespace(id=9, voice_client=voice)
        cog = music.MusicCog(Mock())
        player = cog.get_player(guild.id)
        player.voice_client = voice
        old, current, queued = [music.Track(n, "url", "stream", 1, 2, "user") for n in ("old", "current", "queued")]
        cog.ensure_can_control = AsyncMock(return_value=player)
        epoch = player.request_epoch
        await cog.handle_stop(guild, Mock(), send_error=AsyncMock())
        with pytest.raises(music.MusicUserError):
            await cog.add_tracks(guild, voice, [old], mode="end", request_epoch=epoch)
        assert not player.queue
        player.current = current
        player.queue.append(queued)
        await cog.after_track(player, old)
        assert player.current is current and player.queue == [queued]
        guild.voice_client = None
        with pytest.raises(music.MusicUserError):
            await cog.add_tracks(guild, voice, [old], mode="end", request_epoch=player.request_epoch)
        assert player.queue == [queued]

    asyncio.run(scenario())

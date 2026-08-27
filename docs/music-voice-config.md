# Music Voice Config

Voice playback requires `discord.py[voice] >= 2.7.1` and `davey >= 0.1.6` so the bot can use Discord's current DAVE voice protocol. Reinstall `requirements.txt` when updating an existing host; copying only the Python source is not enough.

For Docker Compose, mount YouTube cookies read-only and use the absolute path inside the container:

```yaml
volumes:
  - ./youtube-cookies.txt:/app/youtube-cookies.txt:ro
environment:
  YTDLP_COOKIE_FILE: /app/youtube-cookies.txt
```

If the file is missing or unreadable, the bot logs one warning at startup and runs yt-dlp with cookies disabled.

The bot forwards the HTTP headers returned by yt-dlp to ffmpeg. This is required for current YouTube media URLs and avoids a connection that appears successful while ffmpeg receives no playable audio.

Spotify and Yandex Music resolvers use metadata only, then search the playable stream through YouTube:

```env
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
YANDEX_MUSIC_TOKEN=
MUSIC_MAX_PLAYLIST_TRACKS=50
```

# Music Voice Config

For Docker Compose, mount YouTube cookies read-only and use the absolute path inside the container:

```yaml
volumes:
  - ./youtube-cookies.txt:/app/youtube-cookies.txt:ro
environment:
  YTDLP_COOKIE_FILE: /app/youtube-cookies.txt
```

If the file is missing or unreadable, the bot logs one warning at startup and runs yt-dlp with cookies disabled.

Spotify and Yandex Music resolvers use metadata only, then search the playable stream through YouTube:

```env
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
YANDEX_MUSIC_TOKEN=
MUSIC_MAX_PLAYLIST_TRACKS=50
```

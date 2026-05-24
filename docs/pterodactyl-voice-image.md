# Pterodactyl voice runtime image

The current BotHost/Pterodactyl logs show `python=3.11.15` and no `ffmpeg`, `ffprobe`, or `deno` in `PATH`. That means the server is not running the repository `Dockerfile`; it is running a generic Python image from the Pterodactyl egg.

Changing Python code cannot install system binaries into that already selected image. The egg/server must use a Docker image that contains the binaries.

## Image to use

Use the image built from `Dockerfile.pterodactyl`:

```text
ghcr.io/dw1rf/pohabshina-bot:pterodactyl-voice
```

In Pterodactyl admin/server settings, replace the current Docker Image from the generic Python egg with the image above, then rebuild/reinstall or redeploy the server. A simple restart is not enough if the old image is still selected.

After the GitHub Actions workflow publishes the image, make the GHCR package public or configure registry credentials in Pterodactyl. If GHCR keeps the package private, the node will not be able to pull it anonymously.

## What the image contains

`Dockerfile.pterodactyl` uses `python:3.11-slim-bookworm`, matching the current runtime family shown in logs, and installs:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*
```

Deno is installed into `/usr/local/bin`:

```dockerfile
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
```

Build-time checks make a broken image fail during build:

```dockerfile
RUN ffmpeg -version
RUN ffprobe -version
RUN deno --version
```

The startup file is:

```text
bot.py
```

The image command is:

```text
python -u bot.py
```

If Pterodactyl overrides the image `CMD`, set the startup command to the same entrypoint.

## How to verify after deploy

The bot startup log must include:

```text
Voice runtime: python=3.11...
Voice runtime: PATH=...
ffmpeg found: path=/usr/bin/ffmpeg version=ffmpeg version ...
ffprobe found: path=/usr/bin/ffprobe version=ffprobe version ...
deno found: path=/usr/local/bin/deno version=deno ...
PyNaCl is installed; Discord voice support can load.
```

If logs still show:

```text
FFmpeg не найден в контейнере
ffprobe not found in PATH
deno not found in PATH
```

then Pterodactyl is still running the old egg image. Change the server/egg Docker Image to `ghcr.io/dw1rf/pohabshina-bot:pterodactyl-voice` and rebuild/redeploy.

If the binary exists but is installed outside standard Linux paths, set `FFMPEG_EXECUTABLE` and optionally `FFPROBE_EXECUTABLE` to absolute paths. With the Debian `ffmpeg` apt package this should not be needed because the bot checks `PATH`, `/usr/bin`, `/usr/local/bin`, and `/bin`.

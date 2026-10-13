FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl gnupg libdrm2 \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
    && printf 'Types: deb\nURIs: https://repo.jellyfin.org/master/debian\nSuites: bookworm\nComponents: main\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/jellyfin.gpg\n' \
        "$(dpkg --print-architecture)" \
        > /etc/apt/sources.list.d/jellyfin.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y jellyfin-ffmpeg8 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY video_converter /app/video_converter

ENV PATH="/usr/lib/jellyfin-ffmpeg:${PATH}" \
    FFMPEG_BIN=/usr/lib/jellyfin-ffmpeg/ffmpeg \
    FFPROBE_BIN=/usr/lib/jellyfin-ffmpeg/ffprobe \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    PYTHONUNBUFFERED=1 \
    INPUT_DIR=/data/input \
    OUTPUT_DIR=/data/output \
    OUTPUT_FORMAT=mp4 \
    VIDEO_CODEC=h264 \
    AUDIO_CODEC=aac \
    MAX_CONCURRENT_CONVERSIONS=1 \
    WEB_PORT=5800 \
    POLL_INTERVAL_SECONDS=2 \
    FILE_STABILITY_SECONDS=5

VOLUME ["/data/input", "/data/output"]
ENTRYPOINT ["python", "-m", "video_converter"]

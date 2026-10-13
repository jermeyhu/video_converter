FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY video_converter /app/video_converter

ENV PYTHONUNBUFFERED=1 \
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

import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    input_dir: Path
    output_dir: Path
    output_format: str
    video_codec: str
    audio_codec: str
    width: int | None
    height: int | None
    max_concurrent_conversions: int
    poll_interval_seconds: float
    file_stability_seconds: float
    ffmpeg_bin: str


def _positive_int(name: str, default: str) -> int:
    value = os.environ.get(name, default)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return parsed


def _positive_even_int(name: str, default: str) -> int:
    parsed = _positive_int(name, default)
    if parsed % 2:
        raise ValueError(f"{name} must be an even integer for H.264 video encoding, got {parsed}")
    return parsed


def _optional_positive_even_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return _positive_even_int(name, value)


def _positive_float(name: str, default: str) -> float:
    value = os.environ.get(name, default)
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number, got {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    return parsed


def load_settings() -> Settings:
    input_dir = Path(os.environ.get("INPUT_DIR", "/data/input")).expanduser()
    output_dir = Path(os.environ.get("OUTPUT_DIR", "/data/output")).expanduser()
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("INPUT_DIR and OUTPUT_DIR must be different directories")

    output_format = os.environ.get("OUTPUT_FORMAT", "mp4").lower().lstrip(".")
    if not output_format.isalnum():
        raise ValueError("OUTPUT_FORMAT must contain only letters and numbers")

    video_codec = os.environ.get("VIDEO_CODEC", "h264").strip().lower()
    audio_codec = os.environ.get("AUDIO_CODEC", "aac")
    for name, value in (("VIDEO_CODEC", video_codec), ("AUDIO_CODEC", audio_codec)):
        if not value or any(char.isspace() for char in value):
            raise ValueError(f"{name} must be a non-empty FFmpeg encoder name")

    width = _optional_positive_even_int("OUTPUT_WIDTH")
    height = _optional_positive_even_int("OUTPUT_HEIGHT")
    if (width is None) != (height is None):
        raise ValueError("OUTPUT_WIDTH and OUTPUT_HEIGHT must both be set or both be omitted")

    return Settings(
        input_dir=input_dir,
        output_dir=output_dir,
        output_format=output_format,
        video_codec=video_codec,
        audio_codec=audio_codec,
        width=width,
        height=height,
        max_concurrent_conversions=_positive_int("MAX_CONCURRENT_CONVERSIONS", "1"),
        poll_interval_seconds=_positive_float("POLL_INTERVAL_SECONDS", "2"),
        file_stability_seconds=_positive_float("FILE_STABILITY_SECONDS", "5"),
        ffmpeg_bin=os.environ.get("FFMPEG_BIN", "ffmpeg"),
    )

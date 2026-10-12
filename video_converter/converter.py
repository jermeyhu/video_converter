import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Iterable

from video_converter.config import Settings

LOGGER = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}
CODEC_PROFILES = {
    "h264": ("libx264", (("h264_nvenc", "nvidia"), ("h264_qsv", "qsv"), ("h264_vaapi", "vaapi"))),
    "h265": ("libx265", (("hevc_nvenc", "nvidia"), ("hevc_qsv", "qsv"), ("hevc_vaapi", "vaapi"))),
}
CODEC_ALIASES = {"auto": "h264", "hevc": "h265"}


def _available_encoders(ffmpeg_bin: str) -> set[str]:
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    )
    encoders: set[str] = set()
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        fields = line.split()
        if (
            len(fields) >= 2
            and len(fields[0]) == 6
            and fields[0][0] in "VAS"
            and fields[1] != "="
        ):
            encoders.add(fields[1])
    return encoders


def _hardware_device(backend: str) -> str | None:
    if backend == "nvidia":
        if Path("/dev/nvidia0").exists():
            return "nvidia"
        try:
            result = subprocess.run(
                ["nvidia-smi", "-L"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return "nvidia" if result.stdout.strip() else None

    if backend in ("qsv", "vaapi"):
        devices = sorted(Path("/dev/dri").glob("renderD*"))
        return str(devices[0]) if devices else None
    return None


def _hardware_candidates(
    encoders: Iterable[str],
    codec: str,
) -> list[tuple[str, str, str | None]]:
    available = set(encoders)
    candidates = []
    for encoder, backend in CODEC_PROFILES[codec][1]:
        if encoder not in available:
            continue
        device = _hardware_device(backend)
        if device and backend == "nvidia":
            candidates.append((encoder, backend, None))
        elif device:
            candidates.append((encoder, backend, device))
    return candidates


def _video_filter(settings: Settings, backend: str) -> str | None:
    filters = []
    if settings.width is not None and settings.height is not None:
        filters.append(
            f"scale={settings.width}:{settings.height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2"
        )
    if backend == "vaapi":
        filters.extend(["format=nv12", "hwupload"])
    return ",".join(filters) or None


def _run_ffmpeg(
    settings: Settings,
    source: Path,
    destination: Path,
    video_codec: str,
    backend: str = "software",
    device: str | None = None,
) -> None:
    command = [settings.ffmpeg_bin, "-hide_banner", "-nostdin", "-n"]
    if backend == "vaapi" and device:
        command.extend(["-vaapi_device", device])
    command.extend(["-i", str(source)])
    video_filter = _video_filter(settings, backend)
    if video_filter:
        command.extend(["-vf", video_filter])
    command.extend(
        [
            "-c:v",
            video_codec,
            "-c:a",
            settings.audio_codec,
            "-sn",
            "-dn",
        ]
    )
    if settings.output_format == "mp4":
        command.extend(["-movflags", "+faststart"])
    command.extend(["-f", settings.output_format, str(destination)])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"FFmpeg failed using {video_codec} (exit {result.returncode}): "
            f"{details[-4000:]}"
        )


def _commit_without_overwrite(temporary_path: Path, destination: Path) -> None:
    try:
        os.link(temporary_path, destination)
    except FileExistsError:
        raise FileExistsError(f"Output already exists: {destination}") from None
    temporary_path.unlink()


def convert_file(settings: Settings, source: Path) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.output_dir / f"{source.stem}.{settings.output_format}"
    if destination.exists():
        raise FileExistsError(f"Output already exists: {destination}")

    temporary_path = settings.output_dir / (
        f".{source.stem}.{uuid.uuid4().hex}.partial.{settings.output_format}"
    )
    try:
        requested_codec = CODEC_ALIASES.get(settings.video_codec, settings.video_codec)
        if requested_codec not in CODEC_PROFILES:
            _run_ffmpeg(settings, source, temporary_path, settings.video_codec)
        else:
            encoders = _available_encoders(settings.ffmpeg_bin)
            software_encoder, _ = CODEC_PROFILES[requested_codec]
            hardware_candidates = _hardware_candidates(encoders, requested_codec)
            succeeded = False
            for encoder, backend, device in hardware_candidates:
                try:
                    LOGGER.info("Trying hardware encoder %s for %s", encoder, source)
                    _run_ffmpeg(
                        settings,
                        source,
                        temporary_path,
                        encoder,
                        backend,
                        device,
                    )
                    succeeded = True
                    break
                except RuntimeError as exc:
                    LOGGER.warning(
                        "Hardware encoder %s failed for %s: %s",
                        encoder,
                        source,
                        exc,
                    )
                    temporary_path.unlink(missing_ok=True)

            if not succeeded:
                if hardware_candidates:
                    LOGGER.warning("Hardware encoders failed for %s; falling back to CPU", source)
                if software_encoder not in encoders:
                    raise RuntimeError(
                        f"No supported hardware encoder worked and FFmpeg does not "
                        f"provide {software_encoder}"
                    )
                _run_ffmpeg(settings, source, temporary_path, software_encoder)

        _commit_without_overwrite(temporary_path, destination)
        LOGGER.info("Converted %s -> %s", source, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)

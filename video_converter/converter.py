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
HARDWARE_ENCODERS = (
    ("h264_nvenc", "nvidia"),
    ("h264_qsv", "qsv"),
    ("h264_vaapi", "vaapi"),
)


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


def _hardware_candidates(encoders: Iterable[str]) -> list[tuple[str, str, str | None]]:
    available = set(encoders)
    candidates = []
    for encoder, backend in HARDWARE_ENCODERS:
        if encoder not in available:
            continue
        device = _hardware_device(backend)
        if backend == "nvidia" and Path("/dev/nvidia0").exists():
            candidates.append((encoder, backend, None))
        elif device:
            candidates.append((encoder, backend, device))
    return candidates


def _scale_filter(settings: Settings, backend: str) -> str:
    scale = (
        f"scale={settings.width}:{settings.height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2"
    )
    if backend == "vaapi":
        return f"{scale},format=nv12,hwupload"
    return scale


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
    command.extend(
        [
            "-i",
            str(source),
            "-vf",
            _scale_filter(settings, backend),
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
        if settings.video_codec != "auto":
            _run_ffmpeg(settings, source, temporary_path, settings.video_codec)
        else:
            encoders = _available_encoders(settings.ffmpeg_bin)
            hardware_candidates = _hardware_candidates(encoders)
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
                if "libx264" not in encoders:
                    raise RuntimeError(
                        "No supported hardware encoder worked and FFmpeg does not provide libx264"
                    )
                _run_ffmpeg(settings, source, temporary_path, "libx264")

        _commit_without_overwrite(temporary_path, destination)
        LOGGER.info("Converted %s -> %s", source, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)

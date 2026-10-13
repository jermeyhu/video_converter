import logging
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from functools import lru_cache
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
    "h264": ("libx264", "h264_vaapi"),
    "h265": ("libx265", "hevc_vaapi"),
}
CODEC_ALIASES = {"auto": "h264", "hevc": "h265"}


def _available_encoders(ffmpeg_bin: str) -> set[str]:
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
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


def _ffmpeg_can_initialize_device(ffmpeg_bin: str, device_spec: str) -> bool:
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-init_hw_device",
                device_spec,
                "-f",
                "lavfi",
                "-i",
                "nullsrc",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


@lru_cache(maxsize=16)
def _hardware_device(ffmpeg_bin: str = "ffmpeg") -> str | None:
    devices = sorted(Path("/dev/dri").glob("renderD*"))
    for device in devices:
        if _ffmpeg_can_initialize_device(ffmpeg_bin, f"vaapi=hw:{device}"):
            return str(device)
    return None


@lru_cache(maxsize=32)
def _vaapi_encoder_works(ffmpeg_bin: str, device: str, encoder: str) -> bool:
    profile = "main"
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-init_hw_device",
        f"vaapi=hw:{device}",
        "-filter_hw_device",
        "hw",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=128x128:rate=1",
        "-frames:v",
        "1",
        "-vf",
        "format=nv12,hwupload",
        "-c:v",
        encoder,
        "-profile:v",
        profile,
        "-f",
        "null",
        "-",
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


@lru_cache(maxsize=16)
def _vaapi_decode_profiles(device: str) -> set[str]:
    result = subprocess.run(
        ["vainfo", "--display", "drm", "--device", device],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    profiles: set[str] = set()
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        match = re.search(r"VAProfile(H264|HEVC)\w*\s*:\s*(.*)", line)
        if match and "VAEntrypointVLD" in match.group(2):
            profiles.add({"H264": "h264", "HEVC": "h265"}[match.group(1)])
    return profiles


def _hardware_candidates(
    encoders: Iterable[str],
    codec: str,
    ffmpeg_bin: str = "ffmpeg",
) -> list[tuple[str, str, str | None]]:
    available = set(encoders)
    encoder = CODEC_PROFILES[codec][1]
    if encoder not in available:
        return []
    device = _hardware_device(ffmpeg_bin)
    if device and _vaapi_encoder_works(ffmpeg_bin, device, encoder):
        return [(encoder, "vaapi", device)]
    return []


def hardware_status(settings: Settings) -> list[dict[str, str | bool]]:
    try:
        encoders = _available_encoders(settings.ffmpeg_bin)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("Could not inspect FFmpeg hardware encoders: %s", exc)
        return [
            {
                "backend": "vaapi",
                "codec": codec,
                "encoder": encoder,
                "ready": False,
                "decode_ready": False,
                "decode_reason": "无法读取 FFmpeg 编码器",
                "encode_ready": False,
                "encode_reason": "无法读取 FFmpeg 编码器",
                "reason": "无法读取 FFmpeg 编码器",
            }
            for codec, (_, encoder) in CODEC_PROFILES.items()
        ]

    device = _hardware_device(settings.ffmpeg_bin)
    result = []
    for codec, (_, encoder) in CODEC_PROFILES.items():
        encoder_available = encoder in encoders
        device_ready = encoder_available and device is not None
        encoder_ready = bool(
            device_ready
            and _vaapi_encoder_works(settings.ffmpeg_bin, device, encoder)
        )
        try:
            decode_ready = bool(
                device is not None and codec in _vaapi_decode_profiles(device)
            )
            decode_reason = (
                f"vainfo 检测到 {codec.upper()} 硬件解码"
                if decode_ready
                else f"vainfo 未检测到 {codec.upper()} 硬件解码入口"
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            LOGGER.warning("Could not inspect VAAPI decode profiles: %s", exc)
            decode_ready = False
            decode_reason = "无法读取 VAAPI 解码能力（vainfo）"
        encode_ready = bool(device_ready and encoder_ready)
        if encode_ready:
            encode_reason = f"VAAPI 编码测试通过：{device}"
        elif not encoder_available:
            encode_reason = "FFmpeg 未包含此 VAAPI 编码器"
        elif device is None:
            encode_reason = "未检测到可初始化的 VAAPI render 设备"
        else:
            encode_reason = "此 GPU/驱动不支持该 VAAPI 编码格式"
        result.append(
            {
                "backend": "vaapi",
                "codec": codec,
                "encoder": encoder,
                "ready": encode_ready,
                "decode_ready": decode_ready,
                "decode_reason": decode_reason,
                "encode_ready": encode_ready,
                "encode_reason": encode_reason,
                "reason": encode_reason,
            }
        )
    return result


def _video_filter(settings: Settings, backend: str) -> str | None:
    scale_and_pad = None
    if settings.width is not None and settings.height is not None:
        scale_and_pad = (
            f"scale={settings.width}:{settings.height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2"
        )
    if backend == "software":
        return scale_and_pad
    if backend == "vaapi":
        if scale_and_pad is None:
            return None
        return f"hwdownload,format=nv12,{scale_and_pad},format=nv12,hwupload"
    if backend == "vaapi_decode":
        download = "hwdownload,format=nv12"
        return f"{download},{scale_and_pad}" if scale_and_pad else download
    raise ValueError(f"Unsupported hardware backend: {backend}")


def _run_ffmpeg(
    settings: Settings,
    source: Path,
    destination: Path,
    video_codec: str,
    backend: str = "software",
    device: str | None = None,
    on_progress: Callable[[dict[str, object]], None] | None = None,
    duration: float | None = None,
) -> None:
    command = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-n",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
    ]
    if backend in {"vaapi", "vaapi_decode"}:
        if not device:
            raise RuntimeError("VAAPI requires an available render device")
        command.extend(
            [
                "-init_hw_device",
                f"vaapi=hw:{device}",
                "-filter_hw_device",
                "hw",
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                "hw",
                "-hwaccel_output_format",
                "vaapi",
            ]
        )
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
    if backend == "vaapi":
        command.extend(["-profile:v", "main"])
    if settings.output_format == "mp4":
        command.extend(["-movflags", "+faststart"])
    command.extend(["-f", settings.output_format, str(destination)])
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start FFmpeg: {exc}") from exc

    output_lines: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if len(output_lines) > 100:
                output_lines.pop(0)
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"out_time_us", "out_time_ms"}:
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                out_time = f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:05.2f}"
                progress = min(99.0, seconds / duration * 100) if duration and duration > 0 else None
                if on_progress:
                    on_progress({"progress": progress, "out_time": out_time})
            elif key == "out_time":
                if on_progress:
                    on_progress({"progress": None, "out_time": value})

    return_code = process.wait()
    if return_code:
        details = "\n".join(output_lines).strip()
        raise RuntimeError(
            f"FFmpeg failed using {video_codec} (exit {return_code}): "
            f"{details[-4000:]}"
        )


def _media_duration(settings: Settings, source: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                settings.ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        LOGGER.warning("Could not determine media duration for %s; percentage progress unavailable", source)
        return None


def _commit_without_overwrite(temporary_path: Path, destination: Path) -> None:
    try:
        os.link(temporary_path, destination)
    except FileExistsError:
        raise FileExistsError(f"Output already exists: {destination}") from None
    temporary_path.unlink()


def convert_file(
    settings: Settings,
    source: Path,
    on_progress: Callable[[dict[str, object]], None] | None = None,
) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.output_dir / f"{source.stem}.{settings.output_format}"
    if destination.exists():
        raise FileExistsError(f"Output already exists: {destination}")

    temporary_path = settings.output_dir / (
        f".{source.stem}.{uuid.uuid4().hex}.partial.{settings.output_format}"
    )
    try:
        requested_codec = CODEC_ALIASES.get(settings.video_codec, settings.video_codec)
        duration = _media_duration(settings, source)
        if requested_codec not in CODEC_PROFILES:
            backend = "vaapi" if settings.video_codec.endswith("_vaapi") else "software"
            device = (
                _hardware_device(settings.ffmpeg_bin)
                if backend == "vaapi"
                else None
            )
            if on_progress:
                detected_backend = backend
                if detected_backend == "software":
                    detected_backend = "cpu" if settings.video_codec.startswith("lib") else "custom"
                on_progress(
                    {"encoder": settings.video_codec, "backend": detected_backend}
                )
            _run_ffmpeg(
                settings, source, temporary_path, settings.video_codec, backend, device,
                on_progress=on_progress, duration=duration,
            )
        else:
            encoders = _available_encoders(settings.ffmpeg_bin)
            software_encoder, _ = CODEC_PROFILES[requested_codec]
            hardware_candidates = _hardware_candidates(
                encoders, requested_codec, settings.ffmpeg_bin
            )
            succeeded = False
            for encoder, backend, device in hardware_candidates:
                try:
                    LOGGER.info("Trying hardware encoder %s for %s", encoder, source)
                    if on_progress:
                        on_progress(
                            {
                                "encoder": encoder,
                                "backend": backend,
                                "progress": 0.0,
                                "out_time": None,
                            }
                        )
                    _run_ffmpeg(
                        settings,
                        source,
                        temporary_path,
                        encoder,
                        backend,
                        device,
                        on_progress,
                        duration,
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
                    LOGGER.warning(
                        "Hardware encoders failed for %s; trying VAAPI decode with CPU encoding",
                        source,
                    )
                if software_encoder not in encoders:
                    raise RuntimeError(
                        f"No supported hardware encoder worked and FFmpeg does not "
                        f"provide {software_encoder}"
                    )
                device = _hardware_device(settings.ffmpeg_bin)
                if device:
                    try:
                        LOGGER.info("Trying VAAPI decode with %s for %s", software_encoder, source)
                        if on_progress:
                            on_progress(
                                {
                                    "encoder": software_encoder,
                                    "backend": "VAAPI 解码 + CPU 编码",
                                    "progress": 0.0,
                                    "out_time": None,
                                }
                            )
                        _run_ffmpeg(
                            settings,
                            source,
                            temporary_path,
                            software_encoder,
                            "vaapi_decode",
                            device,
                            on_progress,
                            duration,
                        )
                        succeeded = True
                    except RuntimeError as exc:
                        LOGGER.warning(
                            "VAAPI decoding failed for %s; falling back to CPU: %s",
                            source,
                            exc,
                        )
                        temporary_path.unlink(missing_ok=True)
            if not succeeded:
                if on_progress:
                    on_progress(
                        {
                            "encoder": software_encoder,
                            "backend": "cpu",
                            "progress": 0.0,
                            "out_time": None,
                        }
                    )
                _run_ffmpeg(
                    settings,
                    source,
                    temporary_path,
                    software_encoder,
                    on_progress=on_progress,
                    duration=duration,
                )

        _commit_without_overwrite(temporary_path, destination)
        if on_progress:
            on_progress({"progress": 100.0, "out_time": "完成"})
        LOGGER.info("Converted %s -> %s", source, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)

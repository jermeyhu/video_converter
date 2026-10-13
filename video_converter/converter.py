import logging
import os
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
    "h264": (
        "libx264",
        {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "vaapi": "h264_vaapi"},
    ),
    "h265": (
        "libx265",
        {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "vaapi": "hevc_vaapi"},
    ),
    "av1": (
        "libsvtav1",
        {"nvenc": "av1_nvenc", "qsv": "av1_qsv", "vaapi": "av1_vaapi"},
    ),
}
CODEC_ALIASES = {"auto": "h264", "hevc": "h265", "av01": "av1"}
ENCODER_PROFILES = {
    "h264": ("main", "high", "constrained_baseline"),
    "h265": ("main",),
    "av1": ("main",),
}
BACKEND_ORDER = ("nvenc", "qsv", "vaapi")
VAAPI_PROFILES = {
    "h264": "H264",
    "h265": "HEVC",
    "av1": "AV1",
}
RAW_FORMATS = {"h264": "h264", "h265": "hevc", "av1": "obu"}
HWACCEL_NAMES = {"vaapi": "vaapi", "qsv": "qsv", "nvenc": "cuda"}
HWFRAME_FORMATS = {"vaapi": "vaapi", "qsv": "qsv", "nvenc": "cuda"}


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


def _backend_device_args(
    backend: str,
    device: str,
    alias: str = "hw",
) -> list[str]:
    if backend == "nvenc":
        return [
            "-init_hw_device",
            f"cuda={alias}:{device}",
            "-filter_hw_device",
            alias,
        ]
    if backend == "qsv":
        vaapi_alias = f"{alias}_va"
        return [
            "-init_hw_device",
            f"vaapi={vaapi_alias}:{device}",
            "-init_hw_device",
            f"qsv={alias}@{vaapi_alias}",
            "-filter_hw_device",
            alias,
        ]
    return [
        "-init_hw_device",
        f"vaapi={alias}:{device}",
        "-filter_hw_device",
        alias,
    ]


def _ffmpeg_can_initialize_backend(
    ffmpeg_bin: str,
    backend: str,
    device: str,
) -> bool:
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                *_backend_device_args(backend, device),
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
def _hardware_devices(ffmpeg_bin: str = "ffmpeg") -> tuple[tuple[str, str], ...]:
    devices: list[tuple[str, str]] = []
    for device in sorted(Path("/dev/dri").glob("renderD*")):
        device_name = str(device)
        for backend in ("qsv", "vaapi"):
            if _ffmpeg_can_initialize_backend(ffmpeg_bin, backend, device_name):
                devices.append((backend, device_name))
    if _ffmpeg_can_initialize_backend(ffmpeg_bin, "nvenc", "0"):
        devices.append(("nvenc", "0"))
    return tuple(devices)


@lru_cache(maxsize=32)
def _software_decode_sample(ffmpeg_bin: str, codec: str) -> bytes | None:
    software_encoder = CODEC_PROFILES[codec][0]
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=1",
        "-frames:v",
        "2",
        "-an",
        "-c:v",
        software_encoder,
    ]
    if codec == "h264":
        command.extend(["-preset", "ultrafast", "-profile:v", "main"])
    elif codec == "h265":
        command.extend(["-preset", "ultrafast", "-x265-params", "pools=1:frame-threads=1"])
    else:
        command.extend(["-preset", "12"])
    command.extend(["-f", RAW_FORMATS[codec], "pipe:1"])
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout or None


@lru_cache(maxsize=64)
def _hardware_encoder_profile(
    ffmpeg_bin: str,
    backend: str,
    device: str,
    codec: str,
    encoder: str,
) -> str | None:
    for profile in ENCODER_PROFILES[codec]:
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            *_backend_device_args(backend, device),
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=1",
            "-frames:v",
            "1",
        ]
        if backend == "vaapi":
            command.extend(["-vf", "format=nv12,hwupload"])
        elif backend == "qsv":
            command.extend(["-vf", "format=nv12,hwupload=extra_hw_frames=64"])
        else:
            command.extend(["-vf", "format=nv12"])
        command.extend(
            [
                "-c:v",
                encoder,
                "-profile:v",
                profile,
                "-b:v",
                "1M",
                "-f",
                "null",
                "-",
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.CalledProcessError:
            continue
        except (OSError, subprocess.TimeoutExpired):
            return None
        return profile
    return None


@lru_cache(maxsize=16)
def _hardware_decoder_works(
    ffmpeg_bin: str,
    backend: str,
    device: str,
    codec: str,
) -> bool:
    sample = _software_decode_sample(ffmpeg_bin, codec)
    if sample is None:
        return False
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *_backend_device_args(backend, device),
        "-hwaccel",
        HWACCEL_NAMES[backend],
        "-hwaccel_device",
        "hw",
        "-hwaccel_output_format",
        HWFRAME_FORMATS[backend],
        "-f",
        RAW_FORMATS[codec],
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        subprocess.run(
            command,
            input=sample,
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _hardware_candidates(
    encoders: Iterable[str],
    codec: str,
    ffmpeg_bin: str = "ffmpeg",
) -> list[tuple[str, str, str, str]]:
    available = set(encoders)
    hardware_encoders = CODEC_PROFILES[codec][1]
    candidates = []
    for backend in BACKEND_ORDER:
        encoder = hardware_encoders[backend]
        if encoder not in available:
            continue
        for device_backend, device in _hardware_devices(ffmpeg_bin):
            if device_backend != backend:
                continue
            profile = _hardware_encoder_profile(
                ffmpeg_bin,
                backend,
                device,
                codec,
                encoder,
            )
            if profile:
                candidates.append((encoder, backend, device, profile))
    return candidates


def _hardware_decoder_candidates(
    codec: str,
    ffmpeg_bin: str = "ffmpeg",
) -> list[tuple[str, str]]:
    return [
        (backend, device)
        for backend, device in _hardware_devices(ffmpeg_bin)
        if _hardware_decoder_works(ffmpeg_bin, backend, device, codec)
    ]


def hardware_status(settings: Settings) -> list[dict[str, str | bool]]:
    try:
        encoders = _available_encoders(settings.ffmpeg_bin)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("Could not inspect FFmpeg hardware encoders: %s", exc)
        return [
            {
                "backend": backend,
                "codec": codec,
                "encoder": encoder,
                "ready": False,
                "decode_ready": False,
                "decode_reason": "无法读取 FFmpeg 编码器",
                "encode_ready": False,
                "encode_reason": "无法读取 FFmpeg 编码器",
                "reason": "无法读取 FFmpeg 编码器",
            }
            for codec, (_, backend_encoders) in CODEC_PROFILES.items()
            for backend, encoder in backend_encoders.items()
        ]

    devices = _hardware_devices(settings.ffmpeg_bin)
    result = []
    for codec, (_, backend_encoders) in CODEC_PROFILES.items():
        for backend, encoder in backend_encoders.items():
            device = next(
                (
                    candidate_device
                    for candidate_backend, candidate_device in devices
                    if candidate_backend == backend
                ),
                None,
            )
            encoder_available = encoder in encoders
            profile = (
                _hardware_encoder_profile(
                    settings.ffmpeg_bin,
                    backend,
                    device,
                    codec,
                    encoder,
                )
                if device and encoder_available
                else None
            )
            decode_ready = any(
                candidate_backend == backend
                and candidate_device == device
                for candidate_backend, candidate_device
                in _hardware_decoder_candidates(codec, settings.ffmpeg_bin)
            )
            encode_ready = bool(device and profile)
            if encode_ready:
                encode_reason = f"{backend.upper()} {profile} 编码测试通过：{device}"
            elif not encoder_available:
                encode_reason = f"FFmpeg 未包含 {encoder} 编码器"
            elif device is None:
                encode_reason = f"未检测到可初始化的 {backend.upper()} 设备"
            else:
                encode_reason = f"此 GPU/驱动不支持 {codec.upper()} 硬件编码"
            decode_reason = (
                f"{backend.upper()} {codec.upper()} 硬件解码测试通过：{device}"
                if decode_ready
                else f"未检测到可用的 {backend.upper()} {codec.upper()} 硬件解码"
            )
            result.append(
                {
                    "backend": backend,
                    "codec": codec,
                    "encoder": encoder,
                    "ready": encode_ready,
                    "decode_ready": decode_ready,
                    "decode_reason": decode_reason,
                    "encode_ready": encode_ready,
                    "encode_profile": profile or "",
                    "encode_reason": encode_reason,
                    "reason": encode_reason,
                }
            )
    return result


def _video_filter(
    settings: Settings,
    backend: str,
    decode_backend: str | None = None,
    same_device: bool = True,
) -> str | None:
    if backend not in {"software", *BACKEND_ORDER}:
        raise ValueError(f"Unsupported hardware backend: {backend}")
    if decode_backend is None:
        decode_backend = backend if backend in BACKEND_ORDER else "software"
    if decode_backend not in {"software", *BACKEND_ORDER}:
        raise ValueError(f"Unsupported decode backend: {decode_backend}")

    scale_and_pad = None
    if settings.width is not None and settings.height is not None:
        scale_and_pad = (
            f"scale={settings.width}:{settings.height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2"
        )
    if backend == "software" and decode_backend == "software":
        return scale_and_pad
    if (
        backend == decode_backend
        and backend in BACKEND_ORDER
        and same_device
        and scale_and_pad is None
    ):
        return None

    software_filter = scale_and_pad
    if decode_backend in BACKEND_ORDER:
        download = "hwdownload,format=nv12"
        software_filter = f"{download},{scale_and_pad}" if scale_and_pad else download

    if backend == "software":
        return software_filter
    if backend == "nvenc":
        return software_filter
    upload = "hwupload=extra_hw_frames=64" if backend == "qsv" else "hwupload"
    prefix = f"{software_filter}," if software_filter else ""
    return f"{prefix}format=nv12,{upload}"


def _run_ffmpeg(
    settings: Settings,
    source: Path,
    destination: Path,
    video_codec: str,
    backend: str = "software",
    device: str | None = None,
    on_progress: Callable[[dict[str, object]], None] | None = None,
    duration: float | None = None,
    encoder_profile: str = "main",
    decode_backend: str | None = None,
    decode_device: str | None = None,
) -> None:
    if decode_backend is None:
        decode_backend = backend if backend in BACKEND_ORDER else "software"
    if decode_backend in BACKEND_ORDER and decode_device is None:
        decode_device = device

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
    encoder_backend = backend if backend in BACKEND_ORDER else None
    encoder_device = device if encoder_backend else None
    decoder_backend = decode_backend if decode_backend in BACKEND_ORDER else None

    shared_hw_context = bool(
        encoder_backend
        and encoder_backend == decoder_backend
        and encoder_device == decode_device
    )
    if encoder_backend:
        if not encoder_device:
            raise RuntimeError(f"{encoder_backend.upper()} requires an available device")
        command.extend(
            _backend_device_args(
                encoder_backend,
                encoder_device,
                "hw" if shared_hw_context or not decoder_backend else "encoder_hw",
            )
        )
    if decode_backend in BACKEND_ORDER:
        if not decode_device:
            raise RuntimeError(f"{decode_backend.upper()} requires an available device")
        decoder_alias = (
            "hw"
            if shared_hw_context or not encoder_backend
            else "decoder_hw"
        )
        if not shared_hw_context:
            command.extend(
                _backend_device_args(
                    decode_backend,
                    decode_device,
                    decoder_alias,
                )
            )
            if encoder_backend:
                command.extend(["-filter_hw_device", "encoder_hw"])
        command.extend(
            [
                "-hwaccel",
                HWACCEL_NAMES[decode_backend],
                "-hwaccel_device",
                decoder_alias,
                "-hwaccel_output_format",
                HWFRAME_FORMATS[decode_backend],
            ]
        )
    command.extend(["-i", str(source)])
    video_filter = _video_filter(
        settings,
        backend,
        decode_backend,
        same_device=shared_hw_context,
    )
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
    if backend in BACKEND_ORDER:
        command.extend(["-profile:v", encoder_profile])
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
            backend = next(
                (
                    candidate
                    for candidate in BACKEND_ORDER
                    if settings.video_codec.endswith(f"_{candidate}")
                ),
                "software",
            )
            devices = dict(_hardware_devices(settings.ffmpeg_bin))
            device = devices.get(backend) if backend != "software" else None
            if backend != "software" and device is None:
                raise RuntimeError(
                    f"No usable {backend.upper()} device for encoder "
                    f"{settings.video_codec}"
                )
            decode_candidates = [
                *_hardware_devices(settings.ffmpeg_bin),
                ("software", None),
            ]
            if on_progress:
                detected_backend = backend
                if detected_backend == "software":
                    detected_backend = "cpu" if settings.video_codec.startswith("lib") else "custom"
                on_progress(
                    {
                        "encoder": settings.video_codec,
                        "backend": detected_backend,
                    }
                )
            for decode_backend, decode_device in decode_candidates:
                try:
                    _run_ffmpeg(
                        settings,
                        source,
                        temporary_path,
                        settings.video_codec,
                        backend,
                        device,
                        on_progress=on_progress,
                        duration=duration,
                        decode_backend=decode_backend,
                        decode_device=decode_device,
                    )
                    break
                except RuntimeError as exc:
                    temporary_path.unlink(missing_ok=True)
                    if decode_backend == "software":
                        raise
                    LOGGER.warning(
                        "%s hardware decode failed for %s with explicit encoder %s: %s",
                        decode_backend.upper(),
                        source,
                        settings.video_codec,
                        exc,
                    )
        else:
            encoders = _available_encoders(settings.ffmpeg_bin)
            software_encoder = CODEC_PROFILES[requested_codec][0]
            hardware_candidates = _hardware_candidates(
                encoders, requested_codec, settings.ffmpeg_bin
            )
            decode_candidates = _hardware_devices(settings.ffmpeg_bin)
            attempts: list[
                tuple[str, str, str | None, str, str, str | None]
            ] = []
            for encoder, encoder_backend, encoder_device, profile in hardware_candidates:
                for decoder_backend, decoder_device in decode_candidates:
                    attempts.append(
                        (
                            encoder,
                            encoder_backend,
                            encoder_device,
                            profile,
                            decoder_backend,
                            decoder_device,
                        )
                    )
            for encoder, encoder_backend, encoder_device, profile in hardware_candidates:
                attempts.append(
                    (
                        encoder,
                        encoder_backend,
                        encoder_device,
                        profile,
                        "software",
                        None,
                    )
                )
            if software_encoder in encoders:
                for decoder_backend, decoder_device in decode_candidates:
                    attempts.append(
                        (
                            software_encoder,
                            "software",
                            None,
                            "",
                            decoder_backend,
                            decoder_device,
                        )
                    )
                attempts.append(
                    (software_encoder, "software", None, "", "software", None)
                )
            if not attempts:
                raise RuntimeError(
                    f"No usable hardware encoder or software encoder for "
                    f"{requested_codec}"
                )

            succeeded = False
            for (
                encoder,
                encoder_backend,
                encoder_device,
                profile,
                decoder_backend,
                decoder_device,
            ) in attempts:
                pipeline_backend = (
                    f"{encoder_backend.upper()} 编码 + "
                    f"{decoder_backend.upper()} 解码"
                    if encoder_backend != "software"
                    and decoder_backend != "software"
                    else (
                        f"{encoder_backend.upper()} 编码"
                        if encoder_backend != "software"
                        else (
                            f"{decoder_backend.upper()} 解码 + CPU 编码"
                            if decoder_backend != "software"
                            else "cpu"
                        )
                    )
                )
                try:
                    LOGGER.info(
                        "Trying %s pipeline for %s",
                        pipeline_backend,
                        source,
                    )
                    if on_progress:
                        on_progress(
                            {
                                "encoder": encoder,
                                "backend": pipeline_backend,
                                "progress": 0.0,
                                "out_time": None,
                            }
                        )
                    _run_ffmpeg(
                        settings,
                        source,
                        temporary_path,
                        encoder,
                        encoder_backend,
                        encoder_device,
                        on_progress,
                        duration,
                        encoder_profile=profile or "main",
                        decode_backend=decoder_backend,
                        decode_device=decoder_device,
                    )
                    succeeded = True
                    break
                except RuntimeError as exc:
                    LOGGER.warning(
                        "%s pipeline failed for %s: %s",
                        pipeline_backend,
                        source,
                        exc,
                    )
                    temporary_path.unlink(missing_ok=True)

            if not succeeded:
                raise RuntimeError(
                    f"All hardware/software pipelines failed for {source}; "
                    "see previous FFmpeg errors"
                )

        _commit_without_overwrite(temporary_path, destination)
        if on_progress:
            on_progress({"progress": 100.0, "out_time": "完成"})
        LOGGER.info("Converted %s -> %s", source, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)

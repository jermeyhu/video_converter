import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_converter.config import Settings
from video_converter.converter import (
    _available_encoders,
    _hardware_candidates,
    _hardware_decoder_works,
    _hardware_devices,
    _hardware_encoder_profile,
    _video_filter,
    _run_ffmpeg,
    convert_file,
    hardware_status,
)


def make_settings(
    output_dir: Path,
    video_codec: str = "h264",
    width: int | None = None,
    height: int | None = None,
) -> Settings:
    return Settings(
        input_dir=output_dir.parent / "input",
        output_dir=output_dir,
        output_format="mp4",
        video_codec=video_codec,
        audio_codec="aac",
        width=width,
        height=height,
        max_concurrent_conversions=1,
        poll_interval_seconds=1,
        file_stability_seconds=1,
        ffmpeg_bin="ffmpeg",
    )


class HardwareDetectionTests(unittest.TestCase):
    def test_parses_ffmpeg_encoder_listing(self) -> None:
        with patch(
            "video_converter.converter.subprocess.run",
            return_value=SimpleNamespace(
                stdout=(
                    " V..... = Video\n"
                    " A..... = Audio\n"
                    " V..... libx264\n"
                    " V..... h264_vaapi\n"
                    " A..... aac\n"
                ),
                stderr="",
            ),
        ):
            encoders = _available_encoders("ffmpeg")

        self.assertEqual(encoders, {"libx264", "h264_vaapi", "aac"})

    def test_vaapi_encoder_probe_runs_a_real_encode(self) -> None:
        _hardware_encoder_profile.cache_clear()
        with patch(
            "video_converter.converter.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            profile = _hardware_encoder_profile(
                "ffmpeg",
                "vaapi",
                "/dev/dri/renderD128",
                "h264",
                "h264_vaapi",
            )

        self.assertEqual(profile, "main")
        command = run.call_args.args[0]
        self.assertIn("vaapi=hw:/dev/dri/renderD128", command)
        self.assertIn("h264_vaapi", command)
        self.assertIn("-profile:v", command)
        self.assertIn("main", command)
        self.assertIn("testsrc2=size=640x360:rate=1", command)
        self.assertIn("-b:v", command)
        _hardware_encoder_profile.cache_clear()

    def test_vaapi_encoder_probe_tries_supported_h264_profiles(self) -> None:
        _hardware_encoder_profile.cache_clear()
        with patch(
            "video_converter.converter.subprocess.run",
            side_effect=[
                subprocess.CalledProcessError(1, "ffmpeg"),
                SimpleNamespace(returncode=0),
            ],
        ) as run:
            profile = _hardware_encoder_profile(
                "ffmpeg",
                "vaapi",
                "/dev/dri/renderD128",
                "h264",
                "h264_vaapi",
            )

        self.assertEqual(profile, "high")
        first_command = run.call_args_list[0].args[0]
        second_command = run.call_args_list[1].args[0]
        self.assertEqual(first_command[first_command.index("-profile:v") + 1], "main")
        self.assertEqual(second_command[second_command.index("-profile:v") + 1], "high")
        _hardware_encoder_profile.cache_clear()

    def test_nvenc_probe_initializes_cuda_and_selects_nvenc(self) -> None:
        _hardware_encoder_profile.cache_clear()
        with patch(
            "video_converter.converter.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            profile = _hardware_encoder_profile(
                "ffmpeg", "nvenc", "0", "av1", "av1_nvenc"
            )

        self.assertEqual(profile, "main")
        command = run.call_args.args[0]
        self.assertIn("cuda=hw:0", command)
        self.assertIn("av1_nvenc", command)
        _hardware_encoder_profile.cache_clear()

    def test_hardware_candidate_requires_encoder_and_device(self) -> None:
        with patch(
            "video_converter.converter._hardware_devices",
            return_value=(("vaapi", "/dev/dri/renderD128"),),
        ) as device, patch(
            "video_converter.converter._hardware_encoder_profile",
            return_value="main",
        ):
            candidates = _hardware_candidates(
                {"h264_vaapi"}, "h264", "ffmpeg"
            )

        self.assertEqual(
            candidates,
            [("h264_vaapi", "vaapi", "/dev/dri/renderD128", "main")],
        )
        device.assert_called_once_with("ffmpeg")

    def test_hardware_encoder_candidates_include_each_available_backend(self) -> None:
        with patch(
            "video_converter.converter._hardware_devices",
            return_value=(
                ("vaapi", "/dev/dri/renderD128"),
                ("nvenc", "0"),
                ("qsv", "/dev/dri/renderD129"),
            ),
        ), patch(
            "video_converter.converter._hardware_encoder_profile",
            return_value="main",
        ):
            candidates = _hardware_candidates(
                {"h264_vaapi", "h264_qsv", "h264_nvenc"},
                "h264",
                "ffmpeg",
            )

        self.assertEqual(
            candidates,
            [
                ("h264_nvenc", "nvenc", "0", "main"),
                ("h264_qsv", "qsv", "/dev/dri/renderD129", "main"),
                ("h264_vaapi", "vaapi", "/dev/dri/renderD128", "main"),
            ],
        )

    def test_hardware_candidate_requires_encoder_profile_support(self) -> None:
        with patch(
            "video_converter.converter._hardware_devices",
            return_value=(("vaapi", "/dev/dri/renderD128"),),
        ), patch(
            "video_converter.converter._hardware_encoder_profile",
            return_value=None,
        ):
            candidates = _hardware_candidates(
                {"h264_vaapi"},
                "h264",
                "ffmpeg",
            )

        self.assertEqual(candidates, [])

    def test_hardware_devices_detect_vaapi_qsv_and_nvenc(self) -> None:
        _hardware_devices.cache_clear()
        render_device = Path("/dev/dri/renderD128")
        with patch(
            "video_converter.converter.Path.glob",
            return_value=[render_device],
        ), patch(
            "video_converter.converter._ffmpeg_can_initialize_backend",
            side_effect=lambda _ffmpeg, backend, _device: backend != "qsv",
        ):
            devices = _hardware_devices("ffmpeg")

        self.assertEqual(
            devices,
            (("vaapi", str(render_device)), ("nvenc", "0")),
        )
        _hardware_devices.cache_clear()

    def test_hardware_status_requires_a_device(self) -> None:
        settings = make_settings(Path("output"))
        with patch(
            "video_converter.converter._available_encoders",
            return_value={"h264_vaapi", "hevc_vaapi"},
        ), patch(
            "video_converter.converter._hardware_devices",
            return_value=(),
        ):
            status = hardware_status(settings)

        h264 = next(
            item for item in status
            if item["backend"] == "vaapi" and item["codec"] == "h264"
        )
        self.assertFalse(h264["ready"])
        self.assertEqual(h264["reason"], "未检测到可初始化的 VAAPI 设备")

    def test_hardware_status_lists_supported_codecs_and_backends(self) -> None:
        settings = make_settings(Path("output"))
        with patch(
            "video_converter.converter._available_encoders",
            return_value={
                "h264_nvenc",
                "hevc_nvenc",
                "av1_nvenc",
                "h264_qsv",
                "hevc_qsv",
                "av1_qsv",
                "h264_vaapi",
                "hevc_vaapi",
                "av1_vaapi",
            },
        ), patch(
            "video_converter.converter._hardware_devices",
            return_value=(
                ("nvenc", "0"),
                ("qsv", "/dev/dri/renderD128"),
                ("vaapi", "/dev/dri/renderD128"),
            ),
        ), patch(
            "video_converter.converter._hardware_encoder_profile",
            return_value="main",
        ), patch(
            "video_converter.converter._hardware_decoder_works",
            return_value=True,
        ):
            status = hardware_status(settings)

        self.assertEqual({item["backend"] for item in status}, {"vaapi", "qsv", "nvenc"})
        self.assertEqual({item["codec"] for item in status}, {"h264", "h265", "av1"})
        self.assertEqual(len(status), 9)
        h264 = next(
            item for item in status
            if item["backend"] == "vaapi" and item["codec"] == "h264"
        )
        h265 = next(
            item for item in status
            if item["backend"] == "vaapi" and item["codec"] == "h265"
        )
        self.assertTrue(h264["decode_ready"])
        self.assertTrue(h264["encode_ready"])
        self.assertTrue(h265["decode_ready"])

    def test_hardware_decoder_probe_runs_real_decode(self) -> None:
        _hardware_decoder_works.cache_clear()
        with patch(
            "video_converter.converter._software_decode_sample",
            return_value=b"encoded-sample",
        ), patch(
            "video_converter.converter.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            works = _hardware_decoder_works(
                "ffmpeg", "qsv", "/dev/dri/renderD128", "av1"
            )

        self.assertTrue(works)
        command = run.call_args.args[0]
        self.assertIn("qsv=hw@hw_va", command)
        self.assertIn("-hwaccel", command)
        self.assertIn("qsv", command)
        self.assertEqual(run.call_args.kwargs["input"], b"encoded-sample")
        _hardware_decoder_works.cache_clear()


class ConversionTests(unittest.TestCase):
    def test_keeps_source_resolution_when_dimensions_are_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp) / "output")
            with patch(
                "video_converter.converter.subprocess.Popen",
                return_value=SimpleNamespace(
                    stdout=iter([]),
                    wait=lambda: 0,
                ),
            ) as run_ffmpeg:
                _run_ffmpeg(
                    settings,
                    Path(temp) / "input.mp4",
                    Path(temp) / "output.mp4",
                    "libx264",
                )

        command = run_ffmpeg.call_args.args[0]
        self.assertNotIn("-vf", command)

    def test_hardware_filter_downloads_before_software_resize_and_uploads_once(self) -> None:
        settings = make_settings(Path("output"), width=1280, height=720)

        for backend in ("vaapi", "qsv", "nvenc"):
            with self.subTest(backend=backend):
                video_filter = _video_filter(settings, backend)
                upload = "hwupload=extra_hw_frames=64" if backend == "qsv" else "hwupload"
                self.assertEqual(
                    video_filter,
                    "hwdownload,format=nv12,"
                    "scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2"
                    if backend == "nvenc"
                    else (
                        "hwdownload,format=nv12,"
                        "scale=1280:720:force_original_aspect_ratio=decrease,"
                        "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                        f"format=nv12,{upload}"
                    ),
                )

    def test_hardware_filter_is_omitted_without_resize(self) -> None:
        settings = make_settings(Path("output"))

        for backend in ("vaapi", "qsv", "nvenc"):
            self.assertIsNone(_video_filter(settings, backend))

    def test_vaapi_decode_filter_downloads_frames_for_cpu_encoder(self) -> None:
        settings = make_settings(Path("output"))
        self.assertEqual(
            _video_filter(settings, "software", "vaapi"),
            "hwdownload,format=nv12",
        )

    def test_vaapi_decode_filter_applies_cpu_resize_without_upload(self) -> None:
        settings = make_settings(Path("output"), width=1280, height=720)
        self.assertEqual(
            _video_filter(settings, "software", "vaapi"),
            "hwdownload,format=nv12,"
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        )

    def test_progress_uses_out_time_field_without_previous_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp) / "output")
            updates: list[dict[str, object]] = []
            with patch(
                "video_converter.converter.subprocess.Popen",
                return_value=SimpleNamespace(
                    stdout=iter(["out_time=00:00:02.500000\n"]),
                    wait=lambda: 0,
                ),
            ):
                _run_ffmpeg(
                    settings,
                    Path(temp) / "input.mp4",
                    Path(temp) / "output.mp4",
                    "libx264",
                    on_progress=updates.append,
                )

        self.assertEqual(
            updates,
            [{"progress": None, "out_time": "00:00:02.500000"}],
        )

    def test_hardware_decode_command_initializes_device_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp) / "output")
            with patch(
                "video_converter.converter.subprocess.Popen",
                return_value=SimpleNamespace(stdout=iter([]), wait=lambda: 0),
            ) as run_ffmpeg:
                _run_ffmpeg(
                    settings,
                    Path(temp) / "input.mp4",
                    Path(temp) / "output.mp4",
                    "h264_vaapi",
                    "vaapi",
                    "/dev/dri/renderD128",
                )

        command = run_ffmpeg.call_args.args[0]
        self.assertIn("vaapi=hw:/dev/dri/renderD128", command)
        self.assertIn("-hwaccel", command)
        self.assertIn("vaapi", command)
        self.assertIn("-hwaccel_output_format", command)
        self.assertIn("-hwaccel_device", command)
        self.assertIn("-profile:v", command)
        self.assertIn("main", command)

    def test_independent_backends_get_distinct_hardware_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp) / "output")
            with patch(
                "video_converter.converter.subprocess.Popen",
                return_value=SimpleNamespace(stdout=iter([]), wait=lambda: 0),
            ) as run_ffmpeg:
                _run_ffmpeg(
                    settings,
                    Path(temp) / "input.mp4",
                    Path(temp) / "output.mp4",
                    "h264_qsv",
                    "qsv",
                    "/dev/dri/renderD129",
                    decode_backend="vaapi",
                    decode_device="/dev/dri/renderD128",
                )

        command = run_ffmpeg.call_args.args[0]
        self.assertIn("vaapi=encoder_hw_va:/dev/dri/renderD129", command)
        self.assertIn("qsv=encoder_hw@encoder_hw_va", command)
        self.assertIn("vaapi=decoder_hw:/dev/dri/renderD128", command)
        self.assertIn("-hwaccel_device", command)
        self.assertIn("decoder_hw", command)
        input_index = command.index("-i")
        filter_device_indices = [
            index
            for index, value in enumerate(command[:input_index])
            if value == "-filter_hw_device"
        ]
        self.assertEqual(
            command[filter_device_indices[-1] + 1],
            "encoder_hw",
        )

    def test_applies_scale_filter_when_dimensions_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = make_settings(Path(temp) / "output", width=1280, height=720)
            with patch(
                "video_converter.converter.subprocess.Popen",
                return_value=SimpleNamespace(
                    stdout=iter([]),
                    wait=lambda: 0,
                ),
            ) as run_ffmpeg:
                _run_ffmpeg(
                    settings,
                    Path(temp) / "input.mp4",
                    Path(temp) / "output.mp4",
                    "libx264",
                )

        command = run_ffmpeg.call_args.args[0]
        self.assertIn("-vf", command)
        self.assertIn(
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            command,
        )

    def test_uses_available_hardware_encoder_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)

            def encode(_settings, _source, destination, _codec, *_args, **_kwargs):
                Path(destination).write_bytes(b"video")

            with (
                patch("video_converter.converter._available_encoders", return_value={"h264_vaapi", "libx264"}),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("h264_vaapi", "vaapi", "/dev/dri/renderD128", "high")],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(),
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode) as run_ffmpeg,
            ):
                destination = convert_file(settings, source)

            self.assertEqual(destination.read_bytes(), b"video")
            self.assertEqual(run_ffmpeg.call_args.args[3], "h264_vaapi")
            self.assertEqual(run_ffmpeg.call_args.kwargs["encoder_profile"], "high")

    def test_falls_back_to_cpu_when_hardware_encoding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)
            calls: list[str] = []

            def encode(_settings, _source, destination, codec, *_args, **_kwargs):
                calls.append(codec)
                if codec == "h264_vaapi":
                    Path(destination).write_bytes(b"incomplete")
                    raise RuntimeError("hardware encoder unavailable")
                Path(destination).write_bytes(b"software video")

            with (
                patch("video_converter.converter._available_encoders", return_value={"h264_vaapi", "libx264"}),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("h264_vaapi", "vaapi", "/dev/dri/renderD128", "main")],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(),
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["h264_vaapi", "libx264"])
            self.assertEqual(destination.read_bytes(), b"software video")
            self.assertEqual(list(output.glob("*.partial.mp4")), [])

    def test_uses_hardware_decode_when_hardware_encoding_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)

            def encode(_settings, _source, destination, codec, backend="software", *_args, **_kwargs):
                Path(destination).write_bytes(b"video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"libx264", "h264_vaapi"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(("vaapi", "/dev/dri/renderD128"),),
                ),
                patch(
                    "video_converter.converter._hardware_decoder_candidates",
                    return_value=[],
                ) as codec_specific_candidates,
                patch(
                    "video_converter.converter._run_ffmpeg",
                    side_effect=encode,
                ) as run_ffmpeg,
            ):
                destination = convert_file(settings, source)

            self.assertEqual(destination.read_bytes(), b"video")
            self.assertEqual(run_ffmpeg.call_args.args[3], "libx264")
            self.assertEqual(run_ffmpeg.call_args.args[4], "software")
            self.assertEqual(run_ffmpeg.call_args.kwargs["decode_backend"], "vaapi")
            codec_specific_candidates.assert_not_called()

    def test_falls_back_to_cpu_decode_when_hardware_decode_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)
            calls: list[str] = []

            def encode(_settings, _source, destination, codec, backend="software", *_args, **kwargs):
                calls.append(kwargs["decode_backend"])
                if kwargs["decode_backend"] == "vaapi":
                    raise RuntimeError("unsupported input profile")
                Path(destination).write_bytes(b"software video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"libx264", "h264_vaapi"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(("vaapi", "/dev/dri/renderD128"),),
                ),
                patch(
                    "video_converter.converter._run_ffmpeg",
                    side_effect=encode,
                ),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["vaapi", "software"])
            self.assertEqual(destination.read_bytes(), b"software video")

    def test_tries_vaapi_before_cpu_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)
            calls: list[str] = []

            def encode(_settings, _source, destination, codec, *_args, **_kwargs):
                calls.append(codec)
                if codec == "h264_vaapi":
                    raise RuntimeError("VAAPI initialization failed")
                Path(destination).write_bytes(b"software video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"h264_vaapi", "libx264"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("h264_vaapi", "vaapi", "/dev/dri/renderD128", "main")],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(),
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["h264_vaapi", "libx264"])
            self.assertEqual(destination.read_bytes(), b"software video")

    def test_never_overwrites_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            destination = output / "clip.mp4"
            destination.write_bytes(b"existing")

            with self.assertRaises(FileExistsError):
                convert_file(make_settings(output), source)

            self.assertEqual(destination.read_bytes(), b"existing")

    def test_h265_hardware_failure_falls_back_to_libx265(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output, video_codec="h265")
            calls: list[str] = []

            def encode(_settings, _source, destination, codec, *_args, **_kwargs):
                calls.append(codec)
                if codec == "hevc_vaapi":
                    raise RuntimeError("hardware encoder unavailable")
                Path(destination).write_bytes(b"software video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"hevc_vaapi", "libx265"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("hevc_vaapi", "vaapi", "/dev/dri/renderD128", "main")],
                ),
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(),
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["hevc_vaapi", "libx265"])
            self.assertEqual(destination.read_bytes(), b"software video")

    def test_explicit_encoder_skips_automatic_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output, video_codec="libx265")

            def encode(_settings, _source, destination, _codec, *_args, **_kwargs):
                Path(destination).write_bytes(b"video")

            with (
                patch("video_converter.converter._available_encoders") as detect,
                patch("video_converter.converter._hardware_devices", return_value=()),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                convert_file(settings, source)

            detect.assert_not_called()

    def test_explicit_hardware_encoder_uses_independent_hardware_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output, video_codec="h264_vaapi")

            decode_backends: list[str] = []

            def encode(_settings, _source, destination, *_args, **kwargs):
                decode_backends.append(kwargs["decode_backend"])
                if kwargs["decode_backend"] == "vaapi":
                    raise RuntimeError("input codec unavailable on VAAPI")
                Path(destination).write_bytes(b"video")

            with (
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(
                        ("vaapi", "/dev/dri/renderD128"),
                        ("nvenc", "0"),
                    ),
                ),
                patch("video_converter.converter._available_encoders") as detect,
                patch(
                    "video_converter.converter._run_ffmpeg",
                    side_effect=encode,
                ) as run_ffmpeg,
            ):
                convert_file(settings, source)

            detect.assert_not_called()
            self.assertEqual(run_ffmpeg.call_args.args[4], "vaapi")
            self.assertEqual(run_ffmpeg.call_args.args[5], "/dev/dri/renderD128")
            self.assertEqual(run_ffmpeg.call_args.kwargs["decode_backend"], "nvenc")
            self.assertEqual(run_ffmpeg.call_args.kwargs["decode_device"], "0")
            self.assertEqual(decode_backends, ["vaapi", "nvenc"])

    def test_explicit_encoder_falls_back_to_cpu_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output, video_codec="h264_vaapi")
            attempted_decoders: list[str] = []

            def encode(_settings, _source, destination, *_args, **kwargs):
                backend = kwargs["decode_backend"]
                attempted_decoders.append(backend)
                if backend == "vaapi":
                    raise RuntimeError("unsupported source codec")
                Path(destination).write_bytes(b"video")

            with (
                patch(
                    "video_converter.converter._hardware_devices",
                    return_value=(("vaapi", "/dev/dri/renderD128"),),
                ),
                patch("video_converter.converter._available_encoders") as detect,
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            detect.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"video")
            self.assertEqual(attempted_decoders, ["vaapi", "software"])

    def test_explicit_nvenc_and_qsv_encoder_names_use_hardware(self) -> None:
        for backend, device in (("nvenc", "0"), ("qsv", "/dev/dri/renderD128")):
            codec = "h264_" + backend
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / "output"
                output.mkdir()
                source = root / "clip.mov"
                source.touch()
                settings = make_settings(output, video_codec=codec)

                def encode(_settings, _source, destination, *_args, **_kwargs):
                    Path(destination).write_bytes(b"video")

                with (
                    patch(
                        "video_converter.converter._hardware_devices",
                        return_value=((backend, device),),
                    ),
                    patch(
                        "video_converter.converter._run_ffmpeg",
                        side_effect=encode,
                    ) as run_ffmpeg,
                ):
                    convert_file(settings, source)

                self.assertEqual(run_ffmpeg.call_args.args[4], backend)
                self.assertEqual(run_ffmpeg.call_args.args[5], device)


if __name__ == "__main__":
    unittest.main()

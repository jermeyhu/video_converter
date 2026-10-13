import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_converter.config import Settings
from video_converter.converter import (
    _available_encoders,
    _hardware_candidates,
    _hardware_device,
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
                    " V..... h264_nvenc\n"
                    " A..... aac\n"
                ),
                stderr="",
            ),
        ):
            encoders = _available_encoders("ffmpeg")

        self.assertEqual(encoders, {"libx264", "h264_nvenc", "aac"})

    def test_hardware_candidate_requires_encoder_and_device(self) -> None:
        with (
            patch("video_converter.converter._hardware_device", return_value="/dev/dri/renderD128") as device,
        ):
            candidates = _hardware_candidates({"h264_qsv"}, "h264", "ffmpeg")

        self.assertEqual(candidates, [("h264_qsv", "qsv", "/dev/dri/renderD128")])
        device.assert_called_once_with("qsv", "ffmpeg")

    def test_h265_candidates_use_hevc_encoders(self) -> None:
        with patch(
            "video_converter.converter._hardware_device",
            side_effect=lambda backend, _ffmpeg_bin: {
                "nvidia": "nvidia",
                "qsv": "/dev/dri/renderD128",
                "vaapi": "/dev/dri/renderD128",
            }[backend],
        ):
            candidates = _hardware_candidates(
                {"hevc_nvenc", "hevc_qsv", "hevc_vaapi", "h264_nvenc"},
                "h265",
                "ffmpeg",
            )

        self.assertEqual(
            candidates,
            [
                ("hevc_nvenc", "nvidia", None),
                ("hevc_qsv", "qsv", "/dev/dri/renderD128"),
                ("hevc_vaapi", "vaapi", "/dev/dri/renderD128"),
            ],
        )

    def test_nvidia_requires_exposed_device(self) -> None:
        with patch("video_converter.converter.Path.exists", return_value=False), patch(
            "video_converter.converter.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertIsNone(_hardware_device("nvidia"))

    def test_qsv_is_not_available_when_device_initialization_fails(self) -> None:
        with patch("video_converter.converter.Path.glob", return_value=[]), patch(
            "video_converter.converter._ffmpeg_can_initialize_device",
            return_value=False,
        ):
            self.assertIsNone(_hardware_device("qsv", "ffmpeg"))

        with patch("video_converter.converter.os.name", "nt"), patch(
            "video_converter.converter._hardware_device",
            return_value=None,
        ):
            candidates = _hardware_candidates({"h264_qsv"}, "h264", "ffmpeg")

        self.assertEqual(candidates, [])

    def test_hardware_status_requires_a_device(self) -> None:
        settings = make_settings(Path("output"))
        with patch(
            "video_converter.converter._available_encoders",
            return_value={"h264_nvenc"},
        ), patch(
            "video_converter.converter._hardware_device",
            return_value=None,
        ):
            status = hardware_status(settings)

        nvidia = next(
            item for item in status
            if item["backend"] == "nvidia" and item["codec"] == "h264"
        )
        self.assertFalse(nvidia["ready"])
        self.assertEqual(nvidia["reason"], "未检测到可用 GPU 设备")


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

        for backend in ("nvidia", "qsv", "vaapi"):
            with self.subTest(backend=backend):
                video_filter = _video_filter(settings, backend)
                self.assertEqual(
                    video_filter,
                    "hwdownload,format=nv12,"
                    "scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                    "format=nv12,hwupload",
                )
                self.assertNotIn("pad_cuda", video_filter)
                self.assertNotIn("pad_qsv", video_filter)
                self.assertNotIn("pad_vaapi", video_filter)

    def test_hardware_filter_is_omitted_without_resize(self) -> None:
        settings = make_settings(Path("output"))

        for backend in ("nvidia", "qsv", "vaapi"):
            with self.subTest(backend=backend):
                self.assertIsNone(_video_filter(settings, backend))

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
            device_for = {
                "nvidia": None,
                "qsv": "/dev/dri/renderD128",
                "vaapi": "/dev/dri/renderD129",
            }
            expected = {
                "nvidia": ["-init_hw_device", "cuda=hw:0", "-filter_hw_device", "hw"],
                "qsv": [
                    "-init_hw_device",
                    "qsv=hw,child_device=/dev/dri/renderD128",
                    "-filter_hw_device",
                    "hw",
                ],
                "vaapi": [
                    "-init_hw_device",
                    "vaapi=hw:/dev/dri/renderD129",
                    "-filter_hw_device",
                    "hw",
                ],
            }
            for backend, device in device_for.items():
                with self.subTest(backend=backend), patch(
                    "video_converter.converter.subprocess.Popen",
                    return_value=SimpleNamespace(stdout=iter([]), wait=lambda: 0),
                ) as run_ffmpeg:
                    _run_ffmpeg(
                        settings,
                        Path(temp) / "input.mp4",
                        Path(temp) / "output.mp4",
                        f"{backend}_encoder",
                        backend,
                        device,
                    )

                command = run_ffmpeg.call_args.args[0]
                for expected_arg in expected[backend]:
                    self.assertIn(expected_arg, command)
                self.assertIn("-hwaccel", command)
                self.assertIn("-hwaccel_output_format", command)

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
                patch("video_converter.converter._available_encoders", return_value={"h264_nvenc", "libx264"}),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("h264_nvenc", "nvidia", None)],
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode) as run_ffmpeg,
            ):
                destination = convert_file(settings, source)

            self.assertEqual(destination.read_bytes(), b"video")
            self.assertEqual(run_ffmpeg.call_args.args[3], "h264_nvenc")

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
                if codec == "h264_nvenc":
                    Path(destination).write_bytes(b"incomplete")
                    raise RuntimeError("hardware encoder unavailable")
                Path(destination).write_bytes(b"software video")

            with (
                patch("video_converter.converter._available_encoders", return_value={"h264_nvenc", "libx264"}),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("h264_nvenc", "nvidia", None)],
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["h264_nvenc", "libx264"])
            self.assertEqual(destination.read_bytes(), b"software video")
            self.assertEqual(list(output.glob("*.partial.mp4")), [])

    def test_tries_next_hardware_backend_before_cpu_fallback(self) -> None:
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
                if codec == "h264_nvenc":
                    raise RuntimeError("NVIDIA initialization failed")
                Path(destination).write_bytes(b"QSV video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"h264_nvenc", "h264_qsv", "libx264"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[
                        ("h264_nvenc", "nvidia", None),
                        ("h264_qsv", "qsv", "/dev/dri/renderD128"),
                    ],
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["h264_nvenc", "h264_qsv"])
            self.assertEqual(destination.read_bytes(), b"QSV video")

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
                if codec == "hevc_nvenc":
                    raise RuntimeError("hardware encoder unavailable")
                Path(destination).write_bytes(b"software video")

            with (
                patch(
                    "video_converter.converter._available_encoders",
                    return_value={"hevc_nvenc", "libx265"},
                ),
                patch(
                    "video_converter.converter._hardware_candidates",
                    return_value=[("hevc_nvenc", "nvidia", None)],
                ),
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                destination = convert_file(settings, source)

            self.assertEqual(calls, ["hevc_nvenc", "libx265"])
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
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                convert_file(settings, source)

            detect.assert_not_called()

    def test_explicit_hardware_encoder_enables_hardware_decode(self) -> None:
        for codec, backend, device in (
            ("h264_nvenc", "nvidia", "nvidia"),
            ("h264_qsv", "qsv", "/dev/dri/renderD128"),
            ("h264_vaapi", "vaapi", "/dev/dri/renderD128"),
        ):
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
                        "video_converter.converter._hardware_device",
                        return_value=device,
                    ),
                    patch("video_converter.converter._available_encoders") as detect,
                    patch(
                        "video_converter.converter._run_ffmpeg",
                        side_effect=encode,
                    ) as run_ffmpeg,
                ):
                    convert_file(settings, source)

                detect.assert_not_called()
                self.assertEqual(run_ffmpeg.call_args.args[4], backend)
                self.assertEqual(run_ffmpeg.call_args.args[5], None if backend == "nvidia" else device)


if __name__ == "__main__":
    unittest.main()

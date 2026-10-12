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
    convert_file,
)


def make_settings(output_dir: Path, video_codec: str = "auto") -> Settings:
    return Settings(
        input_dir=output_dir.parent / "input",
        output_dir=output_dir,
        output_format="mp4",
        video_codec=video_codec,
        audio_codec="aac",
        width=1920,
        height=1080,
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
            candidates = _hardware_candidates({"h264_qsv"})

        self.assertEqual(candidates, [("h264_qsv", "qsv", "/dev/dri/renderD128")])
        device.assert_called_once_with("qsv")

    def test_nvidia_requires_exposed_device(self) -> None:
        with patch("video_converter.converter.Path.exists", return_value=False), patch(
            "video_converter.converter.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertIsNone(_hardware_device("nvidia"))


class ConversionTests(unittest.TestCase):
    def test_uses_available_hardware_encoder_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output)

            def encode(_settings, _source, destination, _codec, *_args):
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

            def encode(_settings, _source, destination, codec, *_args):
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

    def test_explicit_encoder_skips_automatic_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            source = root / "clip.mov"
            source.touch()
            settings = make_settings(output, video_codec="libx265")

            def encode(_settings, _source, destination, _codec, *_args):
                Path(destination).write_bytes(b"video")

            with (
                patch("video_converter.converter._available_encoders") as detect,
                patch("video_converter.converter._run_ffmpeg", side_effect=encode),
            ):
                convert_file(settings, source)

            detect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

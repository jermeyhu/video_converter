import os
import unittest
from unittest.mock import patch

from video_converter.config import load_settings


class LoadSettingsTests(unittest.TestCase):
    def test_defaults_preserve_resolution_and_use_single_worker(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings()

        self.assertEqual(settings.max_concurrent_conversions, 1)
        self.assertEqual(settings.video_codec, "h264")
        self.assertEqual(settings.output_format, "mp4")
        self.assertEqual(settings.web_port, 5800)
        self.assertIsNone(settings.width)
        self.assertIsNone(settings.height)

    def test_environment_configures_concurrency_and_resolution(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAX_CONCURRENT_CONVERSIONS": "3",
                "OUTPUT_WIDTH": "1280",
                "OUTPUT_HEIGHT": "720",
                "OUTPUT_FORMAT": ".mkv",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.max_concurrent_conversions, 3)
        self.assertEqual((settings.width, settings.height), (1280, 720))
        self.assertEqual(settings.output_format, "mkv")

    def test_configures_web_port_from_environment(self) -> None:
        with patch.dict(os.environ, {"WEB_PORT": "8080"}, clear=True):
            settings = load_settings()

        self.assertEqual(settings.web_port, 8080)

    def test_rejects_web_port_outside_tcp_range(self) -> None:
        with patch.dict(os.environ, {"WEB_PORT": "65536"}, clear=True):
            with self.assertRaisesRegex(ValueError, "WEB_PORT must be between 1 and 65535"):
                load_settings()

    def test_selects_h265_profile_from_environment(self) -> None:
        with patch.dict(os.environ, {"VIDEO_CODEC": "h265"}, clear=True):
            settings = load_settings()

        self.assertEqual(settings.video_codec, "h265")

    def test_rejects_same_input_and_output_directory(self) -> None:
        with patch.dict(
            os.environ,
            {"INPUT_DIR": "same", "OUTPUT_DIR": "same"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must be different"):
                load_settings()

    def test_rejects_invalid_concurrency(self) -> None:
        with patch.dict(os.environ, {"MAX_CONCURRENT_CONVERSIONS": "0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "MAX_CONCURRENT_CONVERSIONS"):
                load_settings()

    def test_rejects_odd_output_dimensions(self) -> None:
        with patch.dict(os.environ, {"OUTPUT_WIDTH": "1279"}, clear=True):
            with self.assertRaisesRegex(ValueError, "OUTPUT_WIDTH must be an even integer for video encoding"):
                load_settings()

    def test_requires_both_output_dimensions(self) -> None:
        with patch.dict(os.environ, {"OUTPUT_WIDTH": "1280"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must both be set"):
                load_settings()

    def test_rejects_non_finite_poll_interval(self) -> None:
        with patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "nan"}, clear=True):
            with self.assertRaisesRegex(ValueError, "POLL_INTERVAL_SECONDS"):
                load_settings()


if __name__ == "__main__":
    unittest.main()

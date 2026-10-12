import os
import unittest
from unittest.mock import patch

from video_converter.config import load_settings


class LoadSettingsTests(unittest.TestCase):
    def test_defaults_include_single_worker_and_automatic_codec(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings()

        self.assertEqual(settings.max_concurrent_conversions, 1)
        self.assertEqual(settings.video_codec, "auto")
        self.assertEqual(settings.output_format, "mp4")
        self.assertEqual((settings.width, settings.height), (1920, 1080))

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
            with self.assertRaisesRegex(ValueError, "OUTPUT_WIDTH must be an even integer"):
                load_settings()

    def test_rejects_non_finite_poll_interval(self) -> None:
        with patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "nan"}, clear=True):
            with self.assertRaisesRegex(ValueError, "POLL_INTERVAL_SECONDS"):
                load_settings()


if __name__ == "__main__":
    unittest.main()

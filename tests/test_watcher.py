import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_converter.config import Settings
from video_converter.watcher import _move_to_processed, run


class WatcherQueueTests(unittest.TestCase):
    def test_archiving_does_not_overwrite_processed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            input_dir = Path(temp) / "input"
            processed_dir = input_dir / "processed"
            processed_dir.mkdir(parents=True)
            source = input_dir / "clip.mp4"
            source.write_text("new source")
            (processed_dir / "clip.mp4").write_text("previous archive")

            archived = _move_to_processed(source)

            self.assertEqual(archived.name, "clip (1).mp4")
            self.assertEqual(archived.read_text(), "new source")
            self.assertEqual(
                (processed_dir / "clip.mp4").read_text(),
                "previous archive",
            )

    def test_conversion_workers_are_bounded_and_excess_files_are_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            for index in range(4):
                (input_dir / f"clip-{index}.mp4").touch()

            settings = Settings(
                input_dir=input_dir,
                output_dir=output_dir,
                output_format="mp4",
                video_codec="auto",
                audio_codec="aac",
                width=1920,
                height=1080,
                max_concurrent_conversions=2,
                poll_interval_seconds=0.005,
                file_stability_seconds=0.01,
                ffmpeg_bin="ffmpeg",
            )
            stop = threading.Event()
            all_started = threading.Event()
            lock = threading.Lock()
            active = 0
            maximum_active = 0
            completed = 0

            def convert(_settings: Settings, source: Path, **_kwargs: object) -> Path:
                nonlocal active, maximum_active, completed
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                    completed += 1
                    if completed == 4:
                        all_started.set()
                return output_dir / source.name

            with patch("video_converter.watcher.convert_file", side_effect=convert):
                watcher = threading.Thread(target=run, args=(settings, stop))
                watcher.start()
                try:
                    self.assertTrue(all_started.wait(timeout=3), "not all queued jobs completed")
                finally:
                    stop.set()
                    watcher.join(timeout=3)

            self.assertFalse(watcher.is_alive(), "watcher did not stop")
            self.assertEqual(completed, 4)
            self.assertEqual(maximum_active, 2)
            self.assertEqual(
                sorted(path.name for path in (input_dir / "processed").iterdir()),
                [f"clip-{index}.mp4" for index in range(4)],
            )
            self.assertEqual(
                list(input_dir.glob("*.mp4")),
                [],
            )


if __name__ == "__main__":
    unittest.main()

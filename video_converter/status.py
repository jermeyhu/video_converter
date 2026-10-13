import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StatusStore:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._hardware: list[dict[str, Any]] = []
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._stopping = False

    def set_hardware(self, hardware: list[dict[str, Any]]) -> None:
        with self._lock:
            self._hardware = hardware

    def set_stopping(self) -> None:
        with self._lock:
            self._stopping = True

    def queued(self, source: Path) -> None:
        with self._lock:
            self._jobs[str(source)] = {
                "file": source.name,
                "path": str(source),
                "status": "queued",
                "progress": None,
                "encoder": None,
                "backend": None,
                "out_time": None,
                "error": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def update(self, source: Path, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.setdefault(str(source), {"file": source.name, "path": str(source)})
            job.update(changes)
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            if job.get("status") in {"completed", "failed"}:
                finished = [
                    key for key, item in self._jobs.items()
                    if item.get("status") in {"completed", "failed"}
                ]
                for key in finished[:-50]:
                    del self._jobs[key]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs.values()]
            return {
                "running": not self._stopping,
                "started_at": self._started_at,
                "input_dir": str(self._settings.input_dir),
                "output_dir": str(self._settings.output_dir),
                "video_codec": self._settings.video_codec,
                "max_concurrent_conversions": self._settings.max_concurrent_conversions,
                "hardware": [dict(item) for item in self._hardware],
                "jobs": jobs,
                "counts": {
                    status: sum(job.get("status") == status for job in jobs)
                    for status in ("queued", "running", "completed", "failed")
                },
            }

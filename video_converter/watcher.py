import logging
import signal
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

from video_converter.config import Settings
from video_converter.converter import VIDEO_EXTENSIONS, convert_file, hardware_status
from video_converter.status import StatusStore
from video_converter.web import start_server

LOGGER = logging.getLogger(__name__)


def _move_to_processed(source: Path) -> Path:
    processed_dir = source.parent / "processed"
    processed_dir.mkdir(exist_ok=True)
    destination = processed_dir / source.name
    suffix = 1
    while destination.exists():
        destination = processed_dir / f"{source.stem} ({suffix}){source.suffix}"
        suffix += 1
    source.rename(destination)
    LOGGER.info("Moved converted input %s -> %s", source, destination)
    return destination


def run(
    settings: Settings,
    stop_event: Event | None = None,
    status: StatusStore | None = None,
) -> None:
    stop = stop_event or Event()
    status_store = status or StatusStore(settings)
    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    stability: dict[Path, tuple[tuple[int, int], float]] = {}
    submitted: dict[Path, tuple[int, int]] = {}
    futures: dict[Future[Path], Path] = {}
    executor = ThreadPoolExecutor(
        max_workers=settings.max_concurrent_conversions,
        thread_name_prefix="video-converter",
    )
    server, web_thread = start_server("0.0.0.0", settings.web_port, status_store)
    status_store.set_hardware(hardware_status(settings))
    LOGGER.info(
        "Watching %s; output=%s; concurrent conversions=%d; status page=http://0.0.0.0:%d",
        settings.input_dir,
        settings.output_dir,
        settings.max_concurrent_conversions,
        settings.web_port,
    )

    def convert_with_status(source: Path) -> Path:
        status_store.update(source, status="running", progress=0.0)

        def on_progress(update: dict[str, object]) -> None:
            status_store.update(source, status="running", **update)

        try:
            destination = convert_file(settings, source, on_progress=on_progress)
            _move_to_processed(source)
        except Exception as exc:
            status_store.update(source, status="failed", error=str(exc), progress=None)
            raise
        status_store.update(source, status="completed", progress=100.0, error=None)
        return destination

    try:
        while not stop.is_set():
            now = time.monotonic()
            present: set[Path] = set()
            for source in settings.input_dir.iterdir():
                if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                present.add(source)
                try:
                    stat = source.stat()
                except OSError:
                    LOGGER.exception("Could not inspect input file %s", source)
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)

                if stability.get(source, (None, 0))[0] != signature:
                    stability[source] = (signature, now)
                    continue
                if now - stability[source][1] < settings.file_stability_seconds:
                    continue
                if source in futures.values():
                    continue
                if submitted.get(source) == signature:
                    continue

                destination = settings.output_dir / f"{source.stem}.{settings.output_format}"
                if destination.exists():
                    LOGGER.info("Skipping %s because output already exists: %s", source, destination)
                    submitted[source] = signature
                    continue

                LOGGER.info("Queued %s", source)
                status_store.queued(source)
                future = executor.submit(convert_with_status, source)
                futures[future] = source
                submitted[source] = signature

            for source in set(stability) - present:
                stability.pop(source, None)
                submitted.pop(source, None)

            for future, source in list(futures.items()):
                if not future.done():
                    continue
                futures.pop(future)
                try:
                    future.result()
                except Exception:
                    LOGGER.exception("Conversion failed for %s", source)

            stop.wait(settings.poll_interval_seconds)
    finally:
        status_store.set_stopping()
        executor.shutdown(wait=True, cancel_futures=False)
        server.shutdown()
        server.server_close()
        web_thread.join(timeout=2)


def main() -> None:
    from video_converter.config import load_settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; stopping after queued conversions finish", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        run(load_settings(), stop)
    except (ValueError, OSError) as exc:
        LOGGER.error("Service configuration or startup failed: %s", exc)
        raise SystemExit(2) from exc

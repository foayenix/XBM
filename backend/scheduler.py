"""Optional background sync, off by default.

Enable by setting SYNC_INTERVAL_MINUTES in .env to a positive number. Runs
sync + enrichment on that interval in a daemon thread. A failed run (rate
limit, network blip, expired token) is logged and never crashes the app or
the thread — it just tries again on the next interval.
"""
import logging
import threading

from backend import config, jobs

logger = logging.getLogger("xbm.scheduler")

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _run_loop(interval_minutes: float) -> None:
    logger.info("Scheduled sync enabled: running every %s minutes", interval_minutes)
    while not _stop_event.wait(interval_minutes * 60):
        # Going through the shared job means scheduled runs show live in the
        # web UI and can never overlap a manual "sync now". Failures are
        # captured (and logged) by the job itself; the loop just keeps going.
        if not jobs.run_blocking():
            logger.info("Scheduled sync skipped: another sync is already running")


def start() -> None:
    global _thread
    if config.SYNC_INTERVAL_MINUTES <= 0:
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run_loop, args=(config.SYNC_INTERVAL_MINUTES,), daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)

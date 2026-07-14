"""Optional background sync, off by default.

Enable by setting SYNC_INTERVAL_MINUTES in .env to a positive number. Runs
sync + enrichment on that interval in a daemon thread. A failed run (rate
limit, network blip, expired token) is logged and never crashes the app or
the thread — it just tries again on the next interval.
"""
import logging
import threading

from backend import config
from backend.enrich import run_enrichment
from backend.sync import sync_bookmarks

logger = logging.getLogger("xbm.scheduler")

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _run_loop(interval_minutes: float) -> None:
    logger.info("Scheduled sync enabled: running every %s minutes", interval_minutes)
    while not _stop_event.wait(interval_minutes * 60):
        try:
            sync_summary = sync_bookmarks()
            enrich_summary = run_enrichment()
            logger.info("Scheduled sync complete: %s | %s", sync_summary, enrich_summary)
        except Exception:
            logger.exception("Scheduled sync run failed; will retry on the next interval")


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

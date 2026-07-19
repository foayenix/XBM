"""Background sync job with live progress, shared by the web UI and scheduler.

Exactly one sync+enrich job can run at a time (guarded by _lock), so a
manual "sync now" click can't overlap a scheduled run — both would hit the
X API and bill reads twice for the same data. Progress is a plain dict
snapshot the UI polls via GET /api/sync/status.
"""
import logging
import threading
from datetime import datetime, timezone

from backend.enrich import run_enrichment
from backend.sync import sync_bookmarks

logger = logging.getLogger("xbm.jobs")

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "phase": "idle",  # idle | sync | enrich | done | error
    "started_at": None,
    "finished_at": None,
    "error": None,
    # sync phase: bookmarks processed so far (no total — the X API doesn't
    # report one ahead of pagination)
    "fetched": 0,
    "new": 0,
    "updated": 0,
    # enrich phase: known totals, so the UI can draw a determinate bar
    "enrich_done": 0,
    "enrich_total": 0,
    "tag_done": 0,
    "tag_total": 0,
    "result": None,  # combined summary dict once phase == done
}


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def _update(**kw) -> None:
    with _lock:
        _state.update(kw)


def _begin() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            running=True,
            phase="sync",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            error=None,
            result=None,
            fetched=0,
            new=0,
            updated=0,
            enrich_done=0,
            enrich_total=0,
            tag_done=0,
            tag_total=0,
        )
    return True


def _execute() -> None:
    try:
        sync_summary = sync_bookmarks(progress=_update)
        _update(phase="enrich")
        enrich_summary = run_enrichment(progress=_update)
        _update(
            running=False,
            phase="done",
            finished_at=datetime.now(timezone.utc).isoformat(),
            result={**sync_summary, **enrich_summary},
        )
        logger.info("Sync job complete: %s | %s", sync_summary, enrich_summary)
    except Exception as e:
        logger.exception("Sync job failed")
        _update(
            running=False,
            phase="error",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )


def start_background() -> bool:
    """Start a sync+enrich job in a daemon thread.

    Returns False (without starting anything) if a job is already running.
    """
    if not _begin():
        return False
    threading.Thread(target=_execute, daemon=True, name="xbm-sync-job").start()
    return True


def run_blocking() -> bool:
    """Run a sync+enrich job on the calling thread (used by the scheduler).

    Progress is still published to the shared state, so the UI shows
    scheduled runs too. Returns False if a job is already running. Errors
    are captured into the job state, not raised.
    """
    if not _begin():
        return False
    _execute()
    return True

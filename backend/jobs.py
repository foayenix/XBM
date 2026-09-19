"""Mutual exclusion for the sync and enrichment jobs.

Sync and enrichment both write to the same SQLite database and both spend
real money against the X and Anthropic APIs, so only one may run at a time.
Without this, the background scheduler waking up mid-way through a manual
"Sync now" produces two writers on one database ("database is locked") and,
worse, two concurrent OAuth token refreshes — X rotates refresh tokens, so
the loser's stored token is silently invalidated and you get logged out.

The lock is re-entrant so the scheduler can hold it across sync + enrichment
as a single unit while the functions it calls each acquire it again on the
same thread. A *different* thread never waits: it fails fast with
JobBusyError, which the API layer turns into a 409 rather than piling up
blocked requests.
"""
import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger("xbm.jobs")

_lock = threading.RLock()
_current_job: str | None = None


class JobBusyError(Exception):
    """Raised when another sync/enrichment job is already running."""


@contextmanager
def exclusive(job_name: str):
    global _current_job
    # blocking=False: fail fast instead of queueing up behind a job that may
    # take minutes. RLock means a re-entrant acquire on the same thread wins.
    if not _lock.acquire(blocking=False):
        raise JobBusyError(
            f"Another job ({_current_job or 'unknown'}) is already running. "
            "Wait for it to finish and try again."
        )
    previous = _current_job
    _current_job = job_name
    try:
        yield
    finally:
        _current_job = previous
        _lock.release()


def current_job() -> str | None:
    return _current_job

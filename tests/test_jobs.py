"""Blocker 3: sync and enrichment had no mutual exclusion.

Two overlapping runs meant two writers on one SQLite file ("database is
locked") and, worse, two concurrent OAuth refreshes - X rotates refresh
tokens, so the loser's stored token is silently invalidated.
"""
import threading

import pytest

from backend import jobs
from backend.jobs import JobBusyError


@pytest.fixture(autouse=True)
def _clean_lock():
    """Guarantee the process-wide lock is free before and after each test."""
    yield
    while jobs._lock._is_owned():
        jobs._lock.release()
    jobs._current_job = None


def test_second_thread_is_rejected_immediately():
    held = threading.Event()
    release = threading.Event()
    outcome = {}

    def holder():
        with jobs.exclusive("sync"):
            held.set()
            release.wait(timeout=5)

    def contender():
        held.wait(timeout=5)
        try:
            with jobs.exclusive("enrichment"):
                outcome["result"] = "acquired"
        except JobBusyError as e:
            outcome["result"] = "rejected"
            outcome["message"] = str(e)

    t1, t2 = threading.Thread(target=holder), threading.Thread(target=contender)
    t1.start(); t2.start()
    t2.join(timeout=5)
    release.set()
    t1.join(timeout=5)

    assert outcome["result"] == "rejected"
    assert "sync" in outcome["message"]  # names the job that is holding it


def test_rejection_does_not_block_and_wait():
    """A busy job fails fast rather than queueing behind a multi-minute run."""
    done = threading.Event()

    def holder():
        with jobs.exclusive("sync"):
            done.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        import time
        while jobs.current_job() is None:
            time.sleep(0.01)
        start = time.monotonic()
        with pytest.raises(JobBusyError):
            with jobs.exclusive("enrichment"):
                pass
        assert time.monotonic() - start < 1.0
    finally:
        done.set()
        t.join(timeout=5)


def test_same_thread_can_reacquire():
    """The scheduler holds the lock across sync + enrich, which each re-acquire."""
    with jobs.exclusive("scheduled sync"):
        with jobs.exclusive("sync"):
            with jobs.exclusive("enrichment"):
                assert jobs.current_job() == "enrichment"
        assert jobs.current_job() == "scheduled sync"
    assert jobs.current_job() is None


def test_lock_is_released_after_an_exception():
    with pytest.raises(ValueError):
        with jobs.exclusive("sync"):
            raise ValueError("boom")
    assert jobs.current_job() is None
    with jobs.exclusive("sync"):  # must not deadlock
        pass


def test_sync_endpoint_returns_409_while_a_job_runs(client):
    with jobs.exclusive("scheduled sync"):
        r = client.post("/api/sync")
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


def test_enrich_endpoint_returns_409_while_a_job_runs(client):
    with jobs.exclusive("scheduled sync"):
        r = client.post("/api/enrich")
    assert r.status_code == 409


def test_job_status_endpoint_reports_the_running_job(client):
    assert client.get("/api/jobs/status").json() == {"running": False, "job": None}
    with jobs.exclusive("sync"):
        assert client.get("/api/jobs/status").json() == {"running": True, "job": "sync"}


def test_scheduler_skips_a_run_when_a_manual_job_holds_the_lock(monkeypatch, caplog):
    """The scheduled run logs and waits instead of colliding with a manual sync."""
    from backend import scheduler

    calls = []
    monkeypatch.setattr(scheduler, "sync_bookmarks", lambda: calls.append("sync") or {})
    monkeypatch.setattr(scheduler, "run_enrichment", lambda: calls.append("enrich") or {})

    stop = threading.Event()
    monkeypatch.setattr(scheduler, "_stop_event", stop)

    with jobs.exclusive("sync"):
        t = threading.Thread(target=scheduler._run_loop, args=(0.0001,))
        t.start()
        import time
        time.sleep(0.3)
        stop.set()
        t.join(timeout=5)

    assert calls == []  # never ran while the manual job held the lock

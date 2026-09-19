"""Blocker 5: thread expansion re-ran on every sync.

A tweet's reply chain is immutable, but sync re-walked it for every
bookmarked reply on every run - one billed X API read per ancestor, per
sync, forever. The README even claimed re-syncing would not re-bill.
"""
import pytest

from backend import sync
from backend.x_client import XClient

TWEETS = {
    # A three-tweet self-authored thread by author "a1".
    "300": {"id": "300", "author_id": "a1", "text": "part three", "created_at": "2026-01-03T00:00:00Z",
            "referenced_tweets": [{"type": "replied_to", "id": "200"}]},
    "200": {"id": "200", "author_id": "a1", "text": "part two", "created_at": "2026-01-02T00:00:00Z",
            "referenced_tweets": [{"type": "replied_to", "id": "100"}]},
    "100": {"id": "100", "author_id": "a1", "text": "part one", "created_at": "2026-01-01T00:00:00Z"},
}


class FakeXClient(XClient):
    """Real XClient logic (including expand_thread), canned HTTP layer."""

    def __init__(self):
        self.tweet_lookups = []
        self.bookmark_pages = 0

    def _get(self, path: str, params: dict) -> dict:
        if path.startswith("/tweets/"):
            tweet_id = path.rsplit("/", 1)[1]
            self.tweet_lookups.append(tweet_id)
            return {"data": TWEETS.get(tweet_id)}
        if path.endswith("/bookmarks"):
            self.bookmark_pages += 1
            return {
                "data": [dict(TWEETS["300"], entities={"urls": []})],
                "includes": {"users": [{"id": "a1", "username": "alice", "name": "Alice"}]},
                "meta": {},
            }
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeXClient()
    monkeypatch.setattr(sync, "XClient", lambda: client)
    return client


def test_first_sync_expands_the_thread(conn, logged_in, fake_client):
    summary = sync.sync_bookmarks()

    assert summary["new"] == 1
    assert summary["thread_expansion_api_reads"] == 2  # tweets 200 and 100
    assert fake_client.tweet_lookups == ["200", "100"]

    row = conn.execute("SELECT is_thread, thread_text FROM bookmarks WHERE id = 300").fetchone()
    assert row["is_thread"] == 1
    assert row["thread_text"] == "part one\n\n---\n\npart two\n\n---\n\npart three"


def test_resync_costs_no_thread_lookups(conn, logged_in, fake_client):
    sync.sync_bookmarks()
    fake_client.tweet_lookups.clear()

    summary = sync.sync_bookmarks()

    assert summary["updated"] == 1
    assert summary["thread_expansion_api_reads"] == 0
    assert summary["threads_reused_from_cache"] == 1
    assert fake_client.tweet_lookups == []  # the expensive part did not re-run


def test_resync_preserves_the_stored_thread(conn, logged_in, fake_client):
    sync.sync_bookmarks()
    before = conn.execute("SELECT is_thread, thread_text FROM bookmarks WHERE id = 300").fetchone()

    sync.sync_bookmarks()
    after = conn.execute("SELECT is_thread, thread_text FROM bookmarks WHERE id = 300").fetchone()

    # The upsert must carry the thread forward, not clobber it with NULL.
    assert after["thread_text"] == before["thread_text"]
    assert after["is_thread"] == 1


def test_resync_still_refreshes_mutable_fields(conn, logged_in, fake_client, monkeypatch):
    sync.sync_bookmarks()
    TWEETS["300"]["text"] = "part three (edited)"
    try:
        sync.sync_bookmarks()
        row = conn.execute("SELECT text FROM bookmarks WHERE id = 300").fetchone()
        assert row["text"] == "part three (edited)"
    finally:
        TWEETS["300"]["text"] = "part three"


def test_thread_expansion_stops_at_another_author(conn, logged_in, monkeypatch):
    """A reply to someone else's tweet is not a thread."""
    others = {
        "500": {"id": "500", "author_id": "a1", "text": "my reply", "created_at": "2026-01-05T00:00:00Z",
                "referenced_tweets": [{"type": "replied_to", "id": "400"}]},
        "400": {"id": "400", "author_id": "SOMEONE-ELSE", "text": "their tweet", "created_at": "2026-01-04T00:00:00Z"},
    }

    class OtherAuthorClient(FakeXClient):
        def _get(self, path, params):
            if path.startswith("/tweets/"):
                tid = path.rsplit("/", 1)[1]
                self.tweet_lookups.append(tid)
                return {"data": others.get(tid)}
            return {
                "data": [dict(others["500"], entities={"urls": []})],
                "includes": {"users": [{"id": "a1", "username": "alice", "name": "Alice"}]},
                "meta": {},
            }

    client = OtherAuthorClient()
    monkeypatch.setattr(sync, "XClient", lambda: client)

    sync.sync_bookmarks()
    row = conn.execute("SELECT is_thread, thread_text FROM bookmarks WHERE id = 500").fetchone()
    assert row["is_thread"] == 0
    assert row["thread_text"] is None

    # And the failed attempt is not repeated on the next sync either.
    client.tweet_lookups.clear()
    sync.sync_bookmarks()
    assert client.tweet_lookups == []


def test_sync_is_covered_by_the_job_lock(conn, logged_in, fake_client):
    from backend import jobs
    from backend.jobs import JobBusyError
    import threading

    errors = []

    def contender():
        try:
            sync.sync_bookmarks()
        except JobBusyError as e:
            errors.append(e)

    with jobs.exclusive("scheduled sync"):
        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=5)

    assert len(errors) == 1

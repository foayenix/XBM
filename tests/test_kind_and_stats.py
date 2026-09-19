"""The Console row list needs two things the API did not expose: a content
type per bookmark (the THR/VID/ART/TWT badge) and counts for the rail and
status bar.
"""
from datetime import datetime, timezone

import pytest

from backend.queries import get_stats, list_watch_later, search_bookmarks


def add_link(conn, bookmark_id, url, link_type, status="done"):
    conn.execute(
        "INSERT INTO linked_content (bookmark_id, type, url, status) VALUES (?, ?, ?, ?)",
        (bookmark_id, link_type, url, status),
    )
    conn.commit()


def kind_of(conn, bookmark_id):
    results = search_bookmarks(conn, None, [], 30, 0)["results"]
    return next(r["kind"] for r in results if r["id"] == bookmark_id)


# --- kind -----------------------------------------------------------------

def test_a_plain_tweet_is_a_tweet(conn, seed):
    seed(1, "just a tweet")
    assert kind_of(conn, 1) == "tweet"


def test_a_thread_is_a_thread(conn, seed):
    seed(1, "first", thread_text="first\n\nsecond")
    assert kind_of(conn, 1) == "thread"


def test_a_youtube_link_makes_it_a_video(conn, seed):
    seed(1, "watch this")
    add_link(conn, 1, "https://youtu.be/abc123", "youtube")
    assert kind_of(conn, 1) == "video"


def test_an_article_link_makes_it_an_article(conn, seed):
    seed(1, "read this")
    add_link(conn, 1, "https://example.com/post", "article")
    assert kind_of(conn, 1) == "article"


def test_video_wins_over_thread(conn, seed):
    """One badge slot: video wins because it is the one with a queue."""
    seed(1, "a thread about a video", thread_text="a\n\nb")
    add_link(conn, 1, "https://youtu.be/abc123", "youtube")
    assert kind_of(conn, 1) == "video"


def test_thread_wins_over_a_plain_article_link(conn, seed):
    seed(1, "a thread citing an article", thread_text="a\n\nb")
    add_link(conn, 1, "https://example.com/post", "article")
    assert kind_of(conn, 1) == "thread"


def test_a_pending_link_still_counts(conn, seed):
    """Enrichment may not have run yet; the type is known at seed time."""
    seed(1, "watch this")
    add_link(conn, 1, "https://youtu.be/abc123", "youtube", status="pending")
    assert kind_of(conn, 1) == "video"


def test_kind_is_computed_without_a_query_per_row(conn, seed):
    for i in range(1, 21):
        seed(i, f"bookmark {i}", created_at=f"2026-01-01T00:{i:02d}:00Z")
        add_link(conn, i, f"https://youtu.be/vid{i:04d}", "youtube")

    queries_run = []
    conn.set_trace_callback(queries_run.append)
    try:
        results = search_bookmarks(conn, None, [], 30, 0)["results"]
    finally:
        conn.set_trace_callback(None)

    assert all(r["kind"] == "video" for r in results)
    assert len([q for q in queries_run if "linked_content" in q]) == 1


def test_watch_later_rows_carry_a_kind_too(conn, seed):
    seed(1, "watch this")
    add_link(conn, 1, "https://youtu.be/abc123", "youtube")
    conn.execute(
        "INSERT INTO watch_later (bookmark_id, status, added_at) VALUES (1, 'unwatched', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    assert list_watch_later(conn, None)[0]["kind"] == "video"


# --- stats ----------------------------------------------------------------

def test_stats_on_an_empty_database(conn):
    assert get_stats(conn) == {
        "bookmarks": 0,
        "watch_later_unwatched": 0,
        "watch_later_total": 0,
        "last_synced_at": None,
    }


def test_stats_count_bookmarks_and_the_queue(conn, seed):
    for i in range(1, 6):
        seed(i, f"bookmark {i}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO watch_later (bookmark_id, status, added_at) VALUES (1,'unwatched',?)", (now,))
    conn.execute("INSERT INTO watch_later (bookmark_id, status, added_at) VALUES (2,'unwatched',?)", (now,))
    conn.execute("INSERT INTO watch_later (bookmark_id, status, added_at) VALUES (3,'watched',?)", (now,))
    conn.commit()

    stats = get_stats(conn)
    assert stats["bookmarks"] == 5
    assert stats["watch_later_unwatched"] == 2
    assert stats["watch_later_total"] == 3


def test_last_synced_at_is_the_most_recent_sync(conn, seed):
    seed(1, "old")
    seed(2, "new")
    conn.execute("UPDATE bookmarks SET synced_at = '2026-01-01T00:00:00Z' WHERE id = 1")
    conn.execute("UPDATE bookmarks SET synced_at = '2026-06-01T00:00:00Z' WHERE id = 2")
    conn.commit()
    assert get_stats(conn)["last_synced_at"] == "2026-06-01T00:00:00Z"


def test_the_stats_endpoint_is_reachable(client, conn, seed):
    seed(1, "hello")
    data = client.get("/api/stats").json()
    assert data["bookmarks"] == 1
    assert set(data) == {"bookmarks", "watch_later_unwatched", "watch_later_total", "last_synced_at"}


@pytest.mark.parametrize("endpoint", ["/api/stats", "/api/search", "/api/tags"])
def test_read_endpoints_still_reject_a_rebound_host(client, endpoint):
    assert client.get(endpoint, headers={"host": "evil.example"}).status_code == 403

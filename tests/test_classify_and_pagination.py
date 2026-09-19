"""Link classification and search pagination.

classify_link called everything non-YouTube an "article", so PDFs and
images were downloaded and summarized as prose, while music.youtube.com and
m.youtube.com were misfiled as articles and never reached the transcript
path or the watch-later queue.

/api/search accepted limit/offset but the UI never sent them, so with 1,200
bookmarks the page reported the full count and showed only the first 30
with no way to reach the rest.
"""
import pytest

from backend.enrich import classify_link
from backend.queries import search_bookmarks
from backend.util import extract_youtube_video_id, is_youtube_url


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc123",
    "https://youtube.com/watch?v=abc123",
    "https://m.youtube.com/watch?v=abc123",
    "https://music.youtube.com/watch?v=abc123",
    "https://youtu.be/abc123",
    "https://www.youtube.com/shorts/abc123",
    "https://www.youtube.com/live/abc123",
])
def test_every_youtube_form_is_recognised(url):
    assert classify_link(url) == "youtube"
    assert extract_youtube_video_id(url) == "abc123"


@pytest.mark.parametrize("url", [
    "https://notyoutube.com/watch?v=abc",
    "https://youtube.com.evil.example/watch?v=abc",
])
def test_lookalike_youtube_domains_are_not_treated_as_youtube(url):
    assert not is_youtube_url(url)
    assert classify_link(url) == "article"


@pytest.mark.parametrize("url", [
    "https://example.com/paper.pdf",
    "https://i.imgur.com/photo.png",
    "https://example.com/clip.mp4",
    "https://example.com/data.csv",
    "https://example.com/release.zip",
])
def test_file_downloads_are_not_classified_as_articles(url):
    assert classify_link(url) is None


@pytest.mark.parametrize("url", ["mailto:a@b.com", "javascript:alert(1)", "not a url"])
def test_unfetchable_urls_are_skipped(url):
    assert classify_link(url) is None


def test_real_articles_still_classify_as_articles():
    assert classify_link("https://example.com/blog/post") == "article"


def test_skipped_links_are_never_seeded(conn, seed):
    from backend.enrich import seed_linked_content

    seed(1, "tweet", links=[
        "https://example.com/real-article",
        "https://example.com/paper.pdf",
        "https://music.youtube.com/watch?v=abc123",
    ])
    seed_linked_content(conn)

    rows = conn.execute("SELECT type, url FROM linked_content ORDER BY url").fetchall()
    assert [(r["type"], r["url"]) for r in rows] == [
        ("article", "https://example.com/real-article"),
        ("youtube", "https://music.youtube.com/watch?v=abc123"),
    ]


# --- pagination -----------------------------------------------------------

@pytest.fixture
def many_bookmarks(conn, seed):
    for i in range(1, 76):
        seed(i, f"bookmark number {i}", created_at=f"2026-01-01T00:{i:02d}:00Z")
    return 75


def test_search_reports_pagination_state(conn, many_bookmarks):
    page = search_bookmarks(conn, None, [], 30, 0)
    assert len(page["results"]) == 30
    assert page["total"] == 75
    assert page["limit"] == 30
    assert page["offset"] == 0
    assert page["has_more"] is True


def test_the_last_page_reports_no_more(conn, many_bookmarks):
    page = search_bookmarks(conn, None, [], 30, 60)
    assert len(page["results"]) == 15
    assert page["has_more"] is False


def test_pages_do_not_overlap_or_skip(conn, many_bookmarks):
    seen = []
    for offset in (0, 30, 60):
        seen.extend(r["id"] for r in search_bookmarks(conn, None, [], 30, offset)["results"])
    assert len(seen) == 75
    assert len(set(seen)) == 75  # every bookmark exactly once


def test_pagination_works_with_a_search_query(conn, seed):
    for i in range(1, 46):
        seed(i, f"python topic {i}", created_at=f"2026-01-01T00:{i:02d}:00Z")
    seed(100, "unrelated")

    first = search_bookmarks(conn, "python", [], 30, 0)
    second = search_bookmarks(conn, "python", [], 30, 30)

    assert first["total"] == 45
    assert first["has_more"] is True
    assert len(second["results"]) == 15
    assert second["has_more"] is False
    assert not set(r["id"] for r in first["results"]) & set(r["id"] for r in second["results"])


def test_endpoint_exposes_pagination_to_the_ui(client, many_bookmarks):
    data = client.get("/api/search", params={"limit": 10, "offset": 20}).json()
    assert len(data["results"]) == 10
    assert data["offset"] == 20
    assert data["has_more"] is True


def test_an_offset_past_the_end_is_empty_not_an_error(conn, many_bookmarks):
    page = search_bookmarks(conn, None, [], 30, 500)
    assert page["results"] == []
    assert page["has_more"] is False


# --- thumbnails (batched, previously one query per row) -------------------

def test_thumbnails_come_from_one_query_per_page(conn, seed):
    for i in range(1, 31):
        seed(i, f"video {i}", links=[f"https://youtu.be/vid{i:04d}"])
    from backend.enrich import seed_linked_content
    seed_linked_content(conn)

    queries_run = []
    conn.set_trace_callback(queries_run.append)
    try:
        results = search_bookmarks(conn, None, [], 30, 0)["results"]
    finally:
        conn.set_trace_callback(None)

    assert all(r["thumbnail"] and "img.youtube.com" in r["thumbnail"] for r in results)
    linked_queries = [q for q in queries_run if "linked_content" in q]
    assert len(linked_queries) == 1  # one batched lookup, not one per row


def test_attached_media_wins_over_the_youtube_poster(conn):
    import json
    from datetime import datetime, timezone

    conn.execute(
        """INSERT INTO bookmarks (id, author_id, author_username, author_name, text,
           created_at, media_urls, external_links, is_thread, thread_text, synced_at)
           VALUES (1,'a','alice','Alice','has media','2026-01-01T00:00:00Z',?,'[]',0,NULL,?)""",
        (json.dumps(["https://pbs.example/photo.jpg"]), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    result = search_bookmarks(conn, None, [], 30, 0)["results"][0]
    assert result["thumbnail"] == "https://pbs.example/photo.jpg"


def test_results_do_not_leak_internal_scratch_fields(conn, seed):
    seed(1, "hello")
    result = search_bookmarks(conn, None, [], 30, 0)["results"][0]
    assert "_media_urls" not in result

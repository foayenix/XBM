"""Blocker 4: failed enrichment work retried forever, at cost.

process_linked_content selected everything that was not 'done', and tagging
selected every bookmark with no tags, so a dead link or an unparseable
Claude reply was re-fetched and re-billed on every run - permanently, and
much faster with the background scheduler enabled.
"""
import pytest

from backend import enrich
from backend.enrich import MAX_LINK_ATTEMPTS, MAX_TAG_ATTEMPTS


def add_link(conn, bookmark_id, url, link_type="article", status="pending", attempts=0):
    conn.execute(
        "INSERT INTO linked_content (bookmark_id, type, url, status, attempts) VALUES (?, ?, ?, ?, ?)",
        (bookmark_id, link_type, url, status, attempts),
    )
    conn.commit()
    return conn.execute("SELECT id FROM linked_content WHERE url = ?", (url,)).fetchone()["id"]


@pytest.fixture
def always_failing_fetch(monkeypatch):
    """Make every article fetch fail, counting how often it is attempted."""
    calls = []

    def boom(url, **kwargs):
        calls.append(url)
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr(enrich, "fetch_article_html", boom)
    return calls


# --- linked content -------------------------------------------------------

def test_attempts_increment_on_each_failure(conn, seed, always_failing_fetch):
    seed(1, "tweet")
    link_id = add_link(conn, 1, "https://dead.example/a")

    enrich.process_linked_content(conn)
    assert conn.execute("SELECT attempts FROM linked_content WHERE id=?", (link_id,)).fetchone()["attempts"] == 1

    enrich.process_linked_content(conn)
    assert conn.execute("SELECT attempts FROM linked_content WHERE id=?", (link_id,)).fetchone()["attempts"] == 2


def test_a_dead_link_stops_being_retried_at_the_cap(conn, seed, always_failing_fetch):
    seed(1, "tweet")
    add_link(conn, 1, "https://dead.example/a")

    for _ in range(MAX_LINK_ATTEMPTS):
        enrich.process_linked_content(conn)
    assert len(always_failing_fetch) == MAX_LINK_ATTEMPTS

    # Further runs must not touch the network again.
    for _ in range(5):
        enrich.process_linked_content(conn)
    assert len(always_failing_fetch) == MAX_LINK_ATTEMPTS


def test_exhausted_rows_are_reported_not_silently_dropped(conn, seed, always_failing_fetch):
    seed(1, "tweet")
    add_link(conn, 1, "https://dead.example/a", attempts=MAX_LINK_ATTEMPTS - 1)

    summary = enrich.process_linked_content(conn)
    assert summary["linked_content_gave_up"] == 1

    later = enrich.process_linked_content(conn)
    assert later["linked_content_gave_up"] == 1
    assert later["linked_content_failed"] == 0  # not reprocessed


def test_attempt_is_recorded_before_the_work_so_a_crash_still_counts(conn, seed, monkeypatch):
    """A hard crash (not an exception we catch) must not leave a row spinning."""
    seed(1, "tweet")
    link_id = add_link(conn, 1, "https://dead.example/a")

    def hard_crash(url, **kwargs):
        raise KeyboardInterrupt("simulated hard stop")

    monkeypatch.setattr(enrich, "fetch_article_html", hard_crash)
    with pytest.raises(KeyboardInterrupt):
        enrich.process_linked_content(conn)

    assert conn.execute("SELECT attempts FROM linked_content WHERE id=?", (link_id,)).fetchone()["attempts"] == 1


def test_a_link_that_succeeds_is_never_retried(conn, seed, monkeypatch):
    seed(1, "tweet")
    add_link(conn, 1, "https://good.example/a")

    fetches = []
    html = "<html><body><article>" + ("Real article body. " * 40) + "</article></body></html>"

    monkeypatch.setattr(enrich, "fetch_article_html",
                        lambda url, **kw: fetches.append(url) or (html, url))
    monkeypatch.setattr(enrich, "_claude_client", lambda: object())
    monkeypatch.setattr(enrich, "_summarize_article", lambda c, t, x: "a summary")

    assert enrich.process_linked_content(conn)["linked_content_done"] == 1
    assert enrich.process_linked_content(conn)["linked_content_done"] == 0
    assert len(fetches) == 1


def test_successful_youtube_link_lands_in_watch_later(conn, seed, monkeypatch):
    seed(1, "tweet")
    add_link(conn, 1, "https://youtu.be/abc123", link_type="youtube")
    monkeypatch.setattr(enrich, "_fetch_youtube_transcript", lambda url: ("a transcript", "A Title"))

    enrich.process_linked_content(conn)
    assert conn.execute("SELECT COUNT(*) c FROM watch_later WHERE bookmark_id=1").fetchone()["c"] == 1


# --- tagging --------------------------------------------------------------

@pytest.fixture
def stub_claude(monkeypatch):
    monkeypatch.setattr(enrich, "_claude_client", lambda: object())


def test_tagging_stops_after_repeated_failures(conn, seed, stub_claude, monkeypatch):
    seed(1, "untaggable tweet")
    calls = []

    def boom(client, c, bookmark):
        calls.append(bookmark["id"])
        raise RuntimeError("api error")

    monkeypatch.setattr(enrich, "_tag_bookmark", boom)

    for _ in range(MAX_TAG_ATTEMPTS + 4):
        enrich.tag_untagged_bookmarks(conn)
    assert len(calls) == MAX_TAG_ATTEMPTS


def test_an_empty_tag_reply_counts_against_the_cap(conn, seed, stub_claude, monkeypatch):
    """Claude returning nothing parseable left the bookmark untagged and requeued."""
    seed(1, "tweet")
    calls = []
    monkeypatch.setattr(enrich, "_tag_bookmark", lambda cl, c, b: calls.append(b["id"]) or [])

    for _ in range(MAX_TAG_ATTEMPTS + 4):
        enrich.tag_untagged_bookmarks(conn)
    assert len(calls) == MAX_TAG_ATTEMPTS

    summary = enrich.tag_untagged_bookmarks(conn)
    assert summary["bookmarks_tagging_gave_up"] == 1


def test_a_successfully_tagged_bookmark_is_not_re_sent(conn, seed, stub_claude, monkeypatch):
    seed(1, "tweet about python")
    calls = []
    monkeypatch.setattr(enrich, "_tag_bookmark", lambda cl, c, b: calls.append(b["id"]) or ["python", "testing"])

    enrich.tag_untagged_bookmarks(conn)
    enrich.tag_untagged_bookmarks(conn)

    assert len(calls) == 1
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id=t.id WHERE bt.bookmark_id=1 ORDER BY t.name"
    ).fetchall()]
    assert tags == ["python", "testing"]


def test_a_recovered_bookmark_stops_consuming_attempts(conn, seed, stub_claude, monkeypatch):
    """A transient failure that later succeeds still gets tagged."""
    seed(1, "tweet")
    state = {"n": 0}

    def flaky(client, c, bookmark):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient")
        return ["recovered"]

    monkeypatch.setattr(enrich, "_tag_bookmark", flaky)

    enrich.tag_untagged_bookmarks(conn)
    enrich.tag_untagged_bookmarks(conn)

    assert conn.execute(
        "SELECT COUNT(*) c FROM bookmark_tags WHERE bookmark_id=1"
    ).fetchone()["c"] == 1


def test_enrichment_is_covered_by_the_job_lock(conn):
    import threading
    from backend import jobs
    from backend.jobs import JobBusyError

    errors = []

    def contender():
        try:
            enrich.run_enrichment()
        except JobBusyError as e:
            errors.append(e)

    with jobs.exclusive("scheduled sync"):
        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=5)

    assert len(errors) == 1

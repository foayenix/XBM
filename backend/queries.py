"""Read/update queries backing the web UI: search, tag filters, watch-later.

Kept separate from main.py so the route handlers stay thin.
"""
import json
import re
from datetime import datetime, timezone

from backend.util import extract_youtube_video_id

# Non-printable markers wrapped around FTS5 snippet() match highlights.
# The frontend splits on these instead of trusting HTML from tweet text,
# which is untrusted user content and must never be assigned via innerHTML.
SNIPPET_START = ""
SNIPPET_END = ""


def _thumbnail_for(conn, bookmark_id: int, media_urls: list[str]) -> str | None:
    if media_urls:
        return media_urls[0]
    yt = conn.execute(
        "SELECT url FROM linked_content WHERE bookmark_id = ? AND type = 'youtube' LIMIT 1",
        (bookmark_id,),
    ).fetchone()
    if yt:
        video_id = extract_youtube_video_id(yt["url"])
        if video_id:
            return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    return None


def _attach_tags(conn, bookmarks: list[dict]) -> None:
    if not bookmarks:
        return
    ids = [b["id"] for b in bookmarks]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT bt.bookmark_id, t.name FROM bookmark_tags bt
        JOIN tags t ON t.id = bt.tag_id
        WHERE bt.bookmark_id IN ({placeholders})
        ORDER BY t.name
        """,
        ids,
    ).fetchall()
    tags_by_bookmark: dict[int, list[str]] = {}
    for r in rows:
        tags_by_bookmark.setdefault(r["bookmark_id"], []).append(r["name"])
    for b in bookmarks:
        b["tags"] = tags_by_bookmark.get(b["id"], [])


def _row_to_result(conn, r) -> dict:
    media_urls = json.loads(r["media_urls"] or "[]")
    keys = r.keys()
    return {
        "id": r["id"],
        "author_username": r["author_username"],
        "author_name": r["author_name"],
        "created_at": r["created_at"],
        "text": r["text"],
        "snippet": r["snippet_text"] if "snippet_text" in keys else None,
        "match_snippet": r["match_snippet"] if "match_snippet" in keys else None,
        "is_thread": bool(r["is_thread"]),
        "thumbnail": _thumbnail_for(conn, r["id"], media_urls),
    }


def _attach_state(conn, bookmarks: list[dict]) -> None:
    """Add triage/enrichment state the UI shows on every row: read, watch, link kinds."""
    if not bookmarks:
        return
    ids = [b["id"] for b in bookmarks]
    placeholders = ",".join("?" * len(ids))
    read_ids = {
        r["bookmark_id"]
        for r in conn.execute(f"SELECT bookmark_id FROM bookmark_reads WHERE bookmark_id IN ({placeholders})", ids)
    }
    watch = {
        r["bookmark_id"]: r["status"]
        for r in conn.execute(f"SELECT bookmark_id, status FROM watch_later WHERE bookmark_id IN ({placeholders})", ids)
    }
    kinds: dict[int, set[str]] = {}
    for r in conn.execute(
        f"SELECT bookmark_id, type FROM linked_content WHERE bookmark_id IN ({placeholders})", ids
    ):
        kinds.setdefault(r["bookmark_id"], set()).add(r["type"])
    for b in bookmarks:
        b["is_read"] = b["id"] in read_ids
        b["watch_status"] = watch.get(b["id"])
        b["has_video"] = "youtube" in kinds.get(b["id"], ())
        b["has_article"] = "article" in kinds.get(b["id"], ())


def _public_ids(items: list[dict]) -> list[dict]:
    """Send ids to the browser as strings.

    X snowflake ids are ~1.8e18, past JavaScript's 2**53 safe-integer limit,
    so a numeric id gets rounded by JSON.parse and then points at the wrong
    tweet. Everything inside this module keeps them as ints until here.
    """
    for item in items:
        item["id"] = str(item["id"])
    return items


def _tag_filter_clause(tags: list[str]) -> tuple[str, list[str]]:
    if not tags:
        return "", []
    placeholders = ",".join("?" * len(tags))
    clause = f"""AND b.id IN (
        SELECT bt.bookmark_id FROM bookmark_tags bt
        JOIN tags t ON t.id = bt.tag_id
        WHERE t.name IN ({placeholders})
        GROUP BY bt.bookmark_id
        HAVING COUNT(DISTINCT t.name) = {len(tags)}
    )"""
    return clause, list(tags)


VIEWS = ("inbox", "all", "threads", "articles", "videos", "watch")
WATCH_STATUSES = ("unwatched", "watched")


def _view_filter_clause(view: str, watch: str | None) -> tuple[str, list[str]]:
    if view == "inbox":
        return "AND b.id NOT IN (SELECT bookmark_id FROM bookmark_reads)", []
    if view == "threads":
        return "AND b.is_thread = 1", []
    if view == "articles":
        return "AND EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'article')", []
    if view == "videos":
        return "AND EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'youtube')", []
    if view == "watch":
        if watch in WATCH_STATUSES:
            return "AND b.id IN (SELECT bookmark_id FROM watch_later WHERE status = ?)", [watch]
        return "AND b.id IN (SELECT bookmark_id FROM watch_later)", []
    return "", []


def to_fts_query(q: str) -> str | None:
    """Turn free text typed into the search box into a safe FTS5 MATCH string.

    Raw user input isn't valid FTS5 syntax in general (a stray quote, a
    leading '-', 'AND' on its own all raise errors), so each word becomes a
    quoted prefix term. Words are ANDed, and the prefix match means results
    show up while a word is still being typed.
    """
    words = re.findall(r'[^\s"]+', q or "")
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)


def search_bookmarks(
    conn,
    q: str | None,
    tags: list[str],
    limit: int,
    offset: int,
    view: str = "all",
    watch: str | None = None,
) -> dict:
    match = to_fts_query(q or "")
    tag_clause, tag_params = _tag_filter_clause(tags)
    view_clause, view_params = _view_filter_clause(view, watch)
    filters = f"{view_clause} {tag_clause}"
    filter_params = [*view_params, *tag_params]

    if match:
        source = "FROM bookmarks_fts JOIN bookmarks b ON b.id = bookmarks_fts.rowid WHERE bookmarks_fts MATCH ?"
        source_params = [match]
        # Column 0 is the tweet text; column -1 lets FTS5 pick whichever column
        # matched best, so the UI can show why a result matched when the hit
        # was in a thread, an article summary, or a transcript.
        select_extra = (
            "snippet(bookmarks_fts, 0, ?, ?, '...', 24) AS snippet_text, "
            "snippet(bookmarks_fts, -1, ?, ?, '...', 16) AS match_snippet"
        )
        select_params = [SNIPPET_START, SNIPPET_END, SNIPPET_START, SNIPPET_END]
        order = "ORDER BY rank"
    else:
        source = "FROM bookmarks b WHERE 1=1"
        source_params = []
        select_extra = "NULL AS snippet_text, NULL AS match_snippet"
        select_params = []
        order = "ORDER BY b.created_at DESC"

    rows = conn.execute(
        f"""
        SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
               {select_extra}
        {source} {filters}
        {order}
        LIMIT ? OFFSET ?
        """,
        [*select_params, *source_params, *filter_params, limit, offset],
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) c {source} {filters}", [*source_params, *filter_params]).fetchone()["c"]

    # Tag facets: which tags occur inside the current result set, so the tag
    # index narrows to tags that would actually refine what's on screen.
    facet_rows = conn.execute(
        f"""
        SELECT t.name, COUNT(*) c FROM bookmark_tags bt
        JOIN tags t ON t.id = bt.tag_id
        WHERE bt.bookmark_id IN (SELECT b.id {source} {filters})
        GROUP BY t.id
        ORDER BY c DESC, t.name ASC
        """,
        [*source_params, *filter_params],
    ).fetchall()

    results = [_row_to_result(conn, r) for r in rows]
    _attach_tags(conn, results)
    _attach_state(conn, results)
    return {
        "results": _public_ids(results),
        "total": total,
        "facets": [{"name": r["name"], "count": r["c"]} for r in facet_rows],
    }


def view_counts(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "inbox": one("SELECT COUNT(*) FROM bookmarks WHERE id NOT IN (SELECT bookmark_id FROM bookmark_reads)"),
        "all": one("SELECT COUNT(*) FROM bookmarks"),
        "threads": one("SELECT COUNT(*) FROM bookmarks WHERE is_thread = 1"),
        "articles": one("SELECT COUNT(DISTINCT bookmark_id) FROM linked_content WHERE type = 'article'"),
        "videos": one("SELECT COUNT(DISTINCT bookmark_id) FROM linked_content WHERE type = 'youtube'"),
        "watch": one("SELECT COUNT(*) FROM watch_later WHERE status = 'unwatched'"),
    }


def get_bookmark(conn, bookmark_id: int) -> dict | None:
    r = conn.execute(
        """
        SELECT id, author_username, author_name, created_at, text, media_urls, is_thread, thread_text, synced_at
        FROM bookmarks WHERE id = ?
        """,
        (bookmark_id,),
    ).fetchone()
    if not r:
        return None
    result = _row_to_result(conn, r)
    result["thread_text"] = r["thread_text"]
    result["media_urls"] = json.loads(r["media_urls"] or "[]")
    result["synced_at"] = r["synced_at"]
    result["linked"] = [
        {
            "type": lc["type"],
            "url": lc["url"],
            "title": lc["title"],
            "body": lc["transcript_or_summary"],
            "status": lc["status"],
            "error": lc["error"],
        }
        for lc in conn.execute(
            "SELECT * FROM linked_content WHERE bookmark_id = ? ORDER BY type, id", (bookmark_id,)
        )
    ]
    _attach_tags(conn, [result])
    _attach_state(conn, [result])
    result["related"] = _public_ids(related_bookmarks(conn, bookmark_id))
    return _public_ids([result])[0]


def related_bookmarks(conn, bookmark_id: int, limit: int = 6) -> list[dict]:
    """Bookmarks that share the most tags with this one, then same-author ones.

    Each result carries the tags it shares (or the shared author) so the UI
    can say why it's related instead of just asserting it.
    """
    rows = conn.execute(
        """
        SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
               COUNT(*) AS shared, group_concat(t.name, '|') AS shared_tags
        FROM bookmark_tags mine
        JOIN bookmark_tags other ON other.tag_id = mine.tag_id AND other.bookmark_id != mine.bookmark_id
        JOIN tags t ON t.id = other.tag_id
        JOIN bookmarks b ON b.id = other.bookmark_id
        WHERE mine.bookmark_id = ?
        GROUP BY b.id
        ORDER BY shared DESC, b.created_at DESC
        LIMIT ?
        """,
        (bookmark_id, limit),
    ).fetchall()
    related = []
    for r in rows:
        item = _row_to_result(conn, r)
        item["reason"] = {"tags": sorted(r["shared_tags"].split("|"))}
        related.append(item)

    if len(related) < limit:
        seen = [bookmark_id, *(item["id"] for item in related)]
        placeholders = ",".join("?" * len(seen))
        rows = conn.execute(
            f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread
            FROM bookmarks b
            WHERE b.author_id = (SELECT author_id FROM bookmarks WHERE id = ?) AND b.id NOT IN ({placeholders})
            ORDER BY b.created_at DESC
            LIMIT ?
            """,
            (bookmark_id, *seen, limit - len(related)),
        ).fetchall()
        for r in rows:
            item = _row_to_result(conn, r)
            item["reason"] = {"author": r["author_username"]}
            related.append(item)
    return related


def set_read(conn, bookmark_id: int, read: bool) -> dict | None:
    if not conn.execute("SELECT 1 FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone():
        return None
    if read:
        read_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO bookmark_reads (bookmark_id, read_at) VALUES (?, ?)", (bookmark_id, read_at)
        )
    else:
        conn.execute("DELETE FROM bookmark_reads WHERE bookmark_id = ?", (bookmark_id,))
    conn.commit()
    return {"bookmark_id": str(bookmark_id), "is_read": read}


def list_tags(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.name, COUNT(*) c FROM tags t
        JOIN bookmark_tags bt ON bt.tag_id = t.id
        GROUP BY t.id
        ORDER BY c DESC, t.name ASC
        """
    ).fetchall()
    return [{"name": r["name"], "count": r["c"]} for r in rows]


def list_watch_later(conn, status: str | None) -> list[dict]:
    sql = """
        SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
               wl.status AS watch_status, wl.added_at, wl.watched_at
        FROM watch_later wl
        JOIN bookmarks b ON b.id = wl.bookmark_id
    """
    params = []
    if status:
        sql += " WHERE wl.status = ?"
        params.append(status)
    sql += " ORDER BY wl.added_at DESC"
    rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        result = _row_to_result(conn, r)
        result.update(watch_status=r["watch_status"], added_at=r["added_at"], watched_at=r["watched_at"])
        results.append(result)
    _attach_tags(conn, results)
    return _public_ids(results)


def toggle_watch_later(conn, bookmark_id: int) -> dict | None:
    row = conn.execute("SELECT status FROM watch_later WHERE bookmark_id = ?", (bookmark_id,)).fetchone()
    if not row:
        return None
    new_status = "watched" if row["status"] == "unwatched" else "unwatched"
    watched_at = datetime.now(timezone.utc).isoformat() if new_status == "watched" else None
    conn.execute(
        "UPDATE watch_later SET status = ?, watched_at = ? WHERE bookmark_id = ?",
        (new_status, watched_at, bookmark_id),
    )
    conn.commit()
    return {"bookmark_id": str(bookmark_id), "status": new_status, "watched_at": watched_at}

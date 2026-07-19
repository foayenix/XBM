"""Read/update queries backing the web UI: search, tag filters, watch-later.

Kept separate from main.py so the route handlers stay thin.
"""
import json
from datetime import datetime, timezone

from backend.util import extract_youtube_video_id

# Non-printable markers wrapped around FTS5 snippet() match highlights.
# The frontend splits on these instead of trusting HTML from tweet text,
# which is untrusted user content and must never be assigned via innerHTML.
SNIPPET_START = ""
SNIPPET_END = ""


# Every bookmark renders as exactly one of these types in the UI. Video wins
# over thread (a thread that links a video is something you watch), thread
# wins over article. Must stay in sync with _content_type_and_title below.
CONTENT_TYPE_SQL = """CASE
    WHEN EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'youtube') THEN 'video'
    WHEN b.is_thread THEN 'thread'
    WHEN EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'article') THEN 'article'
    ELSE 'tweet'
END"""

CONTENT_TYPES = ("video", "thread", "article", "tweet")

THREAD_SEPARATOR = "\n\n---\n\n"  # must match the join in sync.py


def _content_type_and_title(conn, bookmark_id: int, is_thread: bool) -> tuple[str, str | None]:
    rows = conn.execute(
        "SELECT type, title FROM linked_content WHERE bookmark_id = ? ORDER BY id",
        (bookmark_id,),
    ).fetchall()
    youtube = next((r for r in rows if r["type"] == "youtube"), None)
    article = next((r for r in rows if r["type"] == "article"), None)
    if youtube:
        return "video", youtube["title"]
    if is_thread:
        return "thread", None
    if article:
        return "article", article["title"]
    return "tweet", None


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
    tags_by_bookmark: dict[str, list[str]] = {}
    for r in rows:
        tags_by_bookmark.setdefault(str(r["bookmark_id"]), []).append(r["name"])
    for b in bookmarks:
        b["tags"] = tags_by_bookmark.get(str(b["id"]), [])


def _row_to_result(conn, r) -> dict:
    media_urls = json.loads(r["media_urls"] or "[]")
    content_type, title = _content_type_and_title(conn, r["id"], bool(r["is_thread"]))
    thread_count = len(r["thread_text"].split(THREAD_SEPARATOR)) if r["is_thread"] and r["thread_text"] else 0
    return {
        # Tweet snowflake ids overflow JavaScript's Number.MAX_SAFE_INTEGER,
        # so they must cross the API boundary as strings, never JSON numbers.
        "id": str(r["id"]),
        "author_username": r["author_username"],
        "author_name": r["author_name"],
        "created_at": r["created_at"],
        "text": r["text"],
        "snippet": r["snippet_text"] if "snippet_text" in r.keys() else None,
        "is_thread": bool(r["is_thread"]),
        "content_type": content_type,
        "title": title,
        "thread_count": thread_count,
        "thumbnail": _thumbnail_for(conn, r["id"], media_urls),
    }


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


def search_bookmarks(conn, q: str | None, tags: list[str], content_type: str | None,
                     limit: int, offset: int) -> dict:
    tag_clause, tag_params = _tag_filter_clause(tags)

    type_clause = ""
    type_params: list[str] = []
    if content_type in CONTENT_TYPES:
        type_clause = f"AND {CONTENT_TYPE_SQL} = ?"
        type_params = [content_type]

    if q:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
                   b.is_thread, b.thread_text,
                   snippet(bookmarks_fts, 0, ?, ?, '...', 24) AS snippet_text
            FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause} {type_clause}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [SNIPPET_START, SNIPPET_END, q, *tag_params, *type_params, limit, offset]).fetchall()

        count_sql = f"""
            SELECT COUNT(*) c FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause} {type_clause}
        """
        total = conn.execute(count_sql, [q, *tag_params, *type_params]).fetchone()["c"]
    else:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
                   b.is_thread, b.thread_text,
                   NULL AS snippet_text
            FROM bookmarks b
            WHERE 1=1 {tag_clause} {type_clause}
            ORDER BY b.created_at DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [*tag_params, *type_params, limit, offset]).fetchall()

        count_sql = f"SELECT COUNT(*) c FROM bookmarks b WHERE 1=1 {tag_clause} {type_clause}"
        total = conn.execute(count_sql, [*tag_params, *type_params]).fetchone()["c"]

    results = [_row_to_result(conn, r) for r in rows]
    _attach_tags(conn, results)
    return {"results": results, "total": total}


def get_stats(conn) -> dict:
    """Counts that drive the sidebar nav and the type-filter chips."""
    total = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]
    type_rows = conn.execute(
        f"SELECT {CONTENT_TYPE_SQL} AS content_type, COUNT(*) c FROM bookmarks b GROUP BY content_type"
    ).fetchall()
    by_type = {t: 0 for t in CONTENT_TYPES}
    for r in type_rows:
        by_type[r["content_type"]] = r["c"]
    unwatched = conn.execute(
        "SELECT COUNT(*) c FROM watch_later WHERE status = 'unwatched'"
    ).fetchone()["c"]
    last_synced = conn.execute("SELECT MAX(synced_at) m FROM bookmarks").fetchone()["m"]
    return {"total": total, "by_type": by_type, "unwatched": unwatched, "last_synced_at": last_synced}


def get_bookmark_detail(conn, bookmark_id: int) -> dict | None:
    r = conn.execute(
        """
        SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
               b.external_links, b.is_thread, b.thread_text
        FROM bookmarks b WHERE b.id = ?
        """,
        (bookmark_id,),
    ).fetchone()
    if not r:
        return None

    result = _row_to_result(conn, r)
    _attach_tags(conn, [result])

    result["thread_parts"] = r["thread_text"].split(THREAD_SEPARATOR) if r["thread_text"] else []
    result["external_links"] = json.loads(r["external_links"] or "[]")

    linked = conn.execute(
        "SELECT type, url, title, transcript_or_summary, status FROM linked_content WHERE bookmark_id = ? ORDER BY id",
        (bookmark_id,),
    ).fetchall()
    result["linked_content"] = [dict(row) for row in linked]

    wl = conn.execute("SELECT status FROM watch_later WHERE bookmark_id = ?", (bookmark_id,)).fetchone()
    result["watch_status"] = wl["status"] if wl else None
    return result


def _tags_for(conn, bookmark_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id
        WHERE bt.bookmark_id = ? ORDER BY t.name
        """,
        (bookmark_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def add_tag(conn, bookmark_id: int, name: str) -> list[str] | None:
    """Attach a tag to a bookmark, creating the tag if needed. Returns the
    bookmark's updated tag list, or None if the bookmark doesn't exist."""
    if not conn.execute("SELECT 1 FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone():
        return None
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)", (bookmark_id, tag_id))
    conn.commit()
    return _tags_for(conn, bookmark_id)


def remove_tag(conn, bookmark_id: int, name: str) -> list[str] | None:
    if not conn.execute("SELECT 1 FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone():
        return None
    tag = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if tag:
        conn.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ? AND tag_id = ?", (bookmark_id, tag["id"]))
        # Drop the tag entirely once nothing references it, so the sidebar
        # tag list doesn't accumulate empty tags.
        still_used = conn.execute(
            "SELECT 1 FROM bookmark_tags WHERE tag_id = ? LIMIT 1", (tag["id"],)
        ).fetchone()
        if not still_used:
            conn.execute("DELETE FROM tags WHERE id = ?", (tag["id"],))
        conn.commit()
    return _tags_for(conn, bookmark_id)


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
        SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
               b.is_thread, b.thread_text,
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
    return results


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

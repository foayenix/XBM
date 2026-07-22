"""Read/update queries backing the web UI: search, tag filters, watch-later.

Kept separate from main.py so the route handlers stay thin.
"""
import json
from datetime import datetime, timezone

from backend.util import extract_youtube_video_id

# Non-printable markers (U+0001 / U+0002) wrapped around FTS5 snippet() match
# highlights. The frontend splits on these instead of trusting HTML from tweet
# text, which is untrusted user content and must never be assigned via
# innerHTML.
SNIPPET_START = "\x01"
SNIPPET_END = "\x02"

# Individual posts of an expanded thread are joined with this separator by
# sync.py, so splitting on it recovers the per-post breakdown for the detail
# view and the "N posts" label.
THREAD_SEP = "\n\n---\n\n"

# Content-type classification. A bookmark's visual type is derived, not
# stored: a YouTube link makes it a video, any other external link makes it
# an article, an expanded reply chain makes it a thread, and anything left is
# a plain tweet. The order here IS the precedence — video wins over article
# wins over thread wins over tweet — and every SQL/Python path below applies
# it the same way so the library rows, the type filter, and the chip counts
# always agree.
_HAS_VIDEO = "EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'youtube')"
_HAS_ARTICLE = "EXISTS (SELECT 1 FROM linked_content lc WHERE lc.bookmark_id = b.id AND lc.type = 'article')"

TYPE_CLAUSES = {
    "video": _HAS_VIDEO,
    "article": f"({_HAS_ARTICLE} AND NOT {_HAS_VIDEO})",
    "thread": f"(b.is_thread = 1 AND NOT {_HAS_VIDEO} AND NOT {_HAS_ARTICLE})",
    "tweet": f"(b.is_thread = 0 AND NOT {_HAS_VIDEO} AND NOT {_HAS_ARTICLE})",
}


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


def _attach_types_and_titles(conn, bookmarks: list[dict]) -> None:
    """Fill in each bookmark's derived content type and, for video/article
    items, the title of the linked content (a YouTube video title or an
    article headline), which the library rows and detail view show above the
    tweet snippet."""
    if not bookmarks:
        return
    ids = [b["id"] for b in bookmarks]
    placeholders = ",".join("?" * len(ids))

    type_rows = conn.execute(
        f"""
        SELECT b.id, b.is_thread,
               {_HAS_VIDEO} AS has_video,
               {_HAS_ARTICLE} AS has_article
        FROM bookmarks b
        WHERE b.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    type_by_id: dict[int, str] = {}
    for r in type_rows:
        if r["has_video"]:
            type_by_id[r["id"]] = "video"
        elif r["has_article"]:
            type_by_id[r["id"]] = "article"
        elif r["is_thread"]:
            type_by_id[r["id"]] = "thread"
        else:
            type_by_id[r["id"]] = "tweet"

    # Prefer a YouTube title, fall back to an article title. Ordering youtube
    # first means a video with both link kinds still shows its video title.
    title_rows = conn.execute(
        f"""
        SELECT bookmark_id, title, type FROM linked_content
        WHERE bookmark_id IN ({placeholders}) AND title IS NOT NULL AND title != ''
        ORDER BY CASE type WHEN 'youtube' THEN 0 ELSE 1 END
        """,
        ids,
    ).fetchall()
    title_by_id: dict[int, str] = {}
    for r in title_rows:
        title_by_id.setdefault(r["bookmark_id"], r["title"])

    for b in bookmarks:
        b["type"] = type_by_id.get(b["id"], "tweet")
        b["title"] = title_by_id.get(b["id"])


def _row_to_result(conn, r) -> dict:
    media_urls = json.loads(r["media_urls"] or "[]")
    keys = r.keys()
    thread_text = r["thread_text"] if "thread_text" in keys else None
    post_count = len(thread_text.split(THREAD_SEP)) if (r["is_thread"] and thread_text) else None
    return {
        "id": r["id"],
        "author_username": r["author_username"],
        "author_name": r["author_name"],
        "created_at": r["created_at"],
        "text": r["text"],
        "snippet": r["snippet_text"] if "snippet_text" in keys else None,
        "is_thread": bool(r["is_thread"]),
        "post_count": post_count,
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


def _type_filter_clause(content_type: str | None) -> str:
    if content_type and content_type in TYPE_CLAUSES:
        return f"AND {TYPE_CLAUSES[content_type]}"
    return ""


def search_bookmarks(
    conn,
    q: str | None,
    tags: list[str],
    content_type: str | None,
    limit: int,
    offset: int,
) -> dict:
    tag_clause, tag_params = _tag_filter_clause(tags)
    type_clause = _type_filter_clause(content_type)
    filters = f"{tag_clause} {type_clause}"

    if q:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
                   b.is_thread, b.thread_text,
                   snippet(bookmarks_fts, 0, ?, ?, '...', 24) AS snippet_text
            FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {filters}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [SNIPPET_START, SNIPPET_END, q, *tag_params, limit, offset]).fetchall()

        count_sql = f"""
            SELECT COUNT(*) c FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {filters}
        """
        total = conn.execute(count_sql, [q, *tag_params]).fetchone()["c"]
    else:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls,
                   b.is_thread, b.thread_text,
                   NULL AS snippet_text
            FROM bookmarks b
            WHERE 1=1 {filters}
            ORDER BY b.created_at DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [*tag_params, limit, offset]).fetchall()

        count_sql = f"SELECT COUNT(*) c FROM bookmarks b WHERE 1=1 {filters}"
        total = conn.execute(count_sql, tag_params).fetchone()["c"]

    results = [_row_to_result(conn, r) for r in rows]
    _attach_tags(conn, results)
    _attach_types_and_titles(conn, results)
    return {"results": results, "total": total}


def library_summary(conn) -> dict:
    """Counts the home screen needs up front: the total, the per-type totals
    behind the filter chips, the unwatched-video count next to Watch Later,
    and when the library was last synced."""
    total = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]

    type_counts = {}
    for name, clause in TYPE_CLAUSES.items():
        type_counts[name] = conn.execute(
            f"SELECT COUNT(*) c FROM bookmarks b WHERE {clause}"
        ).fetchone()["c"]

    unwatched = conn.execute(
        "SELECT COUNT(*) c FROM watch_later WHERE status = 'unwatched'"
    ).fetchone()["c"]
    watched = conn.execute(
        "SELECT COUNT(*) c FROM watch_later WHERE status = 'watched'"
    ).fetchone()["c"]

    last_sync = conn.execute("SELECT MAX(synced_at) s FROM bookmarks").fetchone()["s"]

    return {
        "total": total,
        "types": type_counts,
        "watch_later": {"unwatched": unwatched, "watched": watched},
        "last_sync": last_sync,
    }


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


def get_bookmark_detail(conn, bookmark_id: int) -> dict | None:
    """Everything the item detail view renders: the full expanded thread as an
    ordered list of posts, the linked article summary or video transcript, the
    editable tag list, and the current watch-later state."""
    r = conn.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
    if not r:
        return None

    result = _row_to_result(conn, r)
    _attach_tags(conn, [result])
    _attach_types_and_titles(conn, [result])

    if r["is_thread"] and r["thread_text"]:
        result["posts"] = [p.strip() for p in r["thread_text"].split(THREAD_SEP) if p.strip()]
    else:
        result["posts"] = []

    linked_rows = conn.execute(
        """
        SELECT type, url, title, transcript_or_summary, status
        FROM linked_content WHERE bookmark_id = ? AND status = 'done'
        ORDER BY CASE type WHEN 'youtube' THEN 0 ELSE 1 END
        """,
        (bookmark_id,),
    ).fetchall()
    result["linked"] = [
        {
            "type": lr["type"],
            "url": lr["url"],
            "title": lr["title"],
            "content": lr["transcript_or_summary"],
        }
        for lr in linked_rows
    ]

    wl = conn.execute(
        "SELECT status FROM watch_later WHERE bookmark_id = ?", (bookmark_id,)
    ).fetchone()
    result["watch_status"] = wl["status"] if wl else None

    return result


def add_tag(conn, bookmark_id: int, name: str) -> dict | None:
    """Attach a tag to a bookmark, correcting the auto-tagging. Returns the
    bookmark's updated tag list, or None if the bookmark doesn't exist."""
    name = name.strip().lower()
    if not name:
        return None
    exists = conn.execute("SELECT 1 FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
    if not exists:
        return None
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)",
        (bookmark_id, tag_id),
    )
    conn.commit()
    return _current_tags(conn, bookmark_id)


def remove_tag(conn, bookmark_id: int, name: str) -> dict:
    """Detach a tag from a bookmark. The tag row itself is left alone even if
    it ends up unused; list_tags only surfaces tags that still have bookmarks."""
    conn.execute(
        """
        DELETE FROM bookmark_tags
        WHERE bookmark_id = ?
          AND tag_id = (SELECT id FROM tags WHERE name = ?)
        """,
        (bookmark_id, name.strip().lower()),
    )
    conn.commit()
    return _current_tags(conn, bookmark_id)


def _current_tags(conn, bookmark_id: int) -> dict:
    rows = conn.execute(
        """
        SELECT t.name FROM bookmark_tags bt
        JOIN tags t ON t.id = bt.tag_id
        WHERE bt.bookmark_id = ?
        ORDER BY t.name
        """,
        (bookmark_id,),
    ).fetchall()
    return {"bookmark_id": bookmark_id, "tags": [r["name"] for r in rows]}


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
    _attach_types_and_titles(conn, results)
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
    return {"bookmark_id": bookmark_id, "status": new_status, "watched_at": watched_at}

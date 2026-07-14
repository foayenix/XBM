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
    return {
        "id": r["id"],
        "author_username": r["author_username"],
        "author_name": r["author_name"],
        "created_at": r["created_at"],
        "text": r["text"],
        "snippet": r["snippet_text"] if "snippet_text" in r.keys() else None,
        "is_thread": bool(r["is_thread"]),
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


def search_bookmarks(conn, q: str | None, tags: list[str], limit: int, offset: int) -> dict:
    tag_clause, tag_params = _tag_filter_clause(tags)

    if q:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
                   snippet(bookmarks_fts, 0, ?, ?, '...', 24) AS snippet_text
            FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [SNIPPET_START, SNIPPET_END, q, *tag_params, limit, offset]).fetchall()

        count_sql = f"""
            SELECT COUNT(*) c FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause}
        """
        total = conn.execute(count_sql, [q, *tag_params]).fetchone()["c"]
    else:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
                   NULL AS snippet_text
            FROM bookmarks b
            WHERE 1=1 {tag_clause}
            ORDER BY b.created_at DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [*tag_params, limit, offset]).fetchall()

        count_sql = f"SELECT COUNT(*) c FROM bookmarks b WHERE 1=1 {tag_clause}"
        total = conn.execute(count_sql, tag_params).fetchone()["c"]

    results = [_row_to_result(conn, r) for r in rows]
    _attach_tags(conn, results)
    return {"results": results, "total": total}


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

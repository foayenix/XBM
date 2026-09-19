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


def build_fts_query(q: str) -> str:
    """Turn a user's search box input into a safe FTS5 MATCH expression.

    FTS5 parses its right-hand side as a query language, so raw user input
    blows up on perfectly ordinary text: an apostrophe ("what's"), a plus
    ("c++"), a trailing boolean ("foo AND"), a stray quote, or a colon
    ("x:", read as a column filter) each raise OperationalError. Since the
    UI searches on every keystroke, half-typed input hits this constantly.

    So we don't expose FTS5 syntax at all: split on whitespace and wrap each
    token in a quoted string (doubling any embedded quote, which is how FTS5
    escapes one). Tokens are implicitly ANDed, and every operator, wildcard,
    and column reference is neutralised into a literal. Punctuation the FTS5
    tokenizer ignores just drops out, so "c++" still matches a document
    containing "c++".

    Returns "" when the input has no usable tokens, which callers treat as
    "no query" rather than passing an empty MATCH (a syntax error in itself).
    """
    tokens = [t for t in (q or "").split() if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _attach_media(conn, bookmarks: list[dict]) -> None:
    """Fill in each bookmark's `kind` and `thumbnail` in one batched query.

    `kind` drives the type badge in the row list. Precedence is
    video > thread > article > tweet: the badge answers "what am I about to
    do with this", and a video is the one that lands in the watch-later
    queue, so it wins even when the bookmark is also a thread.
    """
    if not bookmarks:
        return

    ids = [b["id"] for b in bookmarks]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT bookmark_id, type, url FROM linked_content
        WHERE bookmark_id IN ({placeholders})
        ORDER BY id
        """,
        ids,
    ).fetchall()

    youtube_url_by_id: dict[int, str] = {}
    types_by_id: dict[int, set[str]] = {}
    for r in rows:
        types_by_id.setdefault(r["bookmark_id"], set()).add(r["type"])
        if r["type"] == "youtube":
            youtube_url_by_id.setdefault(r["bookmark_id"], r["url"])

    for b in bookmarks:
        media_urls = b.pop("_media_urls")
        link_types = types_by_id.get(b["id"], set())
        video_url = youtube_url_by_id.get(b["id"])

        if video_url:
            b["kind"] = "video"
        elif b["is_thread"]:
            b["kind"] = "thread"
        elif "article" in link_types:
            b["kind"] = "article"
        else:
            b["kind"] = "tweet"

        if media_urls:
            b["thumbnail"] = media_urls[0]
            continue
        video_id = extract_youtube_video_id(video_url) if video_url else None
        b["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else None


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


def _row_to_result(r) -> dict:
    """Shape one row. _media_urls is scratch, removed by _attach_media."""
    return {
        "id": r["id"],
        "author_username": r["author_username"],
        "author_name": r["author_name"],
        "created_at": r["created_at"],
        "text": r["text"],
        "snippet": r["snippet_text"] if "snippet_text" in r.keys() else None,
        "is_thread": bool(r["is_thread"]),
        "_media_urls": json.loads(r["media_urls"] or "[]"),
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

    # Never hand raw user input to FTS5 - see build_fts_query. An input that
    # reduces to no tokens (whitespace, punctuation only) falls through to the
    # plain browse query below instead of matching nothing.
    match_expr = build_fts_query(q) if q else ""

    if match_expr:
        sql = f"""
            SELECT b.id, b.author_username, b.author_name, b.created_at, b.text, b.media_urls, b.is_thread,
                   snippet(bookmarks_fts, 0, ?, ?, '...', 24) AS snippet_text
            FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, [SNIPPET_START, SNIPPET_END, match_expr, *tag_params, limit, offset]).fetchall()

        count_sql = f"""
            SELECT COUNT(*) c FROM bookmarks_fts
            JOIN bookmarks b ON b.id = bookmarks_fts.rowid
            WHERE bookmarks_fts MATCH ? {tag_clause}
        """
        total = conn.execute(count_sql, [match_expr, *tag_params]).fetchone()["c"]
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

    results = [_row_to_result(r) for r in rows]
    _attach_media(conn, results)
    _attach_tags(conn, results)
    return {
        "results": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        # Lets the UI decide whether to offer "Load more" without having to
        # infer it from a short page.
        "has_more": offset + len(results) < total,
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
        result = _row_to_result(r)
        result.update(watch_status=r["watch_status"], added_at=r["added_at"], watched_at=r["watched_at"])
        results.append(result)
    _attach_media(conn, results)
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


def get_stats(conn) -> dict:
    """Counts and last-sync time for the sidebar rail and the status bar."""
    bookmarks = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]
    unwatched = conn.execute(
        "SELECT COUNT(*) c FROM watch_later WHERE status = 'unwatched'"
    ).fetchone()["c"]
    watch_total = conn.execute("SELECT COUNT(*) c FROM watch_later").fetchone()["c"]
    # bookmarks.synced_at is stamped on every sync, so the newest one is the
    # last time a sync actually wrote something.
    last_synced = conn.execute("SELECT MAX(synced_at) m FROM bookmarks").fetchone()["m"]
    return {
        "bookmarks": bookmarks,
        "watch_later_unwatched": unwatched,
        "watch_later_total": watch_total,
        "last_synced_at": last_synced,
    }

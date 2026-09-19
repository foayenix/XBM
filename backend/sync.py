"""Pulls bookmarks from X and upserts them into the local database.

Idempotent: bookmarks.id (the tweet's snowflake id) is the primary key, so
re-running this never creates duplicates — existing rows are updated in
place via INSERT ... ON CONFLICT DO UPDATE.
"""
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from backend import db, jobs, x_auth
from backend.x_client import XClient

logger = logging.getLogger("xbm.sync")


def _extract_media_urls(tweet: dict, media_by_key: dict) -> list[str]:
    keys = (tweet.get("attachments") or {}).get("media_keys", [])
    urls = []
    for key in keys:
        media = media_by_key.get(key)
        if not media:
            continue
        url = media.get("url") or media.get("preview_image_url")
        if url:
            urls.append(url)
    return urls


# Hosts that are X itself (or its link shortener). Links to these are
# in-platform references, not external content worth enriching.
X_HOSTS = {"x.com", "twitter.com", "t.co"}


def _is_x_url(url: str) -> bool:
    """True if the URL points at X/Twitter itself, matched by hostname.

    Matching on hostname rather than a substring matters: a naive
    `"x.com" in url` check also swallows netflix.com, linux.com, phoenix.com
    and every other domain that merely ends in "x.com", silently dropping
    those bookmarks from enrichment and search with no error anywhere.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in X_HOSTS)


def _extract_external_links(tweet: dict) -> list[str]:
    links = []
    for u in (tweet.get("entities") or {}).get("urls", []):
        expanded = u.get("expanded_url") or u.get("url")
        if not expanded:
            continue
        parsed = urlparse(expanded)
        # Only http(s) - skip mailto:, javascript:, and other schemes we
        # would never fetch anyway.
        if parsed.scheme not in ("http", "https"):
            continue
        if _is_x_url(expanded):
            continue
        links.append(expanded)
    return links


def _upsert_bookmark(conn, tweet: dict, author: dict, is_thread: int, thread_text: str | None,
                     media_urls: list[str], external_links: list[str], synced_at: str) -> None:
    conn.execute(
        """
        INSERT INTO bookmarks
            (id, author_id, author_username, author_name, text, created_at,
             media_urls, external_links, is_thread, thread_text, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            author_id=excluded.author_id,
            author_username=excluded.author_username,
            author_name=excluded.author_name,
            text=excluded.text,
            created_at=excluded.created_at,
            media_urls=excluded.media_urls,
            external_links=excluded.external_links,
            is_thread=excluded.is_thread,
            thread_text=excluded.thread_text,
            synced_at=excluded.synced_at
        """,
        (
            int(tweet["id"]),
            tweet["author_id"],
            author.get("username", ""),
            author.get("name"),
            tweet["text"],
            tweet["created_at"],
            json.dumps(media_urls),
            json.dumps(external_links),
            is_thread,
            thread_text,
            synced_at,
        ),
    )


def sync_bookmarks() -> dict:
    with jobs.exclusive("sync"):
        return _sync_bookmarks()


def _sync_bookmarks() -> dict:
    conn = db.get_connection()
    new_count = 0
    updated_count = 0
    bookmark_reads = 0
    thread_tweet_reads = 0
    threads_reused = 0

    try:
        client = XClient()

        user = x_auth.get_logged_in_user()
        if not user:
            me = client.get_me()
            x_auth.store_logged_in_user(me["id"], me["username"])
            user_id = me["id"]
        else:
            user_id = user["user_id"]

        pagination_token = None
        synced_at = datetime.now(timezone.utc).isoformat()

        while True:
            page = client.get_bookmarks(user_id, pagination_token=pagination_token)
            tweets = page.get("data", [])
            bookmark_reads += len(tweets)

            media_by_key = {m["media_key"]: m for m in page.get("includes", {}).get("media", [])}
            users_by_id = {u["id"]: u for u in page.get("includes", {}).get("users", [])}

            for tweet in tweets:
                author = users_by_id.get(tweet["author_id"], {})
                existing = conn.execute(
                    "SELECT is_thread, thread_text FROM bookmarks WHERE id = ?", (int(tweet["id"]),)
                ).fetchone()

                if existing is None:
                    # Only expand threads for bookmarks we have never seen. A
                    # tweet's reply chain is immutable, so re-walking it on
                    # every sync buys nothing and costs one billed X API read
                    # per ancestor, per sync, forever.
                    thread_chain = None
                    refs = tweet.get("referenced_tweets") or []
                    if any(r["type"] == "replied_to" for r in refs):
                        thread_chain = client.expand_thread(tweet)
                        if thread_chain:
                            thread_tweet_reads += len(thread_chain) - 1
                    is_thread = int(bool(thread_chain))
                    thread_text = "\n\n---\n\n".join(t["text"] for t in thread_chain) if thread_chain else None
                    new_count += 1
                else:
                    # Carry the stored thread forward so the upsert does not
                    # clobber it with NULL.
                    is_thread = existing["is_thread"]
                    thread_text = existing["thread_text"]
                    if is_thread:
                        threads_reused += 1
                    updated_count += 1

                media_urls = _extract_media_urls(tweet, media_by_key)
                external_links = _extract_external_links(tweet)

                _upsert_bookmark(conn, tweet, author, is_thread, thread_text,
                                 media_urls, external_links, synced_at)

            conn.commit()  # commit each page so a later failure doesn't lose earlier pages' progress

            pagination_token = page.get("meta", {}).get("next_token")
            if not pagination_token:
                break
    except Exception:
        logger.warning(
            "Sync stopped early after %d new, %d updated bookmarks (%d bookmark reads so far). "
            "Already-synced pages are saved; re-running will pick up from the beginning and skip "
            "nothing thanks to the upsert-by-id logic, just re-reading a bit of already-seen data.",
            new_count, updated_count, bookmark_reads,
        )
        raise
    finally:
        conn.close()

    summary = {
        "new": new_count,
        "updated": updated_count,
        "bookmark_api_reads": bookmark_reads,
        "thread_expansion_api_reads": thread_tweet_reads,
        "threads_reused_from_cache": threads_reused,
    }
    logger.info(
        "Sync complete: %d new, %d updated, %d bookmark reads, %d thread-expansion reads, "
        "%d threads reused from cache",
        new_count, updated_count, bookmark_reads, thread_tweet_reads, threads_reused,
    )
    return summary

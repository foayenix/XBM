"""Pulls bookmarks from X and upserts them into the local database.

Idempotent: bookmarks.id (the tweet's snowflake id) is the primary key, so
re-running this never creates duplicates — existing rows are updated in
place via INSERT ... ON CONFLICT DO UPDATE.
"""
import json
import logging
from datetime import datetime, timezone

from backend import db, x_auth
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


def _extract_external_links(tweet: dict) -> list[str]:
    links = []
    for u in (tweet.get("entities") or {}).get("urls", []):
        expanded = u.get("expanded_url") or u.get("url")
        if expanded and "twitter.com" not in expanded and "x.com" not in expanded:
            links.append(expanded)
    return links


def _upsert_bookmark(conn, tweet: dict, author: dict, thread_chain: list[dict] | None,
                      media_urls: list[str], external_links: list[str], synced_at: str) -> bool:
    """Returns True if this was a new bookmark, False if it already existed."""
    existing = conn.execute("SELECT id FROM bookmarks WHERE id = ?", (int(tweet["id"]),)).fetchone()

    thread_text = "\n\n---\n\n".join(t["text"] for t in thread_chain) if thread_chain else None

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
            int(bool(thread_chain)),
            thread_text,
            synced_at,
        ),
    )
    return existing is None


def sync_bookmarks(progress=None) -> dict:
    """Pull and upsert all bookmarks.

    `progress`, if given, is called as progress(fetched=…, new=…, updated=…)
    after every processed bookmark so callers can show live counts.
    """
    conn = db.get_connection()
    new_count = 0
    updated_count = 0
    bookmark_reads = 0
    thread_tweet_reads = 0

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

                thread_chain = None
                refs = tweet.get("referenced_tweets") or []
                if any(r["type"] == "replied_to" for r in refs):
                    thread_chain = client.expand_thread(tweet)
                    if thread_chain:
                        thread_tweet_reads += len(thread_chain) - 1

                media_urls = _extract_media_urls(tweet, media_by_key)
                external_links = _extract_external_links(tweet)

                is_new = _upsert_bookmark(conn, tweet, author, thread_chain, media_urls, external_links, synced_at)
                if is_new:
                    new_count += 1
                else:
                    updated_count += 1
                if progress:
                    progress(fetched=new_count + updated_count, new=new_count, updated=updated_count)

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
    }
    logger.info(
        "Sync complete: %d new, %d updated, %d bookmark reads, %d thread-expansion reads",
        new_count, updated_count, bookmark_reads, thread_tweet_reads,
    )
    return summary

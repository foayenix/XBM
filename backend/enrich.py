"""Enrichment pipeline: YouTube transcripts, article summaries, and tagging.

Resumable by design:
- linked_content rows are seeded once per (bookmark, url) via INSERT OR
  IGNORE, then only rows with status != 'done' are (re)processed.
- Each linked_content row is committed right after it's processed, so a
  crash partway through only leaves that one row un-done, not the batch.
- Tagging only runs for bookmarks that don't have any tags yet.
"""
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic
from readability import Document
from youtube_transcript_api import YouTubeTranscriptApi

from backend import config, db, jobs
from backend.util import extract_youtube_video_id

logger = logging.getLogger("xbm.enrich")

# Cheap, fast model — this pipeline calls Claude once per article and once
# per bookmark, so cost adds up across a large bookmark backlog.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

HTTP_HEADERS = {"User-Agent": "XBM/0.1 (personal bookmarks tool; not for redistribution)"}
ARTICLE_TEXT_LIMIT = 12000  # chars sent to Claude for summarization
TRANSCRIPT_CHAR_LIMIT = 20000  # cap stored transcript length

# Enrichment reprocesses everything that is not finished, so a link that can
# never succeed (404, paywall, deleted video) would be re-fetched and
# re-summarized on every run - forever, at cost, and worse with the
# background scheduler on. After this many tries a row is left alone; clear
# its attempts column by hand to force a retry.
MAX_LINK_ATTEMPTS = 3
# Same problem for tagging, which selects bookmarks that have no tags yet: a
# bookmark Claude never returns usable tags for stays selected forever.
MAX_TAG_ATTEMPTS = 3


class EnrichmentError(Exception):
    pass


def _claude_client() -> Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise EnrichmentError("ANTHROPIC_API_KEY is not set. Fill it in in .env before running enrichment.")
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def classify_link(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        return "youtube"
    if host:
        return "article"
    return None


def seed_linked_content(conn) -> int:
    """Ensure every external link on every bookmark has a linked_content row."""
    rows = conn.execute("SELECT id, external_links FROM bookmarks").fetchall()
    seeded = 0
    for row in rows:
        links = json.loads(row["external_links"] or "[]")
        for url in links:
            link_type = classify_link(url)
            if not link_type:
                continue
            cursor = conn.execute(
                "INSERT OR IGNORE INTO linked_content (bookmark_id, type, url, status) VALUES (?, ?, ?, 'pending')",
                (row["id"], link_type, url),
            )
            seeded += cursor.rowcount
    conn.commit()
    return seeded


def _fetch_youtube_transcript(url: str) -> tuple[str, str | None]:
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise EnrichmentError(f"Could not parse a YouTube video id out of {url}")

    fetched = YouTubeTranscriptApi().fetch(video_id)
    text = " ".join(snippet.text for snippet in fetched)
    text = re.sub(r"\s+", " ", text).strip()[:TRANSCRIPT_CHAR_LIMIT]
    if not text:
        raise EnrichmentError(f"YouTube returned an empty transcript for {url}")

    title = None
    try:
        oembed = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            headers=HTTP_HEADERS,
            timeout=10.0,
        )
        if oembed.status_code == 200:
            title = oembed.json().get("title")
    except httpx.HTTPError:
        pass  # title is a nice-to-have, not worth failing the whole item over

    return text, title


def _extract_article_text(html: str) -> tuple[str, str]:
    doc = Document(html)
    title = doc.short_title() or ""
    import lxml.html as lh
    fragment = lh.fromstring(doc.summary())
    text = " ".join(fragment.text_content().split())
    return text, title


def _summarize_article(client: Anthropic, title: str, text: str) -> str:
    if not text.strip():
        raise EnrichmentError("Article text extraction produced no readable content")

    prompt = (
        "Summarize the following article in 2-4 short sentences, plain text, "
        "no preamble or headers.\n\n"
        f"Title: {title}\n\n{text[:ARTICLE_TEXT_LIMIT]}"
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=250,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def process_linked_content(conn) -> dict:
    client = None  # lazily created only if an article actually needs summarizing
    pending = conn.execute(
        "SELECT * FROM linked_content WHERE status != 'done' AND attempts < ?",
        (MAX_LINK_ATTEMPTS,),
    ).fetchall()

    gave_up = conn.execute(
        "SELECT COUNT(*) c FROM linked_content WHERE status != 'done' AND attempts >= ?",
        (MAX_LINK_ATTEMPTS,),
    ).fetchone()["c"]

    done = 0
    failed = 0
    claude_calls = 0

    for row in pending:
        now = datetime.now(timezone.utc).isoformat()
        # Count the attempt up front so a hard crash mid-item still burns a
        # try rather than leaving the row to spin forever.
        attempts = row["attempts"] + 1
        conn.execute("UPDATE linked_content SET attempts = ? WHERE id = ?", (attempts, row["id"]))
        conn.commit()
        try:
            if row["type"] == "youtube":
                transcript, title = _fetch_youtube_transcript(row["url"])
                conn.execute(
                    "UPDATE linked_content SET transcript_or_summary=?, title=?, status='done', error=NULL, fetched_at=? WHERE id=?",
                    (transcript, title, now, row["id"]),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO watch_later (bookmark_id, status, added_at) VALUES (?, 'unwatched', ?)",
                    (row["bookmark_id"], now),
                )
            else:  # article
                resp = httpx.get(row["url"], headers=HTTP_HEADERS, timeout=20.0, follow_redirects=True)
                resp.raise_for_status()
                text, title = _extract_article_text(resp.text)
                if client is None:
                    client = _claude_client()
                summary = _summarize_article(client, title, text)
                claude_calls += 1
                conn.execute(
                    "UPDATE linked_content SET transcript_or_summary=?, title=?, status='done', error=NULL, fetched_at=? WHERE id=?",
                    (summary, title, now, row["id"]),
                )
            done += 1
        except Exception as e:
            exhausted = attempts >= MAX_LINK_ATTEMPTS
            logger.warning(
                "Enrichment failed for linked_content id=%s url=%s (attempt %d/%d)%s: %s",
                row["id"], row["url"], attempts, MAX_LINK_ATTEMPTS,
                " - giving up, will not retry" if exhausted else "", e,
            )
            conn.execute(
                "UPDATE linked_content SET status='failed', error=?, fetched_at=? WHERE id=?",
                (str(e)[:500], now, row["id"]),
            )
            failed += 1
            if exhausted:
                gave_up += 1
        conn.commit()  # commit per item so a crash mid-run loses at most one item's progress

    return {
        "linked_content_done": done,
        "linked_content_failed": failed,
        "linked_content_gave_up": gave_up,
        "claude_summary_calls": claude_calls,
    }


def _parse_tags(raw: str) -> list[str]:
    tags = [t.strip().lower() for t in raw.split(",")]
    tags = [t for t in tags if t]
    return tags[:4]


def _tag_bookmark(client: Anthropic, conn, bookmark: dict) -> list[str]:
    parts = [bookmark["text"]]
    if bookmark["thread_text"]:
        parts.append(bookmark["thread_text"])
    linked = conn.execute(
        "SELECT transcript_or_summary FROM linked_content WHERE bookmark_id=? AND status='done'",
        (bookmark["id"],),
    ).fetchall()
    parts.extend(r["transcript_or_summary"] for r in linked if r["transcript_or_summary"])
    content = "\n\n".join(parts)[:ARTICLE_TEXT_LIMIT]

    prompt = (
        "Read the following bookmarked content and output 1 to 4 short topic "
        "tags for it — single words or short phrases, lowercase, comma-separated, "
        "no explanation, no numbering, no surrounding text.\n\n" + content
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_tags(resp.content[0].text)


def tag_untagged_bookmarks(conn) -> dict:
    rows = conn.execute(
        """
        SELECT b.id, b.text, b.thread_text, b.tag_attempts FROM bookmarks b
        WHERE NOT EXISTS (SELECT 1 FROM bookmark_tags bt WHERE bt.bookmark_id = b.id)
          AND b.tag_attempts < ?
        """,
        (MAX_TAG_ATTEMPTS,),
    ).fetchall()

    gave_up = conn.execute(
        """
        SELECT COUNT(*) c FROM bookmarks b
        WHERE NOT EXISTS (SELECT 1 FROM bookmark_tags bt WHERE bt.bookmark_id = b.id)
          AND b.tag_attempts >= ?
        """,
        (MAX_TAG_ATTEMPTS,),
    ).fetchone()["c"]

    if not rows:
        return {"bookmarks_tagged": 0, "claude_tagging_calls": 0, "bookmarks_tagging_gave_up": gave_up}

    client = _claude_client()
    tagged = 0
    calls = 0

    for row in rows:
        # Count the attempt before calling out, so a crash or a reply we
        # cannot parse still costs a try instead of looping forever.
        attempts = row["tag_attempts"] + 1
        conn.execute("UPDATE bookmarks SET tag_attempts = ? WHERE id = ?", (attempts, row["id"]))
        conn.commit()
        try:
            tags = _tag_bookmark(client, conn, row)
            calls += 1
            for tag_name in tags:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)", (row["id"], tag_id))
            if tags:
                tagged += 1
            else:
                # A call that came back with nothing parseable. It counts
                # against the cap like a failure, otherwise this bookmark is
                # re-sent to Claude on every future run.
                logger.warning(
                    "Tagging returned no usable tags for bookmark id=%s (attempt %d/%d)",
                    row["id"], attempts, MAX_TAG_ATTEMPTS,
                )
                if attempts >= MAX_TAG_ATTEMPTS:
                    gave_up += 1
        except Exception as e:
            exhausted = attempts >= MAX_TAG_ATTEMPTS
            logger.warning(
                "Tagging failed for bookmark id=%s (attempt %d/%d)%s: %s",
                row["id"], attempts, MAX_TAG_ATTEMPTS,
                " - giving up, will not retry" if exhausted else "", e,
            )
            if exhausted:
                gave_up += 1
        conn.commit()

    return {
        "bookmarks_tagged": tagged,
        "claude_tagging_calls": calls,
        "bookmarks_tagging_gave_up": gave_up,
    }


def run_enrichment() -> dict:
    with jobs.exclusive("enrichment"):
        return _run_enrichment()


def _run_enrichment() -> dict:
    conn = db.get_connection()
    try:
        seeded = seed_linked_content(conn)
        content_summary = process_linked_content(conn)
        tag_summary = tag_untagged_bookmarks(conn)
    finally:
        conn.close()

    summary = {"linked_content_seeded": seeded, **content_summary, **tag_summary}
    logger.info("Enrichment complete: %s", summary)
    return summary

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
from readability import Document
from youtube_transcript_api import YouTubeTranscriptApi

from backend import config, db, jobs, llm
from backend.fetching import (
    HTTP_HEADERS,
    UnsupportedContentError,
    fetch_article_html,
    looks_like_a_file_download,
)
from backend.llm import LLMError, LLMProvider
from backend.util import extract_youtube_video_id, is_youtube_url

logger = logging.getLogger("xbm.enrich")

# How much text goes to the model per call. Configurable because a local
# model's context window may be far smaller than a hosted one's.
ARTICLE_TEXT_LIMIT = config.LLM_INPUT_CHAR_LIMIT
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


def _llm() -> LLMProvider:
    """The configured model backend, or a readable error about why not."""
    try:
        return llm.get_provider()
    except LLMError as e:
        raise EnrichmentError(str(e)) from e


def classify_link(url: str) -> str | None:
    """Decide how to enrich a link, or None to leave it alone entirely.

    Host matching is by hostname, so music.youtube.com and m.youtube.com are
    recognised as YouTube (they were previously treated as articles, missing
    the transcript and the watch-later queue) while notyoutube.com is not.

    Anything that looks like a file download is skipped rather than
    classified as an article: a PDF or an image used to be fetched in full,
    pushed through the HTML parser, and summarized by Claude as if it were
    prose.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if is_youtube_url(url):
        return "youtube" if extract_youtube_video_id(url) else None
    if looks_like_a_file_download(url):
        return None
    return "article"


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


def _summarize_article(client: LLMProvider, title: str, text: str) -> str:
    if not text.strip():
        raise EnrichmentError("Article text extraction produced no readable content")

    prompt = (
        "Summarize the following article in 2-4 short sentences, plain text, "
        "no preamble or headers.\n\n"
        f"Title: {title}\n\n{text[:ARTICLE_TEXT_LIMIT]}"
    )
    return _strip_reasoning(client.complete(prompt, max_tokens=config.LLM_MAX_TOKENS))


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
    llm_calls = 0

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
                html, _final_url = fetch_article_html(row["url"])
                text, title = _extract_article_text(html)
                if client is None:
                    client = _llm()
                summary = _summarize_article(client, title, text)
                llm_calls += 1
                conn.execute(
                    "UPDATE linked_content SET transcript_or_summary=?, title=?, status='done', error=NULL, fetched_at=? WHERE id=?",
                    (summary, title, now, row["id"]),
                )
            done += 1
        except UnsupportedContentError as e:
            # Not a transient failure: this URL will never be an article, so
            # burn the remaining attempts now instead of refetching it twice
            # more on later runs.
            logger.info("Skipping linked_content id=%s url=%s: %s", row["id"], row["url"], e)
            conn.execute(
                "UPDATE linked_content SET status='failed', error=?, fetched_at=?, attempts=? WHERE id=?",
                (str(e)[:500], now, MAX_LINK_ATTEMPTS, row["id"]),
            )
            failed += 1
            gave_up += 1
            conn.commit()
            continue
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
        "llm_summary_calls": llm_calls,
    }


# A reasoning model emits its scratchpad before the answer. Strip it.
_REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
# A leading label on a line: "Tags:", "**Topics:**", "Sure! Here are the
# tags:". Anything short ending in a colon counts, punctuation included.
_LABEL_PREFIX = re.compile(r"^\s*\**[^:\n]{0,40}\**:\s*")
# Bullets and "1." / "1)" numbering.
_LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022\u2013]|\d{1,2}[.)])\s*")

MAX_TAG_WORDS = 3
MAX_TAG_CHARS = 30


def _strip_reasoning(raw: str) -> str:
    """Drop a reasoning model's <think> scratchpad from its reply."""
    return _REASONING_BLOCK.sub("", raw or "").strip()


def _parse_tags(raw: str) -> list[str]:
    """Pull tags out of whatever shape the model actually replied in.

    Claude follows "lowercase, comma-separated, no surrounding text" closely.
    Small local models mostly do not: they add a preamble, bullet the list,
    number it, bold a label, wrap tags in quotes, or think out loud first.
    Splitting on "," alone turns all of that into junk tags, so parse
    defensively and drop anything that reads like prose rather than a tag.
    """
    text = _strip_reasoning(raw)
    if not text:
        return []

    tags: list[str] = []
    seen: set[str] = set()

    for line in text.splitlines():
        line = _LIST_MARKER.sub("", line)
        line = _LABEL_PREFIX.sub("", line)
        for candidate in line.split(","):
            tag = _LIST_MARKER.sub("", candidate).strip()
            tag = tag.strip("\"'`*_[]()").strip().rstrip(".;:").strip().lower()
            if not tag:
                continue
            # Anything long, multi-word or still carrying a colon is a
            # sentence the model wrapped around the answer, not a tag.
            if len(tag) > MAX_TAG_CHARS or ":" in tag or len(tag.split()) > MAX_TAG_WORDS:
                continue
            if tag in seen:
                continue
            seen.add(tag)
            tags.append(tag)

    return tags[:4]


def _tag_bookmark(client: LLMProvider, conn, bookmark: dict) -> list[str]:
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
    return _parse_tags(client.complete(prompt, max_tokens=config.LLM_MAX_TOKENS))


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
        return {"bookmarks_tagged": 0, "llm_tagging_calls": 0, "bookmarks_tagging_gave_up": gave_up}

    client = _llm()
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
        "llm_tagging_calls": calls,
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

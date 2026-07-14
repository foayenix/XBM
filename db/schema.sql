-- XBM database schema.
-- Bookmark ids are X (Twitter) snowflake ids, which fit in SQLite's 64-bit
-- signed INTEGER, so we use them directly as INTEGER PRIMARY KEY (= rowid).
-- This lets bookmarks_fts below use the same id as its own rowid, so
-- keeping the two in sync is a plain UPDATE/DELETE by rowid.

CREATE TABLE IF NOT EXISTS bookmarks (
    id              INTEGER PRIMARY KEY,
    author_id       TEXT NOT NULL,
    author_username TEXT NOT NULL,
    author_name     TEXT,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    media_urls      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    external_links  TEXT NOT NULL DEFAULT '[]',   -- JSON array, feeds Phase 4 enrichment
    is_thread       INTEGER NOT NULL DEFAULT 0,
    thread_text     TEXT,                          -- full expanded thread, if is_thread
    synced_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_created_at ON bookmarks(created_at);

CREATE TABLE IF NOT EXISTS linked_content (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    bookmark_id           INTEGER NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    type                  TEXT NOT NULL CHECK (type IN ('youtube', 'article')),
    url                   TEXT NOT NULL,
    title                 TEXT,
    transcript_or_summary TEXT,
    status                TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    error                 TEXT,
    fetched_at            TEXT,
    UNIQUE (bookmark_id, url)
);

CREATE INDEX IF NOT EXISTS idx_linked_content_bookmark_id ON linked_content(bookmark_id);
CREATE INDEX IF NOT EXISTS idx_linked_content_status ON linked_content(status);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS bookmark_tags (
    bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (bookmark_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_bookmark_tags_tag_id ON bookmark_tags(tag_id);

CREATE TABLE IF NOT EXISTS watch_later (
    bookmark_id INTEGER PRIMARY KEY REFERENCES bookmarks(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'unwatched' CHECK (status IN ('unwatched', 'watched')),
    added_at    TEXT NOT NULL,
    watched_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_watch_later_status ON watch_later(status);

-- Full-text search over bookmark text + linked content summaries + tags.
-- Not an "external content" FTS5 table on purpose: source data lives across
-- three tables (bookmarks, linked_content, tags), so a plain FTS5 table kept
-- in sync by triggers below is simpler and more reliable than juggling
-- multiple external-content sources.
CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
    tweet_text,
    thread_text,
    author_username,
    linked_summary,
    tags_text
);

CREATE TRIGGER IF NOT EXISTS bookmarks_ai AFTER INSERT ON bookmarks BEGIN
    INSERT INTO bookmarks_fts (rowid, tweet_text, thread_text, author_username, linked_summary, tags_text)
    VALUES (new.id, new.text, new.thread_text, new.author_username, NULL, NULL);
END;

CREATE TRIGGER IF NOT EXISTS bookmarks_au AFTER UPDATE ON bookmarks BEGIN
    UPDATE bookmarks_fts
    SET tweet_text = new.text, thread_text = new.thread_text, author_username = new.author_username
    WHERE rowid = new.id;
END;

CREATE TRIGGER IF NOT EXISTS bookmarks_ad AFTER DELETE ON bookmarks BEGIN
    DELETE FROM bookmarks_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS linked_content_ai AFTER INSERT ON linked_content BEGIN
    UPDATE bookmarks_fts
    SET linked_summary = (SELECT group_concat(transcript_or_summary, ' ') FROM linked_content WHERE bookmark_id = new.bookmark_id)
    WHERE rowid = new.bookmark_id;
END;

CREATE TRIGGER IF NOT EXISTS linked_content_au AFTER UPDATE ON linked_content BEGIN
    UPDATE bookmarks_fts
    SET linked_summary = (SELECT group_concat(transcript_or_summary, ' ') FROM linked_content WHERE bookmark_id = new.bookmark_id)
    WHERE rowid = new.bookmark_id;
END;

CREATE TRIGGER IF NOT EXISTS linked_content_ad AFTER DELETE ON linked_content BEGIN
    UPDATE bookmarks_fts
    SET linked_summary = (SELECT group_concat(transcript_or_summary, ' ') FROM linked_content WHERE bookmark_id = old.bookmark_id)
    WHERE rowid = old.bookmark_id;
END;

CREATE TRIGGER IF NOT EXISTS bookmark_tags_ai AFTER INSERT ON bookmark_tags BEGIN
    UPDATE bookmarks_fts
    SET tags_text = (SELECT group_concat(t.name, ' ') FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id WHERE bt.bookmark_id = new.bookmark_id)
    WHERE rowid = new.bookmark_id;
END;

CREATE TRIGGER IF NOT EXISTS bookmark_tags_ad AFTER DELETE ON bookmark_tags BEGIN
    UPDATE bookmarks_fts
    SET tags_text = (SELECT group_concat(t.name, ' ') FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id WHERE bt.bookmark_id = old.bookmark_id)
    WHERE rowid = old.bookmark_id;
END;

"""The retry counters in blocker 4 are new columns, and schema.sql alone
cannot add a column to a database that already exists - CREATE TABLE IF NOT
EXISTS silently does nothing. These tests cover the migration path that
closes that gap.
"""
import sqlite3

import pytest

from scripts.init_db import LATEST_VERSION, _column_exists, init_db


def test_fresh_database_is_at_the_latest_version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        assert _column_exists(conn, "linked_content", "attempts")
        assert _column_exists(conn, "bookmarks", "tag_attempts")
    finally:
        conn.close()


def test_init_db_is_idempotent(db_path):
    init_db()
    init_db()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    finally:
        conn.close()


def test_a_pre_migration_database_gets_the_new_columns(tmp_path, monkeypatch):
    """Simulate a database created before the retry counters existed."""
    from backend import config

    old_db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY, author_id TEXT NOT NULL, author_username TEXT NOT NULL,
            author_name TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
            media_urls TEXT NOT NULL DEFAULT '[]', external_links TEXT NOT NULL DEFAULT '[]',
            is_thread INTEGER NOT NULL DEFAULT 0, thread_text TEXT, synced_at TEXT NOT NULL
        );
        CREATE TABLE linked_content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
            type TEXT NOT NULL CHECK (type IN ('youtube','article')), url TEXT NOT NULL,
            title TEXT, transcript_or_summary TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
            error TEXT, fetched_at TEXT, UNIQUE (bookmark_id, url)
        );
        INSERT INTO bookmarks VALUES (1,'a','alice','Alice','hi','2026-01-01T00:00:00Z','[]','[]',0,NULL,'2026-01-01T00:00:00Z');
        INSERT INTO linked_content (bookmark_id,type,url,status) VALUES (1,'article','https://e.com/a','failed');
        """
    )
    conn.commit()
    conn.close()

    assert sqlite3.connect(old_db).execute("PRAGMA user_version").fetchone()[0] == 0

    monkeypatch.setattr(config, "DATABASE_PATH", old_db)
    init_db()

    conn = sqlite3.connect(old_db)
    try:
        assert _column_exists(conn, "linked_content", "attempts")
        assert _column_exists(conn, "bookmarks", "tag_attempts")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        # Existing rows survive and default to zero attempts, so they get a
        # fair chance rather than starting out already exhausted.
        assert conn.execute("SELECT attempts FROM linked_content WHERE id=1").fetchone()[0] == 0
        assert conn.execute("SELECT tag_attempts FROM bookmarks WHERE id=1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 1
    finally:
        conn.close()


def test_enrichment_queries_work_against_a_migrated_database(tmp_path, monkeypatch):
    """The real regression: retry-capped SQL must run on an upgraded file."""
    from backend import config, db, enrich

    old_db = tmp_path / "old2.sqlite3"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY, author_id TEXT NOT NULL, author_username TEXT NOT NULL,
            author_name TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
            media_urls TEXT NOT NULL DEFAULT '[]', external_links TEXT NOT NULL DEFAULT '[]',
            is_thread INTEGER NOT NULL DEFAULT 0, thread_text TEXT, synced_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DATABASE_PATH", old_db)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    init_db()

    c = db.get_connection()
    try:
        assert enrich.process_linked_content(c)["linked_content_gave_up"] == 0
        assert enrich.tag_untagged_bookmarks(c)["bookmarks_tagged"] == 0
    finally:
        c.close()

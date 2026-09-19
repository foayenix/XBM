"""Shared fixtures.

Every test runs against a throwaway SQLite file built by the real
scripts/init_db.py, so the schema and its migrations are exercised on each
run rather than mocked.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend import config  # noqa: E402


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    from scripts.init_db import init_db

    init_db()
    return config.DATABASE_PATH


@pytest.fixture
def conn(db_path):
    from backend import db

    connection = db.get_connection()
    yield connection
    connection.close()


@pytest.fixture
def client(db_path):
    from fastapi.testclient import TestClient

    # Imported here, not at module scope, so config.LOG_DIR is already
    # redirected at tmp_path when backend.main configures its file logger.
    from backend.main import app

    # base_url matters: backend/security.py pins the Host header, so the
    # default "testserver" host would be rejected with 403.
    with TestClient(app, base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def logged_in(conn):
    """Seed the single oauth_tokens row so sync believes it is logged in."""
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO oauth_tokens (id, access_token, refresh_token, expires_at, scope, user_id, username, updated_at)
        VALUES (1, 'test-access-token', 'test-refresh-token', ?, 'bookmark.read', '999', 'testuser', ?)
        """,
        ((now.replace(year=now.year + 1)).isoformat(), now.isoformat()),
    )
    conn.commit()
    return {"user_id": "999", "username": "testuser"}


def add_bookmark(conn, bookmark_id, text, *, username="alice", links=None,
                 thread_text=None, created_at="2026-01-01T00:00:00Z"):
    """Insert a bookmark directly, bypassing sync. Returns its id."""
    conn.execute(
        """
        INSERT INTO bookmarks
            (id, author_id, author_username, author_name, text, created_at,
             media_urls, external_links, is_thread, thread_text, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
        """,
        (
            bookmark_id, f"author-{username}", username, username.title(), text, created_at,
            json.dumps(links or []), int(bool(thread_text)), thread_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return bookmark_id


@pytest.fixture
def seed(conn):
    return lambda *a, **kw: add_bookmark(conn, *a, **kw)

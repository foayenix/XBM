"""SQLite connection helper shared by the sync, enrichment, and API layers."""
import sqlite3

from backend import config


# How long a writer waits for another writer's transaction before giving up
# with "database is locked". backend/jobs.py already serialises the long
# sync/enrichment jobs; this covers the short overlaps left over (a UI read
# landing mid-write) with more headroom than sqlite3's 5 second default.
BUSY_TIMEOUT_SECONDS = 30.0


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    return conn

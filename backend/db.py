"""SQLite connection helper shared by the sync, enrichment, and API layers."""
import sqlite3

from backend import config

# Columns added after the initial schema shipped. Existing local databases
# won't have them, so we add them on first connection in-process. New
# databases get them straight from schema.sql and this becomes a no-op.
_ADDED_COLUMNS = {
    "linked_content": {
        "duration_seconds": "INTEGER",
        "reading_minutes": "INTEGER",
    },
}
_migrated = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _migrated
    if _migrated:
        return
    for table, columns in _ADDED_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    _migrated = True


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _ensure_schema(conn)
    return conn

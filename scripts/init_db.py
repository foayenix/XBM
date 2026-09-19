#!/usr/bin/env python3
"""Create (or upgrade) the local SQLite database.

Two things happen here:

1. Migrations run. schema.sql alone cannot evolve a database that already
   exists - CREATE TABLE IF NOT EXISTS silently does nothing when the table
   is there, so a column added to schema.sql would never reach an existing
   install. The migrations below close that gap, tracked with the SQLite
   user_version pragma. They go first because schema.sql may reference
   columns that a migration introduces.
2. db/schema.sql is applied. Every statement in it is IF NOT EXISTS, so this
   creates whatever is missing and leaves existing objects alone.

Safe to re-run.

Usage:
    python scripts/init_db.py
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend import config  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, skipped if the table or column is absent.

    Migrations run before schema.sql, so on a brand new database the table
    does not exist yet and there is nothing to migrate - schema.sql creates
    it already in its current shape. Every step is idempotent so a
    half-applied upgrade re-runs harmlessly.
    """
    if _table_exists(conn, table) and not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migration_1_retry_counters(conn: sqlite3.Connection) -> None:
    """Retry caps for enrichment: linked_content.attempts, bookmarks.tag_attempts."""
    _add_column(conn, "linked_content", "attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "bookmarks", "tag_attempts", "INTEGER NOT NULL DEFAULT 0")
    if _column_exists(conn, "linked_content", "attempts"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linked_content_retry ON linked_content(status, attempts)"
        )


# Applied in order for any database whose user_version is below the entry's
# number. Append new migrations here; never renumber or edit an existing one.
MIGRATIONS = [
    (1, _migration_1_retry_counters),
]

LATEST_VERSION = max(v for v, _ in MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection) -> list[int]:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = []
    for version, migrate in MIGRATIONS:
        if version > current:
            migrate(conn)
            # user_version does not accept a bound parameter.
            conn.execute(f"PRAGMA user_version = {version}")
            applied.append(version)
    return applied


def init_db() -> None:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text()

    conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Migrations run FIRST. schema.sql describes the current shape, and
        # parts of it (indexes over migrated columns) cannot be applied to an
        # older database until those columns exist. On a new database the
        # migrations find no tables, skip, and schema.sql builds the current
        # shape directly.
        applied = _apply_migrations(conn)
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    if applied:
        print(f"Applied migrations: {', '.join(str(v) for v in applied)}")
    print(f"Database ready at {config.DATABASE_PATH} (schema v{LATEST_VERSION})")


if __name__ == "__main__":
    init_db()

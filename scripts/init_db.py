#!/usr/bin/env python3
"""Create (or update) the local SQLite database from db/schema.sql.

Safe to re-run: every statement in schema.sql uses IF NOT EXISTS, so this
only fills in whatever tables/indexes/triggers are missing.

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


def init_db() -> None:
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text()

    conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    print(f"Database ready at {config.DATABASE_PATH}")


if __name__ == "__main__":
    init_db()

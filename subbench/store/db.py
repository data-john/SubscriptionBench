"""Connection helpers and migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_VERSION


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys and WAL enabled."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{current} is newer than code v{SCHEMA_VERSION}")
    for version in range(current, SCHEMA_VERSION):
        conn.executescript(
            "BEGIN;\n" + MIGRATIONS[version] + f"\nPRAGMA user_version = {version + 1};\nCOMMIT;"
        )
    return schema_version(conn)

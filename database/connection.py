"""
SQLite connection helpers.

Every part of the app opens short-lived connections through these functions.
`transaction()` wraps related writes so they either ALL succeed or ALL roll
back - this is what keeps quantities consistent when something fails halfway.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import settings

# Tests point the app at a temporary database by calling set_database_path().
_database_path_override: Path | None = None


def set_database_path(path) -> None:
    """Use a different database file (used by tests)."""
    global _database_path_override
    _database_path_override = Path(path) if path else None


def get_database_path() -> Path:
    """Return the database file currently in use."""
    return _database_path_override or settings.DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    """
    Open a new SQLite connection.

    - Rows behave like dictionaries (row["name"]).
    - Foreign keys are enforced (SQLite turns them off by default!).
    """
    path = get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction(cancellation_token=None):
    """
    Run several writes as one atomic unit.

    Usage:
        with transaction() as conn:
            conn.execute(...)
            conn.execute(...)
    If any statement raises an error, every change in the block is undone.

    `cancellation_token` (optional, see utils/cancellation.py) is used by agent
    tools: if the agent cancelled the tool after a timeout, nothing is saved.
    """
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")  # take the write lock early to avoid double-writes
        yield connection
        if cancellation_token is not None:
            cancellation_token.commit_if_not_cancelled(connection)
        else:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return every row as a plain dictionary."""
    connection = get_connection()
    try:
        rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    """Run a SELECT and return the first row as a dictionary (or None)."""
    rows = fetch_all(sql, params)
    return rows[0] if rows else None

"""Shared fixtures for KB reliability scenario tests.

All fixtures create in-memory SQLite databases with the minimal schema
required by the KB modules under test. Each fixture yields a fresh,
isolated connection so tests are fully independent.
"""
from __future__ import annotations

import sqlite3
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# Ensure the src tree is importable regardless of whether the package is
# installed into the test environment.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

_KNOWLEDGE_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid         TEXT    NOT NULL UNIQUE,
    workspace    TEXT    NOT NULL DEFAULT 'ws',
    source_path  TEXT,
    content_hash TEXT    NOT NULL,
    content      TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    source_type  TEXT    NOT NULL DEFAULT 'test',
    content_type TEXT    NOT NULL DEFAULT 'text',
    expires_at   TEXT
);

CREATE TABLE IF NOT EXISTS embedding_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL REFERENCES knowledge_entries(id),
    status     TEXT    NOT NULL DEFAULT 'pending',
    attempts   INTEGER DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    vector       BLOB,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    accessed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embedding_cache_accessed
    ON embedding_cache (accessed_at);
"""


def make_knowledge_db() -> sqlite3.Connection:
    """Return a fresh in-memory connection with the full KB schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(_KNOWLEDGE_DDL)
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def kb_db() -> Generator[sqlite3.Connection, None, None]:
    """In-memory KB database, freshly created per test."""
    conn = make_knowledge_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Row-insertion helpers (re-exported so test modules can import from here)
# ---------------------------------------------------------------------------


def insert_entry(
    conn: sqlite3.Connection,
    *,
    uuid: str,
    workspace: str = "ws",
    source_path: str = "file.py",
    content_hash: str = "abc123",
    expires_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO knowledge_entries "
        "(uuid, workspace, source_path, content_hash, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uuid, workspace, source_path, content_hash, expires_at),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def insert_queue_item(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    status: str = "pending",
    attempts: int = 0,
    last_error: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO embedding_queue (entry_id, status, attempts, last_error) "
        "VALUES (?, ?, ?, ?)",
        (entry_id, status, attempts, last_error),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def insert_cache_entry(conn: sqlite3.Connection, content_hash: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO embedding_cache (content_hash) VALUES (?)",
        (content_hash,),
    )
    conn.commit()


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

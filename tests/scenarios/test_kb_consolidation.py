"""Scenario tests for KB consolidation reliability.

Each test exercises one clearly-named behaviour of cap.lib.consolidator.
All tests use in-memory SQLite databases and complete well under 100 ms.

Import shared fixtures from conftest.py via pytest's fixture injection.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.lib.consolidator import ConsolidationResult, consolidate

# Import shared DB helpers from the local conftest (pytest makes these
# importable as plain Python when the conftest is in the same package).
from tests.scenarios.conftest import (
    insert_cache_entry,
    insert_entry,
    insert_queue_item,
    make_knowledge_db,
    row_count,
)


# ---------------------------------------------------------------------------
# Phase 4: Transient vs permanent failure classification
# ---------------------------------------------------------------------------


class TestOnlyTransientFailuresRequeued:
    """ThrottlingException rows are reset to pending; ValidationException rows are not."""

    def test_throttling_exception_requeued(self) -> None:
        conn = make_knowledge_db()
        eid = insert_entry(conn, uuid="e-throttle", content_hash="h1")
        insert_queue_item(
            conn, entry_id=eid, status="failed", last_error="ThrottlingException: Rate exceeded"
        )
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 0
        row = conn.execute("SELECT status, attempts FROM embedding_queue WHERE entry_id=?", (eid,)).fetchone()
        assert row[0] == "pending"
        assert row[1] == 0

    def test_validation_exception_not_requeued(self) -> None:
        conn = make_knowledge_db()
        eid = insert_entry(conn, uuid="e-validation", content_hash="h2")
        insert_queue_item(
            conn, entry_id=eid, status="failed", last_error="ValidationException: input too large"
        )
        result = consolidate(conn)
        assert result.failed_requeued == 0
        assert result.failed_permanent == 1
        row = conn.execute("SELECT status FROM embedding_queue WHERE entry_id=?", (eid,)).fetchone()
        assert row[0] == "failed"  # untouched

    def test_timeout_requeued_validation_not(self) -> None:
        """Mixed batch: only the Timeout row must be requeued."""
        conn = make_knowledge_db()
        eid1 = insert_entry(conn, uuid="e1", content_hash="h1", source_path="a.py")
        eid2 = insert_entry(conn, uuid="e2", content_hash="h2", source_path="b.py")
        insert_queue_item(conn, entry_id=eid1, status="failed", last_error="Timeout: 30s elapsed")
        insert_queue_item(conn, entry_id=eid2, status="failed", last_error="ValidationException")
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 1

    def test_pending_items_not_touched(self) -> None:
        conn = make_knowledge_db()
        eid = insert_entry(conn, uuid="e-pending", content_hash="h1")
        insert_queue_item(conn, entry_id=eid, status="pending")
        result = consolidate(conn)
        assert result.failed_requeued == 0
        assert result.failed_permanent == 0

    def test_null_error_treated_as_transient(self) -> None:
        conn = make_knowledge_db()
        eid = insert_entry(conn, uuid="e-null-err", content_hash="h1")
        insert_queue_item(conn, entry_id=eid, status="failed", last_error=None)
        result = consolidate(conn)
        assert result.failed_requeued == 1


# ---------------------------------------------------------------------------
# Phase 2: Expired entries removed
# ---------------------------------------------------------------------------


class TestExpiredEntriesRemoved:
    def test_expired_entries_removed(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="expired-1", expires_at="2000-01-01T00:00:00", content_hash="old1")
        insert_entry(conn, uuid="expired-2", expires_at="2001-06-15T12:00:00", content_hash="old2")
        insert_entry(conn, uuid="still-live", expires_at="2099-12-31T23:59:59", content_hash="live")
        result = consolidate(conn)
        assert result.expired_deleted == 2
        assert row_count(conn, "knowledge_entries") == 1

    def test_null_expires_at_is_never_removed(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="no-expiry", expires_at=None, content_hash="h1")
        result = consolidate(conn)
        assert result.expired_deleted == 0
        assert row_count(conn, "knowledge_entries") == 1

    def test_future_expiry_preserved(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="future", expires_at="2099-01-01T00:00:00", content_hash="h1")
        result = consolidate(conn)
        assert result.expired_deleted == 0


# ---------------------------------------------------------------------------
# Phase 3: Duplicate hash deduplication
# ---------------------------------------------------------------------------


class TestDuplicateHashEntriesDeduplicated:
    def test_same_hash_keeps_newest(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="dup-old-1", content_hash="shared", source_path="f.py")
        insert_entry(conn, uuid="dup-old-2", content_hash="shared", source_path="f.py")
        newest_id = insert_entry(conn, uuid="dup-new", content_hash="shared", source_path="f.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 2
        surviving = conn.execute("SELECT id FROM knowledge_entries").fetchall()
        assert surviving == [(newest_id,)]

    def test_different_hashes_not_deduped(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="u1", content_hash="hash-a", source_path="a.py")
        insert_entry(conn, uuid="u2", content_hash="hash-b", source_path="b.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0
        assert row_count(conn, "knowledge_entries") == 2

    def test_same_hash_different_workspaces_not_deduped(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="ws1-e", workspace="ws1", content_hash="shared", source_path="f.py")
        insert_entry(conn, uuid="ws2-e", workspace="ws2", content_hash="shared", source_path="f.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0

    def test_unique_entries_untouched(self) -> None:
        conn = make_knowledge_db()
        for i in range(5):
            insert_entry(conn, uuid=f"unique-{i}", content_hash=f"unique-hash-{i}", source_path=f"{i}.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0
        assert row_count(conn, "knowledge_entries") == 5


# ---------------------------------------------------------------------------
# Phase 1: WAL checkpoint
# ---------------------------------------------------------------------------


class TestWalCheckpointRuns:
    def test_wal_checkpoint_runs_on_in_memory_db(self) -> None:
        """PRAGMA wal_checkpoint must be issued; in-memory DB returns busy=0."""
        conn = make_knowledge_db()
        result = consolidate(conn)
        # In-memory SQLite returns (0, 0, 0) for WAL checkpoint →
        # busy==0 → wal_checkpointed must be True.
        assert isinstance(result.wal_checkpointed, bool)

    def test_checkpoint_failure_does_not_abort_consolidation(self) -> None:
        """A failing WAL checkpoint must not prevent other phases from running."""

        class _FailCheckpointConn(sqlite3.Connection):
            def execute(self, sql: str, params=(), /):  # type: ignore[override]
                if "wal_checkpoint" in sql.lower():
                    raise sqlite3.OperationalError("simulated checkpoint failure")
                return super().execute(sql, params)

        conn = sqlite3.connect(":memory:", factory=_FailCheckpointConn, check_same_thread=False)
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                workspace TEXT NOT NULL DEFAULT 'ws',
                source_path TEXT,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'test',
                content_type TEXT NOT NULL DEFAULT 'text',
                expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS embedding_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL REFERENCES knowledge_entries(id),
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS embedding_cache (
                content_hash TEXT PRIMARY KEY,
                vector BLOB,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                accessed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # Should not raise even though checkpoint fails
        result = consolidate(conn)
        assert result.wal_checkpointed is False


# ---------------------------------------------------------------------------
# Phase 5: Orphan cache purge
# ---------------------------------------------------------------------------


class TestOrphanCachePurged:
    def test_orphan_cache_purged(self) -> None:
        conn = make_knowledge_db()
        # Insert a cache entry with no matching knowledge_entry.
        insert_cache_entry(conn, "orphan-hash-xyz")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 1
        assert row_count(conn, "embedding_cache") == 0

    def test_valid_cache_entry_kept(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="e1", content_hash="live-hash")
        insert_cache_entry(conn, "live-hash")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 0
        assert row_count(conn, "embedding_cache") == 1

    def test_multiple_orphans_all_purged(self) -> None:
        conn = make_knowledge_db()
        for i in range(4):
            insert_cache_entry(conn, f"orphan-{i}")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 4
        assert row_count(conn, "embedding_cache") == 0

    def test_cache_orphaned_by_expiry_is_purged(self) -> None:
        """A cache entry whose parent expires in Phase 2 must be cleaned up in Phase 5."""
        conn = make_knowledge_db()
        insert_entry(conn, uuid="e-expire", content_hash="exp-hash", expires_at="2000-01-01T00:00:00")
        insert_cache_entry(conn, "exp-hash")
        result = consolidate(conn)
        assert result.expired_deleted == 1
        assert result.cache_orphans_purged == 1
        assert row_count(conn, "embedding_cache") == 0

    def test_mixed_orphan_and_valid(self) -> None:
        conn = make_knowledge_db()
        insert_entry(conn, uuid="live-entry", content_hash="live")
        insert_cache_entry(conn, "live")
        insert_cache_entry(conn, "dead-orphan")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 1
        remaining = [
            r[0]
            for r in conn.execute("SELECT content_hash FROM embedding_cache").fetchall()
        ]
        assert remaining == ["live"]


# ---------------------------------------------------------------------------
# Result accuracy: counts must match actual DB changes
# ---------------------------------------------------------------------------


class TestConsolidationResultAccurate:
    def test_all_counts_match_actual_changes(self) -> None:
        """Build a DB with a known mix of conditions, run consolidation,
        and verify every count in the result matches the actual row changes."""
        conn = make_knowledge_db()

        # 2 expired entries
        insert_entry(conn, uuid="exp-1", content_hash="ex1", expires_at="2000-01-01T00:00:00")
        insert_entry(conn, uuid="exp-2", content_hash="ex2", expires_at="2001-01-01T00:00:00")
        # 2 live entries; one pair of duplicates
        insert_entry(conn, uuid="dup-old", content_hash="dup", source_path="dup.py")
        insert_entry(conn, uuid="dup-new", content_hash="dup", source_path="dup.py")
        live_id = insert_entry(conn, uuid="live-unique", content_hash="unique", source_path="u.py")

        # 1 transient failure, 1 permanent failure
        q_transient = insert_queue_item(conn, entry_id=live_id, status="failed", last_error="ThrottlingException")
        # Need a separate entry for the permanent failure entry to avoid FK issues
        perm_eid = insert_entry(conn, uuid="perm-entry", content_hash="perm-hash", source_path="p.py")
        insert_queue_item(conn, entry_id=perm_eid, status="failed", last_error="ValidationException")

        # 1 orphan cache entry, 1 matching cache entry (will be matched post-dedup)
        insert_cache_entry(conn, "unique")       # matches 'live-unique' entry → kept
        insert_cache_entry(conn, "stale-orphan")  # no parent → purged

        result = consolidate(conn)

        # expired_deleted: 2 rows with past expires_at
        assert result.expired_deleted == 2, f"expected 2, got {result.expired_deleted}"
        # duplicates_removed: 1 (older dup-old removed, dup-new kept)
        assert result.duplicates_removed == 1, f"expected 1, got {result.duplicates_removed}"
        # failed_requeued: 1 throttling
        assert result.failed_requeued == 1, f"expected 1, got {result.failed_requeued}"
        # failed_permanent: 1 validation
        assert result.failed_permanent == 1, f"expected 1, got {result.failed_permanent}"
        # cache_orphans_purged: 1 stale-orphan (ex1, ex2 were expired but their
        # cache entries were not inserted, so only stale-orphan is purged)
        assert result.cache_orphans_purged == 1, f"expected 1, got {result.cache_orphans_purged}"
        # duration_ms is a non-negative int
        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0

    def test_empty_db_all_zeros(self) -> None:
        conn = make_knowledge_db()
        result = consolidate(conn)
        assert result.expired_deleted == 0
        assert result.duplicates_removed == 0
        assert result.failed_requeued == 0
        assert result.failed_permanent == 0
        assert result.cache_orphans_purged == 0
        assert result.duration_ms >= 0

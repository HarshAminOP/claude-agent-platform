"""Tests for cap.lib.consolidator — knowledge base consolidation."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.consolidator import ConsolidationResult, _classify_failure, consolidate


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_db() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the minimal knowledge.db schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL,
            source_path TEXT,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'test',
            content_type TEXT NOT NULL DEFAULT 'text',
            expires_at TEXT
        );

        CREATE TABLE embedding_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES knowledge_entries(id),
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_error TEXT
        );

        CREATE TABLE embedding_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            embedding BLOB
        );
        """
    )
    return conn


def _insert_entry(
    conn: sqlite3.Connection,
    *,
    uuid: str,
    workspace: str = "ws",
    source_path: str = "file.py",
    content_hash: str = "abc",
    expires_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO knowledge_entries (uuid, workspace, source_path, content_hash, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uuid, workspace, source_path, content_hash, expires_at),
    )
    conn.commit()
    return cur.lastrowid


def _insert_queue(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    status: str = "pending",
    attempts: int = 0,
    last_error: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO embedding_queue (entry_id, status, attempts, last_error) VALUES (?, ?, ?, ?)",
        (entry_id, status, attempts, last_error),
    )
    conn.commit()
    return cur.lastrowid


def _insert_cache(conn: sqlite3.Connection, content_hash: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO embedding_cache (content_hash) VALUES (?)", (content_hash,)
    )
    conn.commit()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── _classify_failure ─────────────────────────────────────────────────────────


class TestClassifyFailure:
    def test_none_is_transient(self):
        assert _classify_failure(None) == "transient"

    def test_throttling_is_transient(self):
        assert _classify_failure("ThrottlingException: Rate exceeded") == "transient"
        assert _classify_failure("Throttled by service") == "transient"

    def test_timeout_is_transient(self):
        assert _classify_failure("Timeout connecting to endpoint") == "transient"
        assert _classify_failure("ReadTimeout") == "transient"

    def test_validation_exception_is_permanent(self):
        assert _classify_failure("ValidationException: Bad input") == "permanent"

    def test_generic_error_is_permanent(self):
        assert _classify_failure("InternalServerError") == "permanent"
        assert _classify_failure("AccessDeniedException") == "permanent"

    def test_empty_string_is_permanent(self):
        # An empty string is not None and contains neither pattern
        assert _classify_failure("") == "permanent"


# ── consolidate — result type ─────────────────────────────────────────────────


class TestConsolidationResult:
    def test_empty_db_returns_zero_counts(self):
        conn = _make_db()
        result = consolidate(conn)
        assert isinstance(result, ConsolidationResult)
        assert result.expired_deleted == 0
        assert result.duplicates_removed == 0
        assert result.failed_requeued == 0
        assert result.failed_permanent == 0
        assert result.cache_orphans_purged == 0
        assert result.duration_ms >= 0

    def test_duration_ms_is_populated(self):
        conn = _make_db()
        result = consolidate(conn)
        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0


# ── Phase 2: expired entries ──────────────────────────────────────────────────


class TestPhase2Expired:
    def test_expired_entry_deleted(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", expires_at="2000-01-01T00:00:00")
        result = consolidate(conn)
        assert result.expired_deleted == 1
        assert _count(conn, "knowledge_entries") == 0

    def test_non_expired_entry_kept(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", expires_at="2099-01-01T00:00:00")
        result = consolidate(conn)
        assert result.expired_deleted == 0
        assert _count(conn, "knowledge_entries") == 1

    def test_entry_without_expires_at_kept(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", expires_at=None)
        result = consolidate(conn)
        assert result.expired_deleted == 0
        assert _count(conn, "knowledge_entries") == 1

    def test_mixed_expired_and_valid(self):
        conn = _make_db()
        _insert_entry(conn, uuid="old", expires_at="2000-01-01T00:00:00")
        _insert_entry(conn, uuid="new", expires_at="2099-01-01T00:00:00", content_hash="xyz")
        result = consolidate(conn)
        assert result.expired_deleted == 1
        assert _count(conn, "knowledge_entries") == 1


# ── Phase 3: deduplication ────────────────────────────────────────────────────


class TestPhase3Dedup:
    def test_unique_entries_not_removed(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", content_hash="h1", source_path="a.py")
        _insert_entry(conn, uuid="e2", content_hash="h2", source_path="b.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0
        assert _count(conn, "knowledge_entries") == 2

    def test_duplicates_reduced_to_one(self):
        conn = _make_db()
        # Three rows with the same workspace+source_path+content_hash — keep newest (highest id).
        _insert_entry(conn, uuid="e1", content_hash="h1", source_path="f.py")
        _insert_entry(conn, uuid="e2", content_hash="h1", source_path="f.py")
        id3 = _insert_entry(conn, uuid="e3", content_hash="h1", source_path="f.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 2
        remaining_ids = [r[0] for r in conn.execute("SELECT id FROM knowledge_entries").fetchall()]
        assert remaining_ids == [id3]  # highest id kept

    def test_different_workspaces_not_deduped(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", workspace="ws1", content_hash="h1", source_path="f.py")
        _insert_entry(conn, uuid="e2", workspace="ws2", content_hash="h1", source_path="f.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0
        assert _count(conn, "knowledge_entries") == 2

    def test_different_source_paths_not_deduped(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", content_hash="h1", source_path="a.py")
        _insert_entry(conn, uuid="e2", content_hash="h1", source_path="b.py")
        result = consolidate(conn)
        assert result.duplicates_removed == 0


# ── Phase 4: requeue transient failures ──────────────────────────────────────


class TestPhase4Requeue:
    def test_throttle_error_requeued(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="failed", attempts=2, last_error="ThrottlingException")
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 0
        row = conn.execute("SELECT status, attempts FROM embedding_queue").fetchone()
        assert row[0] == "pending"
        assert row[1] == 0

    def test_timeout_error_requeued(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="failed", attempts=1, last_error="Timeout: 30s")
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 0

    def test_null_error_requeued(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="failed", last_error=None)
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 0

    def test_validation_exception_stays_failed(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="failed", last_error="ValidationException: bad field")
        result = consolidate(conn)
        assert result.failed_requeued == 0
        assert result.failed_permanent == 1
        row = conn.execute("SELECT status FROM embedding_queue").fetchone()
        assert row[0] == "failed"  # unchanged

    def test_mixed_transient_and_permanent(self):
        conn = _make_db()
        eid1 = _insert_entry(conn, uuid="e1", content_hash="h1")
        eid2 = _insert_entry(conn, uuid="e2", content_hash="h2", source_path="b.py")
        _insert_queue(conn, entry_id=eid1, status="failed", last_error="ThrottlingException")
        _insert_queue(conn, entry_id=eid2, status="failed", last_error="ValidationException")
        result = consolidate(conn)
        assert result.failed_requeued == 1
        assert result.failed_permanent == 1

    def test_pending_items_not_touched(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="pending", attempts=0)
        result = consolidate(conn)
        assert result.failed_requeued == 0
        assert result.failed_permanent == 0

    def test_done_items_not_touched(self):
        conn = _make_db()
        eid = _insert_entry(conn, uuid="e1")
        _insert_queue(conn, entry_id=eid, status="done", attempts=1)
        result = consolidate(conn)
        assert result.failed_requeued == 0


# ── Phase 5: cache orphan purge ───────────────────────────────────────────────


class TestPhase5CacheOrphans:
    def test_orphan_cache_entry_purged(self):
        conn = _make_db()
        # Cache entry with no matching knowledge_entry
        _insert_cache(conn, "orphan_hash")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 1
        assert _count(conn, "embedding_cache") == 0

    def test_valid_cache_entry_kept(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", content_hash="valid_hash")
        _insert_cache(conn, "valid_hash")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 0
        assert _count(conn, "embedding_cache") == 1

    def test_mixed_orphan_and_valid(self):
        conn = _make_db()
        _insert_entry(conn, uuid="e1", content_hash="live")
        _insert_cache(conn, "live")
        _insert_cache(conn, "dead")
        result = consolidate(conn)
        assert result.cache_orphans_purged == 1
        remaining = [r[0] for r in conn.execute("SELECT content_hash FROM embedding_cache").fetchall()]
        assert remaining == ["live"]

    def test_cache_orphan_created_by_phase2_expiry(self):
        """Cache entries become orphans when their parent expires in Phase 2."""
        conn = _make_db()
        _insert_entry(conn, uuid="e1", content_hash="soon_gone", expires_at="2000-01-01T00:00:00")
        _insert_cache(conn, "soon_gone")
        result = consolidate(conn)
        assert result.expired_deleted == 1
        assert result.cache_orphans_purged == 1
        assert _count(conn, "embedding_cache") == 0

    def test_cache_orphan_created_by_phase3_dedup(self):
        """Cache entries whose hash is deduplicated away are also cleaned up."""
        conn = _make_db()
        # Two entries same hash same path — older will be deleted
        _insert_entry(conn, uuid="e1", content_hash="shared", source_path="f.py")
        _insert_entry(conn, uuid="e2", content_hash="shared", source_path="f.py")
        # The hash still exists after dedup (newest is kept), so cache is NOT orphaned.
        _insert_cache(conn, "shared")
        result = consolidate(conn)
        assert result.duplicates_removed == 1
        assert result.cache_orphans_purged == 0  # hash still present


# ── WAL checkpoint ────────────────────────────────────────────────────────────


class _CheckpointFailConn(sqlite3.Connection):
    """Connection subclass that raises on wal_checkpoint to test fault tolerance."""

    def execute(self, sql, parameters=(), /):
        if "wal_checkpoint" in sql.lower():
            raise sqlite3.OperationalError("simulated checkpoint failure")
        return super().execute(sql, parameters)


class _CacheDeleteFailConn(sqlite3.Connection):
    """Connection subclass that raises on embedding_cache DELETE to test rollback."""

    def execute(self, sql, parameters=(), /):
        if "embedding_cache" in sql and "DELETE" in sql:
            raise sqlite3.OperationalError("simulated cache table error")
        return super().execute(sql, parameters)


def _make_db_with_class(cls) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", factory=cls, check_same_thread=False)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL,
            source_path TEXT,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'test',
            content_type TEXT NOT NULL DEFAULT 'text',
            expires_at TEXT
        );
        CREATE TABLE embedding_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES knowledge_entries(id),
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_error TEXT
        );
        CREATE TABLE embedding_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            embedding BLOB
        );
        """
    )
    return conn


class TestWalCheckpoint:
    def test_wal_checkpointed_flag_set(self):
        """In-memory DB always succeeds the TRUNCATE checkpoint."""
        conn = _make_db()
        result = consolidate(conn)
        # In-memory databases don't really have WAL pages but the PRAGMA
        # returns (0, 0, 0) meaning busy=0 → checkpointed=True.
        assert isinstance(result.wal_checkpointed, bool)

    def test_checkpoint_failure_does_not_abort(self):
        """A failing WAL checkpoint should log a warning but not raise."""
        conn = _make_db_with_class(_CheckpointFailConn)
        result = consolidate(conn)
        assert result.wal_checkpointed is False


# ── Atomicity: rollback on error ──────────────────────────────────────────────


class TestAtomicity:
    def test_partial_failure_rolls_back(self):
        """If Phase 5 raises, the whole transaction is rolled back."""
        conn = _make_db_with_class(_CacheDeleteFailConn)
        # Insert an expired entry; Phase 2 would normally delete it.
        conn.execute(
            "INSERT INTO knowledge_entries "
            "(uuid, workspace, source_path, content_hash, expires_at) "
            "VALUES ('e1', 'ws', 'f.py', 'h1', '2000-01-01T00:00:00')"
        )
        conn.commit()

        with pytest.raises(sqlite3.OperationalError, match="simulated cache table error"):
            consolidate(conn)

        # Rollback should have restored the expired entry
        assert _count(conn, "knowledge_entries") == 1


# ── Phase 6: learning feedback loop ──────────────────────────────────────────


def _make_routing_db() -> sqlite3.Connection:
    """Minimal routing.db (in-memory) with routing_decisions table."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            session_id TEXT,
            task_description TEXT,
            complexity_score REAL,
            tier_selected TEXT,
            agents_used TEXT,
            task_hash TEXT,
            outcome TEXT
        );
        """
    )
    return conn


def _make_sessions_db_with_events(events: list[tuple]) -> sqlite3.Connection:
    """In-memory sessions.db with session_events populated from (timestamp, content) tuples."""
    import json
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT
        );
        """
    )
    for ts, content in events:
        conn.execute(
            "INSERT INTO session_events (timestamp, event_type, content) VALUES (?, 'workflow_complete', ?)",
            (ts, json.dumps(content) if not isinstance(content, str) else content),
        )
    conn.commit()
    return conn


class TestPhase6LearningFeedback:
    def test_no_sessions_db_path_skips_gracefully(self):
        """consolidate() with no sessions_db_path and an in-memory DB skips Phase 6."""
        conn = _make_db()
        # PRAGMA database_list returns '' for in-memory — so path resolution yields None
        result = consolidate(conn)
        assert result.thresholds_updated is False

    def test_nonexistent_sessions_db_skips_gracefully(self, tmp_path):
        """Pointing at a non-existent file skips Phase 6 without error."""
        conn = _make_db()
        missing = tmp_path / "no_sessions.db"
        result = consolidate(conn, sessions_db_path=missing)
        assert result.thresholds_updated is False

    def test_sessions_db_with_no_events_returns_false(self, tmp_path):
        """An empty sessions.db produces thresholds_updated=False."""
        import json
        sessions_path = tmp_path / "sessions.db"
        s_conn = sqlite3.connect(str(sessions_path))
        s_conn.execute(
            "CREATE TABLE session_events "
            "(id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT, content TEXT)"
        )
        s_conn.commit()
        s_conn.close()

        conn = _make_db()
        result = consolidate(conn, sessions_db_path=sessions_path)
        assert result.thresholds_updated is False

    def test_sessions_db_with_events_sets_thresholds_updated(self, tmp_path):
        """workflow_complete events in sessions.db set thresholds_updated=True."""
        import json, time as _time

        # Build sessions.db on disk
        sessions_path = tmp_path / "sessions.db"
        s_conn = sqlite3.connect(str(sessions_path))
        s_conn.execute(
            "CREATE TABLE session_events "
            "(id INTEGER PRIMARY KEY, timestamp REAL, event_type TEXT, content TEXT)"
        )
        now = _time.time()
        s_conn.execute(
            "INSERT INTO session_events (timestamp, event_type, content) VALUES (?, ?, ?)",
            (now, "workflow_complete", json.dumps({"success": True, "duration": 5.0})),
        )
        s_conn.commit()
        s_conn.close()

        # Build knowledge.db (the routing_db passed to Phase 6 is db itself,
        # which needs routing_decisions to exist; consolidate uses db as routing_db)
        conn = _make_db()
        # Add routing_decisions table so compute_thresholds_from_session_events can query it
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                session_id TEXT,
                task_description TEXT,
                complexity_score REAL,
                tier_selected TEXT,
                agents_used TEXT,
                task_hash TEXT,
                outcome TEXT
            );
            """
        )
        # Insert a routing decision within 60s of the session event
        conn.execute(
            "INSERT INTO routing_decisions (timestamp, tier_selected, complexity_score) VALUES (?, ?, ?)",
            (now - 5, "full", 0.8),
        )
        conn.commit()

        result = consolidate(conn, sessions_db_path=sessions_path)
        assert result.thresholds_updated is True

    def test_malformed_sessions_db_skips_gracefully(self, tmp_path):
        """A corrupt or schema-less sessions.db does not crash consolidate()."""
        sessions_path = tmp_path / "sessions.db"
        # Write a non-SQLite file
        sessions_path.write_bytes(b"not a sqlite database")

        conn = _make_db()
        # Should not raise
        result = consolidate(conn, sessions_db_path=sessions_path)
        assert result.thresholds_updated is False

    def test_thresholds_updated_field_default_is_false(self):
        """ConsolidationResult.thresholds_updated defaults to False."""
        r = ConsolidationResult(
            expired_deleted=0,
            duplicates_removed=0,
            failed_requeued=0,
            failed_permanent=0,
            cache_orphans_purged=0,
            wal_checkpointed=False,
            duration_ms=0,
        )
        assert r.thresholds_updated is False

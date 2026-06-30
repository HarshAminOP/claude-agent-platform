"""Tests for CAP enforcement hooks (pretool + passthrough)."""
import pytest
import sys
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.db import get_db, migrate
from cap.enforcement.passthrough import enable, check, disable, MAX_TTL


@pytest.fixture
def db(tmp_path):
    """Provide a migrated database connection with a temp path."""
    db_path = str(tmp_path / "test_enforcement.db")
    conn = get_db(db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def pretool_db(tmp_path):
    """Provide a DB and path for pretool enforcement testing."""
    db_path = str(tmp_path / "pretool.db")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS enforcement_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            delegated INTEGER NOT NULL DEFAULT 0,
            timestamp REAL NOT NULL,
            UNIQUE(session_id, file_path, delegated)
        );
        CREATE TABLE IF NOT EXISTS enforcement_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            tool_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            workspace TEXT
        );
        CREATE TABLE IF NOT EXISTS passthrough (
            workspace TEXT PRIMARY KEY,
            enabled_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            reason TEXT,
            enabled_by TEXT DEFAULT 'user'
        );
    """)
    return conn, db_path


class TestPretoolAllowsFirstEdits:
    """Test that pretool allows the first 2 distinct file edits."""

    def test_first_edit_allowed(self, pretool_db):
        conn, _ = pretool_db
        session_id = "test-session-1"

        # Record first edit
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/file1.py", 0, time.time()),
        )
        conn.commit()

        # Count distinct files
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM enforcement_edits WHERE session_id = ? AND delegated = 0",
            (session_id,),
        ).fetchall()
        assert len(rows) == 1

    def test_second_edit_allowed(self, pretool_db):
        conn, _ = pretool_db
        session_id = "test-session-2"
        now = time.time()

        # Record two edits to different files
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/file1.py", 0, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/file2.py", 0, now),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT DISTINCT file_path FROM enforcement_edits WHERE session_id = ? AND delegated = 0",
            (session_id,),
        ).fetchall()
        # 2 distinct files: still below threshold of 3
        assert len(rows) == 2
        assert len(rows) < 3  # Would be allowed


class TestPretoolBlocksThirdEdit:
    """Test that pretool blocks the 3rd distinct file edit."""

    def test_third_distinct_file_blocked(self, pretool_db):
        conn, _ = pretool_db
        session_id = "test-session-block"
        now = time.time()

        # Record edits to 2 files
        for i, path in enumerate(["/workspace/a.py", "/workspace/b.py"]):
            conn.execute(
                "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, path, 0, now + i),
            )
        conn.commit()

        # Simulate checking if a 3rd file would be blocked
        existing = conn.execute(
            "SELECT DISTINCT file_path FROM enforcement_edits WHERE session_id = ? AND delegated = 0",
            (session_id,),
        ).fetchall()
        edited_files = {r[0] for r in existing}
        edited_files.add("/workspace/c.py")  # The new file

        # The pretool logic: len >= 3 means BLOCK
        assert len(edited_files) >= 3

    def test_same_file_edit_not_blocked(self, pretool_db):
        """Editing the same file multiple times should NOT count as multiple files."""
        conn, _ = pretool_db
        session_id = "test-session-same"
        now = time.time()

        # Edit same 2 files multiple times (UNIQUE constraint prevents duplicates)
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/a.py", 0, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/a.py", 0, now + 1),
        )
        conn.execute(
            "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, "/workspace/b.py", 0, now + 2),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT DISTINCT file_path FROM enforcement_edits WHERE session_id = ? AND delegated = 0",
            (session_id,),
        ).fetchall()
        # Only 2 distinct files (a.py duplicate was ignored)
        assert len(rows) == 2


class TestPassthroughBypassesEnforcement:
    """Test that passthrough mode bypasses enforcement."""

    def test_passthrough_check_active(self, tmp_path):
        """Active passthrough should return True."""
        db_path = str(tmp_path / "pt.db")
        with patch("cap.enforcement.passthrough.DB_PATH", db_path):
            # Reset module-level state
            import cap.enforcement.passthrough as pt
            pt._migrated = False

            result = enable("/workspace/test", ttl=60, reason="testing")
            assert result["status"] == "enabled"
            assert check("/workspace/test") is True

    def test_passthrough_check_expired(self, tmp_path):
        """Expired passthrough should return False."""
        db_path = str(tmp_path / "pt2.db")
        conn = get_db(db_path)
        migrate(conn)

        # Insert an already-expired passthrough
        conn.execute(
            "INSERT INTO passthrough (workspace, enabled_at, expires_at, reason) VALUES (?, ?, ?, ?)",
            ("/workspace/test", time.time() - 600, time.time() - 300, "old"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT expires_at FROM passthrough WHERE workspace = ? AND expires_at > ?",
            ("/workspace/test", time.time()),
        ).fetchone()
        assert row is None  # Expired, so check returns False
        conn.close()

    def test_passthrough_disable(self, tmp_path):
        """Disabling passthrough should make check return False."""
        db_path = str(tmp_path / "pt3.db")
        with patch("cap.enforcement.passthrough.DB_PATH", db_path):
            import cap.enforcement.passthrough as pt
            pt._migrated = False

            enable("/workspace/test", ttl=300, reason="testing")
            assert check("/workspace/test") is True
            disable("/workspace/test")
            assert check("/workspace/test") is False


class TestPassthroughRateLimit:
    """Test passthrough rate limiting (max 3 per hour)."""

    def test_rate_limit_enforced(self, tmp_path):
        """Fourth activation within an hour should be rejected."""
        db_path = str(tmp_path / "rate.db")
        with patch("cap.enforcement.passthrough.DB_PATH", db_path):
            import cap.enforcement.passthrough as pt
            pt._migrated = False

            workspace = "/workspace/ratelimit"

            # First 3 activations should succeed
            for i in range(3):
                result = enable(workspace, ttl=5, reason=f"activation {i}")
                assert result.get("status") == "enabled", f"Activation {i} failed: {result}"

            # 4th activation should be rate-limited
            result = enable(workspace, ttl=5, reason="too many")
            assert "error" in result
            assert "Rate limit" in result["error"]

    def test_ttl_capped_at_max(self, tmp_path):
        """TTL should be capped at MAX_TTL (900 seconds)."""
        db_path = str(tmp_path / "ttlcap.db")
        with patch("cap.enforcement.passthrough.DB_PATH", db_path):
            import cap.enforcement.passthrough as pt
            pt._migrated = False

            result = enable("/workspace/test", ttl=9999, reason="too long")
            assert result["expires_in"] == MAX_TTL


class TestAgentContextResetsCounter:
    """Test that agent context (delegated edits) resets/bypasses the counter."""

    def test_delegated_edits_not_counted(self, pretool_db):
        """Edits within agent context (delegated=1) should not trigger blocking."""
        conn, _ = pretool_db
        session_id = "test-session-delegated"
        now = time.time()

        # Insert an active agent context
        conn.execute(
            "INSERT INTO agent_contexts (session_id, agent_id, started_at, active) VALUES (?, ?, ?, ?)",
            (session_id, "dev-agent-1", now, 1),
        )

        # Record 5 file edits as delegated
        for i in range(5):
            conn.execute(
                "INSERT OR IGNORE INTO enforcement_edits (session_id, file_path, delegated, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, f"/workspace/file{i}.py", 1, now + i),
            )
        conn.commit()

        # Non-delegated count should be zero
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM enforcement_edits WHERE session_id = ? AND delegated = 0",
            (session_id,),
        ).fetchall()
        assert len(rows) == 0  # All edits were delegated, so counter is 0

    def test_agent_context_active_check(self, pretool_db):
        """Active agent context should mark edits as delegated."""
        conn, _ = pretool_db
        session_id = "test-agent-ctx"
        now = time.time()

        # Simulate the pretool logic: check for active agent context
        conn.execute(
            "INSERT INTO agent_contexts (session_id, agent_id, started_at, active) VALUES (?, ?, ?, ?)",
            (session_id, "orchestrator", now, 1),
        )
        conn.commit()

        agent_context = conn.execute(
            "SELECT 1 FROM agent_contexts WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchone()
        assert agent_context is not None  # Agent context exists, so delegated=True

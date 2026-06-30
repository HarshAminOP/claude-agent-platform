"""Tests for CAP database manager (db.py)."""
import pytest
import sys
import tempfile
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.db import get_db, migrate


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return str(tmp_path / "test_cap.db")


@pytest.fixture
def db(db_path):
    """Provide a migrated database connection."""
    conn = get_db(db_path)
    migrate(conn)
    yield conn
    conn.close()


class TestMigrate:
    """Test that migrate() creates all expected tables."""

    def test_creates_enforcement_tables(self, db):
        tables = _get_tables(db)
        assert "enforcement_edits" in tables
        assert "enforcement_violations" in tables
        assert "agent_contexts" in tables
        assert "passthrough" in tables
        assert "passthrough_log" in tables

    def test_creates_memory_tables(self, db):
        tables = _get_tables(db)
        assert "memory_active" in tables
        assert "memory_archive" in tables
        assert "memory_working" in tables
        assert "memory_fts" in tables

    def test_creates_code_intel_tables(self, db):
        tables = _get_tables(db)
        assert "code_files" in tables
        assert "code_symbols" in tables
        assert "code_relationships" in tables

    def test_creates_learning_tables(self, db):
        tables = _get_tables(db)
        assert "learning_events" in tables
        assert "routing_decisions" in tables
        assert "correction_patterns" in tables
        assert "trust_levels" in tables

    def test_creates_cost_runtime_tables(self, db):
        tables = _get_tables(db)
        assert "cost_events" in tables
        assert "runtime_state" in tables

    def test_creates_reliability_tables(self, db):
        tables = _get_tables(db)
        assert "circuit_breaker_state" in tables
        assert "dead_letter_queue" in tables
        assert "cascade_events" in tables
        assert "agent_health_events" in tables

    def test_creates_dag_tables(self, db):
        tables = _get_tables(db)
        assert "task_plans" in tables
        assert "task_steps" in tables

    def test_creates_witness_tables(self, db):
        tables = _get_tables(db)
        assert "witness_manifests" in tables

    def test_migrate_is_idempotent(self, db):
        """Running migrate() twice should not raise errors."""
        migrate(db)  # second time
        tables = _get_tables(db)
        assert "enforcement_edits" in tables


class TestWALMode:
    """Test that WAL mode is enabled."""

    def test_wal_mode_enabled(self, db_path):
        conn = get_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()

    def test_foreign_keys_enabled(self, db_path):
        conn = get_db(db_path)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()


class TestGetDb:
    """Test that get_db() returns a working connection."""

    def test_returns_connection(self, db_path):
        conn = get_db(db_path)
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_creates_parent_directory(self, tmp_path):
        nested_path = str(tmp_path / "sub" / "dir" / "cap.db")
        conn = get_db(nested_path)
        assert conn is not None
        conn.execute("SELECT 1").fetchone()
        conn.close()

    def test_row_factory_set(self, db_path):
        conn = get_db(db_path)
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_can_insert_and_query(self, db):
        """Verify connection is fully functional with schema."""
        db.execute(
            "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("test_key", "test_value", 1000.0),
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM runtime_state WHERE key = ?", ("test_key",)
        ).fetchone()
        assert row["value"] == "test_value"


def _get_tables(db):
    """Helper to get all table names from the database."""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
    ).fetchall()
    return {r[0] if isinstance(r, (tuple, list)) else r["name"] for r in rows}

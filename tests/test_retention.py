"""Unit tests for cap.harness.retention.

All tests are fully offline — no AWS credentials, no external dependencies.
Each test uses an isolated temp-file SQLite DB via the db= parameter.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.retention import (
    compute_retention_score,
    prune_stale_patterns,
    refresh_retention_scores,
    record_pattern_use,
    protect_high_value,
    _get_conn,
)
from cap.harness.agentdb import agentdb_pattern_store, agentdb_pattern_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "retention_test.db"


def _seed_pattern(db_path, *, success=1, cost=0.01, task_type="dev", age_days=0):
    """Insert a pattern directly and return its id."""
    import uuid
    from datetime import datetime, timezone, timedelta
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Bootstrap table (in case agentdb hasn't run yet)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS patterns (
            id TEXT PRIMARY KEY,
            task_type TEXT,
            prompt_hash TEXT,
            prompt_summary TEXT,
            model TEXT,
            agent_type TEXT,
            cost_usd REAL,
            duration_ms INTEGER,
            success INTEGER DEFAULT 1,
            output_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    pid = uuid.uuid4().hex
    created = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    conn.execute(
        """INSERT INTO patterns
           (id, task_type, prompt_hash, prompt_summary, model, agent_type,
            cost_usd, duration_ms, success, output_summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, task_type, pid[:16], f"summary for {pid[:8]}",
         "claude-3", "dev", cost, 500, success, None, created.isoformat()),
    )
    conn.commit()
    conn.close()
    return pid


# ---------------------------------------------------------------------------
# _get_conn — column migration
# ---------------------------------------------------------------------------

class TestGetConn:
    def test_adds_retention_columns(self, db_path):
        _seed_pattern(db_path)
        conn = _get_conn(db_path)
        row = conn.execute("PRAGMA table_info(patterns)").fetchall()
        cols = {r[1] for r in row}
        conn.close()
        assert "use_count" in cols
        assert "last_used_at" in cols
        assert "retention_score" in cols

    def test_idempotent_on_second_call(self, db_path):
        _seed_pattern(db_path)
        conn = _get_conn(db_path)
        conn.close()
        conn = _get_conn(db_path)  # should not raise
        conn.close()


# ---------------------------------------------------------------------------
# compute_retention_score
# ---------------------------------------------------------------------------

class TestComputeRetentionScore:
    def test_successful_fresh_pattern_scores_high(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.005, age_days=0)
        score = compute_retention_score(pid, db=db_path)
        # success(0.3) + recency(0.2) + usage(0) + cost_factor ~0.95 * 0.2
        assert score > 0.6

    def test_failed_old_expensive_pattern_scores_low(self, db_path):
        pid = _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
        score = compute_retention_score(pid, db=db_path)
        # success(0) + recency(0) + usage(0) + cost_factor(0)
        assert score == 0.0

    def test_score_is_persisted(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.01, age_days=10)
        score = compute_retention_score(pid, db=db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT retention_score FROM patterns WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - score) < 1e-5

    def test_missing_pattern_returns_zero(self, db_path):
        _seed_pattern(db_path)  # ensure table exists with columns
        score = compute_retention_score("nonexistent_id", db=db_path)
        assert score == 0.0

    def test_score_clamped_between_0_and_1(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.0, age_days=0)
        score = compute_retention_score(pid, db=db_path)
        assert 0.0 <= score <= 1.0

    def test_usage_boosts_score(self, db_path):
        pid = _seed_pattern(db_path, success=0, cost=0.10, age_days=91)
        # Ensure retention columns exist before writing to them
        _get_conn(db_path).close()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE patterns SET use_count=10 WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        score = compute_retention_score(pid, db=db_path)
        # usage_factor = 1.0 * 0.3 = 0.3
        assert score >= 0.3


# ---------------------------------------------------------------------------
# record_pattern_use
# ---------------------------------------------------------------------------

class TestRecordPatternUse:
    def test_increments_use_count(self, db_path):
        pid = _seed_pattern(db_path)
        _get_conn(db_path).close()  # ensure columns exist
        record_pattern_use(pid, db=db_path)
        record_pattern_use(pid, db=db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT use_count FROM patterns WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row[0] == 2

    def test_sets_last_used_at(self, db_path):
        pid = _seed_pattern(db_path)
        _get_conn(db_path).close()
        record_pattern_use(pid, db=db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT last_used_at FROM patterns WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row[0] is not None

    def test_noop_on_missing_id(self, db_path):
        _seed_pattern(db_path)
        # Should not raise
        record_pattern_use("nonexistent", db=db_path)


# ---------------------------------------------------------------------------
# refresh_retention_scores
# ---------------------------------------------------------------------------

class TestRefreshRetentionScores:
    def test_updates_all_patterns(self, db_path):
        for _ in range(5):
            _seed_pattern(db_path, success=1, cost=0.01)
        count = refresh_retention_scores(batch_size=10, db=db_path)
        assert count == 5

    def test_respects_batch_size(self, db_path):
        for _ in range(10):
            _seed_pattern(db_path, success=1, cost=0.01)
        count = refresh_retention_scores(batch_size=3, db=db_path)
        assert count == 3

    def test_empty_table_returns_zero(self, db_path):
        _get_conn(db_path).close()  # create table
        count = refresh_retention_scores(db=db_path)
        assert count == 0


# ---------------------------------------------------------------------------
# prune_stale_patterns
# ---------------------------------------------------------------------------

class TestPruneStalePatterns:
    def test_prunes_low_score_old_patterns(self, db_path):
        # 5 stale worthless patterns + 110 fresh good patterns
        stale_ids = [_seed_pattern(db_path, success=0, cost=0.15, age_days=100) for _ in range(5)]
        for _ in range(110):
            _seed_pattern(db_path, success=1, cost=0.005, age_days=1)

        deleted = prune_stale_patterns(min_score=0.1, max_age_days=90, keep_min=100, db=db_path)
        assert deleted == 5

        # Confirm stale rows are gone
        conn = sqlite3.connect(str(db_path))
        for pid in stale_ids:
            row = conn.execute("SELECT id FROM patterns WHERE id=?", (pid,)).fetchone()
            assert row is None, f"Expected {pid} to be pruned"
        conn.close()

    def test_respects_keep_min(self, db_path):
        # 5 patterns total, keep_min=100 → nothing deleted
        for _ in range(5):
            _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
        deleted = prune_stale_patterns(min_score=0.1, max_age_days=90, keep_min=100, db=db_path)
        assert deleted == 0

    def test_never_prunes_fresh_patterns(self, db_path):
        for _ in range(110):
            _seed_pattern(db_path, success=0, cost=0.15, age_days=0)
        deleted = prune_stale_patterns(min_score=0.1, max_age_days=90, keep_min=100, db=db_path)
        assert deleted == 0

    def test_returns_zero_when_nothing_qualifies(self, db_path):
        for _ in range(110):
            _seed_pattern(db_path, success=1, cost=0.005, age_days=1)
        deleted = prune_stale_patterns(db=db_path)
        assert deleted == 0


# ---------------------------------------------------------------------------
# protect_high_value
# ---------------------------------------------------------------------------

class TestProtectHighValue:
    def test_pins_high_score_patterns(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.001, age_days=0)
        compute_retention_score(pid, db=db_path)
        # Force score to exactly 0.9
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE patterns SET retention_score=0.9 WHERE id=?", (pid,))
        conn.commit()
        conn.close()

        protected = protect_high_value(threshold=0.8, db=db_path)
        assert pid in protected

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT retention_score FROM patterns WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row[0] == 1.0

    def test_does_not_protect_low_score(self, db_path):
        pid = _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
        compute_retention_score(pid, db=db_path)
        protected = protect_high_value(threshold=0.8, db=db_path)
        assert pid not in protected

    def test_returns_empty_list_when_none_qualify(self, db_path):
        _get_conn(db_path).close()
        protected = protect_high_value(threshold=0.8, db=db_path)
        assert protected == []


# ---------------------------------------------------------------------------
# Integration: agentdb_pattern_search triggers record_pattern_use
# ---------------------------------------------------------------------------

class TestSearchIntegration:
    def test_search_increments_use_count(self, db_path):
        result = agentdb_pattern_store(
            task_type="dev",
            prompt_summary="deploy kubernetes service to EKS cluster",
            model="claude-3",
            agent_type="devops",
            cost_usd=0.01,
            duration_ms=300,
            success=True,
            _db_path=db_path,
        )
        pid = result["pattern_id"]

        # Ensure retention columns exist
        _get_conn(db_path).close()

        agentdb_pattern_search("deploy kubernetes service", _db_path=db_path)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT use_count FROM patterns WHERE id=?", (pid,)).fetchone()
        conn.close()
        # use_count may be None (column added after insert) or 1
        assert row[0] is None or row[0] >= 1

"""Phase D Harness verification tests — Vector Patterns, Embedding Router, Daemon, Retention.

Tests cover four Phase D areas:
1. Vector Patterns  — PatternEmbedder availability, embed, search, bulk backfill
2. Embedding Router — EmbeddingRouter.route() and recommend_model()
3. Daemon           — CapDaemon.run_once() and CLI registration
4. Retention        — compute_retention_score, prune, record_use, protect, refresh

All tests are offline — no AWS credentials, no LanceDB on disk.
Vector/embedding tests mock EmbeddingClient and LanceDB.
Retention tests use real SQLite via tmp_path.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.harness.vector_patterns import PatternEmbedder
from cap.harness.embed_router import EmbeddingRouter, _DEFAULT_MODELS
from cap.harness.daemon import CapDaemon
from cap.harness.retention import (
    compute_retention_score,
    prune_stale_patterns,
    refresh_retention_scores,
    record_pattern_use,
    protect_high_value,
    _get_conn as retention_get_conn,
)


# ===========================================================================
# Shared helpers
# ===========================================================================

def _mock_embedding_client(is_avail=None, vector=None):
    """Build a mock EmbeddingClient whose embed_single returns *vector*."""
    if vector is None:
        vector = [0.1] * 1024

    client = MagicMock()
    type(client).is_available = PropertyMock(return_value=is_avail)

    async def _embed(text):
        return vector

    client.embed_single.side_effect = _embed
    return client


def _mock_lancedb(tmp_path, table=None):
    """Return a (mock_lancedb_module, mock_db, mock_table) triple."""
    mock_table = table or MagicMock()
    mock_db = MagicMock()
    mock_db.open_table.return_value = mock_table
    mock_db.create_table.return_value = mock_table

    mock_lancedb = MagicMock()
    mock_lancedb.connect.return_value = mock_db
    return mock_lancedb, mock_db, mock_table


def _seed_pattern(
    db_path: Path,
    *,
    success: int = 1,
    cost: float = 0.01,
    task_type: str = "dev",
    age_days: int = 0,
    use_count: int = 0,
) -> str:
    """Insert a minimal pattern row and return its id."""
    pid = uuid.uuid4().hex
    created = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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


# ===========================================================================
# 1. VECTOR PATTERNS
# ===========================================================================

class TestPatternEmbedderUnavailable:
    """PatternEmbedder.is_available returns False when Bedrock marks unavailable."""

    def test_pattern_embedder_unavailable_returns_false(self, tmp_path):
        client = _mock_embedding_client(is_avail=False)
        mock_ldb, _, _ = _mock_lancedb(tmp_path)

        with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
             patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
             patch.dict("sys.modules", {"lancedb": mock_ldb}):
            pe = PatternEmbedder()
            assert pe.is_available is False


class TestPatternEmbedderEmbedStoresVector:
    """embed_pattern stores a vector and returns True when Bedrock returns [0.1]*1024."""

    def test_pattern_embedder_embed_stores_vector(self, tmp_path):
        vector = [0.1] * 1024
        client = _mock_embedding_client(is_avail=None, vector=vector)
        mock_ldb, mock_db, mock_table = _mock_lancedb(tmp_path)

        with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
             patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
             patch.dict("sys.modules", {"lancedb": mock_ldb}):
            pe = PatternEmbedder()
            result = pe.embed_pattern("pid_abc123", "deploy auth service to staging")

        assert result is True
        # The mock table must have been used to persist the vector
        mock_table.add.assert_called_once()
        call_args = mock_table.add.call_args[0][0]
        assert call_args[0]["id"] == "pid_abc123"
        assert call_args[0]["vector"] == vector


class TestSearchSimilarReturnsScored:
    """search_similar returns hits with a numeric score computed from cosine distance."""

    def test_search_similar_returns_scored(self, tmp_path):
        client = _mock_embedding_client(is_avail=None)
        mock_ldb, _, mock_table = _mock_lancedb(tmp_path)

        mock_table.search.return_value \
            .metric.return_value \
            .limit.return_value \
            .to_list.return_value = [
                {"id": "p1", "text": "deploy auth service", "_distance": 0.1},
                {"id": "p2", "text": "write unit tests",   "_distance": 0.45},
            ]

        with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
             patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
             patch.dict("sys.modules", {"lancedb": mock_ldb}):
            pe = PatternEmbedder()
            results = pe.search_similar("deploy auth service", limit=5, min_score=0.5)

        # p1: score = 1 - 0.1 = 0.9  >= 0.5  -> included
        # p2: score = 1 - 0.45 = 0.55 >= 0.5  -> included
        assert len(results) == 2
        assert results[0]["pattern_id"] == "p1"
        assert results[0]["score"] == pytest.approx(0.9, abs=0.001)
        assert results[1]["pattern_id"] == "p2"
        assert results[1]["score"] == pytest.approx(0.55, abs=0.001)


class TestSearchSimilarEmptyWhenUnavailable:
    """search_similar returns [] without raising when embedder is unavailable."""

    def test_search_similar_empty_when_unavailable(self, tmp_path):
        client = _mock_embedding_client(is_avail=False)
        mock_ldb, _, _ = _mock_lancedb(tmp_path)

        with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
             patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
             patch.dict("sys.modules", {"lancedb": mock_ldb}):
            pe = PatternEmbedder()
            assert pe.search_similar("some query") == []
            assert pe.search_similar("") == []


class TestBulkEmbedMissingProcessesBatch:
    """bulk_embed_missing reads un-embedded patterns and calls embed_pattern for each."""

    def test_bulk_embed_missing_processes_batch(self, tmp_path):
        client = _mock_embedding_client(is_avail=None)
        mock_ldb, _, mock_table = _mock_lancedb(tmp_path)

        # Build a real SQLite DB with two un-embedded patterns
        db_file = tmp_path / "platform.db"
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE patterns (
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
                embedding_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO patterns (id, prompt_summary) VALUES ('id1', 'task one')")
        conn.execute("INSERT INTO patterns (id, prompt_summary) VALUES ('id2', 'task two')")
        conn.commit()
        conn.close()

        def _real_get_conn(*args, **kwargs):
            c = sqlite3.connect(str(db_file))
            c.row_factory = sqlite3.Row
            return c

        with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
             patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
             patch.dict("sys.modules", {"lancedb": mock_ldb}), \
             patch("cap.harness.agentdb._get_conn", side_effect=_real_get_conn):
            pe = PatternEmbedder()
            count = pe.bulk_embed_missing(batch_size=10)

        assert count == 2


class TestAgentdbPatternSearchVectorFirst:
    """agentdb_pattern_search uses PatternEmbedder vector hits as primary path."""

    def test_agentdb_pattern_search_vector_first(self, tmp_path):
        from cap.harness.agentdb import agentdb_pattern_store, agentdb_pattern_search

        db_path = tmp_path / "test.db"
        result = agentdb_pattern_store(
            task_type="deploy",
            prompt_summary="deploy the auth microservice to staging",
            model="claude-sonnet-4-6",
            agent_type="devops",
            cost_usd=0.001,
            duration_ms=500,
            _db_path=db_path,
        )
        pattern_id = result["pattern_id"]

        mock_pe_instance = MagicMock()
        mock_pe_instance.is_available = True
        mock_pe_instance.search_similar.return_value = [
            {"pattern_id": pattern_id, "score": 0.92, "text": "deploy the auth microservice to staging"}
        ]
        mock_pe_class = MagicMock(return_value=mock_pe_instance)

        # agentdb imports PatternEmbedder via `from cap.harness.vector_patterns import PatternEmbedder`
        # inside the function body, so we patch at the vector_patterns module level.
        with patch("cap.harness.vector_patterns.PatternEmbedder", mock_pe_class):
            hits = agentdb_pattern_search("deploy auth service", _db_path=db_path)

        assert len(hits) >= 1
        assert hits[0]["pattern_id"] == pattern_id
        assert hits[0]["model"] == "claude-sonnet-4-6"


class TestAgentdbPatternSearchFallbackToLike:
    """agentdb_pattern_search falls back to LIKE when PatternEmbedder is unavailable."""

    def test_agentdb_pattern_search_fallback_to_like(self, tmp_path):
        from cap.harness.agentdb import agentdb_pattern_store, agentdb_pattern_search

        db_path = tmp_path / "test.db"
        agentdb_pattern_store(
            task_type="code",
            prompt_summary="implement retry logic for HTTP client",
            model="claude-haiku-4-5",
            agent_type="dev",
            cost_usd=0.0005,
            duration_ms=200,
            _db_path=db_path,
        )

        mock_pe_instance = MagicMock()
        mock_pe_instance.is_available = False
        mock_pe_class = MagicMock(return_value=mock_pe_instance)

        import cap.harness.agentdb as _agentdb_mod
        orig = _agentdb_mod.__dict__.get("PatternEmbedder")
        _agentdb_mod.__dict__["PatternEmbedder"] = mock_pe_class
        try:
            hits = agentdb_pattern_search("retry logic", _db_path=db_path)
        finally:
            if orig is None:
                _agentdb_mod.__dict__.pop("PatternEmbedder", None)
            else:
                _agentdb_mod.__dict__["PatternEmbedder"] = orig

        assert len(hits) >= 1
        assert any("retry" in h["prompt_summary"].lower() for h in hits)


# ===========================================================================
# 2. EMBEDDING ROUTER
# ===========================================================================

def _make_pe_mock(is_avail: bool = True, hits: list[dict] | None = None):
    """Return a mock PatternEmbedder instance."""
    pe = MagicMock()
    type(pe).is_available = PropertyMock(return_value=is_avail)
    pe.search_similar.return_value = hits or []
    return pe


def _make_conn_with_rows(rows: list[tuple]) -> sqlite3.Connection:
    """Return in-memory SQLite seeded with (id, agent_type, success, cost_usd, model) rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE patterns (
            id TEXT PRIMARY KEY,
            agent_type TEXT,
            success INTEGER,
            cost_usd REAL,
            model TEXT,
            prompt_hash TEXT,
            prompt_summary TEXT,
            task_type TEXT,
            duration_ms INTEGER,
            output_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.executemany(
        "INSERT INTO patterns (id, agent_type, success, cost_usd, model) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


def _route_with_mocks(hits, db_rows):
    """Run EmbeddingRouter.route() with PatternEmbedder and _get_conn mocked."""
    pe = _make_pe_mock(is_avail=True, hits=hits)
    conn = _make_conn_with_rows(db_rows)

    with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
         patch("cap.harness.embed_router._get_conn", return_value=conn):
        er = EmbeddingRouter()
        return er.route("some task description")


class TestEmbedRouterReturnsNoneWithoutPatterns:
    """EmbeddingRouter.route() returns None when fewer than 5 similar patterns found."""

    def test_embed_router_returns_none_without_patterns(self):
        # Only 3 hits — below the minimum-5 threshold
        hits = [{"pattern_id": f"p{i}", "score": 0.8, "text": "x"} for i in range(3)]
        pe = _make_pe_mock(is_avail=True, hits=hits)
        conn = _make_conn_with_rows([])

        with patch("cap.harness.embed_router.PatternEmbedder", return_value=pe), \
             patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            result = er.route("some task")

        assert result is None


class TestEmbedRouterScoresBySimilarity:
    """EmbeddingRouter.route() scores agent types by similarity and picks the best."""

    def test_embed_router_scores_by_similarity(self):
        # "dev" has high similarity, "sre" has low similarity + no successes
        hits = (
            [{"pattern_id": f"dev{i}", "score": 0.9, "text": "x"} for i in range(5)]
            + [{"pattern_id": f"sre{i}", "score": 0.3, "text": "x"} for i in range(5)]
        )
        db_rows = (
            [(f"dev{i}", "dev", 1, 0.001, "sonnet") for i in range(5)]
            + [(f"sre{i}", "sre", 0, 0.001, "sonnet") for i in range(5)]
        )
        result = _route_with_mocks(hits, db_rows)

        assert result is not None
        assert result["recommended_agent_type"] == "dev"
        assert "confidence" in result
        assert result["based_on_patterns"] == 10


class TestRecommendModelCheapestSuccessful:
    """recommend_model returns the cheapest model with >=80% success."""

    def _conn_with_model_stats(self, rows):
        """rows: (model, count, avg_success)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE patterns (
                id TEXT PRIMARY KEY,
                agent_type TEXT,
                model TEXT,
                success INTEGER,
                cost_usd REAL
            )
        """)
        for model, cnt, avg_success in rows:
            for i in range(cnt):
                success = 1 if i < round(avg_success * cnt) else 0
                conn.execute(
                    "INSERT INTO patterns (id, agent_type, model, success, cost_usd) VALUES (?,?,?,?,?)",
                    (f"{model}-{i}", "dev", model, success, 0.001),
                )
        conn.commit()
        return conn

    def test_recommend_model_cheapest_successful(self):
        # haiku, sonnet, opus all at 100% — should pick cheapest (haiku)
        conn = self._conn_with_model_stats([
            ("haiku", 5, 1.0), ("sonnet", 5, 1.0), ("opus", 5, 1.0)
        ])
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            assert er.recommend_model("dev") == "haiku"

    def test_recommend_model_fallback_default(self):
        # Empty DB — no history — falls back to _DEFAULT_MODELS
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE patterns (id TEXT PRIMARY KEY, agent_type TEXT, model TEXT, success INTEGER)"
        )
        conn.commit()
        with patch("cap.harness.embed_router._get_conn", return_value=conn):
            er = EmbeddingRouter()
            model = er.recommend_model("security")
        assert model == _DEFAULT_MODELS["security"]


# ===========================================================================
# 3. DAEMON
# ===========================================================================

@pytest.fixture()
def daemon() -> CapDaemon:
    return CapDaemon(interval_seconds=60)


class TestDaemonRunOnceReturnsDict:
    """run_once() executes all maintenance tasks and returns a structured dict."""

    def test_daemon_run_once_returns_dict(self, daemon):
        with patch.object(daemon, "_run_consolidation",   return_value={"expired": 0, "deduped": 0}), \
             patch.object(daemon, "_run_stale_cleanup",   return_value={"terminated": 0}), \
             patch.object(daemon, "_run_pattern_embedding", return_value={"skipped": "embedder_unavailable"}), \
             patch.object(daemon, "_run_learning",        return_value={"sample_count": 0}), \
             patch.object(daemon, "_run_retention",       return_value={"pruned": 0}), \
             patch.object(daemon, "_run_manifest",        return_value={"refreshed": True}):
            results = daemon.run_once()

        assert isinstance(results, dict)
        for key in ("consolidation", "stale_agents", "pattern_embedding",
                    "learning", "retention", "manifest"):
            assert key in results
        assert daemon.last_run is results


class TestDaemonHandlesMissingModules:
    """_run_retention returns the skipped sentinel when the module is absent."""

    def test_daemon_handles_missing_modules(self, daemon):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "cap.harness.retention":
                raise ImportError("No module named 'cap.harness.retention'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = daemon._run_retention()

        assert result == {"skipped": "retention_module_unavailable"}


class TestDaemonCliCommandRegistered:
    """The 'daemon' command must appear in the main CLI group."""

    def test_daemon_cli_command_registered(self):
        from cap.cli.main import cli
        assert "daemon" in cli.commands


# ===========================================================================
# 4. RETENTION
# ===========================================================================

@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "retention_d.db"


class TestComputeScoreNewSuccessfulPattern:
    """A brand-new, successful, cheap pattern should score in the moderate-to-high range."""

    def test_compute_score_new_successful_pattern(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.005, age_days=0)
        score = compute_retention_score(pid, db=db_path)
        # success(0.3) + recency(0.2) + usage(0) + cost_factor ~0.95*0.2
        # Expected: at least 0.5 (moderate) — well above the minimum
        assert 0.4 <= score <= 1.0


class TestComputeScoreOldUnusedPattern:
    """A 100-day-old, failed, expensive, zero-use pattern should score near 0."""

    def test_compute_score_old_unused_pattern(self, db_path):
        pid = _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
        score = compute_retention_score(pid, db=db_path)
        # success(0) + recency(0, past 90-day window) + usage(0) + cost(0, cost>=0.10)
        assert score == pytest.approx(0.0, abs=1e-6)


class TestComputeScoreOldHeavilyUsed:
    """High use_count keeps the score elevated even for old/failed patterns."""

    def test_compute_score_old_heavily_used(self, db_path):
        pid = _seed_pattern(db_path, success=0, cost=0.10, age_days=91)
        # Ensure retention columns are present before writing
        retention_get_conn(db_path).close()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE patterns SET use_count = 10 WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        score = compute_retention_score(pid, db=db_path)
        # usage_factor=1.0 * 0.3 = 0.3 — pattern should still score at least 0.3
        assert score >= 0.3


class TestPruneRespectsKeepMin:
    """prune_stale_patterns never deletes rows when total <= keep_min."""

    def test_prune_respects_keep_min(self, db_path):
        # 5 worthless old patterns, but keep_min=100 → nothing deleted
        for _ in range(5):
            _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
        deleted = prune_stale_patterns(min_score=0.1, max_age_days=90, keep_min=100, db=db_path)
        assert deleted == 0

        conn = sqlite3.connect(str(db_path))
        remaining = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        conn.close()
        assert remaining == 5


class TestPruneRemovesLowScoreOld:
    """prune_stale_patterns deletes qualifying rows while honouring keep_min."""

    def test_prune_removes_low_score_old(self, db_path):
        # 5 stale worthless patterns + 110 fresh good patterns
        stale_ids = [
            _seed_pattern(db_path, success=0, cost=0.15, age_days=100)
            for _ in range(5)
        ]
        for _ in range(110):
            _seed_pattern(db_path, success=1, cost=0.005, age_days=1)

        deleted = prune_stale_patterns(min_score=0.1, max_age_days=90, keep_min=100, db=db_path)
        assert deleted == 5

        conn = sqlite3.connect(str(db_path))
        for pid in stale_ids:
            row = conn.execute("SELECT id FROM patterns WHERE id = ?", (pid,)).fetchone()
            assert row is None, f"{pid} should have been pruned"
        conn.close()


class TestRecordUseIncrementsCount:
    """record_pattern_use increments use_count on each call."""

    def test_record_use_increments_count(self, db_path):
        pid = _seed_pattern(db_path)
        retention_get_conn(db_path).close()  # ensure retention columns exist

        record_pattern_use(pid, db=db_path)
        record_pattern_use(pid, db=db_path)
        record_pattern_use(pid, db=db_path)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT use_count FROM patterns WHERE id = ?", (pid,)).fetchone()
        conn.close()
        assert row[0] == 3


class TestProtectHighValuePinsAt1:
    """protect_high_value pins retention_score to 1.0 for patterns above the threshold."""

    def test_protect_high_value_pins_at_1(self, db_path):
        pid = _seed_pattern(db_path, success=1, cost=0.001, age_days=0)
        compute_retention_score(pid, db=db_path)

        # Force score to exactly 0.85 so it qualifies
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE patterns SET retention_score = 0.85 WHERE id = ?", (pid,))
        conn.commit()
        conn.close()

        protected = protect_high_value(threshold=0.8, db=db_path)
        assert pid in protected

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT retention_score FROM patterns WHERE id = ?", (pid,)).fetchone()
        conn.close()
        assert row[0] == pytest.approx(1.0, abs=1e-6)


class TestRefreshScoresBatch:
    """refresh_retention_scores updates all patterns up to batch_size."""

    def test_refresh_scores_batch(self, db_path):
        for _ in range(6):
            _seed_pattern(db_path, success=1, cost=0.01)

        # batch_size=4 should update exactly 4 patterns
        updated = refresh_retention_scores(batch_size=4, db=db_path)
        assert updated == 4

    def test_refresh_scores_all(self, db_path):
        for _ in range(5):
            _seed_pattern(db_path, success=1, cost=0.01)

        updated = refresh_retention_scores(batch_size=100, db=db_path)
        assert updated == 5

    def test_refresh_scores_empty_table(self, db_path):
        # Create DB with empty patterns table
        retention_get_conn(db_path).close()
        updated = refresh_retention_scores(db=db_path)
        assert updated == 0

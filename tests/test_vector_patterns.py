"""Tests for cap.harness.vector_patterns.

All tests are fully offline — no Bedrock calls, no LanceDB on disk.
Dependencies are patched at the source module level since EmbeddingClient
is imported lazily inside _init().
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.vector_patterns import PatternEmbedder, _run_async


# ---------------------------------------------------------------------------
# _run_async helper
# ---------------------------------------------------------------------------

def test_run_async_runs_coroutine():
    async def _coro():
        return 42

    assert _run_async(_coro()) == 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Return a mock lancedb module that yields a fake db + table."""
    mock_table = table or MagicMock()
    mock_db = MagicMock()
    mock_db.open_table.return_value = mock_table
    mock_db.create_table.return_value = mock_table

    mock_lancedb = MagicMock()
    mock_lancedb.connect.return_value = mock_db
    return mock_lancedb, mock_db, mock_table


# ---------------------------------------------------------------------------
# PatternEmbedder.is_available
# ---------------------------------------------------------------------------

def test_is_available_false_when_bedrock_marks_unavailable(tmp_path):
    client = _mock_embedding_client(is_avail=False)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.is_available is False


def test_is_available_true_when_bedrock_ok(tmp_path):
    client = _mock_embedding_client(is_avail=None)  # None = not yet tested
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.is_available is True


# ---------------------------------------------------------------------------
# embed_pattern
# ---------------------------------------------------------------------------

def test_embed_pattern_returns_true(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, mock_db, mock_table = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        result = pe.embed_pattern("abc123", "deploy the auth service to production")
        assert result is True
        mock_table.add.assert_called_once()


def test_embed_pattern_creates_table_when_missing(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, mock_db, mock_table = _mock_lancedb(tmp_path)
    mock_db.open_table.side_effect = Exception("table not found")

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        result = pe.embed_pattern("abc123", "some task")
        assert result is True
        mock_db.create_table.assert_called_once()


def test_embed_pattern_returns_false_when_embed_returns_none(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    async def _returns_none(text):
        return None

    client.embed_single.side_effect = _returns_none

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.embed_pattern("id1", "some text") is False


def test_embed_pattern_returns_false_on_empty_text(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.embed_pattern("id1", "") is False
        assert pe.embed_pattern("id1", "   ") is False


def test_embed_pattern_returns_false_when_client_unavailable(tmp_path):
    client = _mock_embedding_client(is_avail=False)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.embed_pattern("id1", "some text") is False


# ---------------------------------------------------------------------------
# search_similar
# ---------------------------------------------------------------------------

def test_search_similar_returns_hits_above_threshold(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, mock_table = _mock_lancedb(tmp_path)

    mock_table.search.return_value.metric.return_value.limit.return_value.to_list.return_value = [
        {"id": "pat1", "text": "deploy auth service", "_distance": 0.1},
        {"id": "pat2", "text": "build lambda function", "_distance": 0.6},
    ]

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        results = pe.search_similar("deploy auth service", limit=5, min_score=0.5)

    # pat1: score = 1 - 0.1 = 0.9 >= 0.5  -> included
    # pat2: score = 1 - 0.6 = 0.4  < 0.5  -> excluded
    assert len(results) == 1
    assert results[0]["pattern_id"] == "pat1"
    assert results[0]["score"] == pytest.approx(0.9, abs=0.001)


def test_search_similar_empty_when_unavailable(tmp_path):
    client = _mock_embedding_client(is_avail=False)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.search_similar("query") == []


def test_search_similar_empty_on_blank_query(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.search_similar("") == []
        assert pe.search_similar("   ") == []


def test_search_similar_sorted_descending(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, mock_table = _mock_lancedb(tmp_path)

    mock_table.search.return_value.metric.return_value.limit.return_value.to_list.return_value = [
        {"id": "low",  "text": "low relevance",  "_distance": 0.45},
        {"id": "high", "text": "high relevance", "_distance": 0.05},
        {"id": "mid",  "text": "mid relevance",  "_distance": 0.25},
    ]

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        results = pe.search_similar("query", min_score=0.5)

    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0]["pattern_id"] == "high"


def test_search_similar_empty_when_embed_returns_none(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    async def _returns_none(text):
        return None

    client.embed_single.side_effect = _returns_none

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.search_similar("some query") == []


# ---------------------------------------------------------------------------
# bulk_embed_missing
# ---------------------------------------------------------------------------

def test_bulk_embed_missing_returns_zero_when_unavailable(tmp_path):
    client = _mock_embedding_client(is_avail=False)
    mock_ldb, _, _ = _mock_lancedb(tmp_path)

    with patch("cap.lib.embeddings.EmbeddingClient", return_value=client), \
         patch("cap.harness.vector_patterns.VECTORS_DIR", tmp_path), \
         patch.dict("sys.modules", {"lancedb": mock_ldb}):
        pe = PatternEmbedder()
        assert pe.bulk_embed_missing() == 0


def test_bulk_embed_missing_embeds_rows(tmp_path):
    client = _mock_embedding_client(is_avail=None)
    mock_ldb, _, mock_table = _mock_lancedb(tmp_path)

    # Build a real SQLite DB with two un-embedded patterns
    db_file = tmp_path / "platform.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE patterns (
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
           )"""
    )
    conn.execute("INSERT INTO patterns (id, prompt_summary) VALUES ('id1', 'task one')")
    conn.execute("INSERT INTO patterns (id, prompt_summary) VALUES ('id2', 'task two')")
    conn.commit()
    conn.close()

    # Patch _get_conn in agentdb to return a real connection to our test DB
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


# ---------------------------------------------------------------------------
# Integration: agentdb_pattern_search uses vector results when available
# ---------------------------------------------------------------------------

def test_agentdb_pattern_search_uses_vector_hits(tmp_path):
    """When PatternEmbedder returns vector hits, search returns enriched rows."""
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

    # PatternEmbedder is imported inside the function — patch at source
    mock_pe_instance = MagicMock()
    mock_pe_instance.is_available = True
    mock_pe_instance.search_similar.return_value = [
        {"pattern_id": pattern_id, "score": 0.92, "text": "deploy the auth microservice to staging"}
    ]
    mock_pe_class = MagicMock(return_value=mock_pe_instance)

    with patch("cap.harness.vector_patterns.PatternEmbedder", mock_pe_class), \
         patch.dict("sys.modules", {"cap.harness.vector_patterns": __import__(
             "cap.harness.vector_patterns", fromlist=["PatternEmbedder"]
         )}):
        # Re-import to pick up the patch cleanly via the import statement inside the function
        import importlib
        import cap.harness.agentdb as _agentdb_mod
        orig = _agentdb_mod.__dict__.get("PatternEmbedder")
        _agentdb_mod.__dict__["PatternEmbedder"] = mock_pe_class  # inject at module level temporarily
        try:
            hits = agentdb_pattern_search("deploy auth service", _db_path=db_path)
        finally:
            if orig is None:
                _agentdb_mod.__dict__.pop("PatternEmbedder", None)
            else:
                _agentdb_mod.__dict__["PatternEmbedder"] = orig

    assert len(hits) == 1
    assert hits[0]["pattern_id"] == pattern_id
    assert hits[0]["model"] == "claude-sonnet-4-6"


def test_agentdb_pattern_search_falls_back_to_like(tmp_path):
    """When PatternEmbedder is unavailable, fall back to LIKE search."""
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

    assert any("retry" in h["prompt_summary"].lower() for h in hits)

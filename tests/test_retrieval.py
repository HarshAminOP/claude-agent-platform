"""Tests for cap.lib.retrieval — hybrid retrieval with RRF."""

import json
import sqlite3

import pytest

from cap.lib.retrieval import (
    SearchResult,
    _compute_weights,
    _resolve_uuids,
    hybrid_search,
    keyword_search,
    rrf_merge,
    semantic_search,
)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title,
    content,
    content='knowledge_entries',
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid         TEXT UNIQUE,
    title        TEXT,
    content      TEXT,
    source_path  TEXT,
    content_type TEXT DEFAULT 'markdown',
    workspace    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
    id          TEXT PRIMARY KEY,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0,
    metadata  TEXT,
    workspace TEXT NOT NULL,
    UNIQUE(source_id, target_id, predicate)
);
"""


def _insert_entry(conn, uuid, title, content, workspace, content_type="markdown"):
    conn.execute(
        "INSERT INTO knowledge_entries (uuid, title, content, content_type, workspace) "
        "VALUES (?, ?, ?, ?, ?)",
        (uuid, title, content, content_type, workspace),
    )
    row = conn.execute("SELECT id FROM knowledge_entries WHERE uuid = ?", (uuid,)).fetchone()
    entry_id = row[0]
    conn.execute(
        "INSERT INTO knowledge_fts(rowid, title, content) VALUES (?, ?, ?)",
        (entry_id, title, content),
    )
    conn.commit()
    return entry_id


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    yield c
    c.close()


@pytest.fixture()
def conn_with_data(conn):
    _insert_entry(conn, "uuid-1", "Kubernetes operator guide", "Manage k8s operators efficiently", "ws1")
    _insert_entry(conn, "uuid-2", "ArgoCD sync strategies", "Progressive delivery with ArgoCD", "ws1")
    _insert_entry(conn, "uuid-3", "Terraform modules", "Reusable infrastructure with Terraform", "ws1")
    _insert_entry(conn, "uuid-4", "Other workspace entry", "Unrelated content", "ws2")
    return conn


# ---------------------------------------------------------------------------
# keyword_search
# ---------------------------------------------------------------------------

class TestKeywordSearch:
    def test_returns_matching_entries(self, conn_with_data):
        results = keyword_search(conn_with_data, "ArgoCD", "ws1")
        ids = [r[0] for r in results]
        row = conn_with_data.execute(
            "SELECT id FROM knowledge_entries WHERE uuid='uuid-2'"
        ).fetchone()
        assert row[0] in ids

    def test_workspace_scoped(self, conn_with_data):
        results = keyword_search(conn_with_data, "Unrelated", "ws1")
        # uuid-4 is in ws2 — should not appear
        ws2_id = conn_with_data.execute(
            "SELECT id FROM knowledge_entries WHERE uuid='uuid-4'"
        ).fetchone()[0]
        assert ws2_id not in [r[0] for r in results]

    def test_no_match_returns_empty(self, conn_with_data):
        results = keyword_search(conn_with_data, "xyzzy_nonexistent_token", "ws1")
        assert results == []

    def test_top_k_limit(self, conn_with_data):
        results = keyword_search(conn_with_data, "the", "ws1", top_k=1)
        assert len(results) <= 1

    def test_fts_table_missing_returns_empty(self, conn):
        # Drop the FTS table to simulate unavailability
        conn.execute("DROP TABLE knowledge_fts")
        results = keyword_search(conn, "anything", "ws1")
        assert results == []


# ---------------------------------------------------------------------------
# semantic_search
# ---------------------------------------------------------------------------

class _MockVectorsTable:
    """Minimal mock for a LanceDB table."""

    def __init__(self, hits):
        self._hits = hits  # list of dicts with uuid, workspace, _distance

    def search(self, vector):
        return self

    def metric(self, m):
        return self

    def where(self, clause):
        # Apply simple workspace filter
        ws = clause.split("'")[1]
        self._filtered = [h for h in self._hits if h["workspace"] == ws]
        return self

    def limit(self, n):
        self._filtered = self._filtered[:n]
        return self

    def to_list(self):
        return self._filtered


class TestSemanticSearch:
    def test_returns_uuid_similarity_pairs(self):
        table = _MockVectorsTable([
            {"uuid": "uuid-1", "workspace": "ws1", "_distance": 0.1},
            {"uuid": "uuid-2", "workspace": "ws1", "_distance": 0.3},
        ])
        results = semantic_search(table, [0.1, 0.2], "ws1", top_k=10)
        assert len(results) == 2
        # Lower distance => higher similarity
        uuids = [r[0] for r in results]
        scores = [r[1] for r in results]
        assert "uuid-1" in uuids
        assert abs(scores[uuids.index("uuid-1")] - 0.9) < 1e-6

    def test_workspace_filter_applied(self):
        table = _MockVectorsTable([
            {"uuid": "uuid-1", "workspace": "ws1", "_distance": 0.1},
            {"uuid": "uuid-2", "workspace": "ws2", "_distance": 0.05},
        ])
        results = semantic_search(table, [0.1], "ws1")
        assert len(results) == 1
        assert results[0][0] == "uuid-1"

    def test_error_returns_empty(self):
        class BrokenTable:
            def search(self, v):
                raise RuntimeError("LanceDB down")

        results = semantic_search(BrokenTable(), [0.1], "ws1")
        assert results == []


# ---------------------------------------------------------------------------
# rrf_merge
# ---------------------------------------------------------------------------

class TestRrfMerge:
    def test_basic_merge(self):
        kw = [(1, -10.0), (2, -5.0)]   # id=1 ranks first (more negative)
        sem = [("uuid-a", 0.9), ("uuid-b", 0.7)]
        gr = [(1, 1.0), (3, 0.5)]
        merged = rrf_merge(kw, sem, gr, weights={"keyword": 0.3, "semantic": 0.5, "graph": 0.2})
        assert len(merged) > 0
        # Result is sorted descending by rrf_score
        scores = [s for _, s in merged]
        assert scores == sorted(scores, reverse=True)

    def test_doc_in_multiple_channels_accumulates(self):
        # entry_id=1 appears in both keyword and graph; entry_id=2 only in keyword
        kw = [(1, -10.0), (2, -5.0)]
        sem = []
        gr = [(1, 1.0)]
        merged = dict(rrf_merge(kw, sem, gr, weights={"keyword": 0.5, "semantic": 0.0, "graph": 0.5}))
        # entry_id=1 (in both) must score higher than entry_id=2 (only keyword)
        assert merged[1] > merged[2]

    def test_top_k_respected(self):
        kw = [(i, float(-i)) for i in range(1, 21)]
        merged = rrf_merge(kw, [], [], weights={"keyword": 1.0, "semantic": 0.0, "graph": 0.0}, top_k=5)
        assert len(merged) <= 5

    def test_empty_channels_return_empty(self):
        assert rrf_merge([], [], [], top_k=10) == []

    def test_default_weights_used(self):
        kw = [(1, -1.0)]
        sem = [("u1", 0.9)]
        merged_default = rrf_merge(kw, sem, [], top_k=10)
        assert len(merged_default) > 0

    def test_k_smoothing_constant(self):
        """Higher k should lower score differences between ranks."""
        kw = [(1, -10.0), (2, -5.0)]
        merged_low_k = dict(rrf_merge(kw, [], [], weights={"keyword": 1.0, "semantic": 0.0, "graph": 0.0}, k=1))
        merged_high_k = dict(rrf_merge(kw, [], [], weights={"keyword": 1.0, "semantic": 0.0, "graph": 0.0}, k=1000))
        diff_low = merged_low_k[1] - merged_low_k[2]
        diff_high = merged_high_k[1] - merged_high_k[2]
        assert diff_low > diff_high


# ---------------------------------------------------------------------------
# _compute_weights
# ---------------------------------------------------------------------------

class TestComputeWeights:
    def test_all_present(self):
        w = _compute_weights(has_keyword=True, has_semantic=True, has_graph=True, query_vector=[0.1])
        assert abs(sum(w.values()) - 1.0) < 1e-6
        assert w["semantic"] == 0.5

    def test_no_semantic(self):
        w = _compute_weights(has_keyword=True, has_semantic=False, has_graph=True, query_vector=None)
        assert w["semantic"] == 0.0
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_no_graph(self):
        w = _compute_weights(has_keyword=True, has_semantic=True, has_graph=False, query_vector=[0.1])
        assert w["graph"] == 0.0
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_keyword_only(self):
        w = _compute_weights(has_keyword=True, has_semantic=False, has_graph=False, query_vector=None)
        assert w["keyword"] == 1.0
        assert w["semantic"] == 0.0
        assert w["graph"] == 0.0

    def test_all_absent(self):
        w = _compute_weights(has_keyword=False, has_semantic=False, has_graph=False, query_vector=None)
        # Falls back — no channels, but weights should not error
        assert isinstance(w, dict)


# ---------------------------------------------------------------------------
# hybrid_search — integration
# ---------------------------------------------------------------------------

class TestHybridSearch:
    def test_keyword_only_path(self, conn_with_data):
        """When query_vector=None and no graph entities, only keyword fires."""
        results = hybrid_search(
            conn=conn_with_data,
            vectors_table=None,
            query="ArgoCD",
            query_vector=None,
            workspace="ws1",
            top_k=5,
        )
        assert isinstance(results, list)
        if results:
            assert all(isinstance(r, SearchResult) for r in results)
            assert all(r.workspace == "ws1" for r in results)

    def test_returns_searchresult_objects(self, conn_with_data):
        results = hybrid_search(
            conn=conn_with_data,
            vectors_table=None,
            query="Kubernetes operator",
            query_vector=None,
            workspace="ws1",
            top_k=3,
        )
        for r in results:
            assert isinstance(r.entry_id, int)
            assert isinstance(r.title, str)
            assert isinstance(r.content_preview, str)
            assert len(r.content_preview) <= 200
            assert isinstance(r.channels, list)

    def test_content_preview_capped_at_200(self, conn):
        long_content = "x" * 500
        _insert_entry(conn, "uuid-long", "Long Entry", long_content, "ws1")
        results = hybrid_search(conn, None, "Long Entry", None, "ws1", top_k=1)
        if results:
            assert len(results[0].content_preview) <= 200

    def test_all_channels_empty_returns_empty_list(self, conn):
        """No entries in DB at all should return empty list, not error."""
        results = hybrid_search(conn, None, "anything", None, "ws1", top_k=5)
        assert results == []

    def test_workspace_scoped(self, conn_with_data):
        results = hybrid_search(
            conn=conn_with_data,
            vectors_table=None,
            query="Unrelated",
            query_vector=None,
            workspace="ws1",
            top_k=5,
        )
        # uuid-4 is in ws2 — must not appear in ws1 results
        titles = [r.title for r in results]
        assert "Other workspace entry" not in titles

    def test_scores_descending(self, conn_with_data):
        results = hybrid_search(
            conn=conn_with_data,
            vectors_table=None,
            query="Terraform",
            query_vector=None,
            workspace="ws1",
            top_k=5,
        )
        if len(results) > 1:
            scores = [r.score for r in results]
            assert scores == sorted(scores, reverse=True)

    def test_semantic_channel_used_when_vector_provided(self, conn_with_data):
        entry_id_1 = conn_with_data.execute(
            "SELECT id FROM knowledge_entries WHERE uuid='uuid-1'"
        ).fetchone()[0]

        table = _MockVectorsTable([
            {"uuid": "uuid-1", "workspace": "ws1", "_distance": 0.05},
        ])
        results = hybrid_search(
            conn=conn_with_data,
            vectors_table=table,
            query="operator",
            query_vector=[0.1, 0.2, 0.3],
            workspace="ws1",
            top_k=5,
        )
        entry_ids = [r.entry_id for r in results]
        assert entry_id_1 in entry_ids
        # The result from the semantic channel should list "semantic" in channels
        hit = next(r for r in results if r.entry_id == entry_id_1)
        assert "semantic" in hit.channels


# ---------------------------------------------------------------------------
# _resolve_uuids
# ---------------------------------------------------------------------------

class TestResolveUuids:
    def test_maps_uuid_to_entry_id(self, conn_with_data):
        row = conn_with_data.execute(
            "SELECT id FROM knowledge_entries WHERE uuid='uuid-1'"
        ).fetchone()
        mapping = _resolve_uuids(conn_with_data, ["uuid-1"], "ws1")
        assert mapping["uuid-1"] == row[0]

    def test_missing_uuid_not_in_result(self, conn_with_data):
        mapping = _resolve_uuids(conn_with_data, ["non-existent-uuid"], "ws1")
        assert mapping == {}

    def test_empty_list(self, conn_with_data):
        assert _resolve_uuids(conn_with_data, [], "ws1") == {}

    def test_workspace_scoped(self, conn_with_data):
        # uuid-4 belongs to ws2 — should not resolve under ws1
        mapping = _resolve_uuids(conn_with_data, ["uuid-4"], "ws1")
        assert mapping == {}

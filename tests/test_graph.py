"""Tests for cap.lib.graph — graph traversal on SQLite adjacency tables."""

import json
import sqlite3
import pytest

from cap.lib.graph import (
    add_edge,
    bfs_traverse,
    find_entities,
    get_node_context,
    get_related_entries,
    get_related_entries_with_depth,
    _node_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GRAPH_SCHEMA = """
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
    source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    predicate TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0,
    metadata  TEXT,
    workspace TEXT NOT NULL,
    UNIQUE(source_id, target_id, predicate)
);
"""


@pytest.fixture()
def conn():
    """In-memory SQLite connection with graph tables."""
    c = sqlite3.connect(":memory:")
    c.executescript(GRAPH_SCHEMA)
    yield c
    c.close()


@pytest.fixture()
def populated_conn(conn):
    """Graph: A -[owns]-> B -[depends_on]-> C; A -[uses]-> D."""
    add_edge(conn, "ServiceA", "service", "ServiceB", "service", "owns", "ws1")
    add_edge(conn, "ServiceB", "service", "ServiceC", "service", "depends_on", "ws1")
    add_edge(conn, "ServiceA", "service", "ServiceD", "service", "uses", "ws1")
    # Different workspace — must not appear in ws1 queries
    add_edge(conn, "ServiceA", "service", "ServiceX", "service", "owns", "ws2")
    return conn


# ---------------------------------------------------------------------------
# find_entities
# ---------------------------------------------------------------------------

class TestFindEntities:
    def test_exact_name_match(self, populated_conn):
        ids = find_entities(populated_conn, "ServiceA", "ws1")
        assert len(ids) == 1
        assert ids[0] == _node_id("ServiceA", "ws1")

    def test_partial_name_match(self, populated_conn):
        ids = find_entities(populated_conn, "Service", "ws1")
        assert len(ids) == 4  # A, B, C, D in ws1

    def test_case_insensitive(self, populated_conn):
        ids = find_entities(populated_conn, "servicea", "ws1")
        assert len(ids) == 1

    def test_workspace_scoped(self, populated_conn):
        ids = find_entities(populated_conn, "ServiceX", "ws1")
        assert ids == []

    def test_no_match(self, populated_conn):
        assert find_entities(populated_conn, "DoesNotExist", "ws1") == []


# ---------------------------------------------------------------------------
# bfs_traverse
# ---------------------------------------------------------------------------

class TestBfsTraverse:
    def test_depth_zero_returns_seeds(self, populated_conn):
        seed = [_node_id("ServiceA", "ws1")]
        result = bfs_traverse(populated_conn, seed, max_depth=0, workspace="ws1")
        assert len(result) == 1
        assert result[0] == (_node_id("ServiceA", "ws1"), 0)

    def test_depth_one(self, populated_conn):
        seed = [_node_id("ServiceA", "ws1")]
        result = dict(bfs_traverse(populated_conn, seed, max_depth=1, workspace="ws1"))
        # B and D are 1 hop away
        assert result[_node_id("ServiceB", "ws1")] == 1
        assert result[_node_id("ServiceD", "ws1")] == 1

    def test_depth_two_reaches_c(self, populated_conn):
        seed = [_node_id("ServiceA", "ws1")]
        result = dict(bfs_traverse(populated_conn, seed, max_depth=2, workspace="ws1"))
        assert result[_node_id("ServiceC", "ws1")] == 2

    def test_cycle_safe(self, conn):
        """Graph with a cycle A->B->A must not loop forever."""
        add_edge(conn, "NodeA", "svc", "NodeB", "svc", "links", "ws1")
        add_edge(conn, "NodeB", "svc", "NodeA", "svc", "links", "ws1")
        seed = [_node_id("NodeA", "ws1")]
        result = bfs_traverse(conn, seed, max_depth=5, workspace="ws1")
        node_ids = [r[0] for r in result]
        # Each node appears at most once
        assert len(node_ids) == len(set(node_ids))

    def test_empty_start_ids(self, populated_conn):
        assert bfs_traverse(populated_conn, [], max_depth=2, workspace="ws1") == []

    def test_workspace_filter_excludes_cross_workspace_nodes(self, populated_conn):
        seed = [_node_id("ServiceA", "ws1")]
        result = dict(bfs_traverse(populated_conn, seed, max_depth=1, workspace="ws1"))
        # ServiceX is only in ws2 — must not appear
        assert _node_id("ServiceX", "ws2") not in result

    def test_multiple_seeds(self, populated_conn):
        seeds = [_node_id("ServiceB", "ws1"), _node_id("ServiceD", "ws1")]
        result = dict(bfs_traverse(populated_conn, seeds, max_depth=1, workspace="ws1"))
        # Both seeds at depth 0
        assert result[_node_id("ServiceB", "ws1")] == 0
        assert result[_node_id("ServiceD", "ws1")] == 0


# ---------------------------------------------------------------------------
# get_related_entries_with_depth
# ---------------------------------------------------------------------------

GRAPH_SCHEMA_WITH_ENTRIES = GRAPH_SCHEMA + """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid         TEXT UNIQUE,
    title        TEXT,
    content      TEXT,
    source_path  TEXT,
    content_type TEXT,
    workspace    TEXT
);
"""


@pytest.fixture()
def conn_with_entries():
    c = sqlite3.connect(":memory:")
    c.executescript(GRAPH_SCHEMA_WITH_ENTRIES)
    yield c
    c.close()


@pytest.fixture()
def graph_with_entries(conn_with_entries):
    conn = conn_with_entries
    # Create an entry
    conn.execute(
        "INSERT INTO knowledge_entries (uuid, title, content, content_type, workspace) "
        "VALUES ('uuid-1', 'Entry One', 'Some content', 'markdown', 'ws1')"
    )
    entry_id = conn.execute("SELECT id FROM knowledge_entries WHERE uuid='uuid-1'").fetchone()[0]

    # Create a node whose metadata references the entry
    node_id = _node_id("ServiceA", "ws1")
    conn.execute(
        "INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace, metadata, created_at) "
        "VALUES (?, 'ServiceA', 'service', 'ws1', ?, '2024-01-01')",
        (node_id, json.dumps({"entry_id": entry_id})),
    )
    conn.commit()
    return conn, entry_id


class TestGetRelatedEntriesWithDepth:
    def test_score_at_depth_zero(self, graph_with_entries):
        conn, entry_id = graph_with_entries
        node_id = _node_id("ServiceA", "ws1")
        result = get_related_entries_with_depth(conn, [(node_id, 0)], "ws1")
        assert len(result) == 1
        assert result[0][0] == entry_id
        assert abs(result[0][1] - 1.0) < 1e-6  # 1/(0+1) = 1.0

    def test_score_at_depth_one(self, graph_with_entries):
        conn, entry_id = graph_with_entries
        node_id = _node_id("ServiceA", "ws1")
        result = get_related_entries_with_depth(conn, [(node_id, 1)], "ws1")
        assert abs(result[0][1] - 0.5) < 1e-6  # 1/(1+1) = 0.5

    def test_empty_nodes(self, graph_with_entries):
        conn, _ = graph_with_entries
        assert get_related_entries_with_depth(conn, [], "ws1") == []

    def test_wrong_workspace(self, graph_with_entries):
        conn, _ = graph_with_entries
        node_id = _node_id("ServiceA", "ws1")
        result = get_related_entries_with_depth(conn, [(node_id, 0)], "ws_other")
        assert result == []


# ---------------------------------------------------------------------------
# add_edge
# ---------------------------------------------------------------------------

class TestAddEdge:
    def test_creates_nodes_and_edge(self, conn):
        add_edge(conn, "Svc1", "service", "Svc2", "service", "calls", "ws1")
        nodes = conn.execute("SELECT entity_name FROM knowledge_graph_nodes ORDER BY entity_name").fetchall()
        assert {r[0] for r in nodes} == {"Svc1", "Svc2"}
        edge = conn.execute("SELECT predicate FROM knowledge_graph_edges").fetchone()
        assert edge[0] == "calls"

    def test_upsert_increments_weight(self, conn):
        add_edge(conn, "Svc1", "service", "Svc2", "service", "calls", "ws1")
        add_edge(conn, "Svc1", "service", "Svc2", "service", "calls", "ws1")
        row = conn.execute("SELECT weight FROM knowledge_graph_edges").fetchone()
        assert row[0] == 2.0

    def test_metadata_stored_as_json(self, conn):
        add_edge(conn, "A", "svc", "B", "svc", "rel", "ws1", metadata={"key": "value"})
        row = conn.execute("SELECT metadata FROM knowledge_graph_edges").fetchone()
        assert json.loads(row[0]) == {"key": "value"}

    def test_different_predicates_create_separate_edges(self, conn):
        add_edge(conn, "A", "svc", "B", "svc", "rel1", "ws1")
        add_edge(conn, "A", "svc", "B", "svc", "rel2", "ws1")
        count = conn.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# get_node_context
# ---------------------------------------------------------------------------

class TestGetNodeContext:
    def test_returns_node_and_edges(self, populated_conn):
        ctx = get_node_context(populated_conn, "ServiceA", "ws1")
        assert ctx["node"]["entity_name"] == "ServiceA"
        assert len(ctx["edges_out"]) == 2  # owns B, uses D
        assert len(ctx["edges_in"]) == 0

    def test_returns_empty_for_missing_node(self, populated_conn):
        assert get_node_context(populated_conn, "Ghost", "ws1") == {}

    def test_workspace_scoped(self, populated_conn):
        # ServiceA in ws2 has different node ID
        ctx = get_node_context(populated_conn, "ServiceA", "ws2")
        assert ctx["node"]["workspace"] == "ws2"
        # Only 1 edge: A->X in ws2
        assert len(ctx["edges_out"]) == 1

    def test_related_nodes_populated(self, populated_conn):
        ctx = get_node_context(populated_conn, "ServiceA", "ws1")
        related_names = {n["entity_name"] for n in ctx["related_nodes"]}
        assert "ServiceB" in related_names
        assert "ServiceD" in related_names

"""Tests for cap.lib.knowledge_graph — higher-level graph abstraction.

Covers:
  - NodeMetadata serialisation / merge semantics
  - KnowledgeGraph write operations (upsert_node, upsert_edge, update_node_metadata,
    remove_stale_edges)
  - Query operations (find_by_type, find_by_domain, get_dependencies, get_dependents,
    get_deployment_chain, get_service_map, get_domain_overview, search, get_stats)
  - Change tracking (mark_analyzed, get_stale_nodes, get_change_propagation_targets)
  - Batch operations (bulk_upsert_nodes, bulk_upsert_edges)
  - Type validation (invalid NodeType / EdgeType raise ValueError)
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import pytest

from cap.lib.knowledge_graph import (
    EdgeType,
    KnowledgeGraph,
    NodeMetadata,
    NodeType,
    _directed_bfs,
    _inbound_sources,
    _node_id,
    _outbound_targets,
)


# ---------------------------------------------------------------------------
# Shared schema (copied from test_graph.py for isolation)
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

WS = "test-workspace"


@pytest.fixture()
def conn():
    """In-memory SQLite connection with graph tables."""
    c = sqlite3.connect(":memory:")
    c.executescript(GRAPH_SCHEMA)
    yield c
    c.close()


@pytest.fixture()
def kg(conn):
    """KnowledgeGraph instance wired to the in-memory connection."""
    return KnowledgeGraph(conn, WS)


# ---------------------------------------------------------------------------
# NodeMetadata
# ---------------------------------------------------------------------------

class TestNodeMetadata:
    def test_to_json_round_trip(self):
        meta = NodeMetadata(
            summary="A test service",
            domain_tags=["platform", "infra"],
            complexity=3,
            health="healthy",
            entry_id=42,
        )
        raw = meta.to_json()
        restored = NodeMetadata.from_json(raw)
        assert restored.summary == "A test service"
        assert restored.domain_tags == ["platform", "infra"]
        assert restored.complexity == 3
        assert restored.health == "healthy"
        assert restored.entry_id == 42

    def test_from_json_none_returns_defaults(self):
        meta = NodeMetadata.from_json(None)
        assert meta.summary == ""
        assert meta.domain_tags == []
        assert meta.health == "unknown"
        assert meta.entry_id is None

    def test_from_json_invalid_returns_defaults(self):
        meta = NodeMetadata.from_json("not-json{{{")
        assert meta.summary == ""

    def test_from_json_unknown_keys_ignored(self):
        raw = json.dumps({"summary": "hello", "unknown_future_key": 99})
        meta = NodeMetadata.from_json(raw)
        assert meta.summary == "hello"

    def test_merge_prefers_other_non_empty(self):
        base = NodeMetadata(summary="old", complexity=2, health="degraded")
        new = NodeMetadata(summary="new", complexity=0)  # complexity=0 means unset
        merged = base.merge_from(new)
        assert merged.summary == "new"
        assert merged.complexity == 2  # kept from base because new has 0
        assert merged.health == "degraded"  # kept from base because new has "unknown"

    def test_merge_new_health_overwrites_unknown(self):
        base = NodeMetadata(health="unknown")
        new = NodeMetadata(health="healthy")
        merged = base.merge_from(new)
        assert merged.health == "healthy"

    def test_merge_extra_dicts_combined(self):
        base = NodeMetadata(extra={"a": 1, "b": 2})
        new = NodeMetadata(extra={"b": 99, "c": 3})
        merged = base.merge_from(new)
        assert merged.extra == {"a": 1, "b": 99, "c": 3}

    def test_merge_domain_tags_prefer_new_if_non_empty(self):
        base = NodeMetadata(domain_tags=["old"])
        new = NodeMetadata(domain_tags=["new1", "new2"])
        merged = base.merge_from(new)
        assert merged.domain_tags == ["new1", "new2"]

    def test_merge_domain_tags_keep_base_if_new_empty(self):
        base = NodeMetadata(domain_tags=["old"])
        new = NodeMetadata(domain_tags=[])
        merged = base.merge_from(new)
        assert merged.domain_tags == ["old"]


# ---------------------------------------------------------------------------
# upsert_node
# ---------------------------------------------------------------------------

class TestUpsertNode:
    def test_creates_node_returns_id(self, kg, conn):
        nid = kg.upsert_node("repo-a", NodeType.REPO)
        assert nid == _node_id("repo-a", WS)
        row = conn.execute(
            "SELECT entity_type FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()
        assert row is not None
        assert row[0] == NodeType.REPO

    def test_metadata_stored(self, kg, conn):
        meta = NodeMetadata(summary="My repo", domain_tags=["platform"])
        nid = kg.upsert_node("repo-b", NodeType.REPO, metadata=meta)
        raw = conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
        stored = NodeMetadata.from_json(raw)
        assert stored.summary == "My repo"
        assert "platform" in stored.domain_tags

    def test_upsert_merges_metadata(self, kg, conn):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(summary="initial", complexity=2))
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(summary="updated"))
        nid = _node_id("svc-a", WS)
        raw = conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
        stored = NodeMetadata.from_json(raw)
        assert stored.summary == "updated"
        assert stored.complexity == 2  # kept from first upsert

    def test_invalid_node_type_raises(self, kg):
        with pytest.raises(ValueError, match="Unknown node_type"):
            kg.upsert_node("x", "not_a_real_type")

    def test_upsert_without_metadata(self, kg, conn):
        nid = kg.upsert_node("bare-node", NodeType.DOMAIN)
        row = conn.execute(
            "SELECT entity_type FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()
        assert row[0] == NodeType.DOMAIN


# ---------------------------------------------------------------------------
# upsert_edge
# ---------------------------------------------------------------------------

class TestUpsertEdge:
    def test_creates_edge(self, kg, conn):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        row = conn.execute("SELECT predicate FROM knowledge_graph_edges").fetchone()
        assert row[0] == EdgeType.DEPENDS_ON

    def test_creates_both_nodes(self, kg, conn):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        count = conn.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone()[0]
        assert count == 2

    def test_invalid_source_type_raises(self, kg):
        with pytest.raises(ValueError, match="source_type"):
            kg.upsert_edge("a", "bad_type", "b", NodeType.SERVICE, EdgeType.DEPENDS_ON)

    def test_invalid_target_type_raises(self, kg):
        with pytest.raises(ValueError, match="target_type"):
            kg.upsert_edge("a", NodeType.SERVICE, "b", "bad_type", EdgeType.DEPENDS_ON)

    def test_invalid_edge_type_raises(self, kg):
        with pytest.raises(ValueError, match="edge_type"):
            kg.upsert_edge("a", NodeType.SERVICE, "b", NodeType.SERVICE, "bad_edge")

    def test_metadata_stored_on_edge(self, kg, conn):
        kg.upsert_edge(
            "svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON,
            metadata={"port": 8080},
        )
        raw = conn.execute("SELECT metadata FROM knowledge_graph_edges").fetchone()[0]
        assert json.loads(raw) == {"port": 8080}


# ---------------------------------------------------------------------------
# update_node_metadata
# ---------------------------------------------------------------------------

class TestUpdateNodeMetadata:
    def test_merges_into_existing(self, kg, conn):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(summary="original", complexity=3))
        kg.update_node_metadata("svc-a", NodeMetadata(summary="patched", health="healthy"))
        nid = _node_id("svc-a", WS)
        raw = conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
        stored = NodeMetadata.from_json(raw)
        assert stored.summary == "patched"
        assert stored.complexity == 3    # preserved
        assert stored.health == "healthy"

    def test_noop_for_missing_node(self, kg):
        # Should not raise; silently skips
        kg.update_node_metadata("ghost", NodeMetadata(summary="ignored"))


# ---------------------------------------------------------------------------
# remove_stale_edges
# ---------------------------------------------------------------------------

class TestRemoveStaleEdges:
    def test_removes_edges_not_in_current_targets(self, kg, conn):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-1", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-2", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-3", NodeType.SERVICE, EdgeType.DEPENDS_ON)

        removed = kg.remove_stale_edges("svc-a", EdgeType.DEPENDS_ON, ["dep-1", "dep-3"])
        assert removed == 1  # dep-2 removed

        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_graph_edges WHERE predicate = ?",
            (EdgeType.DEPENDS_ON,),
        ).fetchone()[0]
        assert count == 2

    def test_returns_zero_when_no_stale(self, kg):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-1", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        removed = kg.remove_stale_edges("svc-a", EdgeType.DEPENDS_ON, ["dep-1"])
        assert removed == 0

    def test_removes_all_when_empty_current(self, kg, conn):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-1", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-2", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        removed = kg.remove_stale_edges("svc-a", EdgeType.DEPENDS_ON, [])
        assert removed == 2

    def test_does_not_remove_different_predicate(self, kg, conn):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-1", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "dep-1", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH)
        kg.remove_stale_edges("svc-a", EdgeType.DEPENDS_ON, [])
        # COMMUNICATES_WITH edge must survive
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_graph_edges WHERE predicate = ?",
            (EdgeType.COMMUNICATES_WITH,),
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# find_by_type
# ---------------------------------------------------------------------------

class TestFindByType:
    def test_returns_matching_nodes(self, kg):
        kg.upsert_node("repo-a", NodeType.REPO)
        kg.upsert_node("repo-b", NodeType.REPO)
        kg.upsert_node("svc-a", NodeType.SERVICE)
        repos = kg.find_by_type(NodeType.REPO)
        assert len(repos) == 2
        names = {r["entity_name"] for r in repos}
        assert names == {"repo-a", "repo-b"}

    def test_returns_empty_for_unknown_type(self, kg):
        kg.upsert_node("repo-a", NodeType.REPO)
        assert kg.find_by_type(NodeType.SERVICE) == []

    def test_workspace_isolation(self, conn):
        kg1 = KnowledgeGraph(conn, "ws1")
        kg2 = KnowledgeGraph(conn, "ws2")
        kg1.upsert_node("repo-a", NodeType.REPO)
        kg2.upsert_node("repo-b", NodeType.REPO)
        assert len(kg1.find_by_type(NodeType.REPO)) == 1
        assert kg1.find_by_type(NodeType.REPO)[0]["entity_name"] == "repo-a"


# ---------------------------------------------------------------------------
# find_by_domain
# ---------------------------------------------------------------------------

class TestFindByDomain:
    def test_finds_via_metadata_tags(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        kg.upsert_node("svc-b", NodeType.SERVICE, NodeMetadata(domain_tags=["platform"]))
        result = kg.find_by_domain("payments")
        assert len(result) == 1
        assert result[0]["entity_name"] == "svc-a"

    def test_finds_via_belongs_to_domain_edge(self, kg):
        kg.upsert_node("domain-payments", NodeType.DOMAIN)
        kg.upsert_node("svc-c", NodeType.SERVICE)
        kg.upsert_edge(
            "svc-c", NodeType.SERVICE,
            "domain-payments", NodeType.DOMAIN,
            EdgeType.BELONGS_TO_DOMAIN,
        )
        result = kg.find_by_domain("domain-payments")
        names = {r["entity_name"] for r in result}
        assert "svc-c" in names

    def test_returns_empty_for_unknown_domain(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["platform"]))
        assert kg.find_by_domain("no-such-domain") == []

    def test_node_tagged_multiple_domains_appears_once(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["platform", "infra"]))
        result = kg.find_by_domain("platform")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_dependencies / get_dependents
# ---------------------------------------------------------------------------

class TestGetDependencies:
    def test_direct_dependencies(self, kg):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        deps = kg.get_dependencies("svc-a", depth=1)
        names = {d["entity_name"] for d in deps}
        assert names == {"svc-b", "svc-c"}

    def test_transitive_dependencies(self, kg):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-b", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        deps = kg.get_dependencies("svc-a", depth=2)
        names = {d["entity_name"] for d in deps}
        assert "svc-b" in names
        assert "svc-c" in names
        assert "svc-a" not in names  # source excluded

    def test_depth_limits_transitive(self, kg):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("svc-b", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        deps = kg.get_dependencies("svc-a", depth=1)
        names = {d["entity_name"] for d in deps}
        assert "svc-b" in names
        assert "svc-c" not in names

    def test_no_depends_on_returns_empty(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE)
        assert kg.get_dependencies("svc-a") == []

    def test_does_not_follow_other_predicates(self, kg):
        kg.upsert_edge(
            "svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH
        )
        assert kg.get_dependencies("svc-a") == []


class TestGetDependents:
    def test_returns_direct_dependents(self, kg):
        kg.upsert_edge("consumer-a", NodeType.SERVICE, "lib-x", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("consumer-b", NodeType.SERVICE, "lib-x", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        dependents = kg.get_dependents("lib-x")
        names = {d["entity_name"] for d in dependents}
        assert names == {"consumer-a", "consumer-b"}

    def test_returns_empty_when_no_dependents(self, kg):
        kg.upsert_node("lib-x", NodeType.SERVICE)
        assert kg.get_dependents("lib-x") == []


# ---------------------------------------------------------------------------
# get_deployment_chain
# ---------------------------------------------------------------------------

class TestGetDeploymentChain:
    def test_missing_node_returns_empty(self, kg):
        result = kg.get_deployment_chain("ghost-repo")
        assert result["source"] is None
        assert result["argocd_apps"] == []
        assert result["clusters"] == []

    def test_traces_repo_to_cluster(self, kg):
        kg.upsert_node("my-repo", NodeType.REPO)
        kg.upsert_node("my-app", NodeType.ARGOCD_APP)
        kg.upsert_node("prod-cluster", NodeType.CLUSTER)

        # repo --DEPLOYED_BY--> argocd_app
        kg.upsert_edge(
            "my-repo", NodeType.REPO, "my-app", NodeType.ARGOCD_APP, EdgeType.DEPLOYED_BY
        )
        # argocd_app --DEPLOYS_TO--> cluster
        kg.upsert_edge(
            "my-app", NodeType.ARGOCD_APP, "prod-cluster", NodeType.CLUSTER, EdgeType.DEPLOYS_TO
        )

        result = kg.get_deployment_chain("my-repo")
        assert result["source"]["entity_name"] == "my-repo"
        app_names = {a["entity_name"] for a in result["argocd_apps"]}
        assert "my-app" in app_names
        cluster_names = {c["entity_name"] for c in result["clusters"]}
        assert "prod-cluster" in cluster_names


# ---------------------------------------------------------------------------
# get_service_map
# ---------------------------------------------------------------------------

class TestGetServiceMap:
    def test_returns_communicates_with_edges(self, kg):
        kg.upsert_edge(
            "svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH
        )
        kg.upsert_edge(
            "svc-b", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH
        )
        service_map = kg.get_service_map()
        assert len(service_map) == 2
        pairs = {(e["source_name"], e["target_name"]) for e in service_map}
        assert ("svc-a", "svc-b") in pairs
        assert ("svc-b", "svc-c") in pairs

    def test_excludes_non_communicates_edges(self, kg):
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        assert kg.get_service_map() == []

    def test_empty_graph_returns_empty(self, kg):
        assert kg.get_service_map() == []


# ---------------------------------------------------------------------------
# get_domain_overview
# ---------------------------------------------------------------------------

class TestGetDomainOverview:
    def test_groups_by_domain_tags(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        kg.upsert_node("svc-b", NodeType.SERVICE, NodeMetadata(domain_tags=["platform"]))
        kg.upsert_node("svc-c", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        overview = kg.get_domain_overview()
        assert len(overview["payments"]) == 2
        assert len(overview["platform"]) == 1

    def test_untagged_nodes_in_unclassified(self, kg):
        kg.upsert_node("orphan", NodeType.SERVICE)
        overview = kg.get_domain_overview()
        unclassified = {n["entity_name"] for n in overview.get("__unclassified__", [])}
        assert "orphan" in unclassified

    def test_multi_domain_node_appears_in_both_groups(self, kg):
        kg.upsert_node("svc-multi", NodeType.SERVICE, NodeMetadata(domain_tags=["alpha", "beta"]))
        overview = kg.get_domain_overview()
        alpha_names = {n["entity_name"] for n in overview.get("alpha", [])}
        beta_names = {n["entity_name"] for n in overview.get("beta", [])}
        assert "svc-multi" in alpha_names
        assert "svc-multi" in beta_names


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_matches_entity_name(self, kg):
        kg.upsert_node("payment-service", NodeType.SERVICE)
        kg.upsert_node("auth-service", NodeType.SERVICE)
        results = kg.search("payment")
        assert len(results) == 1
        assert results[0]["entity_name"] == "payment-service"

    def test_matches_metadata_summary(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(summary="handles billing"))
        kg.upsert_node("svc-b", NodeType.SERVICE, NodeMetadata(summary="auth service"))
        results = kg.search("billing")
        assert len(results) == 1
        assert results[0]["entity_name"] == "svc-a"

    def test_node_type_filter(self, kg):
        kg.upsert_node("repo-billing", NodeType.REPO)
        kg.upsert_node("svc-billing", NodeType.SERVICE)
        results = kg.search("billing", node_type=NodeType.REPO)
        assert len(results) == 1
        assert results[0]["entity_name"] == "repo-billing"

    def test_limit_respected(self, kg):
        for i in range(10):
            kg.upsert_node(f"svc-{i}", NodeType.SERVICE)
        results = kg.search("svc", limit=3)
        assert len(results) <= 3

    def test_no_match_returns_empty(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE)
        assert kg.search("does-not-exist") == []


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_returns_correct_counts(self, kg):
        kg.upsert_node("repo-a", NodeType.REPO)
        kg.upsert_node("svc-a", NodeType.SERVICE)
        kg.upsert_node("svc-b", NodeType.SERVICE)
        kg.upsert_edge("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        stats = kg.get_stats()
        assert stats["total_nodes"] == 3
        assert stats["nodes_by_type"][NodeType.SERVICE] == 2
        assert stats["nodes_by_type"][NodeType.REPO] == 1
        assert stats["total_edges"] == 1
        assert stats["edges_by_predicate"][EdgeType.DEPENDS_ON] == 1

    def test_domain_count(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["payments", "core"]))
        kg.upsert_node("svc-b", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        stats = kg.get_stats()
        assert stats["total_domains"] == 2  # "payments" and "core"

    def test_empty_graph(self, kg):
        stats = kg.get_stats()
        assert stats["total_nodes"] == 0
        assert stats["total_edges"] == 0
        assert stats["total_domains"] == 0


# ---------------------------------------------------------------------------
# mark_analyzed / get_stale_nodes
# ---------------------------------------------------------------------------

class TestChangeTracking:
    def test_mark_analyzed_sets_fields(self, kg, conn):
        kg.upsert_node("svc-a", NodeType.SERVICE)
        kg.mark_analyzed("svc-a", "claude-3-5-sonnet")
        nid = _node_id("svc-a", WS)
        raw = conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
        meta = NodeMetadata.from_json(raw)
        assert meta.analysis_model == "claude-3-5-sonnet"
        assert meta.last_analyzed_at != ""

    def test_get_stale_nodes_returns_unanalyzed(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE)
        stale = kg.get_stale_nodes(older_than_hours=24)
        assert "svc-a" in stale

    def test_recently_analyzed_not_stale(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE)
        kg.mark_analyzed("svc-a", "claude-3-5-sonnet")
        stale = kg.get_stale_nodes(older_than_hours=24)
        assert "svc-a" not in stale

    def test_old_analysis_is_stale(self, kg, conn):
        kg.upsert_node("svc-old", NodeType.SERVICE)
        # Manually set last_analyzed_at to 48 hours ago
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        nid = _node_id("svc-old", WS)
        old_meta = NodeMetadata(last_analyzed_at=old_ts, analysis_model="old-model")
        conn.execute(
            "UPDATE knowledge_graph_nodes SET metadata = ? WHERE id = ?",
            (old_meta.to_json(), nid),
        )
        conn.commit()
        stale = kg.get_stale_nodes(older_than_hours=24)
        assert "svc-old" in stale


# ---------------------------------------------------------------------------
# get_change_propagation_targets
# ---------------------------------------------------------------------------

class TestGetChangePropagationTargets:
    def test_includes_direct_dependents(self, kg):
        kg.upsert_edge("consumer", NodeType.SERVICE, "lib", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        targets = kg.get_change_propagation_targets("lib")
        assert "consumer" in targets

    def test_includes_same_domain_siblings(self, kg):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        kg.upsert_node("svc-b", NodeType.SERVICE, NodeMetadata(domain_tags=["payments"]))
        targets = kg.get_change_propagation_targets("svc-a")
        assert "svc-b" in targets
        assert "svc-a" not in targets  # self excluded

    def test_changed_node_excluded_from_result(self, kg):
        kg.upsert_edge("consumer", NodeType.SERVICE, "lib", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        targets = kg.get_change_propagation_targets("lib")
        assert "lib" not in targets

    def test_no_connections_returns_empty(self, kg):
        kg.upsert_node("isolated", NodeType.SERVICE)
        targets = kg.get_change_propagation_targets("isolated")
        assert targets == []


# ---------------------------------------------------------------------------
# bulk_upsert_nodes
# ---------------------------------------------------------------------------

class TestBulkUpsertNodes:
    def test_inserts_multiple_nodes(self, kg, conn):
        nodes = [
            ("svc-a", NodeType.SERVICE, NodeMetadata(summary="A")),
            ("svc-b", NodeType.SERVICE, NodeMetadata(summary="B")),
            ("repo-x", NodeType.REPO, None),
        ]
        count = kg.bulk_upsert_nodes(nodes)
        assert count == 3
        total = conn.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone()[0]
        assert total == 3

    def test_merges_metadata_on_existing_node(self, kg, conn):
        kg.upsert_node("svc-a", NodeType.SERVICE, NodeMetadata(summary="initial", complexity=4))
        kg.bulk_upsert_nodes([("svc-a", NodeType.SERVICE, NodeMetadata(summary="updated"))])
        nid = _node_id("svc-a", WS)
        raw = conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
        stored = NodeMetadata.from_json(raw)
        assert stored.summary == "updated"
        assert stored.complexity == 4  # kept from previous

    def test_invalid_node_type_raises(self, kg):
        with pytest.raises(ValueError, match="node_type"):
            kg.bulk_upsert_nodes([("svc-a", "invalid_type", None)])

    def test_returns_count_of_processed(self, kg):
        nodes = [(f"svc-{i}", NodeType.SERVICE, None) for i in range(5)]
        assert kg.bulk_upsert_nodes(nodes) == 5


# ---------------------------------------------------------------------------
# bulk_upsert_edges
# ---------------------------------------------------------------------------

class TestBulkUpsertEdges:
    def test_inserts_multiple_edges(self, kg, conn):
        edges = [
            ("svc-a", NodeType.SERVICE, "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON, None),
            ("svc-b", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.DEPENDS_ON, None),
            ("svc-a", NodeType.SERVICE, "svc-c", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH, None),
        ]
        count = kg.bulk_upsert_edges(edges)
        assert count == 3
        total = conn.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone()[0]
        assert total == 3

    def test_invalid_type_raises(self, kg):
        with pytest.raises(ValueError):
            kg.bulk_upsert_edges(
                [("svc-a", "bad", "svc-b", NodeType.SERVICE, EdgeType.DEPENDS_ON, None)]
            )

    def test_returns_count_of_processed(self, kg):
        edges = [
            (f"svc-{i}", NodeType.SERVICE, f"svc-{i+1}", NodeType.SERVICE, EdgeType.DEPENDS_ON, None)
            for i in range(4)
        ]
        assert kg.bulk_upsert_edges(edges) == 4


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestDirectedBfs:
    def test_follows_only_specified_predicate(self, conn, kg):
        kg.upsert_edge("a", NodeType.SERVICE, "b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("a", NodeType.SERVICE, "c", NodeType.SERVICE, EdgeType.COMMUNICATES_WITH)
        source_id = _node_id("a", WS)
        visited = _directed_bfs(conn, source_id, EdgeType.DEPENDS_ON, WS, max_depth=1)
        assert _node_id("b", WS) in visited
        assert _node_id("c", WS) not in visited

    def test_respects_max_depth(self, conn, kg):
        kg.upsert_edge("a", NodeType.SERVICE, "b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("b", NodeType.SERVICE, "c", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        source_id = _node_id("a", WS)
        visited = _directed_bfs(conn, source_id, EdgeType.DEPENDS_ON, WS, max_depth=1)
        assert _node_id("c", WS) not in visited

    def test_safe_against_cycles(self, conn, kg):
        kg.upsert_edge("x", NodeType.SERVICE, "y", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("y", NodeType.SERVICE, "x", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        source_id = _node_id("x", WS)
        visited = _directed_bfs(conn, source_id, EdgeType.DEPENDS_ON, WS, max_depth=5)
        # Should terminate without infinite loop; both nodes visited
        assert _node_id("x", WS) in visited
        assert _node_id("y", WS) in visited


class TestOutboundInboundHelpers:
    def test_outbound_targets(self, conn, kg):
        kg.upsert_edge("a", NodeType.SERVICE, "b", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("a", NodeType.SERVICE, "c", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        source_id = _node_id("a", WS)
        targets = _outbound_targets(conn, source_id, EdgeType.DEPENDS_ON, WS)
        assert targets == {_node_id("b", WS), _node_id("c", WS)}

    def test_inbound_sources(self, conn, kg):
        kg.upsert_edge("a", NodeType.SERVICE, "target", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        kg.upsert_edge("b", NodeType.SERVICE, "target", NodeType.SERVICE, EdgeType.DEPENDS_ON)
        target_id = _node_id("target", WS)
        sources = _inbound_sources(conn, target_id, EdgeType.DEPENDS_ON, WS)
        assert sources == {_node_id("a", WS), _node_id("b", WS)}

    def test_outbound_empty_when_no_edges(self, conn, kg):
        kg.upsert_node("lone", NodeType.SERVICE)
        lone_id = _node_id("lone", WS)
        assert _outbound_targets(conn, lone_id, EdgeType.DEPENDS_ON, WS) == set()

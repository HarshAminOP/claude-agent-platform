# ADR-019: Hub-Aware Graph Traversal

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Week 3 — Knowledge Graph Performance

## Context

The knowledge graph contains high-degree "hub" nodes — entities with hundreds or thousands of edges. Examples in our platform:

| Hub Node | Type | Degree | Why |
|----------|------|--------|-----|
| `terraform-modules` | repo | ~200 edges | Every service depends on shared modules |
| `eks-cluster-prod` | resource | ~150 edges | All workloads deploy to it |
| `platform-sre` | team | ~120 edges | Owns many services |
| `iam-role-base` | resource | ~80 edges | Inherited by most roles |
| `package.json` (common deps) | file | ~300 edges | Imported everywhere |

When a standard BFS traversal hits a hub node at depth 1, it expands all edges, producing an explosion of results that:
1. **Drowns signal in noise** — the user asked about Service A, gets 150 unrelated services that share the same cluster
2. **Blows latency budget** — expanding 200+ edges at depth 2 means 200 x avg_degree further lookups
3. **Exceeds result limits** — top-K truncation at the hub discards potentially relevant deeper paths

The previous approach (top-N truncation: take only the N highest-weight edges from any node) loses paths that go through low-weight hub edges to high-relevance leaf nodes.

**Key constraints:**
- Graph traversal must complete in <50ms for depth=2 queries
- Must not lose relevant results that route through hubs
- Must work with the existing SQLite adjacency table (no graph database migration)
- Hub threshold is dynamic (what's a hub today may not be tomorrow as the graph grows)

## Decision

**Use two-phase BFS for high-degree nodes instead of top-N truncation.**

### Algorithm

```python
# Hub detection threshold
HUB_DEGREE_THRESHOLD = 50  # nodes with > 50 edges are hubs

async def degree_aware_bfs(
    start_entity: str,
    max_depth: int = 2,
    top_k: int = 20,
) -> list[GraphResult]:
    """Two-phase BFS that handles hub nodes without explosion or truncation."""
    
    # Phase 1: Standard BFS, but PAUSE at hubs (don't expand them)
    visited = set()
    results = []
    paused_hubs = []  # (hub_node, depth, path_to_hub)
    queue = deque([(start_entity, 0, [])])
    
    while queue:
        node, depth, path = queue.popleft()
        if node in visited or depth > max_depth:
            continue
        visited.add(node)
        
        edges = get_edges(node)
        degree = len(edges)
        
        if degree > HUB_DEGREE_THRESHOLD and depth > 0:
            # Hub detected — pause, don't expand yet
            paused_hubs.append((node, depth, path))
            results.append(GraphResult(node, depth, path, is_hub=True))
            continue
        
        # Normal node — expand all edges
        for edge in edges:
            target = edge.target_id
            new_path = path + [(node, edge.predicate, target)]
            results.append(GraphResult(target, depth + 1, new_path))
            queue.append((target, depth + 1, new_path))
    
    # Phase 2: Selective hub expansion using query context
    for hub_node, hub_depth, hub_path in paused_hubs:
        if hub_depth >= max_depth:
            continue  # No budget to expand further
            
        # Get edges from hub, scored by relevance to the original query
        hub_edges = get_edges(hub_node)
        scored_edges = score_hub_edges(hub_edges, start_entity, visited)
        
        # Take top-K from hub (context-aware, not arbitrary)
        for edge in scored_edges[:HUB_EXPANSION_LIMIT]:
            target = edge.target_id
            if target not in visited:
                visited.add(target)
                new_path = hub_path + [(hub_node, edge.predicate, target)]
                results.append(GraphResult(target, hub_depth + 1, new_path))
    
    # Score all results and return top-K
    scored_results = score_results(results, start_entity)
    return scored_results[:top_k]
```

### Hub Edge Scoring

When expanding a hub in Phase 2, edges are scored by relevance rather than arbitrarily truncated:

```python
HUB_EXPANSION_LIMIT = 10  # max edges to follow from a hub in Phase 2

def score_hub_edges(
    edges: list[Edge],
    origin_entity: str,
    already_visited: set[str],
) -> list[ScoredEdge]:
    """Score hub edges by relevance to the traversal origin."""
    scored = []
    for edge in edges:
        if edge.target_id in already_visited:
            continue  # Skip already-seen nodes
        
        score = 0.0
        
        # 1. Edge weight (explicit importance)
        score += edge.weight * 0.3
        
        # 2. Predicate relevance (same predicate type as origin's edges)
        if edge.predicate in origin_predicates:
            score += 0.3
        
        # 3. Type affinity (same entity_type as origin)
        target_type = get_entity_type(edge.target_id)
        if target_type == origin_type:
            score += 0.2
        
        # 4. Recency (recently modified entities score higher)
        age_days = days_since_modified(edge.target_id)
        score += max(0, 0.2 * math.exp(-age_days / 30))
        
        scored.append(ScoredEdge(edge, score))
    
    return sorted(scored, key=lambda x: x.score, reverse=True)
```

### Dynamic Hub Threshold

The threshold adapts to graph growth:

```python
def compute_hub_threshold(conn: sqlite3.Connection) -> int:
    """Compute hub threshold as p95 of node degrees."""
    result = conn.execute("""
        SELECT degree FROM (
            SELECT source_id, COUNT(*) as degree
            FROM knowledge_graph_edges
            GROUP BY source_id
        ) ORDER BY degree
    """).fetchall()
    
    if not result:
        return HUB_DEGREE_THRESHOLD  # fallback
    
    p95_index = int(len(result) * 0.95)
    p95_degree = result[p95_index][0]
    
    # Minimum threshold of 30, maximum of 200
    return max(30, min(200, p95_degree))
```

### SQL Optimization

Hub detection is pre-computed and cached:

```sql
-- Materialized view (refreshed on sync)
CREATE TABLE IF NOT EXISTS graph_node_degrees (
    node_id TEXT PRIMARY KEY,
    out_degree INTEGER NOT NULL,
    in_degree INTEGER NOT NULL,
    total_degree INTEGER NOT NULL,
    is_hub INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_gnd_hub ON graph_node_degrees(is_hub) WHERE is_hub = 1;

-- Refresh after sync
INSERT OR REPLACE INTO graph_node_degrees (node_id, out_degree, in_degree, total_degree, is_hub)
SELECT
    n.uuid,
    COALESCE(out_d.cnt, 0),
    COALESCE(in_d.cnt, 0),
    COALESCE(out_d.cnt, 0) + COALESCE(in_d.cnt, 0),
    CASE WHEN COALESCE(out_d.cnt, 0) + COALESCE(in_d.cnt, 0) > :threshold THEN 1 ELSE 0 END
FROM knowledge_graph_nodes n
LEFT JOIN (SELECT source_id, COUNT(*) as cnt FROM knowledge_graph_edges GROUP BY source_id) out_d ON n.uuid = out_d.source_id
LEFT JOIN (SELECT target_id, COUNT(*) as cnt FROM knowledge_graph_edges GROUP BY target_id) in_d ON n.uuid = in_d.target_id;
```

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Top-N truncation (previous approach)** | Simple, predictable result count | Loses relevant paths through low-weight hub edges; arbitrary cutoff has no semantic meaning | Rejected |
| **Uniform BFS with global result limit** | Simple implementation | Hub explosion still happens internally (wastes computation), then truncates at the end — latency still blown | Rejected |
| **Weighted shortest path (Dijkstra)** | Optimal paths | Doesn't find all related entities, just closest ones; misses entities connected through different predicates | Rejected |
| **Pre-computed hub summaries** | Zero-latency hub handling | Summaries go stale; doesn't adapt to query context; storage overhead for all possible summaries | Rejected |
| **Graph partitioning (community detection)** | Reduces cross-partition traversal | Expensive to compute, must re-run on every graph change, adds significant complexity | Rejected |
| **Random walk with restart** | Probabilistic exploration avoids hubs naturally | Non-deterministic results (same query, different answers); harder to explain/debug | Rejected |
| **Bidirectional BFS** | Faster for point-to-point queries | Our use case is single-source exploration (find related), not point-to-point | Not applicable |

## Consequences

### Positive
- **No lost paths:** Phase 2 uses context-aware scoring instead of blind truncation — relevant results through hubs are found
- **Bounded latency:** Phase 1 pauses at hubs (O(1) per hub), Phase 2 expands at most `HUB_EXPANSION_LIMIT` edges per hub
- **Deterministic:** Same query + same graph state = same results (no randomness)
- **Adaptive:** Hub threshold adjusts as graph grows (p95 of degree distribution)
- **Observable:** Results marked with `is_hub=True` so consumers know when they hit a hub
- **Compatible:** Works with existing SQLite adjacency table — no schema migration required (only adds optional `graph_node_degrees` cache table)

### Negative
- **Two passes:** Slightly more complex implementation than single-pass BFS
- **Cache maintenance:** `graph_node_degrees` table must be refreshed after sync (adds ~100ms to sync, acceptable)
- **Tuning required:** `HUB_EXPANSION_LIMIT` and scoring weights need tuning based on real usage patterns
- **Phase 2 latency:** If many hubs are paused in Phase 1, Phase 2 may expand several hubs sequentially (mitigated by depth limit)

### Performance Characteristics

| Scenario | Phase 1 | Phase 2 | Total |
|----------|---------|---------|-------|
| No hubs in path | <10ms | 0ms (skipped) | <10ms |
| 1 hub at depth 1 | <5ms | <15ms | <20ms |
| 2 hubs at depth 1 | <5ms | <30ms | <35ms |
| Hub at depth 2 (max_depth) | <10ms | 0ms (no budget) | <10ms |

All within the 50ms budget for depth=2 traversals.

## Related ADRs

- [ADR-002: Graph Storage](ADR-002-graph-storage.md) — SQLite adjacency table that this algorithm operates on
- [ADR-017: Code Intelligence](ADR-017-code-intelligence.md) — Symbol nodes that create high-degree hubs (e.g., utility functions imported everywhere)
- [ADR-012: Unified Database](ADR-012-unified-database.md) — `graph_node_degrees` cache lives in cap.db

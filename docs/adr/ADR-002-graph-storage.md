# ADR-002: Use SQLite Adjacency Table + In-Memory Python Dict for Graph

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

The system must support graph traversal for:
- **Dependency tracing:** "What Terraform modules does this Lambda depend on?"
- **Blast radius analysis:** "If I change this VPC, what stacks are affected?"
- **Ownership mapping:** "Which services does this IAM role configure?"
- **Knowledge discovery:** Enriching search results with 1-hop neighbors

**Constraints:**
- ~12,000 edges (inferred relationships) expected at steady state
- Traversals must complete in <1ms for 5-hop BFS
- Local-only system; no external graph DB
- Scalability buffer: system should handle 50K+ edges before degradation
- Low operational overhead

## Decision

**Store relationships in SQLite adjacency table (for persistence). Load full graph into Python memory at startup as bidirectional dict. Use BFS traversal in Python. Fall back to on-demand SQLite queries if edge count exceeds 50,000.**

**Rationale:**
- At 12K edges, full graph fits in <2MB RAM (bidirectional adjacency dicts)
- Python BFS is <1ms for typical traversals
- SQLite as permanent store provides atomicity, crash recovery, and audit trail
- No external dependency (NetworkX, Kuzu, Neo4j) needed
- Double-buffering graph reload prevents stale results during ingestion
- Scale guard (50K edge fallback) prevents unbounded memory growth

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Kuzu graph DB** | Purpose-built graph queries, GDBMS features | Extra process, 30MB binary, Cypher learning curve, complexity for 12K edges | Rejected |
| **Neo4j** | Industry standard, powerful query language | Network service, licensing, heavy for laptop workload, 100MB+ binary | Rejected |
| **NetworkX (Python)** | Mature, flexible graph algorithms | Extra dependency, slower traversal than hand-written BFS, more memory overhead | Rejected |
| **Pure SQLite recursive CTE** | No extra code, single storage layer | 5-hop recursive query per traversal, <50ms latency (too slow), hard to debug | Rejected |
| **In-memory dict only (no SQLite)** | Fastest traversal, simplest code | No persistence, crash loses state, no atomic writes, manual sync logic | Rejected |
| **Redis graph** | Fast, supports graph operations | Network service, requires Redis instance, adds operational complexity | Rejected |

## Consequences

### Positive
- **Sub-millisecond traversal:** <1ms for 5-hop BFS at 12K edges
- **Zero dependencies:** Custom Python dict + standard BFS algorithm
- **Durable storage:** SQLite provides ACID guarantees, crash recovery
- **Simple operations:** No graph DB to monitor or upgrade
- **Atomic updates:** Per-repo ingestion with cascade deletes ensures consistency
- **Debuggable:** Plain SQL schema, no query language learning curve

### Negative
- **Memory coupling:** Graph must fit in RAM. At 50K+ edges, triggers fallback mode (slower SQLite queries)
- **Startup latency:** 100ms to load graph from SQLite on first startup
- **Stale traversal during reload:** ~100ms window where new edges not visible (mitigated by double-buffering)
- **No GDBMS features:** No built-in shortest-path, centrality algorithms, or graph analytics
- **Manual relationship inference:** Relationships must be explicitly computed during ingestion

## Fallback Behavior (≥50K edges)

If `edge_count > 50_000` at startup:
1. Set `_fallback_mode = True`
2. Keep SQLite connection open; disable in-memory graphs
3. On BFS traversal, query SQLite iteratively instead of memory dict
4. Latency increases to <50ms per traversal (still acceptable)
5. Log warning; operators should plan for migration to dedicated graph DB

```python
if count > self.MAX_EDGES:
    self._fallback_mode = True
    self._conn = conn
    logger.warning("Graph has %d edges (>%d). Using SQLite fallback.", count, self.MAX_EDGES)
```

## Schema

```sql
CREATE TABLE relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    evidence TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(source_id, target_id, relation_type)
);
```

## Related ADRs

- [ADR-001: Search Engine](ADR-001-search-engine.md) — Graph enriches search results (1-hop neighbors)
- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — Relationship inference during ingestion

## Implementation Notes

**Bidirectional adjacency storage:**
```python
self._outgoing: dict[str, list[tuple[str, str, float]]]  # entity -> [(neighbor, rel_type, weight)]
self._incoming: dict[str, list[tuple[str, str, float]]]  # entity <- [(neighbor, rel_type, weight)]
```

**Double-buffered reload (atomic under GIL):**
```python
new_graph = InMemoryGraphIndex()
new_graph.load_from_sqlite(conn)
# Atomic reference swap
self._outgoing = new_graph._outgoing
self._incoming = new_graph._incoming
```

**SLO:** BFS traversal <1ms for 5-hop paths; fallback mode <50ms on SQLite queries.

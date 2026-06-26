# ADR-006: No Application-Level Cache for v1

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

Search results are computed freshly on each query. Caching is typically used to reduce latency by serving repeated queries from memory instead of the database.

**Performance baseline (SQLite on local NVMe):**
- Point lookups: <1ms
- FTS5 searches (3K entities): <50ms
- Graph traversal (12K edges): <1ms

**Caching layers typically considered:**
1. Query result cache (LRU)
2. Graph cache (already in memory)
3. Embedding cache (for v2)
4. Entity cache (hot sets)

**Question:** Does application-level caching improve user-perceived latency?

## Decision

**No application-level cache for v1. Let SQLite be the cache. Embedding cache added only in v2 (when vectors are added).**

**Rationale:**
- **Marginal benefit:** Cache hit saves <2ms on <50ms query. 4% improvement.
- **High complexity:** Query invalidation, TTL bugs, staleness errors, multi-agent coherence
- **Operational burden:** Cache debugging, cache size tuning, eviction policies
- **Risk:** Stale cache returns wrong results; false cache hits cause silent bugs
- **Simple is better:** No cache = no cache invalidation bugs (one of hardest problems in CS)

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **LRU query cache (1000 entries)** | Hits on repeated queries, fast misses | Invalidation complexity, cache coherence across agents, what about partial results? | Rejected |
| **Redis** | Industry standard caching, TTL support | Network round-trip, extra process, overkill for <50ms baseline | Rejected |
| **Simple dict cache (no TTL)** | Fastest hits, no external dep | Never evicts; memory grows unbounded, stale results forever | Rejected |
| **SQLite built-in page cache** | Already present (32MB), simple | Can't control or monitor; relies on SQLite's default behavior | **Already active** |
| **No cache (this approach)** | Simple, no bugs, no overhead | Slower than cached, misses <2ms optimization | **Accepted** |

## Why SQLite is Already Caching

SQLite with WAL mode and page cache is already a multi-layer cache:

```
MCP Client Request
    │
    ▼
Python MCP Handler (no app cache)
    │
    ▼
SQLite Query Engine
    │
    ├─ Page Cache (32MB, LRU in SQLite)
    ├─ Query Plan Cache (recent queries)
    │
    ▼ (cache miss)
    
Disk I/O (if needed)
```

**SQLite caching benefits:**
- Automatic: no invalidation needed (SQLite handles writes)
- Transparent: doesn't add code or complexity
- Multi-level: page cache + query plan cache
- Safe: SQLite ensures cache coherence with ACID guarantees

## Consequences

### Positive (No Cache)
- **Eliminates cache invalidation bugs:** No "data is stale" issues
- **Simpler code:** ~200 lines saved (no cache LRU, eviction, TTL logic)
- **Guaranteed correctness:** Agents always see current data
- **Easier to debug:** Query not cached = no hidden state
- **Multi-agent coherence:** No need to sync caches across agents
- **Acceptable latency:** <50ms is fast enough for interactive queries

### Negative (No Cache)
- **Repeated queries slower:** Second query for same thing takes <50ms again (vs <2ms from cache)
- **Missed optimization:** 4% theoretical improvement not realized
- **Higher DB load:** SQLite query engine runs for every query (mitigated by page cache)

## When Caching is Worth Adding

Add application-level cache in v2 **only if:**
- Real usage shows repeated query patterns (agents asking same question 3+ times in session)
- P99 query latency exceeds 100ms (won't happen with FTS5 + SQLite cache)
- Database grows to >10K entities and SQLite page cache insufficient
- Agents report "search is slow, I'm waiting on every query"

**Expected never; FTS5 + SQLite page cache keeps queries <50ms even at 10K entities.**

## Caching Strategy for v2+ (Vectors Only)

When vector search is active (implemented via ADR-005), embedding caching becomes justified:

```python
# ONLY added in v2 when vectors ship
class EmbeddingCache:
    def get(content_hash: str, model_hash: str) -> ndarray | None:
        """Return cached embedding if model hasn't changed."""
        row = db.execute(
            "SELECT embedding FROM embedding_cache WHERE content_hash = ? AND model_hash = ?",
            (content_hash, model_hash)
        ).fetchone()
        return np.frombuffer(row[0]) if row else None
    
    def put(content_hash: str, model_hash: str, embedding: ndarray):
        """Cache embedding."""
        db.execute(
            "INSERT INTO embedding_cache (content_hash, model_hash, embedding) VALUES (?, ?, ?)",
            (content_hash, model_hash, embedding.tobytes())
        )
```

**Why v2 is different:**
- Embedding inference is CPU-bound (50ms per 1000 entities)
- Repeated searches reuse same entities (high cache hit rate expected)
- Invalidation is simple (model change = truncate cache)
- SQLite page cache won't help (embeddings are binary, not pages)

## Related ADRs

- [ADR-001: Search Engine](ADR-001-search-engine.md) — FTS5 search is fast baseline; vector search in parallel via ADR-005
- [ADR-002: Graph Storage](ADR-002-graph-storage.md) — In-memory graph is v1's only cache
- [ADR-005: Bedrock Embeddings](ADR-005-bedrock-embeddings.md) — Embedding cache spec for v2+

## Implementation Notes

**SQLite tuning for page caching:**
```python
conn.execute("PRAGMA cache_size=-32000")  # 32MB page cache
conn.execute("PRAGMA query_only=ON")      # Read-only conn uses cache efficiently
```

**SLO:** P50 search <50ms, P95 <200ms, P99 <1s. All achieved without app cache.

**Monitoring:** If P95 latency climbs above 200ms in production, consider adding cache then.

**Debugging:** No cache means cache misses are impossible; easier troubleshooting.

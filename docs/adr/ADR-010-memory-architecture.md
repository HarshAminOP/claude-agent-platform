# ADR-010: 3-Tier Memory with Scoring and Eviction

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP System Design v1)

## Context

Previous memory implementation used flat SQLite tables with no lifecycle management. This led to:

- Unbounded growth of session events and learnings over time
- No distinction between hot (frequently accessed) and cold (stale) entries
- No token budget enforcement — working memory could grow indefinitely, causing context window bloat
- No mechanism to surface the most relevant entries for the current task
- Stale corrections and outdated learnings persisting with equal weight to fresh data

**Key constraints:**
- Claude Code's context window is finite; injecting too much memory degrades quality
- Memory must survive across sessions (persistence requirement)
- Retrieval must be fast (<50ms for working memory, <200ms for active search)
- Disk usage must be bounded (no unbounded growth)
- Entries should age out naturally unless reinforced

## Decision

**Implement a 3-tier memory architecture: Working (15k tokens) / Active (SQLite + FTS5) / Archive (compressed), with a 4-weight composite scoring algorithm controlling promotion and eviction.**

### Tier Description

| Tier | Storage | Budget | Eviction Target | Access Pattern |
|------|---------|--------|-----------------|----------------|
| **Working** | In-process dict (per session) | 15,000 tokens hard cap | Active tier | Hot: loaded at session start, evicted on overflow |
| **Active** | SQLite `memory_active` table + FTS5 | Scored entries, no fixed cap | Archive tier | Warm: searchable, scored, decays over time |
| **Archive** | SQLite `memory_archive` table, zstd compressed | Disk budget (256MB/workspace, 1GB total) | Permanent deletion | Cold: read-only, promoted on access |

### Scoring Algorithm (4 weights)

```
composite_score = 0.25 * recency + 0.25 * importance + 0.35 * relevance + 0.15 * frequency
```

- **Recency (0.25):** Exponential decay with 30-day half-life from last access
- **Importance (0.25):** Static value set at creation (corrections=1.0, decisions=0.8, context=0.5)
- **Relevance (0.35):** FTS5 BM25 score normalized against current query context
- **Frequency (0.15):** Log-scaled access count relative to entry age

### Eviction Rules

- `composite_score < 0.15` — move to Archive
- No access for 90 days — mark stale (importance decays at 0.02/day)
- Stale + >365 days + <3 total accesses — permanent deletion
- Disk budget exceeded — aggressive archival of lowest-scored active entries

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Flat table with TTL** | Simple implementation | No scoring, no relevance-based retrieval, stale entries persist until TTL | Rejected |
| **LRU cache only** | Simple, bounded | Evicts recently unused but highly important entries (corrections) | Rejected |
| **2-tier (hot/cold)** | Simpler than 3-tier | No intermediate search layer; cold entries are effectively invisible | Rejected |
| **Vector-only retrieval** | Semantic matching | Requires embedding on every write, expensive, adds Bedrock dependency for core memory | Rejected |
| **Unbounded with manual cleanup** | No code needed | Grows to GB, search degrades, user must manage | Rejected (current state) |

## Consequences

### Positive
- **Memory stays fresh:** Stale entries decay and eventually get evicted without manual intervention
- **Disk bounded:** Hard caps at workspace (256MB) and global (1GB) level prevent runaway growth
- **Retrieval improves over time:** Frequently accessed, recently reinforced entries float to the top
- **Token budget enforced:** Working memory never exceeds 15k tokens, preventing context bloat
- **Cross-session consolidation:** Similar entries are merged at session end, reducing redundancy
- **Graceful degradation:** If all entries decay, system still works — just with less context

### Negative
- **Scoring tuning:** The 4 weights and thresholds require empirical tuning (initial values from design doc)
- **Consolidation complexity:** Merging similar entries requires FTS5-based similarity detection
- **Archive promotion cost:** Promoting an archived entry requires decompression and re-scoring
- **Token counting approximation:** Uses cl100k_base tokenizer as proxy for Claude's tokenizer (~10% accuracy)

## Implementation Notes

**Key files:**
- `src/cap/memory/scorer.py` — Composite score computation
- `src/cap/memory/manager.py` — Store, recall, search operations
- `src/cap/memory/eviction.py` — Background eviction daemon (runs every 10 minutes within MCP server)
- `src/cap/memory/consolidation.py` — End-of-session deduplication and merging

**Eviction triggers:**
1. On every memory write (check if over token budget)
2. Every 10 minutes (background sweep in MCP server event loop)
3. On session end (consolidation pass)

## Related ADRs

- [ADR-009: Enforcement Hooks](ADR-009-enforcement-hooks.md) — Enforcement history stored in same DB
- [ADR-012: Unified Database](ADR-012-unified-database.md) — All memory tables in cap.db
- [ADR-011: Adaptive Routing](ADR-011-adaptive-routing.md) — Router uses memory to inform decisions

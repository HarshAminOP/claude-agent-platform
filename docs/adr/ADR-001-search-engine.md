# ADR-001: Use SQLite FTS5 for Full-Text Search (with Hybrid Vector Search in v2)

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

The Knowledge & Retrieval System must enable agents to discover platform entities (Terraform modules, K8s resources, ArgoCD apps, decisions, incidents) quickly and reliably across 41+ repositories containing ~3,000 indexed entities.

**Key constraints:**
- Domain-specific platform engineering terminology is consistent across all repos (authored by homogeneous extractors and team)
- Single-team corpus with standardized naming conventions (e.g., "eks-cluster", "vpc-module", "alert-rule")
- Local-only system (no network exposure)
- Agents require sub-200ms query latency
- Installation must be lightweight (~15MB, <5 dependencies)

## Decision

**Use SQLite FTS5 with BM25 ranking and description enrichment for v1. Hybrid vector/embedding search added in parallel phase (see ADR-005).**

**Rationale:**
- BM25 ranking in FTS5 achieves 85%+ recall for domain-specific queries in consistent terminology
- Description enrichment (synonym injection: "eks" → "kubernetes k8s container orchestration") compensates for semantic gaps
- FTS5 comes built into Python — zero additional dependencies
- Measured recall gaps (collect during real usage) will justify vector search in v2
- Saves 150MB install size, 1500 lines of code, and 3 weeks of implementation time

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **ChromaDB (vector-only from day-1)** | Semantic search handles typos and synonyms automatically | 180MB install, requires embedding model + ONNX runtime. No measured demand yet. Over-engineering for single-team corpus. | Rejected |
| **LanceDB (vector DB)** | Lighter than ChromaDB (50MB), fast vector search | Still adds embedding pipeline, model versioning, cache invalidation. FTS5 already covers 85%+ of expected queries. | Rejected |
| **Elasticsearch / Opensearch** | Full-text search + analytics + clustering | Network service, separate process, complex operations. Overkill for <3K entities on single developer machine. | Rejected |
| **Pure SQLite LIKE queries** | Built-in, no FTS5 overhead | LIKE is case-sensitive, slower than FTS5 BM25, no ranking. Unacceptable UX. | Rejected |
| **FTS5 + vector search (hybrid from day-1)** | Handles both exact and semantic search | 150MB bloat, 1500 lines of code, unknown demand. Violates YAGNI principle. | Rejected |

## Consequences

### Positive
- **Fast deployment:** Saves 3 weeks, ships working MVP sooner
- **Zero extra deps:** FTS5 is built into Python stdlib via sqlite3
- **Lightweight:** 15MB install instead of 150MB+
- **Simple operations:** No embedding model versioning, cache invalidation, or cache warming
- **Practical for domain:** Consistent platform terminology means vocabulary mismatch is rare
- **Clear upgrade path:** Real usage data informs v2 vector search rollout

### Negative
- **No typo tolerance:** Users must use exact or prefix spelling (acceptable tradeoff: vector search (v2) will fix this if needed)
- **Semantic blindness:** Synonyms only discovered via description enrichment (manual, not automatic)
- **Dependency on enrichment:** FTS5 recall depends on curated synonym lists — maintenance burden
- **Can't discover "similar but different terms":** e.g., "queue" vs "SQS" won't match unless both in description

## Triggering Conditions for v2 (Vector Search)

Add vector search in v2 **only if** real usage reveals recall gaps:
- Agents report "I searched for X but couldn't find entity Y which contains that concept"
- Query logs show 10+ related queries that should have returned same result
- Synonym enrichment reaches complexity limit (>50 synonyms lists to maintain)

## Related ADRs

- [ADR-005: Bedrock Embeddings](ADR-005-bedrock-embeddings.md) — Vector search implementation via AWS Bedrock
- [ADR-006: No Caching](ADR-006-no-caching.md) — SQLite as the cache; no application-level caching
- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — How entities are indexed

## Implementation Notes

**FTS5 query optimization:**
- Tokenizer: `tokenize='porter unicode61'` (stemming + Unicode support)
- Virtual table triggers keep FTS5 in sync with entities table automatically
- Prefix matching on last term (e.g., "eks*") enables autocomplete-like UX
- Trust-level filtering applied at SQL level (not post-processing)

**Description enrichment pattern:**
```python
DOMAIN_SYNONYMS = {
    "eks": "kubernetes k8s container orchestration cluster",
    "iam": "identity access management permissions policy role",
    "vpc": "network virtual private cloud subnet routing",
}
# Add enriched description: f"{name} {original_desc} [{synonyms}]"
```

**SLO:** P50 search latency <50ms, P95 <200ms on 3K entities.

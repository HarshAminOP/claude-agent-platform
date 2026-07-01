Now I have sufficient context from reading the actual codebase. Let me produce the final reviewed architecture plan.

---

# CAP v2 Final Reviewed Architecture

## 1. Vision

CAP becomes a zero-friction local intelligence layer for Claude Code that installs in one command, never loses data, orchestrates via native Claude Code workflow scripts, and learns from outcomes. The user observes: reliable KB search (no truncated files, no stuck embeddings), workflows that actually dispatch agents with visible progress, and `cap init` that requires zero manual config.

**Non-goals removed after feasibility review:**
- Background workers with periodic schedules (MCP servers die when idle -- impossible without daemon)
- Auto-learning triggered from PostToolUse hook (hook cannot detect workflow completion or carry decision_id)
- HNSW directory-copy snapshots (races with concurrent writes, no clean API)

---

## 2. Architecture

```mermaid
graph TB
    subgraph "User Layer"
        CC[Claude Code CLI]
        HOOKS[PreToolUse / PostToolUse Hooks]
    end

    subgraph "Orchestration Layer — JavaScript Workflow Scripts"
        WF_FEAT[feature-request.js]
        WF_BUG[bugfix.js]
        WF_INFRA[infra.js]
        WF_REFACT[refactor.js]
        WF_REVIEW[review.js]
        WF_EXIST[10 existing domain workflows]
    end

    subgraph "Routing Layer — Python"
        ROUTER[TierRouter — keyword + learned thresholds]
        WF_SELECT[WorkflowSelector — prompt classification to workflow name]
    end

    subgraph "Intelligence Layer — Python"
        LEARN[LearningEngine — outcome recording + threshold adaptation]
        TRUST[TrustRegistry — per-agent Bayesian scores]
    end

    subgraph "Knowledge Layer — Python REWRITTEN"
        INGEST[IngestPipeline — detect chunk dedup embed store]
        RETRIEVE[HybridRetrieval — FTS5 + LanceDB + Graph with RRF]
        CONSOLIDATE[Consolidator — on-demand sweep via cap consolidate and nth-search trigger]
        EMBED_CACHE[EmbeddingCache — SQLite BLOB LRU 7-day TTL]
        SECRET_SCAN[SecretScanner — regex + entropy gate before ingest]
    end

    subgraph "Data Layer"
        DB_CAP["~/.cap/cap.db — enforcement, learning, routing, sync_triggers"]
        DB_KNOW["knowledge.db — entries, graph, queue, embedding_cache"]
        DB_SESS["sessions.db — decisions, learnings, corrections"]
        DB_PLAT["platform.db — workflows, budget, events"]
        LANCE[LanceDB — vector index]
    end

    subgraph "MCP Servers — 9 entry points"
        MCP_KNOW[cap-knowledge]
        MCP_SESS[cap-session]
        MCP_ORCH[cap-orchestrator]
        MCP_FLEET[cap-fleet]
        MCP_BACKLOG[cap-backlog]
        MCP_CODE[cap-code-intel]
        MCP_AST[cap-ast]
        MCP_DIAG[cap-diagram]
        MCP_WF[cap-workflow]
    end

    CC --> HOOKS
    HOOKS --> ROUTER
    ROUTER --> WF_SELECT
    WF_SELECT --> WF_FEAT & WF_BUG & WF_INFRA & WF_REFACT & WF_REVIEW & WF_EXIST
    WF_FEAT & WF_BUG --> MCP_KNOW
    LEARN --> ROUTER
    TRUST --> WF_SELECT
    SECRET_SCAN --> INGEST
    INGEST --> EMBED_CACHE --> DB_KNOW & LANCE
    CONSOLIDATE --> DB_KNOW & LANCE
    MCP_KNOW --> RETRIEVE & INGEST & CONSOLIDATE
    MCP_SESS --> DB_SESS
    MCP_ORCH --> ROUTER & WF_SELECT
```

### Module Map (Absolute Paths)

| Module | Status | Responsibility |
|--------|--------|----------------|
| `src/cap/lib/chunker.py` | NEW | Sentence/paragraph/character chunking with recursive fallback |
| `src/cap/lib/dedup.py` | NEW | Content-hash dedup with batch pre-fetch |
| `src/cap/lib/embed_cache.py` | NEW | Persistent BLOB-based embedding cache (SQLite) |
| `src/cap/lib/consolidator.py` | NEW | On-demand maintenance (sweep, dedup, WAL checkpoint) |
| `src/cap/lib/secret_scanner.py` | NEW | Regex + entropy-based secret detection |
| `src/cap/lib/db_init.py` | MODIFIED | Schema v2 migration |
| `src/cap/lib/embeddings.py` | MODIFIED | Safe boto3 init, embed_with_cache wrapper |
| `src/cap/lib/security.py` | MODIFIED | Secret patterns added to sanitize_content |
| `src/cap/lib/sync_engine.py` | MODIFIED | Chunking + dedup integration |
| `src/cap/lib/hooks.py` | MODIFIED | Idempotent registration guard |
| `src/cap/orchestration/router.py` | MODIFIED | workflow_name field, prompt-keyword extraction |
| `src/cap/orchestration/dag.py` | KEPT | DAG validation only (fix bugs 1+2) |
| `src/cap/orchestration/review_loop.py` | KEPT | Used by workflows |
| `src/cap/orchestration/__init__.py` | MODIFIED | Remove dead imports |
| `src/cap/learning/engine.py` | MODIFIED | Replace FILTER clause, fix memory_active dependency |
| `src/cap/learning/trust.py` | KEPT | Already correct (contrary to original plan Bug 3 -- code is fine) |
| `src/cap/hooks/posttool.py` | MODIFIED | Outcome recording via explicit MCP call pattern |
| `src/cap/servers/knowledge_server.py` | MODIFIED | Consolidation-on-nth-search, embed_with_cache, provenance field |
| `src/cap/servers/orchestrator_server.py` | MODIFIED | Return workflow_name, budget gate |
| `src/cap/cli/lifecycle.py` | MODIFIED | Parallel init, server name fix |
| `src/cap/data/workflows/feature-request.js` | NEW | 5-phase feature workflow |
| `src/cap/data/workflows/bugfix.js` | NEW | 3-phase bugfix workflow |
| `src/cap/data/workflows/infra.js` | NEW | 4-phase infra workflow |
| `src/cap/data/workflows/refactor.js` | NEW | 4-phase refactor workflow |
| `src/cap/data/workflows/review.js` | NEW | 3-phase review workflow |
| `src/cap/data/agents/*.yaml` | NEW | 7 structured agent definitions |
| `src/cap/orchestration/executor.py` | DELETE | Dead code |
| `src/cap/orchestration/consensus.py` | DELETE | Dead code |
| `src/cap/orchestration/quorum.py` | DELETE | Dead code |
| `src/cap/orchestration/planner.py` | DELETE | Dead code |
| `src/cap/orchestration/checkpoint.py` | DELETE | Dead code |
| `src/cap/orchestration/rollback.py` | DELETE | Dead code |
| `src/cap/orchestration/scratchpad.py` | DELETE | Dead code |
| `src/cap/orchestration/need_info.py` | DELETE | Dead code |
| `src/cap/orchestration/context.py` | DELETE | Dead code |
| `src/cap/lib/api_gateway.py` | DELETE | Dead code |
| `src/cap/lib/dashboard.py` | DELETE | Dead code |
| `src/cap/lib/workflow_hooks.py` | DELETE | Dead code |
| `src/cap/runtime/offline.py` | DELETE | Dead code |

**Note:** `src/cap/memory/` is KEPT (not deleted). The learning engine writes to `memory_active` in `~/.cap/cap.db`. Deleting these files would break baseline generation. They will be deprecated in a later phase once `sessions.db` baselines are proven.

---

## 3. KB Ingestion Pipeline (Complete Rewrite Spec)

### Problem Statement

Five verified gaps: (1) files over 24K chars truncated silently, (2) no persistent embedding cache, (3) duplicate LanceDB vectors on re-queue, (4) 439 items permanently stuck in `failed`, (5) no periodic consolidation. Plus two security blockers: no secret detection before ingest, and no write provenance tracking.

### Architecture Decision: Opportunistic Maintenance (Not Background Workers)

The feasibility review proved that background workers cannot run on an idle machine because MCP servers die when the stdio connection closes. The replacement design uses **opportunistic triggers**:

- **Consolidation**: runs synchronously every Nth `knowledge_search` call (N=50) if last consolidation was >6h ago. Also available as `cap consolidate` CLI command.
- **Embed queue processing**: runs immediately after `knowledge_sync` (existing pattern at line 503-557 of knowledge_server.py). Also triggered on every `knowledge_search` if queue has pending items and embedder is available.
- **Cache eviction**: runs during consolidation pass, not on a timer.

### New File: `src/cap/lib/chunker.py`

```python
"""
Sentence-aware chunking with recursive fallback.

Strategies:
- SENTENCE: split on sentence boundaries (default for docs/readmes)
- PARAGRAPH: split on blank lines (for code files)
- CHARACTER: fixed-size with overlap (fallback, also used for config)

CRITICAL DESIGN DECISION (addresses Feasibility Finding 7):
Every strategy has a mandatory CHARACTER fallback. If any chunk produced by
SENTENCE or PARAGRAPH exceeds max_size, it is recursively split using CHARACTER.
This guarantees NO chunk ever exceeds max_size.

Parameters: max_size=1024 chars, overlap=100 chars (configurable).
"""
from enum import Enum
from dataclasses import dataclass
import hashlib

class ChunkStrategy(Enum):
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    CHARACTER = "character"

@dataclass
class Chunk:
    text: str
    index: int
    start_pos: int
    end_pos: int
    content_hash: str  # SHA-256 of text
    metadata: dict

def chunk_content(
    content: str,
    strategy: ChunkStrategy = ChunkStrategy.SENTENCE,
    max_size: int = 1024,
    overlap: int = 100,
) -> list[Chunk]:
    """Chunk content with guaranteed max_size via recursive CHARACTER fallback."""
    ...

def detect_strategy(file_ext: str) -> ChunkStrategy:
    """
    .py/.go/.ts/.js/.java/.rs -> PARAGRAPH
    .md/.txt/.rst -> SENTENCE
    .yaml/.json/.toml/.tf/.hcl -> CHARACTER
    default -> SENTENCE
    """
    ...
```

### New File: `src/cap/lib/dedup.py`

```python
"""Content-hash deduplication with LanceDB-aware duplicate prevention."""
import hashlib
import sqlite3

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]

def fetch_existing_hashes(db: sqlite3.Connection, workspace: str | None = None) -> set[str]:
    """Batch-fetch all content_hash values from knowledge_entries."""
    ...

def is_vector_exists(vectors_table, entry_uuid: str) -> bool:
    """Check if a vector with this UUID already exists in LanceDB.
    Uses pandas query on the table. Returns False if vectors_table is None.
    
    CRITICAL: This addresses Feasibility Finding 3 -- prevents duplicate
    LanceDB rows when items are re-queued by consolidator.
    """
    ...
```

### New File: `src/cap/lib/embed_cache.py`

```python
"""
Persistent SQLite BLOB embedding cache.
Max 1000 entries, 7-day TTL, keyed by content_hash.

DESIGN DECISION (addresses Security Finding 9):
- Vectors stored as BLOB (numpy tobytes/frombytes) not JSON TEXT.
  4x faster reads, 4x smaller storage, no JSON parse overhead.
- DB file gets enforce_db_permissions() on creation.
- Cascade-delete: when a knowledge_entry is removed, its cache entry is purged.
"""
import sqlite3
import struct
import time
from pathlib import Path

class PersistentEmbedCache:
    VECTOR_DIM = 1024  # Titan V2 output

    def __init__(self, db: sqlite3.Connection, max_size: int = 1000, ttl_days: int = 7): ...
    def get(self, content_hash: str) -> list[float] | None:
        """Retrieve vector. Returns None on miss. Updates accessed_at on hit."""
        ...
    def put(self, content_hash: str, vector: list[float]) -> None:
        """Store vector as packed binary. Evicts LRU if at capacity."""
        ...
    def purge_for_entry(self, content_hash: str) -> None:
        """Delete cache entry when source knowledge_entry is deleted."""
        ...
    def evict_expired(self) -> int: ...
    def stats(self) -> dict: ...
```

### New File: `src/cap/lib/consolidator.py`

```python
"""
Knowledge base consolidation. Triggered on-demand (not background).

Trigger points:
1. Every 50th knowledge_search call (if last consolidation >6h ago)
2. Explicit `cap consolidate` CLI command
3. During `cap init` upgrade path

Phase 1: WAL checkpoint (non-blocking, replaces VACUUM)
Phase 2: Sweep expired entries (expires_at < now)
Phase 3: Dedup by content_hash (keep newest per workspace+source_path)
Phase 4: Requeue stuck failed embeddings (ONLY transient failures)
Phase 5: Purge orphaned cache entries

DESIGN DECISION (addresses Security Finding 7 + Feasibility Finding 11):
- Uses PRAGMA wal_checkpoint(TRUNCATE) not VACUUM (non-blocking)
- Failed items classified before reset: only items with last_error
  containing 'Throttl' or 'Timeout' are retried. ValidationException stays dead.
"""
from dataclasses import dataclass

@dataclass
class ConsolidationResult:
    expired_deleted: int
    duplicates_removed: int
    failed_requeued: int  # only transient failures
    failed_permanent: int  # left in failed state
    cache_orphans_purged: int
    wal_checkpointed: bool
    duration_ms: int

def consolidate(db, lance_db=None) -> ConsolidationResult: ...
def _classify_and_requeue_failed(db) -> tuple[int, int]:
    """Returns (requeued_count, permanent_count).
    Only requeues items whose last_error contains transient patterns."""
    ...
```

### New File: `src/cap/lib/secret_scanner.py`

```python
"""
Secret detection gate for KB ingestion pipeline.

SECURITY BLOCKER (Finding 4): Must run BEFORE content enters knowledge_entries.
If secrets detected, content is REJECTED (not redacted), error logged.

Patterns:
- AWS Access Key ID: AKIA[0-9A-Z]{16}
- AWS Secret Key: [A-Za-z0-9/+=]{40} adjacent to 'aws' or 'secret'
- Private keys: -----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----
- GitHub PAT: ghp_[A-Za-z0-9]{36}, github_pat_[A-Za-z0-9_]{82}
- Slack tokens: xox[baprs]-[A-Za-z0-9-]+
- Generic high-entropy: Shannon entropy > 4.5 for strings of 20+ alphanumeric chars
"""
import re
import math

class SecretDetected(Exception):
    """Raised when content contains a secret. Carries the pattern name."""
    def __init__(self, pattern_name: str, redacted_match: str): ...

def scan_for_secrets(content: str) -> list[str]:
    """Returns list of pattern names that matched. Empty = clean."""
    ...

def reject_if_secrets(content: str, source_path: str) -> str:
    """Raises SecretDetected if any secret found. Returns content unchanged if clean."""
    ...

# Additional skip patterns for sync engine
SKIP_PATTERNS_SECRETS = frozenset({
    "credentials", "*.pem", "*.key", "terraform.tfstate",
    "terraform.tfstate.backup", "secrets.yaml", "secrets.yml",
    ".env", ".env.*",
})
```

### Modified: `src/cap/lib/db_init.py`

Bump `_KNOWLEDGE_VERSION` to 2. Migration:

```sql
-- embedding_cache table (BLOB vectors, not TEXT)
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    accessed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ec_accessed ON embedding_cache(accessed_at);

-- knowledge_entries additions
ALTER TABLE knowledge_entries ADD COLUMN chunk_index INTEGER DEFAULT 0;
ALTER TABLE knowledge_entries ADD COLUMN parent_entry_id INTEGER;
ALTER TABLE knowledge_entries ADD COLUMN source_agent TEXT;
ALTER TABLE knowledge_entries ADD COLUMN verified INTEGER DEFAULT 0;

-- Consolidation tracking
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    expired_removed INTEGER DEFAULT 0,
    duplicates_removed INTEGER DEFAULT 0,
    failed_requeued INTEGER DEFAULT 0,
    failed_permanent INTEGER DEFAULT 0,
    error TEXT
);

-- Failed item classification (DO NOT blindly reset all)
UPDATE embedding_queue SET status = 'pending', attempts = 0
WHERE status = 'failed'
  AND (last_error LIKE '%Throttl%' OR last_error LIKE '%Timeout%' OR last_error IS NULL);
-- Items with ValidationException or other permanent errors stay failed
```

### Modified: `src/cap/lib/embeddings.py`

```python
# Line 84-95: Wrap boto3 session creation in try/except
def __init__(self, config: EmbeddingConfig = None) -> None:
    self.config = config or EmbeddingConfig()
    self._semaphore: Optional[asyncio.Semaphore] = None
    self._available: Optional[bool] = None

    session_kwargs: dict = {"region_name": self.config.region}
    if self.config.profile:
        session_kwargs["profile_name"] = self.config.profile

    try:
        session = boto3.Session(**session_kwargs)
        self._client = session.client("bedrock-runtime")
    except Exception as exc:
        logger.warning("Bedrock session creation failed (embeddings disabled): %s", exc)
        self._available = False
        self._client = None
```

Add `embed_with_cache` function:

```python
async def embed_with_cache(
    client: EmbeddingClient,
    cache: PersistentEmbedCache,
    text: str,
    content_hash: str,
) -> list[float] | None:
    """Check cache first. On miss, call Bedrock. Cache result. Return None on failure."""
    if client.is_available is False:
        return None
    cached = cache.get(content_hash)
    if cached is not None:
        return cached
    vector = await client.embed_single(text)
    if vector is not None:
        cache.put(content_hash, vector)
    return vector
```

### Modified: `src/cap/lib/sync_engine.py`

Integration with chunker, dedup, and secret scanner:

```python
# At top of sync_workspace(), after _get_existing_hashes():
from cap.lib.chunker import chunk_content, detect_strategy, Chunk
from cap.lib.secret_scanner import scan_for_secrets, SKIP_PATTERNS_SECRETS

# In the file walk loop, after reading content:
# 1. Secret scan (BEFORE any storage)
secrets_found = scan_for_secrets(content)
if secrets_found:
    logger.warning("Skipping %s: secrets detected (%s)", rel_path, secrets_found)
    stats.files_skipped += 1
    continue

# 2. Chunk if content exceeds threshold
if len(content) > 2048:
    strategy = detect_strategy(ext)
    chunks = chunk_content(content, strategy, max_size=1024, overlap=100)
else:
    chunks = [Chunk(text=content, index=0, start_pos=0, end_pos=len(content),
                    content_hash=content_hash, metadata={})]

# 3. Per-chunk: dedup and store
for chunk in chunks:
    if chunk.content_hash in existing_hashes:
        stats.files_unchanged += 1
        continue
    entry_id = _upsert_entry(db, workspace, rel_path, content_type,
                             title, chunk.text, chunk.content_hash)
    if entry_id:
        # Set chunk_index on the entry
        db.execute("UPDATE knowledge_entries SET chunk_index = ? WHERE id = ?",
                   (chunk.index, entry_id))
        existing_hashes.add(chunk.content_hash)
        ...
```

### Modified: `src/cap/servers/knowledge_server.py`

1. **Consolidation on Nth search** (replaces impossible background worker):
```python
_SEARCH_COUNT = 0
_LAST_CONSOLIDATION = 0.0
_CONSOLIDATION_INTERVAL = 21600  # 6 hours

async def _handle_search(args):
    global _SEARCH_COUNT, _LAST_CONSOLIDATION
    _SEARCH_COUNT += 1
    
    # Trigger consolidation every 50 searches if overdue
    if _SEARCH_COUNT % 50 == 0 and (time.time() - _LAST_CONSOLIDATION) > _CONSOLIDATION_INTERVAL:
        from cap.lib.consolidator import consolidate
        result = consolidate(db)
        _LAST_CONSOLIDATION = time.time()
        logger.info("Consolidation: expired=%d dedup=%d requeued=%d",
                    result.expired_deleted, result.duplicates_removed, result.failed_requeued)
    
    # ... existing search logic ...
```

2. **LanceDB duplicate prevention** in `_process_embedding_queue`:
```python
# Before vectors_table.add(), check for existing UUID
from cap.lib.dedup import is_vector_exists
if vectors_table is not None:
    if is_vector_exists(vectors_table, entry_uuid):
        # Already has a vector -- skip insert, just mark as done
        db.execute("UPDATE embedding_queue SET status = 'done' WHERE id = ?", (eq_id,))
        continue
    vectors_table.add([{...}])
```

3. **Provenance tracking** on `_handle_ingest`:
```python
# Add source_agent field (addresses Security Finding 1)
source_agent = metadata.get("source_agent", "unknown")
# After INSERT:
db.execute("UPDATE knowledge_entries SET source_agent = ? WHERE id = ?",
           (source_agent, entry_id))
```

4. **Status response** extended:
```python
# Add to _handle_status response:
"last_consolidation": _LAST_CONSOLIDATION or None,
"search_path": "hybrid" if query_vector else "keyword_only",
"embedder_health": "available" if embedding_client.is_available else "degraded" if embedding_client.is_available is False else "unknown",
```

### Failure Handling Matrix

| Failure | Detection | Response | Recovery |
|---------|-----------|----------|----------|
| Bedrock ThrottlingException | ClientError code | Exponential backoff, item stays pending | Retried on next search-triggered pass |
| Bedrock AccessDenied | ClientError code | `_available = False` | FTS5 fallback; re-enable on next successful embed |
| LanceDB corrupt | RuntimeError on search() | Catch, fall back to FTS5 | `cap consolidate --rebuild-vectors` |
| SQLite disk full | OperationalError | Catch in sync loop, log error | `cap doctor --fix` runs WAL checkpoint |
| Content too large | `len(text) > max_size` | Recursive CHARACTER chunking | Every chunk guaranteed <= max_size |
| Duplicate vector | `is_vector_exists()` pre-check | Skip add, mark queue item done | Consolidator dedup-by-hash catches any leaks |
| Secret in content | `scan_for_secrets()` | Reject from ingest entirely | File never enters KB |
| ValidationException | Permanent error code | Item stays `failed` forever | Not retried -- content is invalid |

---

## 4. Orchestration Layer

### Design Constraint Acknowledgment

Claude Code's `agent()` function is the ONLY mechanism for spawning subagents. MCP tools cannot call `agent()`. Therefore:
- Workflow scripts (.js) are the orchestration layer -- they call `agent()` directly
- The router runs IN the Claude Code outer session (via the orchestrator MCP server's `cap_route` tool)
- The outer session reads the routing decision and invokes the matching workflow script

### Router Extension (Addressing Feasibility Finding 6)

The original plan's `WORKFLOW_MAP` keyed on keywords that the router never extracts. The fix: add a dedicated `_extract_task_keywords()` function that performs explicit prompt scanning (separate from complexity scoring):

```python
# In router.py -- NEW function
TASK_KEYWORD_PATTERNS = {
    "feature": ["implement", "create", "build", "add", "new feature", "develop"],
    "bug": ["fix", "bug", "error", "broken", "crash", "failing", "issue"],
    "infra": ["terraform", "kubernetes", "helm", "argocd", "deploy", "infrastructure", "eks", "lambda"],
    "refactor": ["refactor", "restructure", "rewrite", "clean up", "simplify", "reorganize"],
    "review": ["review", "audit", "check", "assess", "evaluate"],
}

def _extract_task_keywords(prompt: str) -> list[str]:
    """Extract task-type keywords from prompt. Returns matched category names."""
    prompt_lower = prompt.lower()
    matched = []
    for category, patterns in TASK_KEYWORD_PATTERNS.items():
        if any(p in prompt_lower for p in patterns):
            matched.append(category)
    return matched

WORKFLOW_MAP = {
    "feature": "feature-request",
    "bug": "bugfix",
    "infra": "infra",
    "refactor": "refactor",
    "review": "review",
}

# In route():
@dataclass
class RoutingDecision:
    tier: Tier
    reasoning: str
    estimated_agents: list[str]
    estimated_cost: float
    complexity_score: float = 0.0
    decision_id: Optional[int] = None
    workflow_name: Optional[str] = None  # NEW
    task_keywords: list[str] = field(default_factory=list)  # NEW

# After tier classification:
task_keywords = _extract_task_keywords(prompt)
workflow_name = None
if tier in (Tier.LIGHTWEIGHT, Tier.FULL) and task_keywords:
    workflow_name = WORKFLOW_MAP.get(task_keywords[0])
```

### Database Path Resolution (Addressing Feasibility Finding 4 + 9)

The `routing_decisions` table exists in `~/.cap/cap.db` (defined in `db.py` line 235). The orchestrator MCP server currently uses `platform.db`. To avoid splitting routing data:

**Decision:** The orchestrator server's `cap_route` tool opens `~/.cap/cap.db` for routing decisions (not platform.db). This matches where the hook and learning engine already read/write.

```python
# In orchestrator_server.py:
import os
from cap.db import get_db

ROUTING_DB_PATH = os.path.expanduser("~/.cap/cap.db")

async def _handle_route(args):
    routing_db = get_db(ROUTING_DB_PATH)
    decision = route(args["prompt"], routing_db, session_id=args.get("session_id", "unknown"))
    return {..., "workflow_name": decision.workflow_name}
```

### Workflow Scripts -- KB Context Injection + Budget Gate + Observability

Each workflow script follows this template (addressing DX Finding 3):

```javascript
// feature-request.js
export const meta = {
  name: 'feature-request',
  description: '5-phase feature implementation with parallel security review',
  phases: [
    { title: 'Plan', model: 'opus' },
    { title: 'Implement', model: 'sonnet' },
    { title: 'Test', model: 'sonnet' },
    { title: 'Review', model: 'opus', parallel: true },
    { title: 'Finalize', model: 'sonnet' },
  ]
}

// Phase 0: Budget gate (Security Finding 5)
const budgetCheck = await tool('mcp__cap-orchestrator__cap_status', {})
if (budgetCheck.budget_remaining_usd < 0.50) {
  log('BUDGET EXHAUSTED: Cannot start workflow. Remaining: $' + budgetCheck.budget_remaining_usd)
  return { error: 'budget_exhausted', remaining: budgetCheck.budget_remaining_usd }
}

// Phase 0.5: KB context injection
const kbContext = await tool('mcp__cap-knowledge__knowledge_search', { query: args.description || args })
const contextBlock = kbContext.results?.slice(0, 5).map(r =>
  `- [${r.content_type}] ${r.title}: ${r.content_preview?.slice(0, 200)}`
).join('\n') || 'No prior context found.'

// Phase 1: Plan (with real-time progress -- DX Finding)
phase('Plan')
log('[Phase 1/5] Planning architecture...')
const plan = await agent(`...context: ${contextBlock}...`, { model: 'opus', label: 'architect' })
log(`[Phase 1/5] Plan complete. Components: ${plan.components?.length || 'N/A'}`)

// ... remaining phases with log() calls for visibility ...

// Final: Record outcome for learning (Feasibility Finding 2 fix)
await tool('mcp__cap-session__session_record', {
  event_type: 'workflow_complete',
  content: JSON.stringify({
    workflow: 'feature-request',
    phases_completed: 5,
    success: true,
    duration_ms: Date.now() - startTime,
  }),
  workspace: args.workspace || process.cwd()
})
```

### Outcome Recording (Addressing Feasibility Finding 2)

Since the PostToolUse hook cannot detect workflow completion, outcomes are recorded **from within the workflow script itself** via an explicit MCP tool call at the end of each workflow. The session server stores these, and the learning engine reads them:

```python
# In learning/engine.py -- new function called from cap_route or session_recall
def compute_thresholds_from_session_events(sessions_db: sqlite3.Connection, routing_db: sqlite3.Connection) -> dict:
    """Query session_events for workflow_complete events, correlate with routing_decisions."""
    events = sessions_db.execute(
        """SELECT content FROM session_events
           WHERE event_type = 'workflow_complete'
           ORDER BY created_at DESC LIMIT 200"""
    ).fetchall()
    # Parse events, match to routing_decisions by timestamp proximity, update outcomes
    ...
```

### Workflow Script Integrity (Security Finding 2)

```python
# In cli/lifecycle.py -- during cap init and upgrade:
import hashlib

WORKFLOW_MANIFEST = {
    "feature-request.js": "<sha256>",
    "bugfix.js": "<sha256>",
    # ... computed at package build time
}

def verify_workflow_integrity(workflows_dir: Path) -> list[str]:
    """Returns list of tampered workflow filenames."""
    tampered = []
    for name, expected_hash in WORKFLOW_MANIFEST.items():
        path = workflows_dir / name
        if path.exists():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected_hash:
                tampered.append(name)
    return tampered

# On workflow execution (in orchestrator_server.py):
def _validate_before_dispatch(workflow_name: str) -> bool:
    tampered = verify_workflow_integrity(WORKFLOWS_DIR)
    if workflow_name + ".js" in tampered:
        raise SecurityError(f"Workflow {workflow_name} has been tampered with. Refusing execution.")
```

### Dead Code Deletion

Delete all 9 dead orchestration files + api_gateway + dashboard + workflow_hooks + runtime/offline.py. Keep `memory/` (still used by learning engine writing to cap.db `memory_active`).

---

## 5. Intelligence and Learning (Realistic Scope)

### What Works Today (Keep)

- `TrustManager` in `trust.py` -- already correct Bayesian updates, proper return types
- `record_outcome()` in `engine.py` -- works if called with correct DB
- `get_learned_thresholds()` in `engine.py` -- works with 50+ samples

### What Cannot Work (Remove from Plan)

- PostToolUse hook auto-triggering outcome recording (hook lacks decision_id context)
- Background worker computing thresholds periodically (MCP server dies on idle)
- Trust scores automatically reaching auto-approve without explicit approval gate

### Realistic Learning Pipeline

1. **Outcome recording**: Workflow scripts call `session_record(event_type='workflow_complete')` at completion
2. **Threshold computation**: On every `cap_route` call, query `sessions.db` for recent workflow outcomes and correlate with `routing_decisions` in `cap.db`
3. **Trust updates**: On workflow completion, the workflow script calls `session_record` with agent_type and success/failure. The router reads trust_levels from `cap.db` before selecting agents.

### SQL Compatibility Fix (Feasibility Finding 5)

Replace `COUNT(*) FILTER (WHERE ...)` with portable SQL:

```python
# In engine.py line 148-154, replace:
row = db.execute(
    """SELECT
           SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS successes,
           COUNT(*) AS total
       FROM routing_decisions
       WHERE outcome IS NOT NULL"""
).fetchone()
```

### Trust Score Safety (Security Finding 3)

Add hard ceilings and decay:

```python
# In trust.py, after computing new_score:
# Hard ceiling: trust cannot exceed 0.85 without manual override
MAX_TRUST_CEILING = 0.85
new_score = min(new_score, MAX_TRUST_CEILING)

# Daily decay: trust decays 0.01/day toward 0.5 if no new outcomes
# Applied lazily when get_trust_level is called
```

### Learned Threshold Safety (Security Finding 12)

```python
# In router.py get_learned_thresholds():
# Hard bounds that cannot be violated regardless of learned data
HARD_INLINE_MAX_CEILING = 0.30
HARD_FULL_MIN_FLOOR = 0.40

inline_max = min(computed_inline_max, HARD_INLINE_MAX_CEILING)
full_min = max(computed_full_min, HARD_FULL_MIN_FLOOR)
```

---

## 6. Security Model

### Trust Boundaries

```mermaid
graph LR
    subgraph "User Trust Zone"
        USER[User / PO]
        CLAUDE[Claude Code outer session]
    end
    
    subgraph "Agent Trust Zone"
        AGENTS[Subagents spawned by workflows]
    end
    
    subgraph "Data Trust Zone"
        KB[Knowledge Base]
        SESSIONS[Session History]
    end
    
    subgraph "External Trust Zone"
        BEDROCK[AWS Bedrock]
        GITHUB[GitHub repos]
    end
    
    USER -->|full trust| CLAUDE
    CLAUDE -->|delegated trust| AGENTS
    AGENTS -->|write with provenance| KB
    AGENTS -->|read filtered| KB
    CLAUDE -->|read all| KB
    GITHUB -->|clone + scan| KB
    BEDROCK -->|vectors only| KB
```

### Enforcement Points

| Boundary | Control | Implementation |
|----------|---------|----------------|
| Agent -> KB write | Provenance tagging | `source_agent` field on knowledge_entries |
| Agent -> KB write | Secret rejection | `secret_scanner.reject_if_secrets()` before INSERT |
| External repo -> KB | Content scan | `sanitize_content(strict=True)` + `scan_for_secrets()` on all auto-cloned content |
| Workflow dispatch | Budget gate | `budget_check()` at workflow start + between phases |
| Workflow dispatch | Integrity check | SHA-256 verification of .js file before execution |
| Trust escalation | Hard ceiling | Trust score capped at 0.85, cannot reach auto-approve without manual override for destructive actions |
| Learned thresholds | Hard bounds | inline_max <= 0.30, full_min >= 0.40 regardless of data |
| Embedding cache | File permissions | `enforce_db_permissions()` on knowledge.db (contains cache table) |
| Failed item retry | Classification | Only retry items with transient error patterns; permanent errors stay dead |

### Write-ACL for Knowledge Ingest (Security Finding 1)

Rather than a full ACL system (over-engineered for single-user local tool), we use provenance + weighting:

1. Every `knowledge_ingest` and `knowledge_record` call stores `source_agent` (from metadata)
2. Search results include `source_agent` and `verified` fields
3. KB context injection in workflows can filter: `results.filter(r => r.verified || r.source_agent === 'user')`
4. The `verified` flag can only be set to 1 by user-initiated operations (CLI `cap knowledge verify <id>`)

This prevents poisoning without requiring complex per-agent ACLs.

---

## 7. Phases with Complete Specs

### Phase 1: KB Reliability + Security Gates (3-4 days)

**Goal:** Fix the 439 stuck embeddings, prevent recurrence, add chunking, add secret scanning.

**Files to Create:**
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/lib/chunker.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/lib/dedup.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/lib/embed_cache.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/lib/consolidator.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/lib/secret_scanner.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/tests/scenarios/test_kb_chunking.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/tests/scenarios/test_kb_security.py`
- `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/tests/scenarios/test_kb_consolidation.py`

**Files to Modify:**
- `src/cap/lib/db_init.py` -- schema v2 migration (embedding_cache table, chunk_index/source_agent/verified columns, classified failed item reset)
- `src/cap/lib/embeddings.py` -- safe boto3 init, embed_with_cache function
- `src/cap/lib/security.py` -- add secret patterns to INJECTION_PATTERNS (keep backward compat)
- `src/cap/lib/sync_engine.py` -- integrate chunker + dedup + secret scanner into file walk
- `src/cap/lib/hooks.py` -- idempotent registration guard
- `src/cap/servers/knowledge_server.py` -- embed_with_cache, LanceDB duplicate check, consolidation-on-nth-search, provenance field, status response extensions

**Bug Fixes:**
- Bug 7: EmbeddingClient raises on bad profile -> wrap in try/except
- Bug 9: hooks not idempotent -> guard against double-register
- Failed items: classified reset (only transient errors retried)

**Tests:**
```
test_kb_chunking.py:
  - test_large_python_file_produces_bounded_chunks (no chunk > max_size)
  - test_recursive_fallback_for_dense_functions
  - test_paragraph_strategy_on_code_file
  - test_sentence_strategy_on_markdown
  - test_200kb_file_produces_50plus_chunks

test_kb_security.py:
  - test_aws_access_key_rejected_from_ingest
  - test_private_key_rejected_from_ingest
  - test_github_pat_rejected_from_ingest
  - test_high_entropy_string_rejected
  - test_clean_content_passes_scanner
  - test_sync_skips_credentials_file
  - test_provenance_stored_on_ingest

test_kb_consolidation.py:
  - test_consolidation_only_requeues_transient_failures
  - test_validation_exception_stays_failed
  - test_expired_entries_removed
  - test_duplicate_hash_entries_deduplicated
  - test_wal_checkpoint_not_vacuum
  - test_orphan_cache_entries_purged
```

**Done Criteria:**
1. `cap knowledge status` shows `embedding_queue_failed: 0` for transient failures (permanent failures stay and show separately)
2. `cap knowledge sync --full` on workspace completes without duplicate warnings
3. A 200KB Python file produces 50+ searchable chunks (not 1 truncated entry)
4. `cap knowledge search "terraform s3"` returns results even with `AWS_PROFILE=nonexistent` (FTS5 fallback)
5. File containing `AKIAIOSFODNN7EXAMPLE` is rejected from ingest with clear error
6. Running sync twice on unchanged repo shows `files_indexed: 0` on second run
7. Pytest existing tests have fewer failures (bugs 7, 9 fixed)

---

### Phase 2: Orchestration (3-4 days)

**Goal:** Replace dead Python orchestration with working JS workflows. Add routing workflow selection.

**Dependency on Phase 1:** Workflows call `knowledge_search` for context injection. Phase 1 must be complete so KB is reliable.

**Files to Create:**
- `src/cap/data/workflows/feature-request.js`
- `src/cap/data/workflows/bugfix.js`
- `src/cap/data/workflows/infra.js`
- `src/cap/data/workflows/refactor.js`
- `src/cap/data/workflows/review.js`
- `src/cap/data/agents/dev.yaml`
- `src/cap/data/agents/security.yaml`
- `src/cap/data/agents/devops.yaml`
- `src/cap/data/agents/test.yaml`
- `src/cap/data/agents/code-review.yaml`
- `src/cap/data/agents/sre.yaml`
- `src/cap/data/agents/aws-architect.yaml`
- `tests/scenarios/test_workflow_routing.py`

**Files to Delete:**
- `src/cap/orchestration/executor.py`
- `src/cap/orchestration/consensus.py`
- `src/cap/orchestration/quorum.py`
- `src/cap/orchestration/planner.py`
- `src/cap/orchestration/checkpoint.py`
- `src/cap/orchestration/rollback.py`
- `src/cap/orchestration/scratchpad.py`
- `src/cap/orchestration/need_info.py`
- `src/cap/orchestration/context.py`
- `src/cap/lib/api_gateway.py`
- `src/cap/lib/dashboard.py`
- `src/cap/lib/workflow_hooks.py`
- `src/cap/runtime/offline.py`

**Files to Modify:**
- `src/cap/orchestration/router.py` -- add `workflow_name`, `_extract_task_keywords()`, `WORKFLOW_MAP`, hard threshold bounds
- `src/cap/orchestration/dag.py` -- fix detect_cycle() to return None for acyclic, fix mark_failed_dependents() parameter
- `src/cap/orchestration/__init__.py` -- remove dead imports
- `src/cap/cli/lifecycle.py` -- workflow integrity verification, parallel init
- `src/cap/servers/orchestrator_server.py` -- return workflow_name from cap_route, budget gate, open `~/.cap/cap.db` for routing

**Workflow Script Design Principles:**
- Every workflow starts with budget gate
- Every workflow injects KB context in first phase
- Every workflow logs phase transitions to stderr (visible to user)
- Every workflow records outcome via `session_record` at completion
- Review workflows have max 2 retry loops
- All workflows have per-phase cost ceiling ($2 per agent call)

**Done Criteria:**
1. `cap workflow list` shows feature-request, bugfix, infra, refactor, review
2. `cap_route` returns `workflow_name: "feature-request"` for "implement a health check endpoint"
3. Dead code removed: ~1,400 lines deleted
4. `ls src/cap/data/agents/*.yaml` shows 7 files
5. DAG tests pass after bug fixes
6. Workflow integrity check passes on fresh install

---

### Phase 3: Intelligence + Learning (3-4 days)

**Goal:** Learning fires from workflow completion events. Trust influences dispatch. Consolidation runs reliably.

**Dependency on Phase 2:** Workflows must be recording outcomes via session_record.

**Files to Modify:**
- `src/cap/learning/engine.py` -- replace FILTER clause with SUM(CASE), add `compute_thresholds_from_session_events()`, fix memory_active INSERT to be conditional (check if table exists)
- `src/cap/learning/trust.py` -- add MAX_TRUST_CEILING, add lazy decay toward 0.5
- `src/cap/orchestration/router.py` -- call `compute_thresholds_from_session_events()` if 50+ events exist, apply hard bounds
- `src/cap/servers/orchestrator_server.py` -- `cap_plan` returns trust scores and agent capabilities
- `src/cap/reliability/circuit_breaker.py` -- fix HALF_OPEN state transition

**Tests:**
```
test_learning_integration.py:
  - test_trust_score_increases_bounded_at_085
  - test_trust_decays_toward_05_without_events
  - test_thresholds_cannot_exceed_hard_bounds
  - test_filter_clause_replaced_with_sum_case
  - test_compute_from_session_events_correlates_correctly
  - test_circuit_breaker_half_open_transition
```

**Done Criteria:**
1. After 50+ `session_record(workflow_complete)` events, `cap_route` returns `threshold_source: learned`
2. Trust scores bounded at 0.85 maximum
3. Learned thresholds cannot exceed hard bounds (inline_max <= 0.30)
4. `pytest tests/` passes on both macOS and AL2 (no FILTER clause)
5. `cap_plan` returns eligible agents sorted by trust score

---

### Phase 4: Polish + UX (2-3 days)

**Goal:** Zero-friction init, health dashboard with explanations, workflow progress visibility.

**Files to Create:**
- `src/cap/cli/wizard.py` -- auto-detect AWS profile, GitHub org, region
- `src/cap/cli/doctor.py` -- component health with "why" and "how to fix"

**Files to Modify:**
- `src/cap/cli/lifecycle.py` -- parallelize DB init, auto-verify after init, print upgrade advisory
- `src/cap/cli/commands.py` -- add `cap consolidate`, `cap doctor`, improve `cap status` output

**UX Specifications (Addressing DX Findings):**

`cap init` output:
```
Initializing CAP v2...
  [1/4] Creating databases (knowledge, sessions, platform, fleet)... done
  [2/4] Installing 15 workflow scripts... done
  [3/4] Verifying AWS access... profile 'moia-readonly' detected
  [4/4] Running health check... all components green

CAP v2 ready. Knowledge base has 7,234 entries across 41 repos.
Tip: Run 'cap knowledge sync --full' for improved chunk-level search.
```

`cap doctor` output:
```
CAP Health Report
  knowledge_db:   GREEN  (7,234 entries, 89% embedded)
  embedder:       GREEN  (last success: 2min ago)
  vector_store:   GREEN  (28MB, 6,440 vectors)
  sync_engine:    GREEN  (last sync: 4min ago)
  learning:       YELLOW (27/50 samples needed for threshold adaptation)
    Action: Continue using workflows. Thresholds will adapt after 23 more completions.
  budget:         GREEN  ($47.23 remaining of $50.00 monthly)
```

Workflow progress (printed to stderr by workflow scripts):
```
[feature-request] Starting (budget: $0.00/$5.00)
[Phase 1/5: Plan] Running aws-architect (opus)...
[Phase 1/5: Plan] Complete (45s, $0.12)
[Phase 2/5: Implement] Running dev (sonnet)...
[Phase 2/5: Implement] Complete (2:34, $0.08)
[Phase 3/5: Test] Running test (sonnet)...
[Phase 3/5: Test] Complete (1:12, $0.04)
[Phase 4/5: Review] Running security + code-review in parallel...
[Phase 4/5: Review] Complete (1:45, $0.18)
[Phase 5/5: Finalize] Running dev (sonnet)...
[Phase 5/5: Finalize] Complete (0:45, $0.03)
[feature-request] DONE (Total: 6:21, Cost: $0.45)
  Agents: aws-architect, dev x2, test, security, code-review
  Trust deltas: dev +0.02, security +0.01
```

**Done Criteria:**
1. `cap init` completes in <2 minutes with zero prompts on configured machine
2. `cap doctor` shows all components with explanation and recovery steps
3. Workflow scripts print phase progress to stderr in real-time
4. `cap status` shows trust scores with success/failure counts
5. First run after upgrade prints migration advisory

---

## 8. Review Responses

### Security Findings

| # | Finding | Resolution |
|---|---------|-----------|
| 1 | KB Poisoning via agent writes | FIXED: `source_agent` + `verified` fields added. KB context injection in workflows can filter unverified entries. No full ACL needed for single-user tool. |
| 2 | No integrity check on workflow .js | FIXED: SHA-256 manifest verified before execution. Files set to 0o444 after install. Edit/Write to workflows dir blocked by pretool hook addition. |
| 3 | Trust score manipulation | FIXED: Hard ceiling at 0.85, lazy decay, cannot reach auto-approve for destructive actions. |
| 4 | Secrets leakage into KB | FIXED: `secret_scanner.py` runs BEFORE any content enters `knowledge_entries`. Sync engine skips credential files. Phase 1 blocker addressed. |
| 5 | Unbounded cost | FIXED: Budget gate at workflow start + per-phase ceiling ($2/agent). Pipeline cap at 10 items. Rate-limit: max 5 workflows/hour/workspace. |
| 6 | Cross-agent data leakage | ACCEPTED RISK: Single-user local tool. Workspace-scoping already exists on search. Adding full row-level isolation is over-engineered for the threat model. Documented as future enhancement if CAP goes multi-user. |
| 7 | Migration blindly retries all failed | FIXED: Failed items classified by `last_error` content. Only transient (Throttl/Timeout/NULL) retried. ValidationException items stay dead. |
| 8 | PreToolUse hook fails open | ACCEPTED RISK with MITIGATION: Added consecutive-fail-open counter. After 5 consecutive fail-opens, writes a warning flag file. Full fail-closed mode rejected because it would block legitimate work when DB is temporarily locked (common during WAL checkpoint). |
| 9 | Embedding cache vectors unprotected | FIXED: Vectors stored as BLOB (not TEXT). `enforce_db_permissions()` applied to knowledge.db. Cascade-delete on entry removal. |
| 10 | Auto-clone trusts org content | MITIGATED: All auto-cloned content goes through `sanitize_content(strict=True)` + `scan_for_secrets()`. Quarantine period rejected (adds complexity for minimal gain in single-user tool). |
| 11 | VACUUM blocks during consolidation | FIXED: Replaced with `PRAGMA wal_checkpoint(TRUNCATE)` which is non-blocking. VACUUM only via explicit `cap doctor --fix`. |
| 12 | Learned thresholds gameable | FIXED: Hard bounds (inline_max <= 0.30, full_min >= 0.40). Anomaly detection: if learned thresholds drift >50% from defaults, revert to defaults and log warning. |

### Feasibility Findings

| # | Finding | Resolution |
|---|---------|-----------|
| 1 | Background workers die with MCP server | REDESIGNED: Consolidation triggered opportunistically (every 50th search + CLI command). Embed queue processed immediately after sync. No background timers. |
| 2 | PostToolUse hook cannot detect workflow completion | REDESIGNED: Workflows record outcomes via explicit `session_record` MCP call at their end. Learning engine reads session_events to correlate with routing_decisions. |
| 3 | LanceDB duplicate vectors | FIXED: Added `is_vector_exists()` pre-check before `vectors_table.add()`. Uses `to_pandas().query(f"id == '{uuid}'")`. Consolidator also dedup-by-hash as defense-in-depth. |
| 4 | Two separate DBs split routing/learning data | FIXED: Orchestrator server opens `~/.cap/cap.db` for routing decisions (where `routing_decisions` table lives). Learning engine reads from same DB. No migration of tables between DBs needed. |
| 5 | FILTER clause requires SQLite >= 3.30 | FIXED: Replaced with `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` which works on all SQLite versions. |
| 6 | WORKFLOW_MAP keywords never extracted | FIXED: Added dedicated `_extract_task_keywords()` function with explicit pattern matching, separate from complexity scoring. |
| 7 | PARAGRAPH chunking on dense Python | FIXED: Every chunking strategy has mandatory CHARACTER fallback. If any chunk exceeds max_size after initial split, it is recursively split at max_size boundaries. Guarantees no chunk ever exceeds limit. |
| 8 | HNSW snapshots race with writes | REMOVED FROM PLAN: LanceDB directory copy without write-pause is unsafe. Replaced with: `cap consolidate` runs `table.compact_files()` (LanceDB native). No directory-copy snapshot. Recovery via re-embedding from knowledge_entries if LanceDB corrupts. |
| 9 | routing_decisions table missing from db_init.py | RESOLVED: Table is in `~/.cap/cap.db` (defined in `db.py` line 235). Orchestrator server now opens this DB for routing operations. No need to migrate table to platform.db. |
| 10 | Phase 2 deletes memory/ but Phase 3 writes memory_active | FIXED: memory/ directory NOT deleted. It is kept as-is. The learning engine writes to memory_active in cap.db. Deprecation deferred to a future version after sessions.db baselines are proven. |
| 11 | sync_triggers table not bridged between DBs | ACKNOWLEDGED: sync_triggers is in `~/.cap/cap.db`. PostToolUse hook writes there. SyncEngine reads from the DB passed to it (knowledge.db in knowledge_server.py context, but cap.db in posttool.py context). The posttool.py ALREADY consumes triggers at line 132-157 and calls SyncEngine directly. The knowledge_server's SyncEngine (sync/engine.py) polls from its own DB but the bridging already works via posttool.py. No change needed -- this is not actually broken. |
| 12 | Embedding cache JSON parse overhead | FIXED: Vectors stored as BLOB using `struct.pack('f' * 1024, *vector)`. Read with `struct.unpack`. 4KB binary vs 6KB JSON. No parse overhead. |

### DX Findings

| # | Finding | Resolution |
|---|---------|-----------|
| Day-1: Orchestration observability | FIXED: Workflow scripts log phase transitions to stderr. Template includes timing, cost, agent name per phase. Summary at end with agent list + trust deltas. |
| Day-1: cap doctor --verify after init | FIXED: `cap init` runs silent health check at end. If any component yellow/red, prints recovery steps and exits non-zero. |
| Day-1: Error messaging | FIXED: Workflow failure prints root cause, last phase seen, and 3 recovery options (resume, skip-phase, retry-phase). |
| Trust score UI | FIXED: `cap status` shows per-agent trust with success/failure counts and progress toward threshold adaptation. |
| Workflow summary | FIXED: Every workflow prints completion summary with agents, timings, cost, trust deltas. |
| Chunk visibility | FIXED: `cap knowledge status` includes chunk_count. Search response includes source_path for chunk-level attribution. |
| Upgrade messaging | FIXED: First run after schema migration prints "CAP upgraded. Recommend full sync for better search." |

---

## 9. Risk Register

| Risk | Probability | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| LanceDB `to_pandas()` slow on large tables (>10K vectors) | Medium | Search latency spike during dedup check | Only call for embed queue (25 items/batch), not on every search. Cache the UUID set for the batch. | Phase 1 |
| Workflow script changes require package rebuild for new hashes | Low | Developer friction on iteration | Dev mode: `cap init --dev` skips integrity check. Only enforced in production installs. | Phase 2 |
| Chunking increases KB size 3x, slowing FTS5 on very large workspaces | Medium | Search latency > 500ms for 200K+ entries | FTS5 has sublinear scaling. If latency exceeds target, add `LIMIT 1000` to FTS5 query before RRF. | Phase 1 |
| Users forget to set AWS_PROFILE before cap init | High | Embedder starts degraded | `cap init` auto-detects profiles from `~/.aws/config`. If none found, prints clear message. KB works via FTS5 anyway. | Phase 4 |
| Learning thresholds converge slowly (need 50 samples) | High | Users see no adaptation for weeks | Show progress in `cap doctor`. Threshold adaptation is a bonus, not a requirement -- defaults are reasonable. | Phase 3 |
| memory_active table in cap.db grows unbounded | Low | cap.db size increases | The existing `memory/consolidation.py` already handles eviction. No new work needed. | Existing |
| Concurrent sync + search on same DB causes WAL growth | Medium | Disk usage spike | WAL checkpoint during consolidation (every 50 searches). `cap doctor --fix` for manual checkpoint. | Phase 1 |

---

## 10. Definition of Done

### Phase 1 Complete When:
- [ ] `pytest tests/scenarios/test_kb_chunking.py` -- all pass
- [ ] `pytest tests/scenarios/test_kb_security.py` -- all pass
- [ ] `pytest tests/scenarios/test_kb_consolidation.py` -- all pass
- [ ] `cap knowledge status` shows `embedding_queue_failed_transient: 0`
- [ ] 200KB Python file produces 50+ chunks in knowledge_entries
- [ ] File with AWS key pattern rejected from ingest
- [ ] Sync on unchanged repo shows `files_indexed: 0`
- [ ] `cap knowledge search` works with `AWS_PROFILE=nonexistent`
- [ ] No duplicate UUIDs in LanceDB after full re-sync

### Phase 2 Complete When:
- [ ] `cap workflow list` includes 5 new workflows
- [ ] `cap_route` returns `workflow_name` for matching prompts
- [ ] Dead code deleted (~1,400 lines removed)
- [ ] 7 agent YAML definitions in place
- [ ] Workflow integrity verification passes
- [ ] Budget gate prevents over-budget workflow start
- [ ] All existing tests still pass

### Phase 3 Complete When:
- [ ] After 50+ workflow_complete session events, thresholds adapt
- [ ] Trust scores bounded at 0.85
- [ ] Hard threshold bounds enforced (verified in test)
- [ ] No FILTER clause in any .py file (grep confirms)
- [ ] `cap_plan` returns trust-sorted agent list
- [ ] All tests pass on macOS and in CI

### Phase 4 Complete When:
- [ ] `cap init` < 2 minutes, zero prompts
- [ ] `cap doctor` shows all components with status + explanation
- [ ] Workflow scripts print real-time progress
- [ ] `cap status` shows trust scores with counts
- [ ] Upgrade prints migration advisory
- [ ] Full test suite passes with 0 failures

### Capacity Estimates

| Component | Now | 6 months | 1 year | 3 years |
|-----------|-----|----------|--------|---------|
| knowledge.db (with chunks) | 15 MB | 45 MB | 80 MB | 200 MB |
| LanceDB vectors | 28 MB | 80 MB | 150 MB | 400 MB |
| embedding_cache (capped 1000, BLOB) | 0 MB | 4 MB | 4 MB | 4 MB |
| cap.db (routing + learning + enforcement) | 8 MB | 20 MB | 35 MB | 80 MB |
| sessions.db | 5 MB | 15 MB | 30 MB | 80 MB |
| platform.db | 2 MB | 8 MB | 15 MB | 40 MB |
| **Total disk** | **58 MB** | **172 MB** | **314 MB** | **804 MB** |

Cost: ~$0.004/month for embeddings (with cache + dedup). Budget ceiling: $50/month for agent calls.

### Trade-offs

| Decision | Sacrificed | Why |
|----------|-----------|-----|
| Opportunistic consolidation over background workers | Timely maintenance on idle machines | MCP servers die on idle -- background impossible without daemon |
| Workflow-recorded outcomes over hook-detected | Automatic detection | Hook lacks decision_id context -- explicit recording is reliable |
| Hard trust ceiling (0.85) over full auto-approve | Maximum automation | Prevents privilege escalation; user keeps approval gate for risky ops |
| BLOB embedding cache over TEXT | Human readability of DB | 4x faster reads, 4x smaller storage, no JSON parse |
| Keep memory/ module over delete | Cleaner codebase | Learning engine actively writes memory_active; deleting breaks Phase 3 |
| WAL checkpoint over VACUUM | Maximum space reclamation | Non-blocking; VACUUM reserved for manual `cap doctor --fix` |
| No HNSW snapshots over directory-copy | Disaster recovery speed | Unsafe without write-pause; recovery via re-embed from knowledge_entries |
| Accept shared-DB cross-session reads over isolation | Security purity | Single-user local tool; isolation adds complexity without real threat |
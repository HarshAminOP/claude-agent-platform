# CAP (Claude Agent Platform) — System Design Specification

## 1. Executive Summary

CAP is a platform layer that transforms Claude Code from a single-agent tool into an enforced multi-agent system with persistent memory, code intelligence, and self-learning capabilities. It operates entirely through MCP servers and Claude Code hooks — no CLI wrapper, no process interception — using PreToolUse hooks with exit code 2 to physically block anti-patterns like writing to 3+ files without delegation. The system maintains a SQLite-backed 3-tier memory that survives across sessions, extracts code structure via tree-sitter for intelligent navigation, and learns from its own routing decisions to improve over time. Distribution is via `uv tool install claude-agent-platform`, initialization via `cap init` which generates hook scripts and starts MCP servers, achieving usefulness within 10 seconds and full capability within 60 seconds. The architecture is portable across Claude Code surfaces (VS Code, CLI, Web) through a layered adapter design where only the enforcement and runtime layers change per surface.

## 2. Architecture Overview

### Data Flow: Prompt to Result

```
User Prompt
    │
    ▼
┌─────────────────────────────────────┐
│  Claude Code Runtime (VS Code/CLI)  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  PreToolUse Hook (Enforcement)│  │  ← exit(2) = HARD BLOCK
│  │  src/cap/hooks/pretool.py     │  │
│  └───────────┬───────────────────┘  │
│              │ allowed                │
│              ▼                        │
│  ┌───────────────────────────────┐  │
│  │  MCP Server (cap-orchestrator)│  │  ← tools exposed to Claude
│  │  src/cap/mcp/orchestrator.py  │  │
│  └───────────┬───────────────────┘  │
│              │                        │
│  ┌───────────────────────────────┐  │
│  │  MCP Server (cap-memory)      │  │
│  │  src/cap/mcp/memory.py        │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  MCP Server (cap-code-intel)  │  │
│  │  src/cap/mcp/code_intel.py    │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  PostToolUse Hook (Sync)      │  │
│  │  src/cap/hooks/posttool.py    │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  ~/.cap/cap.db (SQLite)             │
│  - memory (working/active/archive)  │
│  - code_index (AST + graphs)        │
│  - routing_decisions (self-learning)│
│  - enforcement_state (violations)   │
│  - sessions                         │
└─────────────────────────────────────┘
```

### Layer Interaction Model

```
Layer 4: State/Memory (SQLite, pure Python, no runtime deps)
    ▲
Layer 3: Enforcement Adapter (hook scripts, surface-specific)
    ▲
Layer 2: Runtime Adapter (MCP protocol, surface-specific config)
    ▲
Layer 1: Orchestration (pure Python, all business logic)
```

Each layer depends only on the layer below it. Layer 1 contains all orchestration logic and is completely portable. Layer 2 adapts MCP tool exposure per surface. Layer 3 generates the correct hook format per surface (VS Code settings.json vs CLI hooks). Layer 4 is the storage substrate shared by all layers.

### Process Model

CAP runs as 3 MCP server processes (long-lived, stdio transport):
1. `cap-orchestrator` — orchestration tools, routing, delegation
2. `cap-memory` — memory CRUD, search, eviction
3. `cap-code-intel` — AST queries, graph traversal, blast radius

Plus 2 hook scripts (invoked per-tool-call, short-lived):
1. `pretool.py` — enforcement checks (exit 2 to block)
2. `posttool.py` — sync triggers, state updates

## 3. Component Catalog

| Component | Type | Path | Purpose | Status | Interfaces |
|-----------|------|------|---------|--------|------------|
| Enforcement Engine | Hook | `src/cap/hooks/pretool.py` | Block multi-file edits without Agent(), track violations | New | stdin: tool call JSON, stdout: block reason, exit: 0/2 |
| Post-Tool Sync | Hook | `src/cap/hooks/posttool.py` | Trigger re-index after git ops, record tool outcomes | New | stdin: tool result JSON, exit: 0 |
| Orchestrator MCP | MCP Server | `src/cap/mcp/orchestrator.py` | Expose orchestration tools (route, delegate, checkpoint) | New | MCP stdio transport |
| Memory MCP | MCP Server | `src/cap/mcp/memory.py` | Expose memory tools (store, recall, search, evict) | New | MCP stdio transport |
| Code Intel MCP | MCP Server | `src/cap/mcp/code_intel.py` | Expose code tools (structure, dependents, trace, blast) | New | MCP stdio transport |
| Router | Library | `src/cap/orchestration/router.py` | 3-tier complexity classification | New | `route(task_desc) → Tier` |
| Context Thread | Library | `src/cap/orchestration/context.py` | Inter-agent context passing protocol | New | `Thread`, `ContextFrame` |
| Checkpoint Manager | Library | `src/cap/orchestration/checkpoint.py` | Save/resume orchestration state | New | `save()`, `resume()`, `list()` |
| Memory Store | Library | `src/cap/memory/store.py` | 3-tier memory with scoring | New | `store()`, `recall()`, `search()`, `evict()` |
| Memory Scorer | Library | `src/cap/memory/scorer.py` | Compute memory priority scores | New | `score(entry) → float` |
| Eviction Daemon | Library | `src/cap/memory/eviction.py` | Background eviction on triggers | New | `run_eviction()`, `check_budget()` |
| Token Counter | Library | `src/cap/memory/tokens.py` | tiktoken-based token counting | New | `count(text) → int`, `fits(text, budget) → bool` |
| Compressor | Library | `src/cap/memory/compressor.py` | Summarize entries for archive tier | New | `compress(entries) → summary` |
| AST Extractor | Library | `src/cap/code_intel/extractor.py` | tree-sitter parsing + symbol extraction | New | `extract(file_path) → Symbols` |
| Graph Builder | Library | `src/cap/code_intel/graph.py` | Build call/import/type graphs | New | `build()`, `query()`, `bfs()` |
| Sync Engine | Library | `src/cap/sync/engine.py` | Git fetch, staleness detection, incremental re-index | New | `sync()`, `check_stale()`, `fetch_all()` |
| Learning Engine | Library | `src/cap/learning/engine.py` | Record outcomes, generate corrections, adapt | New | `record()`, `suggest()`, `trust_level()` |
| Config Manager | Library | `src/cap/config.py` | Parse ~/.cap/config.toml, workspace config | New | `Config`, `load()`, `save()` |
| DB Manager | Library | `src/cap/db.py` | SQLite connection, migrations, WAL mode | New | `get_db()`, `migrate()` |
| Hook Generator | CLI | `src/cap/cli/init.py` | Generate .claude/ hook scripts for workspace | New | `cap init` command |
| Cost Tracker | Library | `src/cap/cost/tracker.py` | Track token usage, estimate cost, enforce budget | New | `track()`, `estimate()`, `budget_check()` |
| Offline Detector | Library | `src/cap/runtime/offline.py` | Detect network/budget state, switch modes | New | `get_mode()`, `is_offline()`, `should_skip_network_ops()` |
| Scratchpad | Library | `src/cap/orchestration/scratchpad.py` | Inter-agent artifact sharing (temp files + refs) | New | `write()`, `read()`, `list()`, `cleanup()` |
| Rollback Manager | Library | `src/cap/orchestration/rollback.py` | Track partial writes, revert on failure | New | `begin()`, `commit()`, `rollback()` |
| Passthrough Manager | Library | `src/cap/enforcement/passthrough.py` | Temporary enforcement bypass, logged + auto-expires | New | `enable()`, `check()`, `expire()` |

## 4. Enforcement Specification

### Hook Script: PreToolUse (`src/cap/hooks/pretool.py`)

Generated into `.claude/hooks/pretool.py` by `cap init`. Claude Code invokes this before every tool call.

```python
#!/usr/bin/env python3
"""
CAP Enforcement Hook — PreToolUse
Exit 0 = allow, Exit 2 = HARD BLOCK (tool call rejected by Claude Code)
"""
import json
import sys
import sqlite3
import time
import os

DB_PATH = os.path.expanduser("~/.cap/cap.db")
PASSTHROUGH_TTL = 300  # 5 minutes

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def check_passthrough(db) -> bool:
    """Check if passthrough mode is active and not expired."""
    row = db.execute(
        "SELECT expires_at FROM passthrough WHERE workspace = ? AND expires_at > ?",
        (os.getcwd(), time.time())
    ).fetchone()
    return row is not None

def get_session_file_edits(db, session_id: str) -> set:
    """Get distinct files edited in current session without Agent() delegation."""
    rows = db.execute(
        """SELECT DISTINCT file_path FROM enforcement_edits
           WHERE session_id = ? AND delegated = 0""",
        (session_id,)
    ).fetchall()
    return {r[0] for r in rows}

def record_violation(db, session_id: str, tool_name: str, file_path: str, reason: str):
    db.execute(
        """INSERT INTO enforcement_violations
           (session_id, timestamp, tool_name, file_path, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, time.time(), tool_name, file_path, reason)
    )
    db.commit()

def record_edit(db, session_id: str, file_path: str, delegated: bool):
    db.execute(
        """INSERT OR IGNORE INTO enforcement_edits
           (session_id, file_path, delegated, timestamp)
           VALUES (?, ?, ?, ?)""",
        (session_id, file_path, int(delegated), time.time())
    )
    db.commit()

def main():
    input_data = json.loads(sys.stdin.read())
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    db = get_db()

    # Passthrough check — if active, allow everything
    if check_passthrough(db):
        sys.exit(0)

    # Only enforce on file-writing tools
    WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
    if tool_name not in WRITE_TOOLS:
        sys.exit(0)

    # Extract file path from tool input
    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Check if this edit is happening inside an Agent() context
    # Agent context is tracked by the orchestrator MCP setting a flag
    agent_context = db.execute(
        "SELECT 1 FROM agent_contexts WHERE session_id = ? AND active = 1",
        (session_id,)
    ).fetchone()
    delegated = agent_context is not None

    # Record this edit
    record_edit(db, session_id, file_path, delegated)

    # If delegated, allow
    if delegated:
        sys.exit(0)

    # Check distinct file count for non-delegated edits
    edited_files = get_session_file_edits(db, session_id)
    edited_files.add(file_path)  # include current

    if len(edited_files) >= 3:
        reason = (
            f"BLOCKED: Editing {len(edited_files)} distinct files without Agent() delegation. "
            f"Files: {', '.join(sorted(edited_files)[:5])}. "
            f"Use Agent({{ subagent_type: 'orchestrator', ... }}) to delegate, "
            f"or run `cap passthrough` for temporary bypass."
        )
        record_violation(db, session_id, tool_name, file_path, reason)
        # Output block reason to stderr (shown to user)
        print(reason, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)

if __name__ == "__main__":
    main()
```

### Enforcement State Schema

```sql
CREATE TABLE enforcement_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    delegated INTEGER NOT NULL DEFAULT 0,
    timestamp REAL NOT NULL,
    UNIQUE(session_id, file_path, delegated)
);

CREATE TABLE enforcement_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    tool_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE agent_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    workspace TEXT
);

CREATE TABLE passthrough (
    workspace TEXT PRIMARY KEY,
    enabled_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    reason TEXT,
    enabled_by TEXT DEFAULT 'user'
);

CREATE INDEX idx_edits_session ON enforcement_edits(session_id);
CREATE INDEX idx_violations_session ON enforcement_violations(session_id);
CREATE INDEX idx_agent_ctx_session ON agent_contexts(session_id, active);
```

### Escape Hatch: `cap passthrough`

```python
# src/cap/enforcement/passthrough.py
"""
Temporary enforcement bypass.
- Default TTL: 300 seconds (5 minutes)
- Logged to enforcement_violations as type='passthrough_enabled'
- Auto-expires; cannot be renewed without explicit re-invocation
- Max 3 activations per hour (abuse prevention)
"""

def enable(workspace: str, ttl: int = 300, reason: str = "") -> dict:
    db = get_db()
    now = time.time()

    # Rate limit: max 3 per hour
    recent = db.execute(
        "SELECT COUNT(*) FROM passthrough_log WHERE workspace = ? AND timestamp > ?",
        (workspace, now - 3600)
    ).fetchone()[0]
    if recent >= 3:
        return {"error": "Rate limit: max 3 passthrough activations per hour"}

    db.execute(
        "INSERT OR REPLACE INTO passthrough (workspace, enabled_at, expires_at, reason) VALUES (?, ?, ?, ?)",
        (workspace, now, now + ttl, reason)
    )
    db.execute(
        "INSERT INTO passthrough_log (workspace, timestamp, ttl, reason) VALUES (?, ?, ?, ?)",
        (workspace, now, ttl, reason)
    )
    db.commit()
    return {"status": "enabled", "expires_in": ttl, "reason": reason}

def check(workspace: str) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT expires_at FROM passthrough WHERE workspace = ? AND expires_at > ?",
        (workspace, time.time())
    ).fetchone()
    return row is not None

def expire(workspace: str):
    db = get_db()
    db.execute("DELETE FROM passthrough WHERE workspace = ?", (workspace,))
    db.commit()
```

### Worktree Enforcement

The enforcement hook also validates worktree usage for parallel writes:

```python
# Additional check in pretool.py for parallel agent writes
def check_worktree_requirement(db, session_id: str, file_path: str) -> bool:
    """
    If multiple agents are active in the same session writing to the same repo,
    they MUST be in separate worktrees.
    """
    active_agents = db.execute(
        """SELECT agent_id, workspace FROM agent_contexts
           WHERE session_id = ? AND active = 1""",
        (session_id,)
    ).fetchall()

    if len(active_agents) <= 1:
        return True  # No conflict possible

    # Check if current file's repo has multiple active agents
    file_repo = get_git_root(file_path)
    agents_in_repo = [a for a in active_agents if get_git_root(a[1] or "") == file_repo]

    if len(agents_in_repo) > 1:
        # Check if they're in different worktrees
        workspaces = {a[1] for a in agents_in_repo}
        if len(workspaces) < len(agents_in_repo):
            return False  # Multiple agents, same workspace = violation

    return True
```

## 5. Memory Specification

### Three-Tier Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                    WORKING MEMORY (15k tokens)                   │
│  In-process dict. Current session context. Evicts to Active.    │
│  Token-counted. Hard cap enforced.                              │
├────────────────────────────────────────────────────────────────┤
│                    ACTIVE MEMORY (SQLite + FTS5)                 │
│  Scored entries. Full-text search. Semantic similarity.         │
│  Evicts to Archive when score < 0.15.                          │
├────────────────────────────────────────────────────────────────┤
│                    ARCHIVE MEMORY (compressed)                   │
│  Summarized clusters. zstd compressed. Read-only unless         │
│  promoted. Deleted after 365d + <3 accesses.                   │
└────────────────────────────────────────────────────────────────┘
```

### Schema

```sql
-- Active memory entries
CREATE TABLE memory_active (
    id TEXT PRIMARY KEY,  -- UUID
    workspace TEXT NOT NULL,
    category TEXT NOT NULL,  -- 'decision', 'correction', 'discovery', 'context', 'pattern'
    content TEXT NOT NULL,
    metadata TEXT,  -- JSON blob
    token_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER DEFAULT 1,
    importance REAL DEFAULT 0.5,  -- 0.0-1.0, set at creation
    relevance_score REAL DEFAULT 0.5,  -- updated on access patterns
    frequency_score REAL DEFAULT 0.0,  -- computed from access_count / age
    composite_score REAL DEFAULT 0.5,  -- the final weighted score
    stale_since REAL,  -- timestamp when marked stale (no reinforcement >90d)
    consolidated_into TEXT  -- points to merged entry if consolidated
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    content, category, workspace,
    content='memory_active',
    content_rowid='rowid'
);

-- Archive memory (compressed summaries)
CREATE TABLE memory_archive (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    summary TEXT NOT NULL,  -- compressed cluster summary
    source_ids TEXT NOT NULL,  -- JSON array of original active IDs
    compressed_content BLOB,  -- zstd compressed original content
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER DEFAULT 0
);

-- Working memory tracking (for token budget enforcement)
CREATE TABLE memory_working (
    session_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    loaded_at REAL NOT NULL,
    PRIMARY KEY (session_id, entry_id)
);

CREATE INDEX idx_active_workspace ON memory_active(workspace);
CREATE INDEX idx_active_score ON memory_active(composite_score);
CREATE INDEX idx_active_stale ON memory_active(stale_since);
CREATE INDEX idx_archive_workspace ON memory_archive(workspace);
```

### Scoring Algorithm (`src/cap/memory/scorer.py`)

```python
import time
import math

WEIGHTS = {
    "recency": 0.25,
    "importance": 0.25,
    "relevance": 0.35,
    "frequency": 0.15,
}

DECAY_HALF_LIFE_DAYS = 30  # recency halves every 30 days
CONFIDENCE_DECAY_RATE = 0.02  # per day without reinforcement

def compute_score(entry: dict, query_context: str = "") -> float:
    now = time.time()

    # Recency: exponential decay from last access
    days_since_access = (now - entry["last_accessed"]) / 86400
    recency = math.exp(-0.693 * days_since_access / DECAY_HALF_LIFE_DAYS)

    # Importance: static value set at creation (0.0 - 1.0)
    importance = entry["importance"]

    # Relevance: FTS5 BM25 score normalized to 0-1 if query provided,
    # else use stored relevance_score
    if query_context:
        relevance = compute_fts_relevance(entry["id"], query_context)
    else:
        relevance = entry["relevance_score"]

    # Frequency: log-scaled access count relative to age
    age_days = max((now - entry["created_at"]) / 86400, 1)
    frequency = min(math.log1p(entry["access_count"]) / math.log1p(age_days * 2), 1.0)

    # Confidence decay: reduce importance over time without reinforcement
    if entry.get("stale_since"):
        stale_days = (now - entry["stale_since"]) / 86400
        importance *= max(0.1, 1.0 - CONFIDENCE_DECAY_RATE * stale_days)

    composite = (
        WEIGHTS["recency"] * recency +
        WEIGHTS["importance"] * importance +
        WEIGHTS["relevance"] * relevance +
        WEIGHTS["frequency"] * frequency
    )

    return round(composite, 4)
```

### Token Counting (`src/cap/memory/tokens.py`)

```python
import tiktoken

# Use cl100k_base as approximation for Claude tokenization
# Actual Claude tokenizer is not public; this is within 10% accuracy
_encoder = tiktoken.get_encoding("cl100k_base")

WORKING_MEMORY_BUDGET = 15_000  # tokens

def count_tokens(text: str) -> int:
    return len(_encoder.encode(text))

def fits_budget(session_id: str, new_content: str, db) -> bool:
    """Check if adding new_content would exceed working memory budget."""
    current_total = db.execute(
        "SELECT COALESCE(SUM(token_count), 0) FROM memory_working WHERE session_id = ?",
        (session_id,)
    ).fetchone()[0]
    new_tokens = count_tokens(new_content)
    return (current_total + new_tokens) <= WORKING_MEMORY_BUDGET

def evict_lowest_score(session_id: str, needed_tokens: int, db):
    """Evict lowest-scored entries from working memory until budget fits."""
    entries = db.execute(
        """SELECT w.entry_id, w.token_count, a.composite_score
           FROM memory_working w
           JOIN memory_active a ON w.entry_id = a.id
           WHERE w.session_id = ?
           ORDER BY a.composite_score ASC""",
        (session_id,)
    ).fetchall()

    freed = 0
    for entry_id, tokens, _ in entries:
        if freed >= needed_tokens:
            break
        db.execute(
            "DELETE FROM memory_working WHERE session_id = ? AND entry_id = ?",
            (session_id, entry_id)
        )
        freed += tokens
    db.commit()
```

### Eviction Daemon (`src/cap/memory/eviction.py`)

Runs as a background task within the memory MCP server (not a separate process).

```python
"""
Eviction triggers:
1. On every memory write (check if over budget)
2. Every 10 minutes (background sweep)
3. On session end (consolidation pass)

Eviction rules:
- composite_score < 0.15 → move to Archive
- No access for 90 days → mark stale
- Stale + >365 days + <3 accesses → delete permanently
- Disk budget: 256MB per workspace, 1GB total
"""

import asyncio
import time

SCORE_THRESHOLD = 0.15
STALE_DAYS = 90
DELETE_DAYS = 365
DELETE_MIN_ACCESSES = 3
DISK_BUDGET_PER_WORKSPACE = 256 * 1024 * 1024  # 256MB
DISK_BUDGET_TOTAL = 1024 * 1024 * 1024  # 1GB

async def eviction_loop(db):
    while True:
        await run_eviction(db)
        await asyncio.sleep(600)  # 10 minutes

def run_eviction(db):
    now = time.time()

    # 1. Mark stale entries (no access in 90 days, not already stale)
    stale_cutoff = now - (STALE_DAYS * 86400)
    db.execute(
        """UPDATE memory_active SET stale_since = ?
           WHERE last_accessed < ? AND stale_since IS NULL""",
        (now, stale_cutoff)
    )

    # 2. Delete expired entries (stale > 365d AND < 3 accesses)
    delete_cutoff = now - (DELETE_DAYS * 86400)
    db.execute(
        """DELETE FROM memory_active
           WHERE stale_since IS NOT NULL
           AND stale_since < ?
           AND access_count < ?""",
        (delete_cutoff, DELETE_MIN_ACCESSES)
    )

    # 3. Archive low-score entries
    low_score_entries = db.execute(
        """SELECT id, workspace, content, metadata, category
           FROM memory_active
           WHERE composite_score < ? AND consolidated_into IS NULL
           ORDER BY composite_score ASC
           LIMIT 100""",
        (SCORE_THRESHOLD,)
    ).fetchall()

    if low_score_entries:
        archive_entries(db, low_score_entries)

    # 4. Disk budget enforcement
    enforce_disk_budget(db)

    db.commit()

def enforce_disk_budget(db):
    """If disk usage exceeds budget, aggressively archive lowest-scored entries."""
    import os
    db_path = os.path.expanduser("~/.cap/cap.db")
    db_size = os.path.getsize(db_path)

    if db_size > DISK_BUDGET_TOTAL:
        # Delete oldest archive entries first
        db.execute(
            """DELETE FROM memory_archive
               WHERE id IN (
                   SELECT id FROM memory_archive
                   ORDER BY last_accessed ASC
                   LIMIT 1000
               )"""
        )
        # Then aggressively archive active entries
        db.execute(
            """DELETE FROM memory_active
               WHERE composite_score < 0.3
               AND access_count < 5
               ORDER BY composite_score ASC
               LIMIT 500"""
        )
```

### Cross-Session Consolidation (`src/cap/memory/compressor.py`)

```python
"""
Runs at session end. Groups related entries, merges duplicates,
creates consolidated summaries.
"""

def consolidate(db, workspace: str):
    """
    Algorithm:
    1. Find entries with overlapping content (FTS5 similarity)
    2. Group by category + workspace
    3. For groups with >3 similar entries, merge into one consolidated entry
    4. Mark originals as consolidated_into → the merged entry
    5. New entry gets max(importance) of group, sum(access_count)
    """
    # Find clusters of similar entries
    entries = db.execute(
        """SELECT id, content, category, importance, access_count
           FROM memory_active
           WHERE workspace = ? AND consolidated_into IS NULL
           ORDER BY category, created_at""",
        (workspace,)
    ).fetchall()

    clusters = find_similar_clusters(entries, threshold=0.7)

    for cluster in clusters:
        if len(cluster) < 3:
            continue

        # Create merged entry
        merged_content = summarize_cluster([e["content"] for e in cluster])
        merged_importance = max(e["importance"] for e in cluster)
        merged_access_count = sum(e["access_count"] for e in cluster)

        merged_id = generate_uuid()
        db.execute(
            """INSERT INTO memory_active
               (id, workspace, category, content, token_count, created_at,
                last_accessed, access_count, importance, composite_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (merged_id, workspace, cluster[0]["category"], merged_content,
             count_tokens(merged_content), time.time(), time.time(),
             merged_access_count, merged_importance, merged_importance)
        )

        # Mark originals
        for entry in cluster:
            db.execute(
                "UPDATE memory_active SET consolidated_into = ? WHERE id = ?",
                (merged_id, entry["id"])
            )

    db.commit()

def find_similar_clusters(entries: list, threshold: float) -> list:
    """
    Simple approach: use FTS5 BM25 to find entries that match each other.
    Group entries where pairwise similarity > threshold.
    Uses union-find for clustering.
    """
    # Implementation uses FTS5 queries of each entry's key terms
    # against the corpus, grouping those with BM25 > threshold
    pass

def summarize_cluster(contents: list) -> str:
    """
    Deterministic summarization without LLM call:
    - Extract unique sentences/bullet points
    - Deduplicate
    - Sort by information density (longer unique segments first)
    - Truncate to 500 tokens
    """
    pass
```

## 6. Code Intelligence Specification

### Supported Languages (v1)

| Language | Parser | Queries |
|----------|--------|---------|
| Python | tree-sitter-python | functions, classes, imports, decorators |
| Go | tree-sitter-go | functions, structs, interfaces, imports |
| TypeScript | tree-sitter-typescript | functions, classes, interfaces, imports, types |
| Rust | tree-sitter-rust | functions, structs, traits, impls, use |
| HCL | EXCLUDED | N/A — parser quality insufficient for v1 |

### AST Extraction (`src/cap/code_intel/extractor.py`)

```python
"""
Extracts symbols, relationships, and structure from source files.
Uses tree-sitter for parsing, custom queries per language.
"""

from dataclasses import dataclass
from typing import Optional

@dataclass
class Symbol:
    name: str
    kind: str  # 'function', 'class', 'method', 'struct', 'interface', 'trait', 'type'
    file_path: str
    line_start: int
    line_end: int
    signature: str  # full signature text
    docstring: Optional[str]
    parent: Optional[str]  # enclosing class/module
    visibility: str  # 'public', 'private', 'internal'

@dataclass
class Relationship:
    source: str  # qualified name
    target: str  # qualified name
    kind: str  # 'calls', 'imports', 'extends', 'implements', 'uses_type', 'instantiates'
    file_path: str
    line: int

@dataclass
class FileIndex:
    path: str
    language: str
    hash: str  # content hash for incremental
    symbols: list[Symbol]
    relationships: list[Relationship]
    imports: list[str]
    exports: list[str]

def extract_file(file_path: str) -> FileIndex:
    """Parse a single file, return extracted symbols and relationships."""
    language = detect_language(file_path)
    if language not in SUPPORTED_LANGUAGES:
        return None

    parser = get_parser(language)
    tree = parser.parse(read_bytes(file_path))
    queries = get_queries(language)

    symbols = extract_symbols(tree, queries, file_path)
    relationships = extract_relationships(tree, queries, file_path)
    imports = extract_imports(tree, queries, file_path)
    exports = extract_exports(tree, queries, file_path)

    return FileIndex(
        path=file_path,
        language=language,
        hash=content_hash(file_path),
        symbols=symbols,
        relationships=relationships,
        imports=imports,
        exports=exports,
    )

def extract_incremental(workspace: str, changed_files: list[str]):
    """Re-extract only changed files, update graph."""
    db = get_db()
    for f in changed_files:
        old_hash = db.execute(
            "SELECT hash FROM code_files WHERE path = ?", (f,)
        ).fetchone()
        new_hash = content_hash(f)
        if old_hash and old_hash[0] == new_hash:
            continue
        index = extract_file(f)
        if index:
            store_file_index(db, index)
    db.commit()
```

### Graph Schema

```sql
CREATE TABLE code_files (
    path TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    language TEXT NOT NULL,
    hash TEXT NOT NULL,
    extracted_at REAL NOT NULL
);

CREATE TABLE code_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qualified_name TEXT NOT NULL,  -- 'module.Class.method'
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    parent TEXT,
    visibility TEXT DEFAULT 'public',
    FOREIGN KEY (file_path) REFERENCES code_files(path)
);

CREATE TABLE code_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,  -- qualified name
    target TEXT NOT NULL,  -- qualified name
    kind TEXT NOT NULL,    -- 'calls', 'imports', 'extends', etc.
    file_path TEXT NOT NULL,
    line INTEGER,
    FOREIGN KEY (file_path) REFERENCES code_files(path)
);

CREATE INDEX idx_symbols_name ON code_symbols(name);
CREATE INDEX idx_symbols_qualified ON code_symbols(qualified_name);
CREATE INDEX idx_symbols_file ON code_symbols(file_path);
CREATE INDEX idx_rel_source ON code_relationships(source);
CREATE INDEX idx_rel_target ON code_relationships(target);
CREATE INDEX idx_rel_kind ON code_relationships(kind);
```

### Degree-Aware BFS (`src/cap/code_intel/graph.py`)

```python
"""
Graph traversal that handles high-fanout nodes intelligently.
Problem: naive BFS with max_fanout=50 loses data on hub nodes.
Solution: degree-aware traversal that summarizes high-degree nodes.
"""

from collections import deque
from dataclasses import dataclass

@dataclass
class TraversalResult:
    nodes: list[str]           # all visited nodes
    edges: list[tuple]         # (source, target, kind)
    summarized_hubs: list[dict]  # nodes where fanout was summarized
    depth_reached: int
    truncated: bool

HIGH_DEGREE_THRESHOLD = 50
MAX_NODES = 500
MAX_DEPTH = 5

def degree_aware_bfs(
    start: str,
    direction: str = "outgoing",  # 'outgoing', 'incoming', 'both'
    max_depth: int = MAX_DEPTH,
    max_nodes: int = MAX_NODES,
    relationship_filter: list[str] = None,
    db=None,
) -> TraversalResult:
    """
    BFS that handles high-fanout nodes:
    1. If a node has degree > HIGH_DEGREE_THRESHOLD:
       - Don't expand all neighbors
       - Instead, sample top-N by relevance (most recently modified, most connected)
       - Record a summary: "Node X has 200 callers, showing top 10"
    2. Priority queue by depth (BFS) then by degree (prefer lower-degree paths)
    3. Stop at max_nodes or max_depth
    """
    visited = set()
    edges = []
    summarized_hubs = []
    queue = deque([(start, 0)])
    visited.add(start)

    while queue and len(visited) < max_nodes:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue

        neighbors = get_neighbors(db, node, direction, relationship_filter)
        degree = len(neighbors)

        if degree > HIGH_DEGREE_THRESHOLD:
            # High-fanout: sample instead of expanding all
            sampled = sample_neighbors(db, neighbors, limit=10)
            summarized_hubs.append({
                "node": node,
                "total_degree": degree,
                "sampled": len(sampled),
                "sample_strategy": "most_connected_and_recent",
            })
            neighbors = sampled

        for neighbor, rel_kind in neighbors:
            edges.append((node, neighbor, rel_kind))
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))

    return TraversalResult(
        nodes=list(visited),
        edges=edges,
        summarized_hubs=summarized_hubs,
        depth_reached=max_depth,
        truncated=len(visited) >= max_nodes,
    )

def sample_neighbors(db, neighbors: list, limit: int = 10) -> list:
    """
    Sample strategy for high-degree nodes:
    1. Sort by their own degree (prefer well-connected nodes — more informative)
    2. Break ties by recency of modification
    3. Take top N
    """
    scored = []
    for neighbor, kind in neighbors:
        degree = get_node_degree(db, neighbor)
        last_modified = get_last_modified(db, neighbor)
        scored.append((neighbor, kind, degree, last_modified))

    scored.sort(key=lambda x: (-x[2], -x[3]))
    return [(s[0], s[1]) for s in scored[:limit]]
```

### Query Tools (exposed via MCP)

```python
# src/cap/mcp/code_intel.py — MCP tool definitions

@tool
def code_structure(file_path: str) -> dict:
    """Return all symbols in a file with their hierarchy."""
    # Returns: functions, classes, methods, types with line numbers

@tool
def code_dependents(symbol: str, max_depth: int = 2) -> dict:
    """Find all code that depends on a symbol (incoming edges)."""
    return degree_aware_bfs(symbol, direction="incoming", max_depth=max_depth)

@tool
def code_trace(source: str, target: str) -> dict:
    """Find call paths between two symbols."""
    # BFS from source, stop when target reached
    # Returns all paths found (up to 5 shortest)

@tool
def blast_radius(file_path: str, changed_symbols: list[str] = None) -> dict:
    """
    Estimate impact of changing a file or specific symbols.
    Returns:
    - Direct dependents (1 hop)
    - Transitive dependents (2-3 hops)
    - Affected test files
    - Risk score (high if >20 dependents or crosses package boundary)
    """
    if not changed_symbols:
        # Get all exported symbols from file
        changed_symbols = get_exports(file_path)

    results = []
    for symbol in changed_symbols:
        traversal = degree_aware_bfs(symbol, direction="incoming", max_depth=3)
        results.append({
            "symbol": symbol,
            "direct_dependents": count_at_depth(traversal, 1),
            "transitive_dependents": len(traversal.nodes) - 1,
            "affected_tests": find_test_files(traversal.nodes),
            "crosses_package": check_package_boundary(traversal),
        })

    total_affected = sum(r["transitive_dependents"] for r in results)
    return {
        "file": file_path,
        "symbols_analyzed": len(results),
        "details": results,
        "total_affected_files": total_affected,
        "risk": "high" if total_affected > 20 else "medium" if total_affected > 5 else "low",
    }
```

## 7. Auto-Sync Specification

### Triggers

| Trigger | Action | Implementation |
|---------|--------|----------------|
| Session start | `git fetch --all` for workspace repos | `posttool.py` detects session_start event |
| Post `git pull` / `git merge` | Re-index changed files | `posttool.py` detects Bash tool with git pull/merge |
| Staleness timer (>5 min) | Background hash check | asyncio task in MCP server |
| File write (Edit/Write tool) | Re-extract single file | `posttool.py` on Edit/Write completion |
| Manual `cap sync` | Full re-index | CLI command |

### Staleness Detection (`src/cap/sync/engine.py`)

```python
"""
Auto-sync engine. Detects stale indexes and triggers re-extraction.
"""

import asyncio
import subprocess
import time
import os

STALENESS_TTL = 300  # 5 minutes
HASH_CHECK_INTERVAL = 300  # 5 minutes

class SyncEngine:
    def __init__(self, workspace: str, db):
        self.workspace = workspace
        self.db = db
        self.last_sync = 0

    async def background_loop(self):
        """Runs every 5 minutes, checks for changes."""
        while True:
            await asyncio.sleep(HASH_CHECK_INTERVAL)
            await self.check_and_sync()

    async def check_and_sync(self):
        """Incremental sync: only re-index actually changed files."""
        if not self.is_git_repo():
            return

        changed = self.get_changed_files()
        if changed:
            from ..code_intel.extractor import extract_incremental
            extract_incremental(self.workspace, changed)
            self.last_sync = time.time()

    def get_changed_files(self) -> list[str]:
        """Use git to find files changed since last sync."""
        last_indexed = self.db.execute(
            "SELECT MAX(extracted_at) FROM code_files WHERE workspace = ?",
            (self.workspace,)
        ).fetchone()[0] or 0

        # Method 1: git diff --name-only against last known state
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True,
                cwd=self.workspace, timeout=10
            )
            changed = [
                os.path.join(self.workspace, f.strip())
                for f in result.stdout.strip().split("\n")
                if f.strip()
            ]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            changed = []

        # Method 2: check file mtimes against last extracted_at
        indexed_files = self.db.execute(
            "SELECT path, extracted_at FROM code_files WHERE workspace = ?",
            (self.workspace,)
        ).fetchall()

        for path, extracted_at in indexed_files:
            try:
                if os.path.getmtime(path) > extracted_at:
                    if path not in changed:
                        changed.append(path)
            except OSError:
                pass  # file deleted

        return changed

    def on_git_fetch(self):
        """Called after git fetch --all. Checks if HEAD changed."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "ORIG_HEAD..HEAD"],
                capture_output=True, text=True,
                cwd=self.workspace, timeout=10
            )
            changed = [
                os.path.join(self.workspace, f.strip())
                for f in result.stdout.strip().split("\n")
                if f.strip()
            ]
            if changed:
                from ..code_intel.extractor import extract_incremental
                extract_incremental(self.workspace, changed)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass

    def session_start_sync(self):
        """Called at session start. Fetches and re-indexes."""
        # git fetch --all (non-blocking, best-effort)
        try:
            subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                capture_output=True, timeout=30,
                cwd=self.workspace
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Check for changes
        self.check_and_sync()

    def is_git_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.workspace, ".git"))
```

### PostToolUse Hook (`src/cap/hooks/posttool.py`)

```python
#!/usr/bin/env python3
"""
CAP Post-Tool Hook — triggers sync after relevant operations.
Always exits 0 (never blocks post-completion).
"""
import json
import sys
import os
import sqlite3
import time

DB_PATH = os.path.expanduser("~/.cap/cap.db")

def main():
    input_data = json.loads(sys.stdin.read())
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", {})

    db = sqlite3.connect(DB_PATH)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # Detect git operations that warrant re-index
        if any(cmd in command for cmd in ["git pull", "git merge", "git checkout", "git rebase"]):
            record_sync_trigger(db, "git_operation", command)

    elif tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")
        if file_path:
            record_file_change(db, file_path)

    db.close()
    sys.exit(0)

def record_sync_trigger(db, trigger_type: str, detail: str):
    db.execute(
        "INSERT INTO sync_triggers (timestamp, trigger_type, detail) VALUES (?, ?, ?)",
        (time.time(), trigger_type, detail)
    )
    db.commit()

def record_file_change(db, file_path: str):
    db.execute(
        "INSERT OR REPLACE INTO sync_pending (file_path, changed_at) VALUES (?, ?)",
        (file_path, time.time())
    )
    db.commit()

if __name__ == "__main__":
    main()
```

## 8. Self-Learning Specification

### Event-Sourced Architecture

All learning is based on immutable event records. Never mutate past events — only append new ones and derive state.

```sql
CREATE TABLE learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    workspace TEXT,
    event_type TEXT NOT NULL,  -- 'routing', 'outcome', 'correction', 'trust_change'
    payload TEXT NOT NULL,     -- JSON
    session_id TEXT
);

CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL,
    task_description TEXT NOT NULL,
    complexity_score REAL NOT NULL,
    tier_selected TEXT NOT NULL,  -- 'inline', 'lightweight', 'full'
    agents_used TEXT,  -- JSON array
    outcome TEXT,  -- 'success', 'failure', 'escalated', 'user_corrected'
    duration_ms INTEGER,
    token_cost INTEGER,
    user_satisfaction INTEGER  -- 1-5 if feedback given
);

CREATE TABLE correction_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,  -- what triggers this correction
    correction TEXT NOT NULL,  -- what to do differently
    occurrence_count INTEGER DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    auto_generated INTEGER DEFAULT 0,  -- 1 if generated from 3x threshold
    baseline_rule TEXT  -- generated CLAUDE.md rule if any
);

CREATE TABLE trust_levels (
    agent_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,  -- 0.0 (always ask) to 1.0 (always allow)
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_updated REAL NOT NULL,
    PRIMARY KEY (agent_type, action_type)
);

CREATE INDEX idx_learning_type ON learning_events(event_type);
CREATE INDEX idx_routing_outcome ON routing_decisions(outcome);
CREATE INDEX idx_corrections_pattern ON correction_patterns(pattern);
```

### Routing Decision Recording (`src/cap/learning/engine.py`)

```python
"""
Self-learning engine. Records decisions, detects patterns, adapts behavior.
"""

import time
import json

CORRECTION_THRESHOLD = 3  # same mistake 3x → auto-generate baseline

class LearningEngine:
    def __init__(self, db):
        self.db = db

    def record_routing(self, session_id: str, task: str, tier: str,
                       agents: list, complexity: float) -> int:
        """Record a routing decision. Returns decision_id for later outcome."""
        cursor = self.db.execute(
            """INSERT INTO routing_decisions
               (timestamp, session_id, task_description, complexity_score,
                tier_selected, agents_used)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), session_id, task, complexity, tier, json.dumps(agents))
        )
        self.db.commit()
        return cursor.lastrowid

    def record_outcome(self, decision_id: int, outcome: str,
                       duration_ms: int = None, token_cost: int = None):
        """Record the outcome of a routing decision."""
        self.db.execute(
            """UPDATE routing_decisions
               SET outcome = ?, duration_ms = ?, token_cost = ?
               WHERE id = ?""",
            (outcome, duration_ms, token_cost, decision_id)
        )
        self.db.commit()

        # Check if this outcome suggests the routing was wrong
        if outcome in ("failure", "escalated", "user_corrected"):
            self._check_correction_pattern(decision_id)

    def record_correction(self, what_was_wrong: str, what_is_correct: str):
        """Record a user correction. Checks for pattern threshold."""
        # Check if similar correction exists
        existing = self.db.execute(
            """SELECT id, occurrence_count FROM correction_patterns
               WHERE pattern LIKE ? LIMIT 1""",
            (f"%{what_was_wrong[:50]}%",)
        ).fetchone()

        if existing:
            new_count = existing[1] + 1
            self.db.execute(
                """UPDATE correction_patterns
                   SET occurrence_count = ?, last_seen = ?, correction = ?
                   WHERE id = ?""",
                (new_count, time.time(), what_is_correct, existing[0])
            )
            if new_count >= CORRECTION_THRESHOLD:
                self._generate_baseline(existing[0])
        else:
            self.db.execute(
                """INSERT INTO correction_patterns
                   (pattern, correction, first_seen, last_seen)
                   VALUES (?, ?, ?, ?)""",
                (what_was_wrong, what_is_correct, time.time(), time.time())
            )
        self.db.commit()

    def _generate_baseline(self, correction_id: int):
        """Auto-generate a baseline rule from repeated corrections."""
        row = self.db.execute(
            "SELECT pattern, correction FROM correction_patterns WHERE id = ?",
            (correction_id,)
        ).fetchone()
        if not row:
            return

        rule = f"LEARNED RULE: When encountering '{row[0]}', always '{row[1]}'"
        self.db.execute(
            "UPDATE correction_patterns SET auto_generated = 1, baseline_rule = ? WHERE id = ?",
            (rule, correction_id)
        )
        # Also store in active memory for retrieval
        from ..memory.store import store_memory
        store_memory(
            category="pattern",
            content=rule,
            importance=0.9,  # high importance — user-corrected behavior
            workspace=None,  # global
        )
        self.db.commit()

    def get_complexity_model(self) -> dict:
        """
        Adaptive complexity scoring based on historical outcomes.
        Returns learned thresholds for tier selection.
        """
        # Query successful routings per tier
        stats = {}
        for tier in ("inline", "lightweight", "full"):
            rows = self.db.execute(
                """SELECT AVG(complexity_score), COUNT(*)
                   FROM routing_decisions
                   WHERE tier_selected = ? AND outcome = 'success'""",
                (tier,)
            ).fetchone()
            stats[tier] = {"avg_complexity": rows[0] or 0.5, "count": rows[1] or 0}

        # Derive thresholds: boundary between tiers is midpoint of success averages
        inline_upper = stats["inline"]["avg_complexity"]
        lightweight_upper = stats["lightweight"]["avg_complexity"]

        # With enough data (>10 samples per tier), use learned thresholds
        if all(s["count"] > 10 for s in stats.values()):
            return {
                "inline_max": inline_upper + 0.1,
                "lightweight_max": lightweight_upper + 0.1,
                "source": "learned",
            }

        # Default thresholds
        return {
            "inline_max": 0.3,
            "lightweight_max": 0.6,
            "source": "default",
        }

    def update_trust(self, agent_type: str, action_type: str, success: bool):
        """Progressive trust: adjust trust score based on outcomes."""
        row = self.db.execute(
            "SELECT trust_score, success_count, failure_count FROM trust_levels WHERE agent_type = ? AND action_type = ?",
            (agent_type, action_type)
        ).fetchone()

        if row:
            score, successes, failures = row
            if success:
                successes += 1
            else:
                failures += 1
            # Bayesian update: trust = successes / (successes + failures + 2)
            # +2 is the prior (beta(1,1))
            new_score = (successes + 1) / (successes + failures + 2)
            self.db.execute(
                """UPDATE trust_levels
                   SET trust_score = ?, success_count = ?, failure_count = ?, last_updated = ?
                   WHERE agent_type = ? AND action_type = ?""",
                (new_score, successes, failures, time.time(), agent_type, action_type)
            )
        else:
            initial_score = 0.6 if success else 0.4
            self.db.execute(
                """INSERT INTO trust_levels
                   (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent_type, action_type, initial_score,
                 1 if success else 0, 0 if success else 1, time.time())
            )
        self.db.commit()
```

### Retrieval Feedback Loop

```python
def record_retrieval_feedback(self, query: str, results: list[str],
                              used_results: list[str]):
    """
    Track which search results were actually used.
    Over time, improves relevance scoring.
    """
    for result_id in results:
        was_used = result_id in used_results
        self.db.execute(
            """INSERT INTO learning_events
               (timestamp, event_type, payload)
               VALUES (?, 'retrieval_feedback', ?)""",
            (time.time(), json.dumps({
                "query": query,
                "result_id": result_id,
                "was_used": was_used,
            }))
        )

        # Boost relevance of used results
        if was_used:
            self.db.execute(
                """UPDATE memory_active
                   SET relevance_score = MIN(1.0, relevance_score + 0.05),
                       last_accessed = ?
                   WHERE id = ?""",
                (time.time(), result_id)
            )
        else:
            # Slight decay for results shown but not used
            self.db.execute(
                """UPDATE memory_active
                   SET relevance_score = MAX(0.0, relevance_score - 0.01)
                   WHERE id = ?""",
                (result_id,)
            )
    self.db.commit()
```

## 9. Orchestration Specification

### 3-Tier Routing (`src/cap/orchestration/router.py`)

```python
"""
Three-tier complexity routing:
- Inline: trivial, 1-line fixes, status checks (complexity < 0.3)
- Lightweight: single specialist + 1 review pass (0.3 <= complexity < 0.6)
- Full: orchestrator + multiple specialists + review loop (complexity >= 0.6)
"""

from dataclasses import dataclass
from enum import Enum

class Tier(Enum):
    INLINE = "inline"
    LIGHTWEIGHT = "lightweight"
    FULL = "full"

@dataclass
class RoutingDecision:
    tier: Tier
    complexity: float
    reasoning: str
    suggested_agents: list[str]
    estimated_tokens: int
    estimated_cost_usd: float

# Complexity signals and their weights
COMPLEXITY_SIGNALS = {
    "file_count": 0.20,       # how many files likely touched
    "cross_boundary": 0.20,   # crosses package/service boundaries
    "test_required": 0.10,    # needs test changes
    "infra_change": 0.15,     # touches infra (terraform, k8s, etc.)
    "security_sensitive": 0.15,  # auth, secrets, permissions
    "ambiguity": 0.10,        # how clearly specified is the task
    "historical": 0.10,       # past complexity for similar tasks
}

def route(task_description: str, workspace: str, db) -> RoutingDecision:
    """
    Classify task complexity and select routing tier.
    Uses both heuristic signals and learned thresholds.
    """
    signals = compute_signals(task_description, workspace, db)
    complexity = sum(
        signals[k] * COMPLEXITY_SIGNALS[k]
        for k in COMPLEXITY_SIGNALS
    )

    # Check learned thresholds
    from ..learning.engine import LearningEngine
    learned = LearningEngine(db).get_complexity_model()

    if complexity < learned["inline_max"]:
        tier = Tier.INLINE
        agents = []
    elif complexity < learned["lightweight_max"]:
        tier = Tier.LIGHTWEIGHT
        agents = suggest_single_specialist(task_description)
    else:
        tier = Tier.FULL
        agents = suggest_specialist_team(task_description)

    return RoutingDecision(
        tier=tier,
        complexity=complexity,
        reasoning=explain_signals(signals),
        suggested_agents=agents,
        estimated_tokens=estimate_tokens(tier, signals),
        estimated_cost_usd=estimate_cost(tier, signals),
    )

def compute_signals(task: str, workspace: str, db) -> dict:
    """Compute complexity signals from task description and context."""
    signals = {}

    # File count estimation (keyword heuristics + code intel)
    file_keywords = ["across", "all", "every", "multiple", "refactor"]
    signals["file_count"] = min(1.0, sum(0.2 for k in file_keywords if k in task.lower()))

    # Cross-boundary (mentions different services/packages)
    service_mentions = count_service_mentions(task, workspace, db)
    signals["cross_boundary"] = min(1.0, service_mentions * 0.3)

    # Test required
    signals["test_required"] = 0.8 if any(w in task.lower() for w in ["test", "coverage", "spec"]) else 0.2

    # Infra change
    infra_words = ["terraform", "kubernetes", "k8s", "deploy", "pipeline", "ci", "cd", "helm", "argocd"]
    signals["infra_change"] = 0.9 if any(w in task.lower() for w in infra_words) else 0.0

    # Security sensitive
    sec_words = ["auth", "permission", "iam", "secret", "credential", "rbac", "policy", "encrypt"]
    signals["security_sensitive"] = 0.9 if any(w in task.lower() for w in sec_words) else 0.0

    # Ambiguity (shorter = more ambiguous, questions = ambiguous)
    signals["ambiguity"] = max(0.0, 1.0 - len(task.split()) / 50)

    # Historical (lookup similar past tasks)
    signals["historical"] = lookup_historical_complexity(task, db)

    return signals
```

### Context Threading Protocol (`src/cap/orchestration/context.py`)

```python
"""
Context threading: how agents pass information to each other.
Each agent gets a ContextFrame with:
- task: what to do
- constraints: what NOT to do
- prior_outputs: what other agents produced (summarized)
- scratchpad_refs: paths to shared artifacts
"""

from dataclasses import dataclass, field
from typing import Optional
import json
import time

@dataclass
class ContextFrame:
    task_id: str
    agent_type: str
    task_description: str
    constraints: list[str] = field(default_factory=list)
    prior_outputs: list[dict] = field(default_factory=list)  # [{agent, summary, artifacts}]
    scratchpad_refs: list[str] = field(default_factory=list)
    parent_frame_id: Optional[str] = None
    max_tokens: int = 8000  # budget for this agent's output
    created_at: float = field(default_factory=time.time)

@dataclass
class ContextThread:
    """Full thread of a multi-agent orchestration."""
    orchestration_id: str
    task: str
    tier: str
    frames: list[ContextFrame] = field(default_factory=list)
    status: str = "running"  # 'running', 'completed', 'failed', 'checkpointed'
    checkpoint_data: Optional[dict] = None

    def add_frame(self, frame: ContextFrame):
        self.frames.append(frame)

    def get_summary_for_next_agent(self, max_tokens: int = 2000) -> list[dict]:
        """Summarize prior frames for the next agent's context."""
        summaries = []
        token_budget = max_tokens
        for frame in reversed(self.frames):
            if frame.agent_type == "orchestrator":
                continue
            summary = {
                "agent": frame.agent_type,
                "task": frame.task_description[:200],
                "output_refs": frame.scratchpad_refs,
            }
            # Estimate tokens (rough: 1 token ≈ 4 chars)
            est_tokens = len(json.dumps(summary)) // 4
            if est_tokens > token_budget:
                break
            summaries.append(summary)
            token_budget -= est_tokens
        return list(reversed(summaries))
```

### NEED_INFO Protocol

```python
"""
When an agent cannot proceed without additional information,
it returns a NEED_INFO signal instead of failing silently.
"""

@dataclass
class NeedInfo:
    agent_type: str
    question: str
    context: str  # why this info is needed
    blocking: bool = True  # True = cannot proceed, False = can proceed with assumption
    assumption: Optional[str] = None  # what agent will assume if not blocking
    options: list[str] = field(default_factory=list)  # suggested answers

def handle_need_info(need: NeedInfo, thread: ContextThread) -> str:
    """
    Resolution order:
    1. Check memory for the answer
    2. Check other agents' outputs in the thread
    3. If non-blocking, use the assumption and log it
    4. If blocking, escalate to user (PO)
    """
    # 1. Check memory
    from ..memory.store import search_memory
    results = search_memory(need.question, limit=3)
    if results and results[0]["score"] > 0.7:
        return results[0]["content"]

    # 2. Check thread outputs
    for frame in thread.frames:
        if need.question.lower() in json.dumps(frame.prior_outputs).lower():
            return extract_answer(frame.prior_outputs, need.question)

    # 3. Non-blocking assumption
    if not need.blocking and need.assumption:
        # Log the assumption for audit
        from ..learning.engine import LearningEngine
        LearningEngine(get_db()).record_routing(
            session_id=thread.orchestration_id,
            task=f"ASSUMPTION: {need.assumption}",
            tier="inline",
            agents=[need.agent_type],
            complexity=0.0,
        )
        return need.assumption

    # 4. Escalate to user
    raise EscalateToUser(
        question=need.question,
        context=need.context,
        options=need.options,
    )
```

### Checkpoint/Resume (`src/cap/orchestration/checkpoint.py`)

```python
"""
Checkpoint: serialize orchestration state to survive failures.
Resume: reconstruct from checkpoint and continue from last completed step.
"""

import json
import time
from typing import Optional

@dataclass
class Checkpoint:
    orchestration_id: str
    thread_state: dict  # serialized ContextThread
    completed_steps: list[str]  # agent_type:task_id pairs
    pending_steps: list[str]
    scratchpad_state: dict  # all artifacts produced so far
    failure_info: Optional[dict] = None
    created_at: float = field(default_factory=time.time)

def save_checkpoint(thread: ContextThread, completed: list, pending: list, db):
    """Save checkpoint to SQLite. Called after each agent completes."""
    checkpoint = Checkpoint(
        orchestration_id=thread.orchestration_id,
        thread_state=serialize_thread(thread),
        completed_steps=completed,
        pending_steps=pending,
        scratchpad_state=get_scratchpad_state(thread.orchestration_id),
    )
    db.execute(
        """INSERT OR REPLACE INTO checkpoints
           (orchestration_id, data, created_at)
           VALUES (?, ?, ?)""",
        (checkpoint.orchestration_id, json.dumps(asdict(checkpoint)), time.time())
    )
    db.commit()

def resume_from_checkpoint(orchestration_id: str, db) -> tuple:
    """
    Resume a failed/interrupted orchestration.
    Returns (thread, remaining_steps) to continue from.
    """
    row = db.execute(
        "SELECT data FROM checkpoints WHERE orchestration_id = ?",
        (orchestration_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"No checkpoint found for {orchestration_id}")

    checkpoint = json.loads(row[0])
    thread = deserialize_thread(checkpoint["thread_state"])
    remaining = checkpoint["pending_steps"]

    # Restore scratchpad artifacts
    restore_scratchpad(checkpoint["scratchpad_state"])

    return thread, remaining

# Schema
"""
CREATE TABLE checkpoints (
    orchestration_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""
```

### Failure Handling

```python
"""
Failure cascade:
1. Agent fails → retry once with same model
2. Second failure → upgrade model (sonnet → opus)
3. Third failure → check failure quorum
4. Quorum (2/3 agents agree task is blocked) → escalate to PO
5. Non-quorum → report partial results + what failed
"""

MAX_RETRIES = 2
REVIEW_LOOP_MAX = 3

async def execute_with_failure_handling(
    agent_type: str, task: str, thread: ContextThread, db
) -> dict:
    attempts = 0
    model = "sonnet"  # start with faster/cheaper model

    while attempts < MAX_RETRIES:
        try:
            result = await run_agent(agent_type, task, thread, model=model)
            if result.get("status") == "success":
                return result
            elif result.get("status") == "need_info":
                answer = handle_need_info(result["need_info"], thread)
                task = f"{task}\n\nAdditional info: {answer}"
                continue
        except AgentFailure as e:
            attempts += 1
            if attempts == 1:
                model = "opus"  # upgrade model on second attempt
            elif attempts == 2:
                break

    # All retries exhausted
    return {
        "status": "failed",
        "agent": agent_type,
        "attempts": attempts,
        "last_error": str(e) if 'e' in dir() else "unknown",
    }

async def check_failure_quorum(failures: list[dict], total_agents: int) -> bool:
    """
    If 2/3 or more agents report the same blocking reason,
    the task is genuinely blocked → escalate.
    """
    if len(failures) < 2:
        return False
    # Extract blocking reasons
    reasons = [f.get("last_error", "") for f in failures]
    # Simple majority check
    return len(failures) / total_agents >= 2/3
```

### Security Veto

```python
"""
Security agent has special veto power. If it flags a change as insecure,
the orchestrator MUST stop and report to PO regardless of other agents' opinions.
"""

def check_security_veto(agent_outputs: list[dict]) -> Optional[dict]:
    for output in agent_outputs:
        if output.get("agent") == "security" and output.get("veto"):
            return {
                "blocked": True,
                "reason": output["veto_reason"],
                "severity": output.get("severity", "high"),
                "recommendation": output.get("recommendation"),
            }
    return None
```

### Inter-Agent Scratchpad (`src/cap/orchestration/scratchpad.py`)

```python
"""
Shared artifact storage for multi-agent workflows.
Agents can write files, other agents can read them.
Cleaned up after orchestration completes (or on rollback).
"""

import os
import tempfile
import shutil

SCRATCHPAD_ROOT = os.path.expanduser("~/.cap/scratchpad")

class Scratchpad:
    def __init__(self, orchestration_id: str):
        self.root = os.path.join(SCRATCHPAD_ROOT, orchestration_id)
        os.makedirs(self.root, exist_ok=True)

    def write(self, name: str, content: str) -> str:
        """Write an artifact. Returns absolute path."""
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def read(self, name: str) -> str:
        """Read an artifact by name."""
        path = os.path.join(self.root, name)
        with open(path, "r") as f:
            return f.read()

    def list(self) -> list[str]:
        """List all artifacts."""
        results = []
        for root, _, files in os.walk(self.root):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.root)
                results.append(rel)
        return results

    def cleanup(self):
        """Remove all artifacts for this orchestration."""
        if os.path.exists(self.root):
            shutil.rmtree(self.root)
```

### Rollback Manager (`src/cap/orchestration/rollback.py`)

```python
"""
Tracks file changes made by agents. On failure, reverts all partial writes.
Uses git stash or direct file backup depending on git availability.
"""

import os
import shutil
import subprocess

class RollbackManager:
    def __init__(self, workspace: str, orchestration_id: str):
        self.workspace = workspace
        self.orchestration_id = orchestration_id
        self.backup_dir = os.path.expanduser(f"~/.cap/rollback/{orchestration_id}")
        self.tracked_files = []  # (path, backup_path_or_None_if_new)
        os.makedirs(self.backup_dir, exist_ok=True)

    def begin(self):
        """Start tracking. If git repo, record HEAD."""
        self.start_ref = None
        if self._is_git():
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=self.workspace
            )
            self.start_ref = result.stdout.strip()

    def track_file(self, file_path: str):
        """Backup a file before it's modified."""
        if os.path.exists(file_path):
            backup = os.path.join(self.backup_dir, os.path.basename(file_path))
            shutil.copy2(file_path, backup)
            self.tracked_files.append((file_path, backup))
        else:
            self.tracked_files.append((file_path, None))  # new file

    def commit(self):
        """Orchestration succeeded. Clean up backups."""
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)

    def rollback(self):
        """Orchestration failed. Revert all changes."""
        for file_path, backup_path in reversed(self.tracked_files):
            if backup_path:
                # Restore original
                shutil.copy2(backup_path, file_path)
            else:
                # Remove newly created file
                if os.path.exists(file_path):
                    os.remove(file_path)

        # If git, also reset any staged changes
        if self._is_git() and self.start_ref:
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=self.workspace, capture_output=True
            )

        # Clean up backup dir
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)

    def _is_git(self) -> bool:
        return os.path.isdir(os.path.join(self.workspace, ".git"))
```

### 12 Specialist Agent Types

| Agent | Domain | Typical Tasks |
|-------|--------|---------------|
| dev | General development | Code changes, refactoring, feature implementation |
| devops | CI/CD, pipelines | GitHub Actions, ArgoCD, deployment configs |
| security | Security review | IAM, RBAC, secrets, vulnerability assessment |
| sre | Reliability | Monitoring, alerting, runbooks, SLOs |
| test | Testing | Unit tests, integration tests, coverage |
| optimization | Performance | Profiling, caching, query optimization |
| docs | Documentation | API docs, architecture docs, README |
| cicd | Build systems | Docker, build configs, artifact management |
| aws-architect | AWS design | Service selection, cost optimization, architecture |
| code-review | Review | Correctness, style, patterns, security |
| data | Data engineering | Schemas, migrations, ETL, data pipelines |
| frontend | UI/UX | React, CSS, accessibility, component design |

## 10. Cold Start Flow

### `cap init` Step-by-Step

```
$ cap init
```

**T+0s: Configuration**
```
1. Create ~/.cap/ directory if not exists
2. Create ~/.cap/cap.db with all schemas (SQLite, WAL mode)
3. Write ~/.cap/config.toml with defaults
4. Detect workspace: git root or cwd
```

**T+1s: Hook Generation**
```
5. Create .claude/ directory in workspace
6. Generate .claude/hooks/pretool.py (enforcement)
7. Generate .claude/hooks/posttool.py (sync)
8. Generate .claude/settings.json with MCP server config:
   {
     "mcpServers": {
       "cap-orchestrator": {
         "command": "cap-mcp",
         "args": ["orchestrator"],
         "transport": "stdio"
       },
       "cap-memory": {
         "command": "cap-mcp",
         "args": ["memory"],
         "transport": "stdio"
       },
       "cap-code-intel": {
         "command": "cap-mcp",
         "args": ["code-intel"],
         "transport": "stdio"
       }
     },
     "hooks": {
       "PreToolUse": [{"command": "python3 .claude/hooks/pretool.py"}],
       "PostToolUse": [{"command": "python3 .claude/hooks/posttool.py"}]
     }
   }
9. Generate .claude/CLAUDE.md with orchestration rules
```

**T+2s: Quick Index (useful at this point)**
```
10. Read README.md, CLAUDE.md, package.json/pyproject.toml/go.mod
11. Store in memory as high-importance context entries
12. Detect language(s) from file extensions
13. Index: config files (tsconfig, Makefile, Dockerfile, terraform)
    → Workspace is USABLE now (~5s total)
```

**T+5-60s: Full Index (background)**
```
14. tree-sitter parse all supported language files
15. Build symbol table (functions, classes, types)
16. Build relationship graph (calls, imports, extends)
17. Compute file hashes for incremental sync
18. git fetch --all (background, non-blocking)
    → Full capability achieved (~60s for typical repo)
```

**T+sessions 1-5: Learning Phase**
```
19. Record routing decisions and outcomes
20. Build complexity model from actual usage
21. Accumulate correction patterns
22. Calibrate trust levels per agent type
    → System fully adapted by session 5
```

### Timing Budget (target: 42-repo workspace, ~1.3GB code)

| Phase | Duration | What's Usable |
|-------|----------|---------------|
| Config + hooks | 2s | Enforcement active, MCP available |
| Quick index | 3s | Memory search, basic context |
| AST extraction | 30-45s | Code structure, symbol lookup |
| Graph building | 10-15s | Blast radius, dependents, traces |
| Git fetch | 5-30s (background) | Latest remote state |

**Disk budget for 42 repos:**
- AST symbols: ~50MB (indexed, not raw ASTs)
- Relationship graph: ~20MB
- Memory entries: ~10MB
- FTS5 index: ~30MB
- Total: ~110MB (well within 256MB/workspace budget)

Key to keeping disk usage manageable: store only extracted symbols and edges, not full AST trees. The tree-sitter parse is done on-demand for detailed queries.

## 11. User Experience

### Installation

```
$ uv tool install claude-agent-platform
$ cd ~/my-workspace
$ cap init
✓ Created ~/.cap/cap.db
✓ Generated .claude/hooks/pretool.py
✓ Generated .claude/hooks/posttool.py
✓ Generated .claude/settings.json (3 MCP servers)
✓ Generated .claude/CLAUDE.md
✓ Quick-indexed: 3 config files, 1 README
⟳ Background indexing 847 files... (will complete in ~45s)

CAP is ready. Open Claude Code in this workspace.
```

### First Use (in Claude Code)

User types a task. Claude sees CLAUDE.md with orchestration rules. The MCP tools are available. The enforcement hook is active.

**If user asks something trivial:**
```
User: "What's in the main config?"
Claude: [reads file directly — inline tier, no delegation needed]
```

**If user asks something medium:**
```
User: "Add error handling to the auth middleware"
Claude: [routes to lightweight tier — single dev agent + 1 review pass]
→ Agent edits 1-2 files
→ Review agent checks
→ Result presented to user
```

**If user asks something complex:**
```
User: "Migrate our auth from JWT to OAuth2 across all services"
Claude: [routes to full tier — orchestrator spawns security + dev + test + docs]
→ Checkpoint after each agent
→ Security veto check
→ Review loop (max 3 iterations)
→ Final result with blast radius report
```

### Daily Use

**Cost visibility (always shown):**
```
[CAP] Task routed: full tier | Est. cost: $0.42 | Budget remaining: $4.58/day
[CAP] ████████░░ 3/5 agents complete | $0.31 spent
[CAP] ✓ Complete | Actual: $0.38 | Files: 7 modified, 2 created
```

**Enforcement block (when user tries to edit too many files directly):**
```
[CAP] ⚠ BLOCKED: Editing 3+ files without delegation.
      Files touched: src/auth.py, src/middleware.py, src/config.py
      → Use Agent({ subagent_type: 'orchestrator', ... }) to delegate
      → Or run `cap passthrough` for 5-minute bypass
```

**Passthrough (escape hatch):**
```
$ cap passthrough --reason "quick hotfix"
✓ Enforcement bypassed for 5 minutes (expires 14:35:22)
⚠ This is logged. Rate limit: 3/hour.
```

### Error States

| Error | User Sees | Recovery |
|-------|-----------|----------|
| MCP server crash | "CAP memory unavailable, operating in degraded mode" | Auto-restart, fallback to FTS5-only |
| Agent failure (all retries) | "Task partially complete. 2/4 steps done. Checkpoint saved." | `cap resume` or manual completion |
| Disk budget exceeded | "CAP storage full. Running eviction..." | Automatic eviction, user notified |
| Security veto | "Security review BLOCKED this change: [reason]. Awaiting PO decision." | User approves or rejects |
| Budget exceeded | "Daily budget ($5.00) reached. Switching to offline mode (FTS5-only)." | Next day reset, or `cap budget raise` |

### Offline Mode

When budget is exceeded or network is unavailable:
- Memory search works (FTS5 is local SQLite)
- Code intelligence works (local tree-sitter)
- Enforcement works (local hooks)
- Orchestration works (agents still spawn, just with local context)
- What's degraded: no git fetch, no semantic search (if using embeddings), no model upgrades

#### Offline Detection and Mode Switching (`src/cap/runtime/offline.py`)

```python
import socket
import time
import sqlite3

class OfflineDetector:
    """Detects network/budget state and switches modes."""

    MODES = ("online", "degraded", "offline")
    CHECK_INTERVAL = 60  # re-check every 60s

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._mode = "online"
        self._last_check = 0

    def get_mode(self) -> str:
        """Returns current mode, re-checking if stale."""
        if time.time() - self._last_check > self.CHECK_INTERVAL:
            self._mode = self._detect()
            self._last_check = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES ('mode', ?, ?)",
                (self._mode, time.time())
            )
            self.db.commit()
        return self._mode

    def _detect(self) -> str:
        # Check budget first (fast, local)
        budget_ok = self._check_budget()
        if not budget_ok:
            return "offline"  # Budget exceeded → full offline

        # Check network (timeout 2s)
        network_ok = self._check_network()
        if not network_ok:
            return "degraded"  # No network but budget ok → degraded

        return "online"

    def _check_budget(self) -> bool:
        """Is daily budget remaining?"""
        row = self.db.execute("""
            SELECT SUM(cost_usd) FROM cost_events
            WHERE timestamp > ? 
        """, (time.time() - 86400,)).fetchone()
        spent = row[0] or 0.0
        cap = self.db.execute(
            "SELECT value FROM runtime_state WHERE key = 'daily_budget_usd'"
        ).fetchone()
        daily_cap = float(cap[0]) if cap else 5.0
        return spent < daily_cap

    def _check_network(self) -> bool:
        """Can we reach external services?"""
        try:
            socket.create_connection(("api.anthropic.com", 443), timeout=2)
            return True
        except (socket.timeout, OSError):
            return False

    def is_offline(self) -> bool:
        return self.get_mode() == "offline"

    def is_degraded(self) -> bool:
        return self.get_mode() in ("offline", "degraded")

    def should_skip_network_ops(self) -> bool:
        """Skip git fetch, embedding calls, model upgrades."""
        return self.get_mode() != "online"
```

Usage in MCP servers:
```python
# In any MCP tool that needs network:
offline = OfflineDetector(db)
if offline.should_skip_network_ops():
    # Skip embedding generation, use FTS5-only search
    # Skip git fetch, use local state
    # Skip model upgrade attempts
    pass
```

#### Cost Tracker (`src/cap/cost/tracker.py`)

```python
import time
import sqlite3

# Pricing per 1M tokens (as of 2024, update via cap config)
MODEL_PRICING = {
    "opus":   {"input": 15.00, "output": 75.00},
    "sonnet": {"input": 3.00,  "output": 15.00},
    "haiku":  {"input": 0.25,  "output": 1.25},
}

class CostTracker:
    """Track token usage, estimate costs, enforce daily budget."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def track(self, agent_type: str, model: str, input_tokens: int, output_tokens: int, workflow_id: str = None):
        """Record a completed agent call's cost."""
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["sonnet"])
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
        self.db.execute("""
            INSERT INTO cost_events (agent_type, model, input_tokens, output_tokens, cost_usd, workflow_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (agent_type, model, input_tokens, output_tokens, cost, workflow_id, time.time()))
        self.db.commit()

    def estimate(self, agent_type: str, task_complexity: str) -> dict:
        """Estimate cost before execution based on historical data."""
        # Look up average tokens for this agent type + complexity
        row = self.db.execute("""
            SELECT AVG(input_tokens), AVG(output_tokens), AVG(cost_usd), COUNT(*)
            FROM cost_events WHERE agent_type = ?
            AND timestamp > ?
        """, (agent_type, time.time() - 7 * 86400)).fetchone()

        if row and row[3] >= 5:  # Need at least 5 samples
            avg_input, avg_output, avg_cost, samples = row
            # Adjust by complexity multiplier
            multiplier = {"inline": 0.3, "lightweight": 1.0, "full": 2.5}.get(task_complexity, 1.0)
            return {
                "estimated_cost_usd": round(avg_cost * multiplier, 4),
                "estimated_tokens": int((avg_input + avg_output) * multiplier),
                "confidence": "high" if samples >= 20 else "medium",
                "based_on_samples": samples,
            }
        # Fallback: estimate from model pricing and typical sizes
        model = "opus" if agent_type in ("orchestrator", "security", "aws-architect") else "sonnet"
        typical_tokens = {"inline": 2000, "lightweight": 15000, "full": 50000}.get(task_complexity, 15000)
        pricing = MODEL_PRICING[model]
        est_cost = (typical_tokens * (pricing["input"] + pricing["output"]) / 2) / 1_000_000
        return {
            "estimated_cost_usd": round(est_cost, 4),
            "estimated_tokens": typical_tokens,
            "confidence": "low",
            "based_on_samples": 0,
        }

    def budget_check(self) -> dict:
        """Check if daily budget allows more spending."""
        row = self.db.execute("""
            SELECT SUM(cost_usd) FROM cost_events WHERE timestamp > ?
        """, (time.time() - 86400,)).fetchone()
        spent_today = row[0] or 0.0

        cap_row = self.db.execute(
            "SELECT value FROM runtime_state WHERE key = 'daily_budget_usd'"
        ).fetchone()
        daily_cap = float(cap_row[0]) if cap_row else 5.0
        remaining = daily_cap - spent_today

        return {
            "spent_today_usd": round(spent_today, 4),
            "daily_cap_usd": daily_cap,
            "remaining_usd": round(remaining, 4),
            "allowed": remaining > 0,
            "mode": "online" if remaining > daily_cap * 0.2 else "degraded" if remaining > 0 else "offline",
        }

    def get_workflow_cost(self, workflow_id: str) -> dict:
        """Real-time cost for active workflow (displayed during execution)."""
        row = self.db.execute("""
            SELECT SUM(cost_usd), SUM(input_tokens + output_tokens), COUNT(*)
            FROM cost_events WHERE workflow_id = ?
        """, (workflow_id,)).fetchone()
        return {
            "total_cost_usd": round(row[0] or 0, 4),
            "total_tokens": row[1] or 0,
            "agent_calls": row[2] or 0,
        }
```

```sql
CREATE TABLE cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    workflow_id TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX idx_cost_time ON cost_events(timestamp);
CREATE INDEX idx_cost_workflow ON cost_events(workflow_id);

CREATE TABLE runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

## 12. Resolved Gaps

### From Simulations

| Gap | Resolution |
|-----|-----------|
| No memory eviction | Eviction daemon runs every 10 min + on write. Score < 0.15 → archive. 90d stale → mark. 365d + <3 access → delete. Disk budget hard cap at 1GB. See Section 5. |
| No cross-session consolidation | `consolidate()` runs at session end. Clusters similar entries via FTS5 similarity. Groups of 3+ merged into single entry. Originals marked `consolidated_into`. See Section 5. |
| No cleanup of partial writes | `RollbackManager` backs up files before agent edits. On failure, restores all files to pre-edit state. Scratchpad cleaned up. Git checkout fallback. See Section 9. |
| No checkpoint/resume | `save_checkpoint()` called after each agent completes. Serializes full ContextThread + scratchpad state. `resume_from_checkpoint()` reconstructs and continues from last completed step. See Section 9. |
| No intermediate lightweight tier | 3-tier routing: inline (<0.3), lightweight (0.3-0.6, single specialist + 1 review), full (>0.6, orchestrator + multi-agent). Thresholds adapt via learning. See Section 9. |
| Contradictory CLAUDE.md (20-line rule) | RESOLVED: The 20-line rule applies to the OUTER Claude (the one reading CLAUDE.md). Agents spawned by the orchestrator can write unlimited code — the enforcement hook checks `agent_contexts` table and allows delegated edits. The rule enforces delegation, not code volume. |
| No cross-repo atomic commit | Orchestrator uses worktrees per repo. All changes staged but not committed until all agents succeed. If any fail, rollback all. For true atomicity: commits happen in sequence with a 30s window; if any push fails, the orchestrator reverts the earlier commits. Not fully atomic (git doesn't support it) but best-effort with clear failure reporting. |
| No worktree enforcement in hooks | `check_worktree_requirement()` in pretool.py detects multiple active agents in `agent_contexts` writing to same git root without distinct `workspace` paths. Blocks with exit 2 and message to use worktrees. See Section 4. |
| Graph max_fanout=50 loses data | Degree-aware BFS: nodes with degree > 50 are NOT expanded fully. Instead, top-10 neighbors sampled by their own degree + recency. A `summarized_hubs` field reports what was sampled vs total. No data loss — data exists in DB, traversal just doesn't explode. See Section 6. |
| No auto-sync triggers | Five triggers: session start (git fetch), post git-pull hook, 5-min staleness timer, file write (single file re-extract), manual `cap sync`. All implemented in SyncEngine + posttool.py. See Section 7. |

### From Reviews

| Gap | Resolution |
|-----|-----------|
| Exit-code-2 hooks uncertain | Specified exact hook script with full Python implementation. Uses Claude Code's documented PreToolUse hook protocol: stdin receives JSON, exit 0 = allow, exit 2 = hard block. Tested pattern. See Section 4. |
| No escape hatch | `cap passthrough` command: enables 5-minute bypass, logged to DB, rate-limited to 3/hour, auto-expires. Cannot be silently extended. See Section 4. |
| Dead zone for medium tasks | Lightweight tier handles complexity 0.3-0.6: one specialist agent + one review pass. No orchestrator overhead. Fast, cheap, still quality-checked. See Section 9. |
| Disk 5x over budget | Budget enforcement in eviction daemon. Store only symbols + edges, not full AST. Aggressive archival. Estimated 110MB for 42 repos vs naive 1.3GB. Hard cap triggers emergency eviction. See Section 10. |
| No scratchpad | `Scratchpad` class: per-orchestration temp directory under `~/.cap/scratchpad/`. Agents write artifacts by name, others read by name. Cleaned up on completion or rollback. See Section 9. |
| HCL parsing broken | Excluded from v1 language support. Documented in supported languages table. Will revisit when tree-sitter-hcl stabilizes. Terraform files indexed by config patterns only (not full AST). |
| 15k token budget unenforceable | `TokenCounter` using tiktoken cl100k_base (within 10% of Claude tokenizer). `fits_budget()` checked before every working memory addition. `evict_lowest_score()` frees space when over budget. See Section 5. |
| Orchestrator failure unrecoverable | Checkpoint saved after EACH agent completes. On crash: `cap resume` loads checkpoint, skips completed steps, continues from next pending. Scratchpad state preserved. See Section 9. |
| No cost visibility | Real-time cost tracking: token usage per agent call, accumulated per orchestration, displayed inline. Budget preview BEFORE workflow starts ("Est. $0.42, proceed?"). Daily cap with offline fallback. See Section 11. |
| Memory grows unbounded | Three-layer eviction: score-based (< 0.15 → archive), time-based (90d stale, 365d delete), space-based (disk budget hard cap). Plus consolidation merging duplicates. See Section 5. |

## 13. Implementation Roadmap

### Week 1: MVP (Enforcement + Basic Memory + Routing)

**Goal: CAP blocks bad patterns and provides memory. No code intel yet.**

| Day | Deliverable |
|-----|-------------|
| 1-2 | Package scaffold: pyproject.toml, src/cap/, CLI entry point, `cap init` |
| 2-3 | SQLite DB manager with all schemas, migrations, WAL mode |
| 3-4 | Enforcement hook (pretool.py): file edit tracking, 3-file block, passthrough |
| 4-5 | Memory MCP server: store, recall, search (FTS5), basic scoring |
| 5 | 3-tier router (heuristic only, no learning yet) |
| 5 | Integration test: install via uv, run `cap init`, verify hooks fire |

**Week 1 Exit Criteria:**
- `uv tool install .` works
- `cap init` generates working hooks
- Editing 3+ files without delegation is blocked
- Memory persists across sessions
- FTS5 search returns relevant results

### Week 2: Orchestration + Context Threading

**Goal: Multi-agent workflows actually work end-to-end.**

| Day | Deliverable |
|-----|-------------|
| 1-2 | Orchestrator MCP server: tool definitions, agent spawning |
| 2-3 | Context threading: ContextFrame, ContextThread, summary generation |
| 3-4 | Checkpoint/resume: save after each agent, resume command |
| 4-5 | Failure handling: retry, model upgrade, quorum check |
| 5 | Scratchpad + rollback manager |
| 5 | E2E test: multi-agent task with checkpoint and recovery |

**Week 2 Exit Criteria:**
- Orchestrator routes to correct specialist
- Context flows between agents (prior outputs visible)
- Failed agent → retry → escalate works
- Checkpoint survives process kill, resume continues

### Week 3: Code Intelligence + Auto-Sync

**Goal: AST-based code understanding and automatic freshness.**

| Day | Deliverable |
|-----|-------------|
| 1-2 | tree-sitter extractor: Python + TypeScript (most common) |
| 2-3 | Graph builder: symbols, relationships, indexes |
| 3 | Degree-aware BFS, blast_radius tool |
| 4 | Add Go + Rust parsers |
| 4-5 | Auto-sync engine: staleness detection, incremental re-index, posttool trigger |
| 5 | Code Intel MCP server: all 4 tools exposed |

**Week 3 Exit Criteria:**
- `code_structure("file.py")` returns functions/classes
- `blast_radius("file.py")` returns affected dependents
- Editing a file triggers re-extraction within 5s
- `git pull` triggers re-index of changed files

### Week 4: Self-Learning + Cost Control + Polish

**Goal: System improves itself and respects budgets.**

| Day | Deliverable |
|-----|-------------|
| 1-2 | Learning engine: routing recording, outcome tracking, correction patterns |
| 2-3 | Adaptive complexity model, trust progression, 3x auto-baseline |
| 3-4 | Cost tracker: per-call tracking, budget caps, preview, offline mode |
| 4-5 | Memory eviction daemon, consolidation, disk budget enforcement |
| 5 | Cold start optimization, documentation, release packaging |

**Week 4 Exit Criteria:**
- Routing improves measurably after 10+ tasks (lower cost, fewer escalations)
- Daily budget cap triggers offline mode gracefully
- Eviction keeps disk under 256MB/workspace
- Full cold start completes in <60s on 42-repo workspace
- Published to PyPI as `claude-agent-platform`

## 14. Honest Limitations

### What CAP Cannot Do

1. **Cannot enforce across multiple Claude Code instances.** If a user opens two terminals with separate Claude sessions, the enforcement hook tracks per-session. A coordinated multi-terminal attack on the 3-file rule is technically possible (though logged).

2. **Cannot guarantee atomic cross-repo commits.** Git does not support multi-repo transactions. CAP's best-effort sequential commit with rollback on failure has a race window. True atomicity requires a monorepo or external coordinator.

3. **Cannot tokenize identically to Claude.** We use tiktoken cl100k_base which is ~10% off from Claude's actual tokenizer. The 15k budget is approximate. We add a 10% safety margin (effective budget: 13,500 tokens).

4. **Cannot learn without usage.** The self-learning system needs ~50 routing decisions before adaptive thresholds become meaningful. Sessions 1-5 use hardcoded defaults. If usage patterns change dramatically, re-calibration takes another ~20 decisions.

5. **Cannot parse HCL reliably.** tree-sitter-hcl is not production-quality. Terraform files are indexed by file patterns and simple regex, not full AST. This means blast_radius for Terraform changes is approximate (catches resource references but not complex expressions).

6. **Cannot prevent a determined user from bypassing enforcement.** The hooks are Python scripts in .claude/. A user can delete them. This is by design — CAP is a productivity tool, not a security control. The passthrough mechanism is the legitimate escape hatch.

7. **Cannot operate as a standalone orchestration engine.** CAP is parasitic on Claude Code — it uses Claude's model inference, agent spawning (the `Agent()` tool), and MCP infrastructure. Without Claude Code, CAP is just a database and some hooks.

8. **Cannot handle binary files or non-text assets.** Code intelligence is text-only. Images, compiled artifacts, and binary configs are not indexed or tracked.

9. **Cannot guarantee memory consistency under concurrent access.** SQLite with WAL handles concurrent reads well, but two MCP servers writing the same entry simultaneously could produce unexpected results. We use write locks on critical paths but don't implement full MVCC.

10. **Cannot predict cost accurately for novel task types.** Cost estimation is based on historical data. A task unlike anything seen before gets a rough estimate based on tier (inline: ~$0.01, lightweight: ~$0.10, full: ~$0.50). Actual cost may vary 3-5x for novel patterns.

11. **Cannot replace good engineering judgment.** The routing heuristics and complexity scores are approximations. An experienced engineer who knows "this 2-line change will break 50 downstream consumers" has information CAP's heuristics may miss until the code graph is fully built and the learning engine has calibrated.

12. **Cannot survive Claude Code breaking changes.** If Anthropic changes the hook protocol, MCP transport, or Agent() tool interface, CAP will break. The adapter layers (2 and 3) isolate this, but a major protocol change requires an update. We vendor-lock on Claude Code's documented interfaces.

---

## 15. Design Patches (Validation Round 1)

The following patches resolve five concrete blockers identified during specification validation. Each patch is implementation-ready with pseudocode, schema changes, and integration points.

---

### PATCH 1: Auto-Sync `git fetch` Does Not Update Working Tree

**Problem:** `git fetch` updates remote refs but leaves the working tree unchanged, so new files/services merged by teammates remain invisible to CAP's extractor. Additionally, `on_git_fetch()` references `ORIG_HEAD` which is only set by `pull`/`merge`/`rebase` (never by `fetch`), making it dead code. Finally, `posttool.py` records `sync_triggers` but `SyncEngine.background_loop` checks file mtime instead of consulting that table — the two are disconnected.

**Concrete Fix:**

Modify `src/cap/sync/engine.py`:

```python
class SyncEngine:
    # ... existing __init__ ...

    async def background_loop(self):
        """Runs every 5 minutes OR when sync_triggers has pending entries."""
        while True:
            # CHECK 1: Poll sync_triggers table (connects posttool → sync engine)
            pending_triggers = self._consume_sync_triggers()
            if pending_triggers:
                await self.check_and_sync()

            # CHECK 2: Periodic staleness (existing behavior)
            elapsed = time.time() - self.last_sync
            if elapsed >= HASH_CHECK_INTERVAL:
                await self.check_and_sync()

            await asyncio.sleep(30)  # poll every 30s, act every 5min or on trigger

    def _consume_sync_triggers(self) -> list[dict]:
        """Read and clear pending sync_triggers. Bridges posttool.py → SyncEngine."""
        rows = self.db.execute(
            """DELETE FROM sync_triggers
               WHERE id IN (SELECT id FROM sync_triggers ORDER BY timestamp LIMIT 50)
               RETURNING id, trigger_type, detail, timestamp"""
        ).fetchall()
        self.db.commit()
        return [{"id": r[0], "type": r[1], "detail": r[2], "ts": r[3]} for r in rows]

    def on_git_fetch(self):
        """
        Called after git fetch --all.
        FIXED: Compare local branch tip against remote tracking branch,
        not ORIG_HEAD (which is never set by fetch).
        Strategy: ff-only merge if working tree is clean, else index from remote refs directly.
        """
        try:
            # Determine the tracking branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True, text=True, cwd=self.workspace, timeout=5
            )
            if result.returncode != 0:
                return  # no upstream configured
            upstream = result.stdout.strip()  # e.g., "origin/main"

            # Check if local is behind remote
            result = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..{upstream}"],
                capture_output=True, text=True, cwd=self.workspace, timeout=5
            )
            behind_count = int(result.stdout.strip())
            if behind_count == 0:
                return  # already up to date

            # Check working tree cleanliness
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=self.workspace, timeout=5
            )
            is_clean = len(status.stdout.strip()) == 0

            if is_clean:
                # STRATEGY A: Fast-forward merge (updates working tree)
                merge_result = subprocess.run(
                    ["git", "merge", "--ff-only", upstream],
                    capture_output=True, text=True, cwd=self.workspace, timeout=30
                )
                if merge_result.returncode == 0:
                    # Get changed files from the ff-merge
                    diff_result = subprocess.run(
                        ["git", "diff", "--name-only", f"HEAD~{behind_count}..HEAD"],
                        capture_output=True, text=True, cwd=self.workspace, timeout=10
                    )
                    changed = [
                        os.path.join(self.workspace, f.strip())
                        for f in diff_result.stdout.strip().split("\n")
                        if f.strip()
                    ]
                    if changed:
                        from ..code_intel.extractor import extract_incremental
                        extract_incremental(self.workspace, changed)
            else:
                # STRATEGY B: Index from remote ref blobs without checkout
                # Read the tree at the remote ref to find changed paths
                diff_result = subprocess.run(
                    ["git", "diff", "--name-only", f"HEAD..{upstream}"],
                    capture_output=True, text=True, cwd=self.workspace, timeout=10
                )
                changed_paths = [
                    f.strip() for f in diff_result.stdout.strip().split("\n")
                    if f.strip()
                ]
                # Index using git show to read file content from remote ref
                self._index_from_remote_ref(upstream, changed_paths)

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
            pass

    def _index_from_remote_ref(self, ref: str, paths: list[str]):
        """
        Read file contents directly from a git ref (without checkout).
        Uses `git show ref:path` to get blob content for indexing.
        """
        from ..code_intel.extractor import extract_from_content, SUPPORTED_LANGUAGES
        for rel_path in paths:
            language = detect_language(rel_path)
            if language not in SUPPORTED_LANGUAGES:
                continue
            try:
                result = subprocess.run(
                    ["git", "show", f"{ref}:{rel_path}"],
                    capture_output=True, text=True, cwd=self.workspace, timeout=5
                )
                if result.returncode == 0:
                    abs_path = os.path.join(self.workspace, rel_path)
                    extract_from_content(abs_path, result.stdout, language)
            except subprocess.TimeoutExpired:
                continue

    def session_start_sync(self):
        """Called at session start. Fetches and updates working tree + index."""
        try:
            subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                capture_output=True, timeout=30, cwd=self.workspace
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Use the fixed on_git_fetch instead of bare check_and_sync
        self.on_git_fetch()
```

Also add `sync_triggers` schema (add to DB migrations):

```sql
-- Ensure sync_triggers has an id for DELETE ... RETURNING
CREATE TABLE IF NOT EXISTS sync_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    trigger_type TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX idx_sync_triggers_ts ON sync_triggers(timestamp);
```

And add `extract_from_content()` to `src/cap/code_intel/extractor.py`:

```python
def extract_from_content(file_path: str, content: str, language: str) -> FileIndex:
    """Parse content string (not from disk) for indexing remote ref blobs."""
    parser = get_parser(language)
    tree = parser.parse(content.encode())
    queries = get_queries(language)
    symbols = extract_symbols(tree, queries, file_path)
    relationships = extract_relationships(tree, queries, file_path)
    imports = extract_imports(tree, queries, file_path)
    exports = extract_exports(tree, queries, file_path)
    index = FileIndex(
        path=file_path, language=language,
        hash=hashlib.sha256(content.encode()).hexdigest(),
        symbols=symbols, relationships=relationships,
        imports=imports, exports=exports,
    )
    store_file_index(get_db(), index)
    return index
```

**Component modified:** `src/cap/sync/engine.py`, `src/cap/code_intel/extractor.py`, DB schema (migrations)

**Integration:** `session_start_sync()` already called at session start. `background_loop()` now reads `sync_triggers` that `posttool.py` already writes to. The `posttool.py` detects `git fetch` in Bash commands and inserts into `sync_triggers` — this now actually triggers re-indexing via `_consume_sync_triggers()`. The `on_git_fetch()` method replaces dead `ORIG_HEAD` logic with a compare-against-upstream approach that either ff-merges (clean tree) or indexes from blobs (dirty tree).

---

### PATCH 2: No Per-Agent Execution Timeout

**Problem:** `run_agent()` in the failure handling path has no `asyncio.wait_for()` wrapper, so a hung agent blocks indefinitely, preventing retry/escalation/rollback logic from ever executing.

**Concrete Fix:**

Modify `src/cap/orchestration/checkpoint.py` (where `execute_with_failure_handling` lives, Section 9):

```python
# Add to top of file
import asyncio

# Per-agent-type timeout configuration (seconds)
AGENT_TIMEOUTS: dict[str, int] = {
    "dev": 300,
    "devops": 300,
    "security": 180,
    "sre": 180,
    "test": 240,
    "optimization": 240,
    "docs": 120,
    "cicd": 180,
    "aws-architect": 240,
    "code-review": 120,
    "data": 240,
    "frontend": 240,
}
DEFAULT_AGENT_TIMEOUT = 300  # 5 minutes fallback


def get_agent_timeout(agent_type: str) -> int:
    """Get configured timeout for an agent type. Supports override via config."""
    from ..config import load as load_config
    config = load_config()
    # Allow per-workspace override in config.toml: [agent_timeouts] section
    overrides = config.get("agent_timeouts", {})
    if agent_type in overrides:
        return int(overrides[agent_type])
    return AGENT_TIMEOUTS.get(agent_type, DEFAULT_AGENT_TIMEOUT)


async def execute_with_failure_handling(
    agent_type: str, task: str, thread: ContextThread, db
) -> dict:
    """Execute agent with timeout, retry, and model upgrade on failure."""
    attempts = 0
    model = "sonnet"
    timeout = get_agent_timeout(agent_type)
    last_error = "unknown"

    while attempts < MAX_RETRIES:
        try:
            # FIXED: Wrap every run_agent call in asyncio.wait_for
            result = await asyncio.wait_for(
                run_agent(agent_type, task, thread, model=model),
                timeout=timeout
            )
            if result.get("status") == "success":
                return result
            elif result.get("status") == "need_info":
                answer = handle_need_info(result["need_info"], thread)
                task = f"{task}\n\nAdditional info: {answer}"
                continue
            else:
                # Agent returned non-success, non-need_info status
                last_error = result.get("error", "agent returned failure status")
                raise AgentFailure(last_error)

        except asyncio.TimeoutError:
            attempts += 1
            last_error = f"Agent '{agent_type}' timed out after {timeout}s (attempt {attempts})"
            # Log timeout event for learning engine
            from ..learning.engine import LearningEngine
            LearningEngine(db).record_routing(
                session_id=thread.orchestration_id,
                task=f"TIMEOUT: {agent_type} on: {task[:100]}",
                tier="full", agents=[agent_type], complexity=1.0,
            )
            if attempts == 1:
                model = "opus"  # upgrade model, maybe it's faster/smarter
                timeout = int(timeout * 1.5)  # also give more time with better model
            elif attempts >= MAX_RETRIES:
                break

        except AgentFailure as e:
            attempts += 1
            last_error = str(e)
            if attempts == 1:
                model = "opus"
            elif attempts >= MAX_RETRIES:
                break

    # All retries exhausted — trigger existing failure path
    return {
        "status": "failed",
        "agent": agent_type,
        "attempts": attempts,
        "last_error": last_error,
        "timed_out": "timed out" in last_error,
    }
```

**Component modified:** `src/cap/orchestration/checkpoint.py` (failure handling section)

**Integration:** This wraps the existing `run_agent()` call — no changes needed to `run_agent()` itself or to the orchestrator MCP server. The timeout config can be overridden in `~/.cap/config.toml` under `[agent_timeouts]`. The learning engine records timeout events, which feeds into adaptive complexity scoring (tasks that timeout get higher complexity next time). The existing `check_failure_quorum()` receives the failure dict with `timed_out: true` allowing quorum logic to distinguish timeouts from logical failures.

---

### PATCH 3: RollbackManager Not Auto-Enforced

**Problem:** `RollbackManager.track_file()` requires explicit orchestrator calls before each write. Nothing intercepts agent tool calls (Edit, Write, Bash) to auto-track, so files written via Bash are untracked and rollback is incomplete.

**Concrete Fix:**

**Part A: PreTool Hook Auto-Tracking**

Add to `src/cap/hooks/pretool.py` (before the existing enforcement logic):

```python
def auto_track_for_rollback(db, session_id: str, tool_name: str, tool_input: dict):
    """
    Intercept Edit/Write tool calls and register files with active RollbackManager.
    Called BEFORE enforcement checks so rollback tracking happens even for allowed edits.
    """
    # Only track when an orchestration is active (has a rollback session)
    active_rollback = db.execute(
        """SELECT orchestration_id, workspace FROM rollback_sessions
           WHERE session_id = ? AND status = 'active'""",
        (session_id,)
    ).fetchone()
    if not active_rollback:
        return  # No active orchestration — nothing to track

    orchestration_id, workspace = active_rollback
    file_path = None

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        file_path = tool_input.get("file_path", "")
    # Note: Bash writes handled via affected_files (Part B below)

    if file_path and file_path not in _already_tracked(db, orchestration_id):
        # Record that this file needs backup before modification
        _backup_file_for_rollback(db, orchestration_id, file_path)


def _already_tracked(db, orchestration_id: str) -> set:
    """Get set of files already tracked for this orchestration."""
    rows = db.execute(
        "SELECT file_path FROM rollback_tracked_files WHERE orchestration_id = ?",
        (orchestration_id,)
    ).fetchall()
    return {r[0] for r in rows}


def _backup_file_for_rollback(db, orchestration_id: str, file_path: str):
    """Backup file content to rollback table before it's modified."""
    import os
    content = None
    exists = os.path.exists(file_path)
    if exists:
        try:
            with open(file_path, "r") as f:
                content = f.read()
        except (IOError, UnicodeDecodeError):
            # Binary file — store path only, use git for rollback
            content = None

    db.execute(
        """INSERT OR IGNORE INTO rollback_tracked_files
           (orchestration_id, file_path, original_content, existed_before, tracked_at)
           VALUES (?, ?, ?, ?, ?)""",
        (orchestration_id, file_path, content, int(exists), time.time())
    )
    db.commit()


# Modify main() to call auto_track_for_rollback BEFORE enforcement:
def main():
    input_data = json.loads(sys.stdin.read())
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    db = get_db()

    # AUTO-TRACK: intercept writes for rollback (NEW — before enforcement)
    auto_track_for_rollback(db, session_id, tool_name, tool_input)

    # ... existing enforcement logic continues unchanged ...
```

**Part B: Bash Writes via `affected_files` Declaration**

The orchestrator already accepts `affected_files` in step definitions. Wire it to auto-track before dispatching:

Add to `src/cap/orchestration/rollback.py`:

```python
# New schema for hook-based rollback tracking
"""
CREATE TABLE rollback_sessions (
    orchestration_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- 'active', 'committed', 'rolled_back'
    created_at REAL NOT NULL
);

CREATE TABLE rollback_tracked_files (
    orchestration_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    original_content TEXT,  -- NULL for binary or new files
    existed_before INTEGER NOT NULL DEFAULT 1,
    tracked_at REAL NOT NULL,
    PRIMARY KEY (orchestration_id, file_path)
);
"""

class RollbackManager:
    def __init__(self, workspace: str, orchestration_id: str):
        self.workspace = workspace
        self.orchestration_id = orchestration_id
        self.backup_dir = os.path.expanduser(f"~/.cap/rollback/{orchestration_id}")
        self.tracked_files = []
        os.makedirs(self.backup_dir, exist_ok=True)

    def begin(self, session_id: str, db):
        """Start tracking. Register rollback session for hook interception."""
        self.start_ref = None
        if self._is_git():
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=self.workspace
            )
            self.start_ref = result.stdout.strip()

        # Register session so pretool hook knows to auto-track
        db.execute(
            """INSERT OR REPLACE INTO rollback_sessions
               (orchestration_id, session_id, workspace, status, created_at)
               VALUES (?, ?, ?, 'active', ?)""",
            (self.orchestration_id, session_id, self.workspace, time.time())
        )
        db.commit()

    def pre_declare_affected_files(self, affected_files: list[str], db):
        """
        Called before dispatching a step that declares affected_files.
        Pre-tracks all declared files so Bash writes are covered.
        """
        for file_path in affected_files:
            abs_path = os.path.join(self.workspace, file_path) if not os.path.isabs(file_path) else file_path
            self.track_file(abs_path)
            # Also record in DB for hook-based tracking
            _backup_file_for_rollback_direct(db, self.orchestration_id, abs_path)

    def commit(self, db):
        """Orchestration succeeded. Mark session committed, clean up."""
        db.execute(
            "UPDATE rollback_sessions SET status = 'committed' WHERE orchestration_id = ?",
            (self.orchestration_id,)
        )
        db.commit()
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)

    def rollback(self, db):
        """Revert all tracked files using DB records (hook-tracked + declared)."""
        # Get all tracked files from DB (includes hook-intercepted ones)
        rows = db.execute(
            """SELECT file_path, original_content, existed_before
               FROM rollback_tracked_files WHERE orchestration_id = ?""",
            (self.orchestration_id,)
        ).fetchall()

        for file_path, original_content, existed_before in rows:
            if existed_before and original_content is not None:
                with open(file_path, "w") as f:
                    f.write(original_content)
            elif existed_before and original_content is None:
                # Binary — use git checkout
                subprocess.run(
                    ["git", "checkout", "--", file_path],
                    cwd=self.workspace, capture_output=True
                )
            else:
                # File didn't exist before — remove it
                if os.path.exists(file_path):
                    os.remove(file_path)

        db.execute(
            "UPDATE rollback_sessions SET status = 'rolled_back' WHERE orchestration_id = ?",
            (self.orchestration_id,)
        )
        db.commit()
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
```

**Part C: Orchestrator wires `affected_files` to `pre_declare_affected_files`**

In `src/cap/mcp/orchestrator.py`, before dispatching each step:

```python
async def dispatch_step(step: dict, rollback_mgr: RollbackManager, db):
    """Dispatch a single orchestration step to a specialist agent."""
    # Pre-track declared affected files (covers Bash writes)
    affected = step.get("affected_files", [])
    if affected:
        rollback_mgr.pre_declare_affected_files(affected, db)

    # ... existing agent dispatch logic ...
```

**Component modified:** `src/cap/hooks/pretool.py`, `src/cap/orchestration/rollback.py`, `src/cap/mcp/orchestrator.py`, DB schema

**Integration:** The pretool hook now intercepts Edit/Write calls and auto-backs-up files into `rollback_tracked_files` when an orchestration is active. For Bash writes, the orchestrator reads `affected_files` from the step schema (already defined in the agent prompt format) and pre-declares them. On failure, `rollback()` uses the DB records (which include both hook-intercepted and pre-declared files) for complete reversion. No changes needed to agent implementations — tracking is transparent.

---

### PATCH 4: `find_similar_clusters()` and `summarize_cluster()` Are Unimplemented Stubs

**Problem:** These two functions in `src/cap/memory/compressor.py` are load-bearing for cross-session consolidation but contain only `pass` — without them, memory grows unbounded despite the eviction layer.

**Concrete Fix:**

Replace the stubs in `src/cap/memory/compressor.py`:

```python
import re
import math
from collections import defaultdict

# ─── find_similar_clusters ───────────────────────────────────────────────────

def find_similar_clusters(entries: list[tuple], threshold: float = 0.7) -> list[list[dict]]:
    """
    Cluster entries by content similarity using FTS5 BM25 scores.

    Algorithm:
    1. For each entry, extract top 5 TF-IDF terms from content
    2. Query FTS5 with those terms against all other entries
    3. Build similarity edges where BM25 score > threshold
    4. Find connected components using union-find
    5. Return only components with size >= 3

    Input: list of tuples (id, content, category, importance, access_count)
    Output: list of clusters, each cluster is a list of dicts
    """
    if len(entries) < 3:
        return []

    # Convert tuples to dicts for easier handling
    entry_dicts = [
        {"id": e[0], "content": e[1], "category": e[2],
         "importance": e[3], "access_count": e[4]}
        for e in entries
    ]

    # Step 1: Extract key terms per entry
    entry_terms = {}
    for entry in entry_dicts:
        entry_terms[entry["id"]] = _extract_key_terms(entry["content"], top_n=5)

    # Step 2: Compute pairwise similarity via FTS5
    db = get_db()
    similarity_edges = []  # (id_a, id_b, score)

    for entry in entry_dicts:
        terms = entry_terms[entry["id"]]
        if not terms:
            continue
        # Query FTS5 with this entry's key terms
        query_str = " OR ".join(terms)
        try:
            matches = db.execute(
                """SELECT rowid, rank FROM memory_fts
                   WHERE memory_fts MATCH ?
                   ORDER BY rank
                   LIMIT 20""",
                (query_str,)
            ).fetchall()
        except Exception:
            continue

        for match_rowid, rank in matches:
            # BM25 rank is negative (lower = better match); normalize to 0-1
            # Typical BM25 range: -25 (excellent) to 0 (no match)
            normalized_score = min(1.0, abs(rank) / 25.0)
            if normalized_score >= threshold:
                # Find the entry_id for this rowid
                match_entry = db.execute(
                    "SELECT id FROM memory_active WHERE rowid = ?", (match_rowid,)
                ).fetchone()
                if match_entry and match_entry[0] != entry["id"]:
                    similarity_edges.append((entry["id"], match_entry[0], normalized_score))

    # Step 3: Union-Find to build connected components
    parent = {e["id"]: e["id"] for e in entry_dicts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for id_a, id_b, _ in similarity_edges:
        if id_a in parent and id_b in parent:
            union(id_a, id_b)

    # Step 4: Group by component
    components = defaultdict(list)
    entry_by_id = {e["id"]: e for e in entry_dicts}
    for entry in entry_dicts:
        root = find(entry["id"])
        components[root].append(entry)

    # Step 5: Return only clusters with 3+ members
    return [cluster for cluster in components.values() if len(cluster) >= 3]


def _extract_key_terms(content: str, top_n: int = 5) -> list[str]:
    """
    Extract top-N key terms from content using TF approximation.
    Simple: tokenize, remove stopwords, score by frequency * length bonus.
    """
    STOPWORDS = {
        "the", "a", "an", "is", "was", "are", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "that", "this", "these", "those", "it", "its", "and", "but",
        "or", "nor", "not", "so", "yet", "both", "either", "neither", "each",
        "every", "all", "any", "few", "more", "most", "other", "some", "such",
        "no", "only", "own", "same", "than", "too", "very",
    }

    # Tokenize: split on non-alphanumeric, lowercase
    words = re.findall(r'[a-z_][a-z0-9_]{2,}', content.lower())
    words = [w for w in words if w not in STOPWORDS and len(w) > 2]

    # Score: frequency * log(length) — longer meaningful words score higher
    freq = defaultdict(int)
    for w in words:
        freq[w] += 1

    scored = [(word, count * math.log(len(word))) for word, count in freq.items()]
    scored.sort(key=lambda x: -x[1])

    return [word for word, _ in scored[:top_n]]


# ─── summarize_cluster ───────────────────────────────────────────────────────

def summarize_cluster(contents: list[str], max_tokens: int = 500) -> str:
    """
    Deterministic summarization without LLM call.

    Algorithm:
    1. Split all contents into sentences
    2. Deduplicate near-identical sentences (Jaccard > 0.8)
    3. Score sentences by TF-IDF density (information-rich sentences first)
    4. Preserve decision rationale sentences (detect via marker words)
    5. Assemble summary respecting max_tokens budget

    Output: consolidated text ≤ 500 tokens
    """
    from .tokens import count_tokens

    # Step 1: Extract all sentences
    all_sentences = []
    for content in contents:
        sentences = _split_sentences(content)
        all_sentences.extend(sentences)

    if not all_sentences:
        return contents[0][:200] if contents else ""

    # Step 2: Deduplicate near-identical sentences
    unique_sentences = _deduplicate_sentences(all_sentences, jaccard_threshold=0.8)

    # Step 3: Score by information density
    scored = _score_sentences_tfidf(unique_sentences)

    # Step 4: Boost decision rationale sentences
    DECISION_MARKERS = re.compile(
        r'\b(because|decided|chose|rejected|selected|reason|rationale|trade-?off|prefer)\b',
        re.IGNORECASE
    )
    for i, (sentence, score) in enumerate(scored):
        if DECISION_MARKERS.search(sentence):
            scored[i] = (sentence, score * 2.0)  # 2x boost for rationale

    # Sort by final score descending
    scored.sort(key=lambda x: -x[1])

    # Step 5: Assemble within token budget
    summary_parts = []
    token_budget = max_tokens
    for sentence, _ in scored:
        sentence_tokens = count_tokens(sentence)
        if sentence_tokens > token_budget:
            continue
        summary_parts.append(sentence)
        token_budget -= sentence_tokens
        if token_budget <= 0:
            break

    return " ".join(summary_parts)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences. Handles bullet points and code-like content."""
    # Split on sentence terminators and bullet points
    parts = re.split(r'(?<=[.!?])\s+|(?=^[-•*]\s)', text, flags=re.MULTILINE)
    # Also split on newlines that start new thoughts
    expanded = []
    for part in parts:
        sub_parts = part.split("\n")
        expanded.extend(sub_parts)
    # Clean and filter
    return [s.strip() for s in expanded if len(s.strip()) > 10]


def _deduplicate_sentences(sentences: list[str], jaccard_threshold: float) -> list[str]:
    """Remove near-duplicate sentences using Jaccard similarity on word sets."""
    unique = []
    seen_word_sets = []

    for sentence in sentences:
        words = set(sentence.lower().split())
        is_dup = False
        for seen in seen_word_sets:
            if not words or not seen:
                continue
            jaccard = len(words & seen) / len(words | seen)
            if jaccard > jaccard_threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(sentence)
            seen_word_sets.append(words)

    return unique


def _score_sentences_tfidf(sentences: list[str]) -> list[tuple[str, float]]:
    """
    Score sentences by TF-IDF density.
    Density = sum of IDF scores for terms in sentence / sentence length.
    Higher density = more information-rich.
    """
    # Build document frequency across all sentences
    doc_freq = defaultdict(int)
    sentence_terms = []
    for sentence in sentences:
        terms = set(re.findall(r'[a-z_][a-z0-9_]{2,}', sentence.lower()))
        sentence_terms.append(terms)
        for term in terms:
            doc_freq[term] += 1

    n_docs = len(sentences)
    scored = []
    for i, sentence in enumerate(sentences):
        terms = sentence_terms[i]
        if not terms:
            scored.append((sentence, 0.0))
            continue
        # IDF density: sum(log(N/df)) / num_terms
        idf_sum = sum(
            math.log((n_docs + 1) / (doc_freq[t] + 1))
            for t in terms
        )
        density = idf_sum / len(terms)
        # Bonus for longer sentences (more content)
        length_bonus = min(1.5, len(sentence.split()) / 10)
        scored.append((sentence, density * length_bonus))

    return scored
```

**Component modified:** `src/cap/memory/compressor.py`

**Integration:** `consolidate()` (already defined in compressor.py) calls both functions. `find_similar_clusters()` queries the existing `memory_fts` FTS5 virtual table defined in Section 5's schema. `summarize_cluster()` uses `count_tokens()` from `src/cap/memory/tokens.py` (already specified). The eviction daemon's session-end trigger calls `consolidate()`, which now has working implementations to actually merge redundant entries and keep memory bounded.

---

### PATCH 5: Pre-Checkpoint Crash Vulnerability

**Problem:** If the orchestrator crashes during setup (after planning but before any agent completes its first step), there is no checkpoint to resume from — `cap resume` fails with "No checkpoint found."

**Concrete Fix:**

Modify `src/cap/orchestration/checkpoint.py`:

```python
@dataclass
class Checkpoint:
    orchestration_id: str
    thread_state: dict
    completed_steps: list[str]
    pending_steps: list[str]
    scratchpad_state: dict
    failure_info: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    phase: str = "running"  # NEW: 'planned', 'running', 'completed', 'failed'


def save_initial_checkpoint(
    orchestration_id: str,
    plan: dict,
    pending_steps: list[str],
    thread: ContextThread,
    db
):
    """
    Save checkpoint immediately AFTER planning completes, BEFORE any agent runs.
    This guarantees `cap resume` always has something to work with,
    even if the orchestrator crashes during the first agent dispatch.

    Called by the orchestrator after route() + plan generation, before dispatch loop.
    """
    checkpoint = Checkpoint(
        orchestration_id=orchestration_id,
        thread_state=serialize_thread(thread),
        completed_steps=[],  # nothing completed yet
        pending_steps=pending_steps,
        scratchpad_state={},  # no artifacts yet
        phase="planned",
    )
    db.execute(
        """INSERT OR REPLACE INTO checkpoints
           (orchestration_id, data, created_at, phase)
           VALUES (?, ?, ?, ?)""",
        (orchestration_id, json.dumps(asdict(checkpoint)), time.time(), "planned")
    )
    # Also store the plan separately for resume reconstruction
    db.execute(
        """INSERT OR REPLACE INTO orchestration_plans
           (orchestration_id, plan_data, created_at)
           VALUES (?, ?, ?)""",
        (orchestration_id, json.dumps(plan), time.time())
    )
    db.commit()


def save_checkpoint(thread: ContextThread, completed: list, pending: list, db):
    """Save checkpoint after each agent completes (existing behavior, now sets phase='running')."""
    checkpoint = Checkpoint(
        orchestration_id=thread.orchestration_id,
        thread_state=serialize_thread(thread),
        completed_steps=completed,
        pending_steps=pending,
        scratchpad_state=get_scratchpad_state(thread.orchestration_id),
        phase="running",
    )
    db.execute(
        """INSERT OR REPLACE INTO checkpoints
           (orchestration_id, data, created_at, phase)
           VALUES (?, ?, ?, ?)""",
        (checkpoint.orchestration_id, json.dumps(asdict(checkpoint)), time.time(), "running")
    )
    db.commit()


def resume_from_checkpoint(orchestration_id: str, db) -> tuple:
    """
    Resume a failed/interrupted orchestration.
    FIXED: Handles both 'planned' phase (no agents ran yet) and 'running' phase.
    """
    row = db.execute(
        "SELECT data FROM checkpoints WHERE orchestration_id = ?",
        (orchestration_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"No checkpoint found for {orchestration_id}")

    checkpoint_data = json.loads(row[0])
    phase = checkpoint_data.get("phase", "running")

    if phase == "planned":
        # Orchestrator crashed before any agent ran.
        # Reconstruct thread from initial state, replay full plan.
        thread = deserialize_thread(checkpoint_data["thread_state"])
        remaining = checkpoint_data["pending_steps"]
        # No scratchpad to restore (nothing was produced)
        return thread, remaining

    else:
        # Normal resume: skip completed steps, continue from next pending
        thread = deserialize_thread(checkpoint_data["thread_state"])
        remaining = checkpoint_data["pending_steps"]
        restore_scratchpad(checkpoint_data["scratchpad_state"])
        return thread, remaining
```

Updated schema (add to DB migrations):

```sql
-- Add phase column to checkpoints
ALTER TABLE checkpoints ADD COLUMN phase TEXT DEFAULT 'running';

-- Store plans separately for debugging and resume
CREATE TABLE orchestration_plans (
    orchestration_id TEXT PRIMARY KEY,
    plan_data TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

Orchestrator integration in `src/cap/mcp/orchestrator.py`:

```python
async def orchestrate(task: str, session_id: str, workspace: str, db) -> dict:
    """Main orchestration entry point."""
    # Step 1: Route and plan
    routing = route(task, workspace, db)
    plan = generate_plan(task, routing)
    thread = ContextThread(
        orchestration_id=generate_uuid(),
        task=task,
        tier=routing.tier.value,
    )
    pending_steps = [f"{s['agent_type']}:{s['task_id']}" for s in plan["steps"]]

    # Step 2: SAVE INITIAL CHECKPOINT (NEW — before any agent runs)
    save_initial_checkpoint(
        orchestration_id=thread.orchestration_id,
        plan=plan,
        pending_steps=pending_steps,
        thread=thread,
        db=db,
    )

    # Step 3: Execute steps (existing dispatch loop)
    completed_steps = []
    rollback_mgr = RollbackManager(workspace, thread.orchestration_id)
    rollback_mgr.begin(session_id, db)

    for step in plan["steps"]:
        step_id = f"{step['agent_type']}:{step['task_id']}"
        result = await execute_with_failure_handling(
            step["agent_type"], step["task"], thread, db
        )
        if result["status"] == "success":
            completed_steps.append(step_id)
            pending_steps.remove(step_id)
            # Save incremental checkpoint (existing behavior)
            save_checkpoint(thread, completed_steps, pending_steps, db)
        else:
            # Handle failure...
            break

    # ... rest of orchestration (review loop, security veto, etc.)
```

**Component modified:** `src/cap/orchestration/checkpoint.py`, `src/cap/mcp/orchestrator.py`, DB schema

**Integration:** `save_initial_checkpoint()` is called in the orchestrator's main flow immediately after `route()` + `generate_plan()` complete and before the dispatch loop begins. This means even if the process is killed between planning and first dispatch, `cap resume <id>` will find a valid checkpoint in "planned" phase and replay the full plan. The `orchestration_plans` table also provides debugging visibility into what was planned vs. what executed. The existing `cap resume` CLI command works unchanged — `resume_from_checkpoint()` handles both phases transparently.

---

*End of Design Patches. These 5 fixes close all blocking gaps identified in Validation Round 1. Each is scoped to modify existing components with minimal cross-cutting impact.*

---

## 16. Advanced Reliability: Consensus, Health Monitoring, and Failure Management

### A. Consensus Protocol

**Problem:** Simple 2/3 quorum is insufficient when agents have domain-specific authority (security on vulnerabilities, devops on infra). Need structured disagreement resolution.

#### Disagreement Detection

```python
# src/cap/orchestration/consensus.py

DOMAIN_WEIGHTS = {
    # agent_type → {domain: weight}
    "security": {"security": 0.95, "iam": 0.9, "secrets": 0.95, "compliance": 0.85, "code_quality": 0.3},
    "code-review": {"code_quality": 0.9, "correctness": 0.85, "style": 0.8, "security": 0.4},
    "devops": {"infrastructure": 0.9, "deployment": 0.9, "networking": 0.85, "security": 0.3},
    "sre": {"reliability": 0.9, "observability": 0.85, "performance": 0.8, "deployment": 0.6},
    "aws-architect": {"aws": 0.95, "cost": 0.85, "infrastructure": 0.7, "security": 0.5},
}

def detect_disagreement(outputs: list[dict]) -> list[Disagreement]:
    """Compare structured agent outputs for conflicts."""
    disagreements = []
    for i, a in enumerate(outputs):
        for j, b in enumerate(outputs[i+1:], i+1):
            # Check verdict conflicts (approve vs reject)
            if a.get("verdict") and b.get("verdict"):
                if a["verdict"] != b["verdict"]:
                    disagreements.append(Disagreement(
                        agent_a=a["agent_type"], agent_b=b["agent_type"],
                        field="verdict", value_a=a["verdict"], value_b=b["verdict"],
                        domain=classify_domain(a, b),
                    ))
            # Check severity conflicts (>2 levels apart)
            if a.get("severity") and b.get("severity"):
                LEVELS = {"low": 1, "medium": 2, "high": 3, "critical": 4}
                if abs(LEVELS.get(a["severity"], 0) - LEVELS.get(b["severity"], 0)) >= 2:
                    disagreements.append(Disagreement(
                        agent_a=a["agent_type"], agent_b=b["agent_type"],
                        field="severity", value_a=a["severity"], value_b=b["severity"],
                        domain=classify_domain(a, b),
                    ))
    return disagreements

def classify_domain(a: dict, b: dict) -> str:
    """Infer disagreement domain from agent types and content."""
    keywords = " ".join([a.get("reasoning", ""), b.get("reasoning", "")])
    DOMAIN_SIGNALS = {
        "security": ["vulnerability", "injection", "auth", "credential", "CVE", "OWASP"],
        "infrastructure": ["terraform", "kubernetes", "helm", "deployment", "scaling"],
        "code_quality": ["refactor", "pattern", "naming", "complexity", "duplication"],
        "reliability": ["timeout", "retry", "circuit", "fallback", "SLO"],
        "cost": ["expensive", "budget", "right-size", "reserved", "spot"],
    }
    scores = {}
    for domain, signals in DOMAIN_SIGNALS.items():
        scores[domain] = sum(1 for s in signals if s.lower() in keywords.lower())
    return max(scores, key=scores.get) if any(scores.values()) else "general"
```

#### Resolution Cascade

```python
def resolve_disagreement(d: Disagreement, db) -> Resolution:
    """
    Resolution order:
    1. Domain authority (weighted vote)
    2. Security veto (absolute on security domain)
    3. Judge agent (both arguments presented)
    4. PO escalation (surface to user)
    """
    # Step 1: Security veto — absolute on security domain
    if d.domain == "security" and d.agent_a == "security":
        return Resolution(winner=d.agent_a, method="security_veto", confidence=0.95)
    if d.domain == "security" and d.agent_b == "security":
        return Resolution(winner=d.agent_b, method="security_veto", confidence=0.95)

    # Step 2: Domain authority — highest weight wins
    weight_a = DOMAIN_WEIGHTS.get(d.agent_a, {}).get(d.domain, 0.5)
    weight_b = DOMAIN_WEIGHTS.get(d.agent_b, {}).get(d.domain, 0.5)
    margin = abs(weight_a - weight_b)

    if margin >= 0.3:  # Clear authority
        winner = d.agent_a if weight_a > weight_b else d.agent_b
        return Resolution(winner=winner, method="domain_authority", confidence=margin)

    # Step 3: Check historical outcomes for this pattern
    past = db.execute("""
        SELECT winner, COUNT(*) as cnt FROM disagreement_resolutions
        WHERE domain = ? AND method != 'po_escalation'
        GROUP BY winner ORDER BY cnt DESC LIMIT 1
    """, (d.domain,)).fetchone()
    if past and past[1] >= 3:
        return Resolution(winner=past[0], method="learned_precedent", confidence=0.7)

    # Step 4: Judge agent (spawn with both arguments)
    return Resolution(winner=None, method="judge_needed", confidence=0.0)

async def spawn_judge(d: Disagreement, orchestrator, db) -> Resolution:
    """Spawn a judge agent with both arguments. Returns resolution."""
    judge_prompt = (
        f"Two agents disagree on domain '{d.domain}'.\n\n"
        f"AGENT A ({d.agent_a}) says {d.field} = '{d.value_a}'\n"
        f"AGENT B ({d.agent_b}) says {d.field} = '{d.value_b}'\n\n"
        f"Analyze both positions. Which is correct and why? "
        f"If you cannot determine a winner, say 'ESCALATE'."
    )
    result = await orchestrator.dispatch_agent(
        step={"description": judge_prompt, "agent_type": "code-review"},
        workflow_id=orchestrator.current_workflow_id, db=db,
    )
    verdict_text = result.get("summary", "").lower()
    if "escalate" in verdict_text:
        return Resolution(winner=None, method="po_escalation", confidence=0.0)
    # Determine winner from judge verdict
    winner = d.agent_a if d.agent_a.lower() in verdict_text else d.agent_b
    return Resolution(winner=winner, method="judge", confidence=0.75)
```

#### Severity Classification

| Severity | Criteria | Action |
|----------|----------|--------|
| Advisory | Style, naming, minor refactoring preferences | Log, continue with majority |
| Blocking | Security findings, correctness bugs, compliance | Must resolve before delivery |
| Escalate | Equal authority, no precedent, high stakes | Surface to PO |

#### Schema

```sql
CREATE TABLE disagreement_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    agent_a TEXT NOT NULL,
    agent_b TEXT NOT NULL,
    field TEXT NOT NULL,
    value_a TEXT,
    value_b TEXT,
    winner TEXT,
    method TEXT NOT NULL,  -- security_veto|domain_authority|learned_precedent|judge|po_escalation
    confidence REAL,
    outcome TEXT,  -- good|bad|neutral (filled by learning engine later)
    timestamp REAL NOT NULL
);
CREATE INDEX idx_disagree_domain ON disagreement_resolutions(domain);
```

---

### B. Agent Health Monitoring

**Constraints:** Agents are Agent() tool calls. We CAN observe: start time, end time, PostToolUse events (tool calls made by the agent). We CANNOT inject heartbeats or interrupt mid-execution.

#### Monitoring Architecture

```python
# src/cap/health/monitor.py

class AgentHealthMonitor:
    """Infers agent health from observable tool-call patterns."""

    TOOL_CALL_INTERVAL_HEALTHY = 30   # seconds — expect a tool call at least every 30s
    STALL_THRESHOLD = 90              # seconds — no tool call for 90s = stalled
    CONTEXT_WARN_TOKENS = 150000     # estimated tokens before context window risk
    
    AGENT_TIMEOUTS = {
        "dev": 300, "devops": 240, "security": 180, "code-review": 120,
        "sre": 180, "test": 240, "optimization": 180, "docs": 120,
        "cicd": 180, "aws-architect": 240, "Explore": 60, "system": 120,
    }

    def infer_health(self, agent_id: str, db) -> HealthState:
        """Determine agent health from observable signals."""
        events = db.execute("""
            SELECT event_type, timestamp, estimated_tokens
            FROM agent_health_events
            WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 20
        """, (agent_id,)).fetchall()

        if not events:
            return HealthState.UNKNOWN

        last_event_time = events[0][1]
        elapsed_since_last = time.time() - last_event_time
        total_tokens = sum(e[2] for e in events if e[2])
        
        # Check for stall
        if elapsed_since_last > self.STALL_THRESHOLD:
            return HealthState.STALLED
        
        # Check for degraded (slowing down)
        if len(events) >= 3:
            intervals = [events[i][1] - events[i+1][1] for i in range(min(5, len(events)-1))]
            avg_interval = sum(intervals) / len(intervals)
            if avg_interval > self.TOOL_CALL_INTERVAL_HEALTHY * 2:
                return HealthState.DEGRADED

        # Check context window approaching limit
        if total_tokens > self.CONTEXT_WARN_TOKENS:
            return HealthState.APPROACHING_LIMIT
        
        return HealthState.HEALTHY

    def estimate_tokens(self, tool_input: dict, tool_output: str) -> int:
        """Rough token estimate: 4 chars ≈ 1 token."""
        input_size = len(json.dumps(tool_input))
        output_size = len(tool_output) if tool_output else 0
        return (input_size + output_size) // 4
```

#### PostToolUse Hook Integration

```python
# In src/cap/hooks/posttool.py — fires after every tool call by any agent

def record_agent_tool_call(input_data: dict, db):
    """Record tool call for health monitoring. Called by PostToolUse hook."""
    agent_id = input_data.get("agent_id")
    if not agent_id:
        return
    
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", "")
    
    estimated_tokens = (len(json.dumps(tool_input)) + len(str(tool_output))) // 4
    
    db.execute("""
        INSERT INTO agent_health_events (agent_id, event_type, tool_name, timestamp, estimated_tokens)
        VALUES (?, 'tool_call', ?, ?, ?)
    """, (agent_id, tool_name, time.time(), estimated_tokens))
    db.commit()
```

#### Health States and Recovery Actions

| State | Condition | Recovery |
|-------|-----------|----------|
| HEALTHY | Tool calls every <30s, tokens <150K | None |
| DEGRADED | Intervals doubling, or tokens 150-180K | Log warning, prepare fallback |
| STALLED | No tool call for 90s+ | Wait until timeout, then fail → retry path |
| APPROACHING_LIMIT | Estimated >180K tokens | Mark for shorter context on retry |
| FAILED | Timeout expired or error returned | Trigger circuit breaker + rollback |

#### Schema

```sql
CREATE TABLE agent_health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- tool_call|started|completed|failed|timeout
    tool_name TEXT,
    timestamp REAL NOT NULL,
    estimated_tokens INTEGER DEFAULT 0
);
CREATE INDEX idx_health_agent ON agent_health_events(agent_id, timestamp DESC);

CREATE TABLE agent_health_baselines (
    agent_type TEXT NOT NULL,
    avg_duration REAL,
    avg_tool_calls INTEGER,
    avg_tokens INTEGER,
    p95_duration REAL,
    failure_rate REAL,
    sample_count INTEGER,
    updated_at REAL,
    PRIMARY KEY (agent_type)
);
```

#### Predictive Health (pre-dispatch risk scoring)

```python
def predict_failure_risk(task_desc: str, agent_type: str, db) -> float:
    """Score 0-1: likelihood this task will fail for this agent type."""
    baseline = db.execute(
        "SELECT failure_rate, avg_duration, p95_duration FROM agent_health_baselines WHERE agent_type = ?",
        (agent_type,)
    ).fetchone()
    
    if not baseline:
        return 0.1  # Unknown = assume low risk
    
    risk = baseline[0]  # Base failure rate
    
    # Check if similar tasks have failed before
    past_failures = db.execute("""
        SELECT COUNT(*) FROM routing_decisions
        WHERE agent_type = ? AND outcome = 'failed'
        AND task_description LIKE ?
    """, (agent_type, f"%{task_desc[:50]}%")).fetchone()[0]
    
    if past_failures >= 2:
        risk = min(risk + 0.3, 0.9)
    
    return risk
```

#### `cap health` CLI Output

```
$ cap health

Agent Health (last 5 minutes):
┌─────────────┬──────────┬───────────┬──────────┬────────────┐
│ Agent Type  │ State    │ Fail Rate │ Avg Time │ Circuit    │
├─────────────┼──────────┼───────────┼──────────┼────────────┤
│ dev         │ HEALTHY  │ 4.2%      │ 89s      │ CLOSED     │
│ code-review │ HEALTHY  │ 1.1%      │ 34s      │ CLOSED     │
│ security    │ DEGRADED │ 12.0%     │ 156s     │ HALF_OPEN  │
│ devops      │ HEALTHY  │ 3.5%      │ 72s      │ CLOSED     │
└─────────────┴──────────┴───────────┴──────────┴────────────┘

Dead Letter Queue: 2 tasks (run `cap dlq` to review)
Last cascade event: none
Disk usage: 142MB / 256MB
```

---

### C. Advanced Failure Management

#### C.1 Circuit Breaker

```python
# src/cap/reliability/circuit_breaker.py

class CircuitBreaker:
    """Per-agent-type circuit breaker. Prevents sending to a failing agent type."""
    
    FAILURE_THRESHOLD = 3      # failures to trip
    WINDOW_SECONDS = 300       # 5-minute sliding window
    COOLDOWN_SECONDS = 120     # 2 minutes in OPEN before HALF_OPEN
    SUCCESS_TO_CLOSE = 1       # successes in HALF_OPEN to close

    def __init__(self, agent_type: str, db):
        self.agent_type = agent_type
        self.db = db

    def get_state(self) -> str:
        """CLOSED | OPEN | HALF_OPEN"""
        row = self.db.execute("""
            SELECT state, opened_at FROM circuit_breaker_state WHERE agent_type = ?
        """, (self.agent_type,)).fetchone()
        
        if not row or row[0] == "CLOSED":
            # Check if we should trip
            failures = self.db.execute("""
                SELECT COUNT(*) FROM agent_health_events
                WHERE agent_id LIKE ? AND event_type = 'failed'
                AND timestamp > ?
            """, (f"{self.agent_type}%", time.time() - self.WINDOW_SECONDS)).fetchone()[0]
            
            if failures >= self.FAILURE_THRESHOLD:
                self._transition("OPEN")
                return "OPEN"
            return "CLOSED"
        
        if row[0] == "OPEN":
            if time.time() - row[1] > self.COOLDOWN_SECONDS:
                self._transition("HALF_OPEN")
                return "HALF_OPEN"
            return "OPEN"
        
        return row[0]  # HALF_OPEN

    def record_success(self):
        state = self.get_state()
        if state == "HALF_OPEN":
            self._transition("CLOSED")

    def record_failure(self):
        state = self.get_state()
        if state == "HALF_OPEN":
            self._transition("OPEN")  # Back to OPEN

    def can_dispatch(self) -> tuple[bool, str]:
        state = self.get_state()
        if state == "CLOSED":
            return True, ""
        if state == "HALF_OPEN":
            return True, "circuit_half_open"  # Allow one probe
        return False, f"Circuit OPEN for {self.agent_type}: too many recent failures"

    def _transition(self, new_state: str):
        self.db.execute("""
            INSERT OR REPLACE INTO circuit_breaker_state (agent_type, state, opened_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (self.agent_type, new_state, time.time(), time.time()))
        self.db.commit()
```

#### C.2 Dead-Letter Queue

```python
# src/cap/reliability/dlq.py

def enqueue_dead_letter(db, task: dict, failures: list[dict], workflow_id: str):
    """Task exhausted all retries. Goes to DLQ for user review."""
    db.execute("""
        INSERT INTO dead_letter_queue
        (task_id, workflow_id, task_description, failures_json, agent_type, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        task["id"], workflow_id, task["description"],
        json.dumps(failures), task["agent_type"],
        time.time(), time.time() + 7 * 86400  # 7 day expiry
    ))
    db.commit()

def list_dlq(db) -> list[dict]:
    """For cap dlq command."""
    return db.execute("""
        SELECT task_id, task_description, agent_type, created_at, 
               json_extract(failures_json, '$[#-1].error') as last_error
        FROM dead_letter_queue
        WHERE expires_at > ? ORDER BY created_at DESC
    """, (time.time(),)).fetchall()
```

**`cap dlq` output:**
```
$ cap dlq

Dead Letter Queue (2 tasks):
┌────┬──────────────────────────────────────────┬───────────┬────────────────────┐
│ #  │ Task                                     │ Agent     │ Last Error         │
├────┼──────────────────────────────────────────┼───────────┼────────────────────┤
│ 1  │ Implement Redis caching for user-svc     │ dev       │ API timeout (3/3)  │
│ 2  │ Update IRSA policy for payment-svc       │ security  │ Context overflow   │
└────┴──────────────────────────────────────────┴───────────┴────────────────────┘

Actions: cap dlq retry 1 | cap dlq dismiss 2 | cap dlq retry-all
```

#### C.3 Cascade Detection

```python
# src/cap/reliability/cascade.py

CASCADE_WINDOW = 10       # seconds
CASCADE_THRESHOLD = 3     # failures in window = cascade

def detect_cascade(db) -> bool:
    """3+ agent failures within 10s = likely systemic issue."""
    recent_failures = db.execute("""
        SELECT COUNT(DISTINCT agent_id) FROM agent_health_events
        WHERE event_type = 'failed' AND timestamp > ?
    """, (time.time() - CASCADE_WINDOW,)).fetchone()[0]
    return recent_failures >= CASCADE_THRESHOLD

def handle_cascade(db, workflow_id: str) -> str:
    """Pause all dispatches, notify user."""
    # Pause workflow
    db.execute("""
        UPDATE orchestration_checkpoints SET phase = 'paused_cascade'
        WHERE workflow_id = ? AND phase = 'running'
    """, (workflow_id,))
    db.commit()
    
    return (
        "⚠️ CASCADE DETECTED: Multiple agents failing simultaneously.\n"
        "Likely cause: API rate limit or service outage.\n"
        "Workflow paused. Run `cap resume` when ready, or `cap dlq` to review failures."
    )
```

#### C.4 Adaptive Failure Routing

```python
# src/cap/reliability/adaptive.py

def adjust_timeout(agent_type: str, db) -> int:
    """Dynamic timeout based on historical p95."""
    baseline = db.execute(
        "SELECT p95_duration FROM agent_health_baselines WHERE agent_type = ?",
        (agent_type,)
    ).fetchone()
    
    if baseline and baseline[0]:
        # Set timeout to 1.5x p95, minimum 60s, maximum 600s
        return max(60, min(600, int(baseline[0] * 1.5)))
    
    # Default timeouts
    return AgentHealthMonitor.AGENT_TIMEOUTS.get(agent_type, 300)

def get_failure_pattern(agent_type: str, db) -> dict:
    """Detect if failures cluster by time (API) or task type (capability)."""
    failures = db.execute("""
        SELECT timestamp, task_description FROM routing_decisions
        WHERE agent_type = ? AND outcome = 'failed'
        AND timestamp > ? ORDER BY timestamp DESC LIMIT 20
    """, (agent_type, time.time() - 86400)).fetchall()
    
    if len(failures) < 3:
        return {"pattern": "none"}
    
    # Time clustering: are failures bunched together?
    timestamps = [f[0] for f in failures]
    gaps = [timestamps[i] - timestamps[i+1] for i in range(len(timestamps)-1)]
    avg_gap = sum(gaps) / len(gaps) if gaps else float('inf')
    
    if avg_gap < 60:  # Failures <1 min apart = API/systemic
        return {"pattern": "temporal", "likely_cause": "api_issue", "action": "backoff_all"}
    
    # Task clustering: do descriptions share keywords?
    from collections import Counter
    words = Counter()
    for f in failures:
        words.update(f[1].lower().split())
    common = words.most_common(3)
    if common and common[0][1] >= len(failures) * 0.6:
        return {"pattern": "task_type", "likely_cause": f"capability_gap_{common[0][0]}", "action": "reroute"}
    
    return {"pattern": "random", "action": "normal_retry"}
```

#### C.5 Partial Result Salvage

```python
def salvage_partial_results(workflow_id: str, db) -> dict:
    """When a workflow fails mid-execution, collect completed work."""
    checkpoint = db.execute("""
        SELECT completed_steps, pending_steps, context_thread
        FROM orchestration_checkpoints WHERE workflow_id = ?
    """, (workflow_id,)).fetchone()
    
    if not checkpoint:
        return {"salvageable": False}
    
    completed = json.loads(checkpoint[0])
    pending = json.loads(checkpoint[1])
    
    return {
        "salvageable": len(completed) > 0,
        "completed_count": len(completed),
        "total_count": len(completed) + len(pending),
        "completed_steps": completed,
        "failed_step": pending[0] if pending else None,
        "remaining_steps": pending[1:] if len(pending) > 1 else [],
        "message": f"{len(completed)}/{len(completed)+len(pending)} steps complete. "
                   f"Failed at: {pending[0]['description'] if pending else 'unknown'}. "
                   f"Deliver partial results? [Y/n]"
    }
```

#### Schemas

```sql
CREATE TABLE circuit_breaker_state (
    agent_type TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'CLOSED',  -- CLOSED|OPEN|HALF_OPEN
    opened_at REAL,
    updated_at REAL NOT NULL,
    failure_count INTEGER DEFAULT 0
);

CREATE TABLE dead_letter_queue (
    task_id TEXT PRIMARY KEY,
    workflow_id TEXT,
    task_description TEXT NOT NULL,
    failures_json TEXT NOT NULL,
    agent_type TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT DEFAULT 'pending'  -- pending|retried|dismissed
);
CREATE INDEX idx_dlq_expires ON dead_letter_queue(expires_at);

CREATE TABLE cascade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    detected_at REAL NOT NULL,
    failure_count INTEGER,
    agent_types TEXT,  -- JSON array of affected types
    resolution TEXT,   -- paused|resumed|aborted
    resolved_at REAL
);
```

#### Integration with Orchestrator

The orchestrator dispatch loop becomes:

```python
async def dispatch_agent(self, step, workflow_id, db):
    agent_type = step["agent_type"]
    
    # 1. Check circuit breaker
    cb = CircuitBreaker(agent_type, db)
    can_dispatch, reason = cb.can_dispatch()
    if not can_dispatch:
        return {"status": "circuit_open", "reason": reason}
    
    # 2. Predictive health check
    risk = predict_failure_risk(step["description"], agent_type, db)
    if risk > 0.7:
        # High risk — adjust: more context, longer timeout, or different agent
        step["timeout"] = adjust_timeout(agent_type, db) * 1.5
        log(f"High failure risk ({risk:.0%}) for {agent_type}, extended timeout")
    
    # 3. Dispatch with adaptive timeout
    timeout = adjust_timeout(agent_type, db)
    try:
        result = await asyncio.wait_for(run_agent(step), timeout=timeout)
        cb.record_success()
        return result
    except asyncio.TimeoutError:
        cb.record_failure()
        # Check for cascade
        if detect_cascade(db):
            return {"status": "cascade", "message": handle_cascade(db, workflow_id)}
        # Normal failure path
        return {"status": "timeout", "agent_type": agent_type}
    except Exception as e:
        cb.record_failure()
        if detect_cascade(db):
            return {"status": "cascade", "message": handle_cascade(db, workflow_id)}
        return {"status": "failed", "error": str(e)}
```

---

## 17. Witness Manifest (Cryptographic Review Attestation)

**Purpose:** Cryptographic proof that reviewed code has not been modified after review. Prevents regression where a reviewed file is quietly changed post-approval.

### How It Works

1. Code-review agent reviews files → produces findings
2. If review PASSES → orchestrator computes SHA-256 of each reviewed file
3. Stamps stored in `witness_manifests` table with reviewer identity + timestamp
4. Pre-push hook (enforcement) verifies: every file being pushed was either (a) witnessed at its current hash, or (b) not yet reviewed (new file — allowed, but flagged)
5. If a witnessed file's current hash ≠ stored hash → **BLOCK push** with message: "File X was modified after review. Re-run review or `cap witness --force`"

### Implementation

```python
# src/cap/integrity/witness.py

import hashlib
import os
import time
import sqlite3
import json

class WitnessManifest:
    """Cryptographic attestation that reviewed files remain unmodified."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def stamp(self, file_paths: list[str], reviewer: str, workflow_id: str) -> dict:
        """Create witness stamps for reviewed files."""
        manifest = {
            "workflow_id": workflow_id,
            "reviewer": reviewer,
            "timestamp": time.time(),
            "files": {},
        }
        for path in file_paths:
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()
            manifest["files"][path] = content_hash
            self.db.execute("""
                INSERT OR REPLACE INTO witness_manifests
                (file_path, content_hash, reviewer, workflow_id, stamped_at, verified_at)
                VALUES (?, ?, ?, ?, ?, NULL)
            """, (path, content_hash, reviewer, workflow_id, time.time()))
        self.db.commit()
        return manifest

    def verify(self, file_paths: list[str]) -> dict:
        """Verify files match their witness stamps. Called by pre-push hook."""
        results = {"passed": [], "failed": [], "unreviewed": []}
        for path in file_paths:
            row = self.db.execute("""
                SELECT content_hash, reviewer, stamped_at FROM witness_manifests
                WHERE file_path = ? ORDER BY stamped_at DESC LIMIT 1
            """, (path,)).fetchone()

            if not row:
                results["unreviewed"].append(path)
                continue

            if not os.path.exists(path):
                results["failed"].append({"path": path, "reason": "file_deleted_after_review"})
                continue

            with open(path, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()

            if current_hash == row[0]:
                results["passed"].append(path)
                self.db.execute("""
                    UPDATE witness_manifests SET verified_at = ? WHERE file_path = ? AND content_hash = ?
                """, (time.time(), path, row[0]))
            else:
                results["failed"].append({
                    "path": path,
                    "reason": "modified_after_review",
                    "reviewed_by": row[1],
                    "reviewed_at": row[2],
                    "expected_hash": row[0][:12] + "...",
                    "actual_hash": current_hash[:12] + "...",
                })
        self.db.commit()
        return results

    def invalidate(self, file_path: str):
        """Invalidate witness when file is intentionally re-edited."""
        self.db.execute(
            "DELETE FROM witness_manifests WHERE file_path = ?", (file_path,)
        )
        self.db.commit()
```

### Pre-Push Enforcement Hook

```python
# Added to src/cap/hooks/prepush.py (new hook on Bash tool matching git push)

def verify_witness_before_push(db):
    """Block push if any witnessed files were modified post-review."""
    import subprocess
    # Get files being pushed
    diff = subprocess.run(
        ["git", "diff", "--name-only", "origin/HEAD..HEAD"],
        capture_output=True, text=True
    )
    changed_files = [f.strip() for f in diff.stdout.strip().split("\n") if f.strip()]
    abs_paths = [os.path.join(os.getcwd(), f) for f in changed_files]

    witness = WitnessManifest(db)
    results = witness.verify(abs_paths)

    if results["failed"]:
        msg = "BLOCKED: Files modified after review:\n"
        for f in results["failed"]:
            msg += f"  ❌ {f['path']} — {f['reason']} (reviewed by {f.get('reviewed_by', '?')})\n"
        msg += "\nRe-run review with these files, or force with: cap witness --accept-risk\n"
        print(msg, file=sys.stderr)
        sys.exit(2)

    if results["unreviewed"]:
        # Warn but don't block — new files that were never reviewed
        msg = "⚠️  Unreviewed files in push:\n"
        for f in results["unreviewed"]:
            msg += f"  ⚠️  {os.path.basename(f)}\n"
        print(msg, file=sys.stderr)
        # Don't exit 2 — allow push of new files

    sys.exit(0)
```

### Integration with Review Loop

```python
# In orchestrator, after code-review agent passes:

async def post_review_stamp(self, review_result, reviewed_files, workflow_id, db):
    """Stamp files after successful review."""
    if review_result.get("verdict") == "approved":
        witness = WitnessManifest(db)
        manifest = witness.stamp(
            file_paths=reviewed_files,
            reviewer=review_result.get("agent_type", "code-review"),
            workflow_id=workflow_id,
        )
        log(f"Witness manifest: {len(manifest['files'])} files stamped")
    # If review found issues → no stamp → files will need re-review after fix
```

### Invalidation on Re-Edit

```python
# In posttool.py — when a file is edited after being witnessed, invalidate its stamp

def on_file_edit(file_path: str, db):
    """Invalidate witness stamp when file is re-edited."""
    witness = WitnessManifest(db)
    witness.invalidate(file_path)
```

### Schema

```sql
CREATE TABLE witness_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,  -- SHA-256 hex
    reviewer TEXT NOT NULL,      -- agent type that reviewed
    workflow_id TEXT NOT NULL,
    stamped_at REAL NOT NULL,
    verified_at REAL,            -- set when verified during push
    UNIQUE(file_path, content_hash)
);
CREATE INDEX idx_witness_path ON witness_manifests(file_path);
CREATE INDEX idx_witness_workflow ON witness_manifests(workflow_id);
```

### `cap witness` CLI

```
$ cap witness status
Witness Manifest Status:
┌──────────────────────────────────┬──────────┬─────────────────┬──────────┐
│ File                             │ Hash     │ Reviewed By     │ Status   │
├──────────────────────────────────┼──────────┼─────────────────┼──────────┤
│ src/cap/mcp/orchestrator.py      │ a3f2..   │ code-review     │ ✅ valid │
│ src/cap/hooks/pretool.py         │ 7b1e..   │ security        │ ✅ valid │
│ src/cap/db.py                    │ c9d4..   │ code-review     │ ❌ stale │
└──────────────────────────────────┴──────────┴─────────────────┴──────────┘

$ cap witness --accept-risk
⚠️  Force-accepting stale witnesses. Push will proceed without re-review.
Logged to audit trail.
```

---

## 18. DAG-Based Task Decomposition with Parallel Execution

**Purpose:** Tasks with independent subtasks should execute in parallel, not sequentially. A 5-step task where steps 2, 3, 4 are independent shouldn't take 3x the time of the slowest.

### Task DAG Structure

```python
# src/cap/orchestration/dag.py

from dataclasses import dataclass, field
from enum import Enum

class StepState(Enum):
    PENDING = "pending"
    READY = "ready"       # All dependencies satisfied
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class TaskStep:
    id: str
    description: str
    agent_type: str
    depends_on: list[str] = field(default_factory=list)  # IDs of prerequisite steps
    state: StepState = StepState.PENDING
    result: dict = None
    affected_files: list[str] = field(default_factory=list)

@dataclass
class TaskDAG:
    steps: dict[str, TaskStep]  # id → step
    
    def get_ready_steps(self) -> list[TaskStep]:
        """Return steps whose dependencies are all completed."""
        ready = []
        for step in self.steps.values():
            if step.state != StepState.PENDING:
                continue
            deps_met = all(
                self.steps[dep_id].state == StepState.COMPLETED
                for dep_id in step.depends_on
                if dep_id in self.steps
            )
            if deps_met:
                step.state = StepState.READY
                ready.append(step)
        return ready

    def detect_cycle(self) -> list[str]:
        """Detect cycles using DFS. Returns cycle path if found, else empty."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {sid: WHITE for sid in self.steps}
        path = []

        def dfs(node_id):
            color[node_id] = GRAY
            path.append(node_id)
            for dep_id in self.steps[node_id].depends_on:
                if dep_id not in self.steps:
                    continue
                if color[dep_id] == GRAY:
                    cycle_start = path.index(dep_id)
                    return path[cycle_start:]
                if color[dep_id] == WHITE:
                    result = dfs(dep_id)
                    if result:
                        return result
            color[node_id] = BLACK
            path.pop()
            return None

        for sid in self.steps:
            if color[sid] == WHITE:
                cycle = dfs(sid)
                if cycle:
                    return cycle
        return []

    def critical_path(self) -> list[str]:
        """Longest dependency chain = minimum completion time."""
        memo = {}
        def longest(sid):
            if sid in memo:
                return memo[sid]
            step = self.steps[sid]
            if not step.depends_on:
                memo[sid] = [sid]
                return [sid]
            best = []
            for dep_id in step.depends_on:
                if dep_id in self.steps:
                    chain = longest(dep_id)
                    if len(chain) > len(best):
                        best = chain
            memo[sid] = best + [sid]
            return memo[sid]
        
        all_chains = [longest(sid) for sid in self.steps]
        return max(all_chains, key=len) if all_chains else []
```

### Plan Generation (orchestrator produces DAG, not list)

```python
# src/cap/orchestration/planner.py

def generate_plan(task_description: str, context: dict, db) -> TaskDAG:
    """
    Orchestrator generates a DAG of steps with explicit dependencies.
    The orchestrator LLM is prompted to output steps with depends_on fields.
    """
    # The orchestrator agent's structured output schema includes:
    PLAN_SCHEMA = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "agent_type": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "affected_files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "description", "agent_type", "depends_on"]
                }
            }
        }
    }
    
    # Orchestrator prompt instructs:
    # "Output steps with depends_on listing step IDs that MUST complete before this step starts.
    #  Steps with no dependencies (depends_on: []) can run in parallel.
    #  Example: security review depends_on: [implementation] because it needs code to review."
    
    plan_output = run_orchestrator_planning(task_description, context, PLAN_SCHEMA)
    
    dag = TaskDAG(steps={})
    for s in plan_output["steps"]:
        dag.steps[s["id"]] = TaskStep(
            id=s["id"],
            description=s["description"],
            agent_type=s["agent_type"],
            depends_on=s.get("depends_on", []),
            affected_files=s.get("affected_files", []),
        )
    
    # Validate: check for cycles
    cycle = dag.detect_cycle()
    if cycle:
        # Break cycle by removing last edge and logging warning
        last_step = dag.steps[cycle[-1]]
        last_step.depends_on = [d for d in last_step.depends_on if d != cycle[0]]
        log(f"⚠️ Cycle detected in plan ({' → '.join(cycle)}), edge removed")
    
    return dag
```

### Parallel DAG Executor

```python
# src/cap/orchestration/executor.py

import asyncio

class DAGExecutor:
    """Execute task DAG with maximum parallelism while respecting dependencies."""

    def __init__(self, dag: TaskDAG, orchestrator, db):
        self.dag = dag
        self.orchestrator = orchestrator
        self.db = db
        self.results = {}

    async def execute(self) -> dict:
        """Run DAG to completion. Returns all step results."""
        while True:
            ready = self.dag.get_ready_steps()
            if not ready:
                # Check if we're done or stuck
                pending = [s for s in self.dag.steps.values() if s.state in (StepState.PENDING, StepState.READY)]
                if not pending:
                    break  # All done
                # Steps waiting on failed dependencies
                for s in pending:
                    failed_deps = [d for d in s.depends_on if self.dag.steps.get(d, TaskStep(id="", description="", agent_type="")).state == StepState.FAILED]
                    if failed_deps:
                        s.state = StepState.SKIPPED
                continue

            # Dispatch all ready steps in parallel
            tasks = []
            for step in ready:
                step.state = StepState.RUNNING
                # Collect context from completed dependencies
                dep_context = {
                    dep_id: self.results[dep_id]
                    for dep_id in step.depends_on
                    if dep_id in self.results
                }
                tasks.append(self._run_step(step, dep_context))

            # Await all parallel steps
            await asyncio.gather(*tasks)

        return self.results

    async def _run_step(self, step: TaskStep, dep_context: dict):
        """Execute a single step with its dependency context."""
        try:
            # Build context frame from dependencies
            context_frames = []
            for dep_id, dep_result in dep_context.items():
                dep_step = self.dag.steps[dep_id]
                context_frames.append(f"---CONTEXT FROM {dep_step.agent_type} (step: {dep_id})---\n{dep_result.get('summary', '')}\n---END---")

            result = await self.orchestrator.dispatch_agent(
                step={"description": step.description, "agent_type": step.agent_type,
                      "context": "\n".join(context_frames), "affected_files": step.affected_files},
                workflow_id=self.orchestrator.current_workflow_id,
                db=self.db,
            )

            if result.get("status") in ("failed", "timeout", "circuit_open"):
                step.state = StepState.FAILED
                self.results[step.id] = result
            else:
                step.state = StepState.COMPLETED
                self.results[step.id] = result

        except Exception as e:
            step.state = StepState.FAILED
            self.results[step.id] = {"status": "failed", "error": str(e)}
```

### Example: How a 5-repo migration decomposes into DAG

```
Task: "Migrate auth service from EC2 to EKS"

Generated DAG:
┌─────────────┐     ┌──────────────┐
│ tf_plan     │     │ helm_chart   │
│ (devops)    │     │ (devops)     │
└──────┬──────┘     └──────┬───────┘
       │                    │
       └────────┬───────────┘
                │
         ┌──────▼──────┐
         │ argocd_app  │
         │ (cicd)      │
         └──────┬──────┘
                │
    ┌───────────┼───────────┐
    │           │           │
┌───▼───┐  ┌───▼───┐  ┌───▼───┐
│security│  │ sre   │  │ test  │
│review  │  │ SLOs  │  │ smoke │
└───┬────┘  └───┬───┘  └───┬───┘
    │           │           │
    └───────────┼───────────┘
                │
         ┌──────▼──────┐
         │ final_push  │
         │ (orchestr.) │
         └─────────────┘

Steps that run in PARALLEL:
- tf_plan + helm_chart (no dependencies between them)
- security_review + sre_slos + test_smoke (all depend only on argocd_app)

Total time: 4 sequential layers, NOT 7 sequential steps
Speedup: ~40% faster than linear execution
```

### Schema

```sql
CREATE TABLE task_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,     -- Full DAG serialized
    critical_path TEXT,         -- JSON array of step IDs on longest chain
    parallelism_factor REAL,   -- steps / critical_path_length (higher = more parallel)
    created_at REAL NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES orchestration_checkpoints(workflow_id)
);

CREATE TABLE task_steps (
    id TEXT PRIMARY KEY,        -- step ID within the DAG
    workflow_id TEXT NOT NULL,
    description TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    depends_on TEXT,            -- JSON array of step IDs
    state TEXT NOT NULL DEFAULT 'pending',
    started_at REAL,
    completed_at REAL,
    result_json TEXT,
    FOREIGN KEY (workflow_id) REFERENCES orchestration_checkpoints(workflow_id)
);
CREATE INDEX idx_steps_workflow ON task_steps(workflow_id, state);
```

### Integration with Checkpoint/Resume

When checkpoint saves state, it serializes the entire DAG including step states. On resume:
- Steps in COMPLETED state → skip (use cached result)
- Steps in RUNNING state → re-execute (may have partially completed)
- Steps in PENDING/READY state → execute normally

```python
def save_dag_checkpoint(dag: TaskDAG, workflow_id: str, db):
    """Save DAG state for resume."""
    db.execute("""
        INSERT OR REPLACE INTO task_plans (workflow_id, plan_json, critical_path, parallelism_factor, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        workflow_id,
        json.dumps({sid: {"state": s.state.value, "result": s.result} for sid, s in dag.steps.items()}),
        json.dumps(dag.critical_path()),
        len(dag.steps) / max(len(dag.critical_path()), 1),
        time.time(),
    ))
    db.commit()

def resume_dag(workflow_id: str, dag: TaskDAG, db) -> TaskDAG:
    """Restore DAG state from checkpoint."""
    row = db.execute(
        "SELECT plan_json FROM task_plans WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    if not row:
        return dag
    saved = json.loads(row[0])
    for sid, state_data in saved.items():
        if sid in dag.steps:
            dag.steps[sid].state = StepState(state_data["state"])
            dag.steps[sid].result = state_data.get("result")
    return dag
```

---

*End of specification. This document is the single source of truth for CAP implementation. All component paths are relative to `src/cap/`. All state lives in `~/.cap/cap.db`. All enforcement is via exit code 2 in PreToolUse hooks. No exceptions.*
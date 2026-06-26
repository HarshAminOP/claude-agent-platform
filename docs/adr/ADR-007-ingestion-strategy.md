# ADR-007: Incremental Git-Diff Based Ingestion with Per-Repo Atomic Writes

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

The system must index 41+ repositories efficiently. Repos are updated frequently as engineers commit changes. Ingestion must:
- **Speed:** Fast incremental updates after `git pull`
- **Correctness:** No partial updates; crash-safe
- **Concurrency:** Non-blocking (agents aren't stalled waiting for ingestion)
- **Scalability:** ~7s incremental vs ~43s full rescan
- **Observability:** Clear progress reporting

## Decision

**Use git-diff for change detection. Scan changed files in parallel. Extract, infer relationships, and write atomically per-repo to SQLite. Run ingestion in background thread; don't block MCP tool calls.**

**Rationale:**
- **git diff is fast:** Detects only changed files since last index (100ms for 41 repos)
- **Parallel scanning:** All repos scanned concurrently; async I/O
- **Per-repo atomic writes:** Crash leaves clean state (full repo or nothing; no partial)
- **No tool blocking:** Background thread ingestion; agents get response immediately
- **Scalable:** 7s incremental vs 43s full is 6x improvement

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Full rescan every ingest** | Simple, no state tracking | 43s every time; blocks agents on pull; slow UX | Rejected |
| **inotify file watcher (filesystem events)** | Automatic on file changes | Linux-only, complex event debouncing, can miss events under high load | Rejected |
| **Scheduled cron job (hourly indexing)** | Decoupled from user actions | Stale index; agents miss recent changes; missed data windows | Rejected |
| **git-diff with incremental state** | Fast, precise change detection | State tracking adds complexity (this approach) | **Accepted** |
| **Polling last-modified time (mtime)** | Portable cross-OS | Fragile; clocks drift, NFS time skew, broken on some filesystems | Rejected |

## Ingestion Architecture

```
Session Start / Manual Trigger
    │
    ├─> Get list of repos to ingest
    │
    ├─> Parallel: git diff --name-status [last_sha] HEAD for each repo
    │       └─> Detect changed files (A=added, M=modified, D=deleted)
    │
    ├─> Parallel: Security gate + extraction for changed files
    │       └─> For each file: check for secrets, extract entities
    │
    ├─> Sequential Per-Repo: Relationship inference + atomic write
    │       └─> DELETE old entities for this repo
    │       └─> INSERT new entities + relationships in single transaction
    │       └─> UPDATE ingestion_state
    │
    ├─> On success: Reload graph (double-buffered)
    │
    └─> Mark complete; agents can search new data
```

## State Tracking

Persisted in SQLite `ingestion_state` table:

```sql
CREATE TABLE ingestion_state (
    repo_name TEXT PRIMARY KEY,
    last_commit_sha TEXT,
    last_indexed_at TEXT,
    entity_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ready'  -- ready, indexing, failed
);
```

**On startup:**
1. Read `last_commit_sha` for each repo
2. If NULL, assume full index needed
3. Otherwise, run `git diff [last_sha] HEAD`

**After successful indexing:**
1. Run `git rev-parse HEAD` → get current SHA
2. Update `last_commit_sha` and `last_indexed_at`
3. Set status to 'ready'

**If interrupted mid-ingestion:**
1. On restart, detect `status='indexing'` (in-progress flag)
2. Delete incomplete entities for that repo
3. Re-run full index for that repo (clean state)

## Atomicity Guarantees

Per-repo writes are atomic via SQLite transactions:

```python
def write_repo(conn, repo_name, entities, relationships):
    """Atomic: all or nothing."""
    with conn:  # Transaction context; auto-rollback on error
        # Delete old entities (cascade deletes relationships)
        conn.execute("DELETE FROM entities WHERE source_repo = ?", (repo_name,))
        
        # Insert new entities
        conn.executemany("INSERT INTO entities (...) VALUES (...)", entities)
        
        # Insert relationships
        for rel in relationships:
            conn.execute("INSERT INTO relationships (...) VALUES (...)", rel)
        
        # Update ingestion state
        conn.execute("""INSERT OR REPLACE INTO ingestion_state 
                       (repo_name, last_commit_sha, ...) VALUES (...)""")
```

**Guarantees:**
- All-or-nothing per repo: if any INSERT fails, entire repo rollback
- Cascade deletes: old relationships auto-deleted with entities
- No partial state: agents never see half-indexed repo

## Performance Budget

| Phase | Full Rescan (42 repos) | Incremental (~50 files) |
|-------|------------------------|------------------------|
| git diff (parallel) | ~500ms | ~200ms |
| Security gate | ~2s | ~500ms |
| Extract + enrich (parallel) | ~25s | ~3s |
| Relationship inference | ~10s | ~2s |
| Write to SQLite | ~5s | ~1s |
| Graph reload | ~200ms | ~200ms |
| **TOTAL** | **~43s** | **~7s** |

**Incremental savings:** 6x faster; acceptable for post-pull ingestion.

## Non-Blocking Ingestion

Ingestion runs in background thread; MCP tool calls don't wait:

```python
class IngestionManager:
    def start_ingestion(self, repos=None, mode="incremental"):
        """Start background job. Return immediately."""
        with self._lock:
            if self._running:
                return {"status": "already_running", "job": self._current_job}
            
            self._running = True
            self._current_job = {"job_id": "ing-20260625-143000", ...}
        
        # Spawn background thread
        thread = threading.Thread(target=self._run, args=(repos, mode), daemon=True)
        thread.start()
        
        return self._current_job  # Return immediately
```

**Tool response is instant:**
```
Agent calls: knowledge_system(action="ingest")
    ▼
Server returns: {"status": "started", "job_id": "ing-..."}
    ▼
(Background thread indexes 41 repos in parallel)
    ▼
Agent can search immediately; new data appears as indexing completes
```

**Query behavior during indexing:**
- In-progress ingestion doesn't block queries
- Agents see existing data (pre-ingestion state) immediately
- New entities appear as repos finish (streaming availability)
- Graph reloaded once all repos complete

## Crash Recovery

**Scenario:** Power loss during per-repo write

1. On restart, detect `ingestion_state.status = "in_progress"` 
2. Mark as "failed"; trigger full rebuild in background
3. Serve existing data immediately (won't have latest changes, but consistent)
4. Once rebuild completes, new data available

```python
def startup():
    state = db.read().execute(
        "SELECT value FROM system_state WHERE key='ingestion_status'"
    ).fetchone()
    
    if state and json.loads(state[0]).get("status") == "in_progress":
        logger.warning("Incomplete ingestion detected. Rebuilding.")
        needs_rebuild = True
```

## Consequences

### Positive
- **6x faster incremental:** 7s vs 43s post-pull
- **Non-blocking:** Agents get response immediately; ingestion runs in background
- **Crash-safe:** Per-repo atomic writes ensure consistency
- **Observable:** Job ID, progress tracking, completion reporting
- **Scalable:** Parallel repo scanning handles growth to 100+ repos
- **Clean state on error:** Failed repos rebuild completely (no partial data)

### Negative
- **Complex state tracking:** `ingestion_state` table, last_sha tracking, in-progress detection
- **Git dependency:** Requires git to be available; fails if repos not in git
- **Background complexity:** Threading, synchronization, failure handling
- **Limited observability:** Progress updates require polling job status
- **Graph reload latency:** After per-repo write, full graph reloads (~200ms); agents don't see new entities until reload

## Ingestion Triggers

**Manual (via MCP tool):**
```
knowledge_system(action="ingest", mode="incremental", repos=["aws-infra"])
```

**Automatic (via Claude Code hook, once configured):**
- After successful `git pull` on workspace
- On session start (if >1 hour since last ingest)

**Non-blocking:** Returns immediately with job ID.

## Related ADRs

- [ADR-002: Graph Storage](ADR-002-graph-storage.md) — Graph reloaded after per-repo writes
- [ADR-003: MCP Transport](ADR-003-mcp-transport.md) — Ingestion triggered via `knowledge_system` tool
- [ADR-004: Security Model](ADR-004-security-model.md) — Security gate runs during ingestion

## Implementation Notes

**Incremental mode requires git:**
```python
proc = await asyncio.create_subprocess_exec(
    "git", "diff", "--name-status", last_sha, "HEAD",
    cwd=str(repo_path)
)
```

**Full mode rescans all files in all repos:**
```python
# Full scan: find all indexable files
all_files = []
for ext in [".tf", ".yaml", ".yml", ".md", "Dockerfile"]:
    all_files.extend(repo_path.glob(f"**/*{ext}"))
```

**SLO:** Incremental ingestion <10s; full rescan <60s.

**Monitoring:** Track ingestion times per repo; alert if trending >15s incremental or >90s full.

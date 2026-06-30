# ADR-018: Automatic Knowledge Freshness

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Week 3 — Knowledge Freshness & Sync

## Context

The knowledge base must stay fresh without requiring manual intervention from the user. Stale knowledge is worse than missing knowledge — it causes agents to make decisions based on outdated state (old file paths, removed resources, renamed modules).

The previous design (Section 10 of ARCHITECTURE.md) defined 6 trigger types but left it unclear which were mandatory vs. optional, and whether the user ever needed to run `cap sync` manually. Users reported confusion about when knowledge was stale and whether their actions were needed.

**Key constraints:**
- Knowledge must be fresh at the start of every agent session (agents cannot work on stale data)
- Sync must be invisible to the user during normal operation — zero manual maintenance
- Sync must be fast enough to not block session start (<2s for incremental, <30s for full)
- Must handle offline/disconnected scenarios gracefully (no crash on network failure)
- Must not run redundant full syncs (expensive: traverses git history + re-embeds)

## Decision

**Implement 5 automatic sync triggers that together guarantee freshness without any manual sync requirement. The `cap sync` CLI command remains available as an escape hatch but is never needed during normal operation.**

### The 5 Triggers

| # | Trigger | When | Scope | Latency Budget |
|---|---------|------|-------|----------------|
| 1 | **Session start** | Every `session_start()` call | Staleness check + incremental if needed | <2s |
| 2 | **Post-pull** | After `git pull`/`git fetch` via git hook | Diff-based incremental on changed files | <5s |
| 3 | **Staleness TTL** | When knowledge age exceeds TTL (configurable, default 60min) | Incremental sync of workspace | <10s |
| 4 | **Background** | Periodic timer (every `scheduled_interval_minutes`) | Full reconciliation + embedding backfill | <30s |
| 5 | **Manual** | User runs `cap sync` | Full or targeted sync | Unbounded |

### Trigger Details

#### 1. Session Start (Mandatory, Blocking)

```python
async def session_start_sync(workspace: str) -> SyncResult:
    """Lightweight freshness check — blocks session start."""
    sync_state = get_sync_state(workspace)
    
    # Fast path: check if HEAD matches last sync
    current_sha = get_head_sha(workspace)
    if current_sha == sync_state.last_commit_sha:
        return SyncResult(status="up_to_date", duration_ms=5)
    
    # Changed: do incremental sync
    return await incremental_sync(workspace, since_sha=sync_state.last_commit_sha)
```

#### 2. Post-Pull (Automatic via Git Hook)

Installed by `cap init` as `.git/hooks/post-merge`:

```bash
#!/bin/sh
# Async — does not block git pull completion
cap sync --trigger git_post_pull --workspace "$(pwd)" &
```

Also registered in `.claude/settings.json` as a PostToolUse hook for the `Bash` tool when it detects `git pull` or `git fetch` in the command.

#### 3. Staleness TTL (Passive Check)

Every `knowledge_search()` call checks entry staleness before returning results:

```python
async def knowledge_search(query: str, workspace: str = None, **kwargs):
    """Search with automatic staleness detection."""
    if workspace:
        sync_state = get_sync_state(workspace)
        age_minutes = minutes_since(sync_state.last_sync_at)
        if age_minutes > config.staleness_ttl_minutes:
            # Fire-and-forget background sync
            asyncio.create_task(incremental_sync(workspace))
            # Still return current results (stale > nothing)
    
    return await execute_search(query, workspace, **kwargs)
```

#### 4. Background (Periodic Reconciliation)

Runs on a timer inside the knowledge MCP server process:

```python
class BackgroundSync:
    """Periodic full reconciliation."""
    
    async def run(self):
        while True:
            await asyncio.sleep(config.scheduled_interval_minutes * 60)
            for workspace in get_registered_workspaces():
                try:
                    await full_reconciliation(workspace)
                except Exception as e:
                    logger.warning(f"Background sync failed for {workspace}: {e}")
```

Full reconciliation includes:
- Detect deleted files (present in index, absent on disk)
- Detect new untracked files matching index patterns
- Re-embed entries with `embedding_status = 'stale'`
- Update graph edges for moved/renamed files

#### 5. Manual (Escape Hatch)

```bash
# Force full re-sync (rare, for recovery)
cap sync --full --workspace /path/to/repo

# Targeted sync (single file)
cap sync --file src/cap/db.py --workspace .
```

### Staleness Indicators

The `knowledge_status` tool reports sync health:

```json
{
  "workspace": "/path/to/repo",
  "status": "fresh",
  "last_sync_at": "2026-06-30T14:23:00Z",
  "last_commit_synced": "abc1234",
  "current_head": "abc1234",
  "staleness_minutes": 0,
  "entries_indexed": 847,
  "entries_stale": 0,
  "embeddings_pending": 3,
  "next_background_sync_in": "47m"
}
```

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Manual sync only** | Simple, user controls when | Knowledge goes stale between syncs, users forget, agents make decisions on old data | Rejected |
| **File watcher (inotify/fsevents)** | Real-time, no staleness possible | Resource-intensive (41 repos x many files), battery drain on laptops, race conditions with editor autosave | Rejected as default (opt-in via `file_watch_enabled`) |
| **Sync on every search** | Always fresh | Adds latency to every search (unacceptable for <50ms target), redundant work | Rejected |
| **Sync on commit** | Captures all local changes | Doesn't capture upstream changes (pull), doesn't handle external edits | Rejected (insufficient coverage) |
| **3 triggers (session + post-pull + manual)** | Simpler | Misses the case where user works for hours without pull or restart; staleness TTL catches this | Rejected (gap in coverage) |
| **6 triggers (add file_watch)** | Most complete | File watching is resource-intensive and causes issues with editor temp files; 5 triggers already guarantee freshness within TTL | Rejected for default (opt-in available) |

## Consequences

### Positive
- **Zero manual maintenance:** Users never need to remember to run `cap sync`
- **Freshness guarantee:** Knowledge is at most `staleness_ttl_minutes` old during active use
- **Non-blocking:** Only session-start sync is blocking (and fast: <2s); all others are async
- **Graceful degradation:** If sync fails (offline, network error), existing knowledge is still served
- **Observable:** `knowledge_status` always shows current freshness state
- **Efficient:** Incremental sync only processes changed files (git diff based)

### Negative
- **Background CPU:** Periodic sync uses some CPU/IO even when idle (mitigated by long interval: 60min default)
- **Git hook installation:** Requires `cap init` to install post-merge hook (one-time setup)
- **Eventual consistency:** Between triggers, knowledge may be slightly stale (bounded by TTL)
- **Embedding backlog:** If many files change at once, embedding queue may lag behind (async, non-blocking)

## Configuration

```toml
[knowledge.sync]
auto_sync_on_session_start = true       # Trigger 1
auto_sync_on_git_pull = true            # Trigger 2
staleness_ttl_minutes = 60              # Trigger 3
scheduled_interval_minutes = 60         # Trigger 4
file_watch_enabled = false              # Optional 6th trigger (opt-in)
max_file_size_kb = 500                  # Skip large files
```

## Related ADRs

- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — Incremental git-diff based ingestion (the mechanism these triggers invoke)
- [ADR-005: Bedrock Embeddings](ADR-005-bedrock-embeddings.md) — Embedding pipeline that background sync feeds into
- [ADR-017: Code Intelligence](ADR-017-code-intelligence.md) — Symbol extraction runs as part of sync pipeline

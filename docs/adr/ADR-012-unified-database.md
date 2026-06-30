# ADR-012: Single SQLite Database with WAL Mode

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP System Design v1)

## Context

The previous CAP design used 4 separate SQLite databases (`platform.db`, `knowledge.db`, `sessions.db`, `fleet.db`), each owned by a dedicated MCP server process. This introduced operational complexity:

- Cross-database queries required the inbox/message-passing pattern (JSONL files polled by owning server)
- Backup required 4 separate operations, each with its own WAL checkpoint
- Schema migrations needed coordination across 4 databases
- PID lockfiles were required per database to enforce single-writer semantics
- Disk space was fragmented across 4 WAL files, 4 SHM files, and 4 backup sets
- Atomic operations spanning tables in different databases were impossible

**Key constraints:**
- SQLite WAL mode supports concurrent readers with a single writer
- Hook scripts (short-lived processes) need fast reads without blocking MCP servers
- Total data volume is modest (<100MB typical, <1GB maximum)
- Single-user system — no multi-user concurrency concerns

## Decision

**Consolidate all state into a single SQLite database at `~/.cap/cap.db` with WAL mode enabled for concurrent reads.**

### Database Location and Configuration

```
~/.cap/cap.db          # Single database file
~/.cap/cap.db-wal      # WAL (Write-Ahead Log)
~/.cap/cap.db-shm      # Shared memory for WAL coordination
```

**PRAGMA configuration:**
```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;  -- safe with WAL mode
PRAGMA wal_autocheckpoint = 1000;  -- pages before auto-checkpoint
```

### Table Groups

| Group | Tables | Primary Writer |
|-------|--------|----------------|
| Memory | `memory_active`, `memory_archive`, `memory_working`, `memory_fts` | Memory MCP Server |
| Enforcement | `enforcement_edits`, `enforcement_violations`, `agent_contexts`, `passthrough` | PreToolUse Hook |
| Routing | `routing_decisions` | Orchestrator MCP Server |
| Sessions | `sessions`, `session_events` | Memory MCP Server |
| Cost | `cost_ledger`, `cost_budgets` | Cost Tracker (any writer) |

### Concurrency Model

- **Readers:** Hooks and MCP servers read concurrently via WAL (no blocking)
- **Writers:** Application-level table-group ownership prevents write conflicts
- **Busy timeout:** 5000ms busy_timeout handles rare write contention gracefully
- **Checkpointing:** WAL auto-checkpoints at 1000 pages; manual checkpoint on `cap doctor`

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **4 separate DBs (previous design)** | Complete write isolation, no contention possible | Cross-DB queries impossible, 4x backup/migration overhead, inbox pattern complexity | Rejected (over-engineering for single-user) |
| **Single DB, no WAL** | Simplest possible setup | Readers block on writes, hooks would stall during MCP writes | Rejected |
| **PostgreSQL** | Full MVCC, rich features | Server process, overkill for local single-user tool, dependency burden | Rejected |
| **Single DB with WAL + table-level locking** | Maximum concurrency | SQLite does not support table-level locks; WAL provides adequate reader concurrency | Not applicable |
| **Multiple DBs with shared connection pool** | Could share connections | SQLite connections are not safely shared across processes | Rejected |

## Consequences

### Positive
- **Simpler operations:** One file to back up, one schema to migrate, one integrity check
- **Atomic cross-table queries:** Can JOIN memory entries with routing decisions in a single query
- **Single backup target:** `sqlite3 cap.db ".backup backup.db"` captures everything
- **Reduced disk overhead:** One WAL file instead of four; one SHM file instead of four
- **Simplified initialization:** `cap init` creates one database with all tables
- **Easier debugging:** `sqlite3 ~/.cap/cap.db` gives full system visibility

### Negative
- **Single point of failure:** Database corruption affects all subsystems (mitigated by WAL crash safety + backups)
- **Write serialization:** All writes go through one WAL (acceptable for single-user workload)
- **Larger WAL file:** All writes accumulate in one WAL (mitigated by auto-checkpoint at 1000 pages)
- **Migration risk:** Schema changes affect the entire database (mitigated by backup-before-migrate)

## Implementation Notes

**Key file:** `src/cap/db.py`

**Connection factory:**
```python
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.expanduser("~/.cap/cap.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn
```

**Migration strategy:** Version table tracks schema version; `cap init` runs forward migrations; `cap doctor` verifies schema integrity.

**Backup strategy:** Automatic backup before any schema migration; daily backup via maintenance schedule; keep last 5 backups.

## Related ADRs

- [ADR-009: Enforcement Hooks](ADR-009-enforcement-hooks.md) — Enforcement tables in cap.db
- [ADR-010: Memory Architecture](ADR-010-memory-architecture.md) — Memory tables in cap.db
- [ADR-011: Adaptive Routing](ADR-011-adaptive-routing.md) — Routing decisions table in cap.db

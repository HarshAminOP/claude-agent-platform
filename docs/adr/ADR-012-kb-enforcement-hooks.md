# ADR-012: Knowledge-Base-First Enforcement via PreToolUse Hooks

**Status:** Accepted  
**Date:** 2026-07-02  
**Context:** Version 2 (CAP System Design v1)

## Context

`CLAUDE.md` instructs agents to call `knowledge_search` before reaching for `grep`, `find`, or similar filesystem search tools. This instruction is advisory — it is ignored under time pressure, during multi-step workflows where the agent has already "started", or simply because the LLM deprioritises meta-instructions when processing a long tool chain.

The result: agents routinely bypass the knowledge base and perform raw filesystem scans, producing slower queries, uncached results, and zero benefit from the indexed entity graph. The KB exists to provide sub-200ms answers that grep cannot; skipping it defeats the entire system.

**Key constraints:**
- Enforcement must be stateless from Claude's perspective (no additional MCP tool call required to "unlock")
- The block must be hard — advisory messages are insufficient
- Exemption logic must not block legitimate non-search commands (build tools, git, docker, etc.)
- Agents operating in passthrough mode (e.g. during active `knowledge_search` workflows) must not be re-blocked immediately after calling the tool
- Violations must be auditable without requiring external infrastructure

## Decision

**Implement a PreToolUse hook that hard-blocks (`exit 2`) grep/find-family commands unless a `knowledge_search` call has been recorded in the local SQLite `kb_search_flags` table within the last 10 minutes.**

### Hook Architecture

Two hooks collaborate:

| Hook | Trigger | Action |
|------|---------|--------|
| **PreToolUse** | Any invocation of the `Bash` tool | Inspects first word of command; exits `2` (hard block) if command is in the search-family and no valid KB flag exists |
| **PostToolUse** | Completion of `mcp__cap-knowledge__knowledge_search` | Inserts a flag row into `kb_search_flags` with `ts = now()` |

### Search-Family Detection

Detection is **first-word-only** — the hook extracts `command.strip().split()[0]` and checks against:

```
grep  find  rg  ag  ack
```

Commands containing these words as arguments (e.g. `git log --grep=foo`) are **not** blocked. This eliminates the most common false-positive class.

### Exemption List

The following command prefixes bypass enforcement unconditionally:

```
git  npm  pip  uv  docker  kubectl  python3  python  echo  cat  ls
```

Exemptions are evaluated before the search-family check. If the first word matches any exemption prefix, the hook exits `0` immediately.

### Passthrough Mode

A workspace can be placed in passthrough mode by inserting a row into the `passthrough` table:

```sql
INSERT INTO passthrough (workspace, expires_at)
VALUES ('<workspace_path>', datetime('now', '+5 minutes'));
```

The PreToolUse hook queries this table before any other check. If a non-expired row exists for the current workspace, the hook exits `0` (allow). TTL is 5 minutes.

Use cases for passthrough:
- Active `knowledge_search` result processing (agent is already in KB context)
- Scripted bulk ingestion runs where grep is structural, not a search substitute

### Violation Recording

Every hard block is written to the `enforcement_violations` table before exit:

```sql
INSERT INTO enforcement_violations (workspace, command, blocked_at, reason)
VALUES ('<workspace_path>', '<full_command>', datetime('now'), 'no_kb_search_within_ttl');
```

This table is append-only and never pruned automatically, enabling full audit history.

### Database Schema

```sql
-- Written by PostToolUse after every knowledge_search call
CREATE TABLE IF NOT EXISTS kb_search_flags (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT    NOT NULL,
    ts        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Written by PreToolUse on every hard block
CREATE TABLE IF NOT EXISTS enforcement_violations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT    NOT NULL,
    command   TEXT    NOT NULL,
    blocked_at TEXT   NOT NULL DEFAULT (datetime('now')),
    reason    TEXT    NOT NULL
);

-- Managed externally; entries expire by TTL column
CREATE TABLE IF NOT EXISTS passthrough (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace  TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);
```

### Hook Implementation Sketch

**PreToolUse** (`~/.claude/pretool.py` — Claude Code pipes stdin as JSON):

```python
import json, sqlite3, sys
from pathlib import Path
from datetime import datetime, timedelta

payload   = json.load(sys.stdin)
tool_name = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "").strip()
first   = command.split()[0] if command else ""

EXEMPTIONS    = {"git","npm","pip","uv","docker","kubectl","python3","python","echo","cat","ls"}
SEARCH_FAMILY = {"grep","find","rg","ag","ack"}

if first in EXEMPTIONS or first not in SEARCH_FAMILY:
    sys.exit(0)

db_path   = Path.home() / ".claude" / "cap.db"
workspace = str(Path.cwd())
ttl_cutoff = (datetime.utcnow() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

con = sqlite3.connect(db_path)

# Check passthrough
row = con.execute(
    "SELECT 1 FROM passthrough WHERE workspace=? AND expires_at > datetime('now')",
    (workspace,)
).fetchone()
if row:
    con.close()
    sys.exit(0)

# Check KB flag within TTL
row = con.execute(
    "SELECT 1 FROM kb_search_flags WHERE workspace=? AND ts > ?",
    (workspace, ttl_cutoff)
).fetchone()
if row:
    con.close()
    sys.exit(0)

# Hard block — record violation and exit 2
con.execute(
    "INSERT INTO enforcement_violations (workspace, command, reason) VALUES (?,?,?)",
    (workspace, command, "no_kb_search_within_ttl")
)
con.commit()
con.close()

print(
    f"[CAP ENFORCEMENT] Blocked: '{first}' requires knowledge_search first.\n"
    f"Call mcp__cap-knowledge__knowledge_search, then retry.",
    file=sys.stderr
)
sys.exit(2)
```

**PostToolUse** (`~/.claude/posttool.py`):

```python
import json, sqlite3, sys
from pathlib import Path

payload   = json.load(sys.stdin)
tool_name = payload.get("tool_name", "")

if tool_name != "mcp__cap-knowledge__knowledge_search":
    sys.exit(0)

db_path   = Path.home() / ".claude" / "cap.db"
workspace = str(Path.cwd())

con = sqlite3.connect(db_path)
con.execute(
    "INSERT INTO kb_search_flags (workspace) VALUES (?)",
    (workspace,)
)
con.commit()
con.close()
sys.exit(0)
```

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Advisory message in CLAUDE.md (current state)** | Zero implementation cost | Ignored under time pressure; no audit trail; no enforcement | Rejected |
| **Soft block (exit 0 with warning)** | Non-disruptive | Agents proceed anyway; identical outcome to advisory message | Rejected |
| **LLM-level system prompt injection** | No hook infrastructure needed | Prompt injection is overrideable by task context; same reliability problem as CLAUDE.md | Rejected |
| **Per-session flag (never resets)** | Simplest TTL logic | Agents call knowledge_search once at session start then grep freely for hours; KB becomes stale | Rejected |
| **Regex full-command scan (not first-word-only)** | Catches more edge cases | Produces false positives on `git log --grep=foo`, `docker build --no-cache --find-*`, etc. | Rejected |
| **MCP tool gate (KB must return before Bash unlocks)** | Architecturally clean | Requires MCP server round-trip in hot path; adds 50-200ms latency to every Bash call | Rejected |

## Consequences

### Positive
- **Hard enforcement:** Agents cannot bypass the KB under time pressure — exit code 2 is unrecoverable by the agent without calling `knowledge_search`
- **Faster information retrieval:** KB indexed results return in <50ms vs. multi-second filesystem grep across large repos
- **Auditable:** Every violation is written to `enforcement_violations` with timestamp, workspace, and exact command — queryable for compliance review
- **Low false-positive rate:** First-word-only detection eliminates the most common false-positive class (search keywords in arguments)
- **No external dependencies:** Entire enforcement mechanism is SQLite + a Python stdlib script; no network, no daemon required

### Negative
- **10-minute TTL requires periodic re-validation:** Agents working across long sessions must re-call `knowledge_search` at least every 10 minutes or be blocked — this is intentional but adds friction in extended workflows
- **First-word-only detection has residual gaps:** Aliased commands (`alias search=grep`) or shell functions wrapping grep are not detected; mitigated by the fact that Claude Code does not persist shell aliases across Bash tool calls
- **Passthrough TTL management is manual:** Callers inserting passthrough rows must manage expiry — there is no automatic cleanup daemon in v1 (rows are checked by TTL column at read time, so expired rows are inert but accumulate)
- **Hook infrastructure must be installed separately:** `pretool.py` / `posttool.py` are not shipped in the `cap` wheel; they require `cap install-hooks` or manual placement

## Related ADRs

- [ADR-009: Hard Enforcement via PreToolUse Exit Code 2](ADR-009-enforcement-hooks.md) — Foundation enforcement mechanism this ADR extends
- [ADR-010: 3-Tier Memory Architecture](ADR-010-memory-architecture.md) — `cap.db` houses `kb_search_flags` alongside memory tables
- [ADR-011: Adaptive Routing](ADR-011-adaptive-routing.md) — Router reads routing decisions from the same `cap.db`
- [ADR-013: Workspace & Endpoint Registry](ADR-013-workspace-registry.md) — Workspace path used as partition key in `kb_search_flags`

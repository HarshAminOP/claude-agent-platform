# CAP v2 Architecture

## System Overview

Claude Agent Platform (CAP) v2 is a production-grade multi-agent orchestration platform. It coordinates 139+ specialist agents across AWS Bedrock, manages knowledge graphs spanning workspaces, enforces budget controls, and provides persistent memory across sessions.

The system ships as a unified CLI (`cap`) that runs in the Claude Code host process. All state is consolidated into SQLite databases with WAL mode for concurrent access. MCP servers expose tools to Claude Code; agents communicate via message bus and shared state.

---

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Claude Code (Host Process)                               │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  CLAUDE.md Rules Layer                                                │  │
│  │  - Route to orchestrator on complexity threshold                      │  │
│  │  - Auto-orchestration rules per task type                            │  │
│  │  - Session memory queries before bash grep                           │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│                              ▼                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  9 MCP Servers (stdio-based)                                          │  │
│  │  ├─ cap-knowledge    (FTS5 + semantic + graph retrieval)             │  │
│  │  ├─ cap-session      (cross-session memory, learnings, decisions)    │  │
│  │  ├─ cap-orchestrator (routing, planning, execution)                 │  │
│  │  ├─ cap-harness      (agent spawn, coordination, cost tracking)     │  │
│  │  ├─ cap-backlog      (tasks, decisions, conflict resolution)        │  │
│  │  ├─ cap-fleet        (server lifecycle, health checks)              │  │
│  │  ├─ cap-ast          (AST search, match, refactor)                  │  │
│  │  ├─ cap-code-intel   (structure, dependents, trace)                 │  │
│  │  └─ cap-diagram      (mermaid/graphviz rendering)                   │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  Core Libraries (src/cap/lib/)                                       │  │
│  │  ├─ agent_bus.py         (pub/sub + request/response)               │  │
│  │  ├─ config.py            (TOML loading, typed dataclasses)          │  │
│  │  ├─ coordination_engine.py (DAG execution, parallel dispatch)       │  │
│  │  ├─ sync_engine.py        (file-level incremental sync)             │  │
│  │  ├─ embeddings.py         (Titan V2 + sentence-transformers)        │  │
│  │  ├─ retrieval.py          (hybrid search: keyword+semantic+graph)   │  │
│  │  ├─ knowledge_graph.py    (node/edge management, traversal)         │  │
│  │  └─ repo_resolver.py      (GitHub auto-clone for missing deps)     │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  Orchestration Layer (src/cap/orchestration/)                        │  │
│  │  ├─ router.py         (complexity scoring: inline/lightweight/full)  │  │
│  │  ├─ dag.py            (TaskDAG: dependency graph, cycle detection)   │  │
│  │  └─ review_loop.py    (automated code review integration)            │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  Storage Layer                                                        │  │
│  │  ├─ ~/.cap/cap.db        (workflows, budget, audit, fleet)          │  │
│  │  ├─ ~/.cap/knowledge.db  (entries, embeddings queue, graph)         │  │
│  │  ├─ ~/.cap/sessions.db   (sessions, learnings, decisions)           │  │
│  │  └─ ~/.cap/knowledge_vectors/ (LanceDB vector store)                │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
          ┌─────────────┐  ┌──────────────┐  ┌─────────────┐
          │AWS Bedrock  │  │External MCP  │  │Git Repos    │
          │(Claude +    │  │Servers       │  │(Workspaces) │
          │Titan V2)    │  │(managed)     │  │             │
          └─────────────┘  └──────────────┘  └─────────────┘
```

---

## Component Map

### CLI Layer (`src/cap/cli/`)

**main.py** — Click entry point, command registration, version check
- Handles subcommands: init, doctor, index, daemon, orchestrate
- Health probes and diagnostics
- Config loading from `~/.cap/config.toml`

**lifecycle.py** — Installation and teardown
- `cap init` — Create ~/.cap directory, initialize databases, register MCP servers
- `cap uninstall` — Backup state, deregister servers, cleanup
- Config versioning and migration

**commands.py** — Administrative commands
- `cap health` — Probe all MCP servers and databases
- `cap dlq list|retry|mark-processed` — Dead letter queue management
- `cap doctor` — Diagnose common issues
- `cap orch-status` — Real-time workflow status dashboard

**index_cmd.py** — Knowledge base indexing
- `cap index run` — Full index of all workspaces
- `cap index status` — Index health and staleness
- `cap index deps` — Detect and resolve missing repo dependencies
- `cap index graph` — Visualize knowledge graph

**daemon.py, daemon_service.py** — Background processes
- Embedding pipeline: process queued entries, batch to Bedrock
- Fleet health checks: verify MCP server PIDs alive
- Session cleanup: archive ended sessions
- Budget ledger rollup (monthly)

**watch.py** — Live workflow status with progress bars
- Real-time terminal UI showing agent allocation, token burn, budget
- Keyboard controls: pause/resume/kill workflows

---

### MCP Servers (`src/cap/servers/`)

Nine independent MCP servers, each owning a table group in SQLite:

#### cap-orchestrator
- **Owns**: workflows, workflow_steps, concurrency_slots
- **Tools**:
  - `cap_route(task_description)` → complexity score (0-100) + suggested workflow type
  - `cap_plan(task_description)` → TaskDAG (nodes, edges, critical path, estimated cost)
  - `cap_execute(workflow_id, step_index)` → spawn agent, await result, update ledger
  - `cap_status(workflow_id)` → real-time phase, tokens, agents, remaining budget
  - `cap_health()` → platform health summary

#### cap-knowledge
- **Owns**: knowledge_entries, knowledge_fts, knowledge_graph_nodes, knowledge_graph_edges, embedding_queue, sync_state, business_knowledge
- **Tools**:
  - `knowledge_search(query, top_k=10, strategy="hybrid")` → RRF-ranked results with context
  - `knowledge_ingest(source, content_type, title, metadata)` → add entry, queue embedding
  - `knowledge_graph_query(entity, depth=2)` → traverse graph BFS to depth
  - `knowledge_resolve_repo(repo_name)` → GitHub auto-clone, index, return path
  - `knowledge_status()` → index health, embedding backlog, staleness per workspace

#### cap-session
- **Owns**: sessions, session_events, learnings, decisions, corrections, checkpoints
- **Tools**:
  - `session_start(workspace, context)` → create session, load relevant past learnings
  - `session_record(event_type, content, workspace)` → record decision/correction/discovery/error
  - `session_recall(query, workspace)` → semantic search over past learnings
  - `session_feedback(what_was_wrong, what_is_correct, workspace)` → record correction, boost confidence
  - `session_end(session_id, summary, learnings)` → close session, persist final state

#### cap-harness
- **Owns**: agent_registry, execution_log, cost_ledger, shared_state
- **Tools**:
  - `agent_spawn(agent_type, context, model, timeout_sec=300)` → invoke agent via Bedrock
  - `agent_health()` → check all spawned agents, count active
  - `coordination_lock(resource_type, timeout_sec=30)` → distributed lock (Bedrock-hosted)
  - `shared_state_write(key, value)` → write to workflow-scoped key-value store
  - `shared_state_read(key)` → read from workflow-scoped store

#### cap-backlog
- **Owns**: backlog_tasks, decisions, conflicts
- **Tools**:
  - `backlog_create(title, priority, acceptance_criteria)` → create task
  - `backlog_claim(task_id, agent_id)` → reserve task for execution
  - `backlog_complete(task_id, output)` → mark task done, record output
  - `decision_propose(title, options, recommendation_index)` → offer choice to PO
  - `conflict_raise(title, severity, side_a, side_b)` → escalate disagreement

#### cap-fleet
- **Owns**: fleet_servers, fleet_events, server_health_log
- **Tools**:
  - `fleet_status(server_name=None)` → health check all or one server
  - `fleet_register(name, command, args, env, health_check)` → add new server
  - `fleet_restart(name, reason)` → flag for restart (Claude Code owns stdio)
  - `fleet_discover(workspace)` → scan for MCP servers in workspace config
  - `fleet_logs(name, lines=50)` → recent events for a server

#### cap-ast
- **Owns**: ast_cache (per-file parsed trees)
- **Tools**:
  - `ast_search(query, language)` → find functions/classes by pattern across workspace
  - `ast_match(file_path, pattern)` → match AST pattern in one file
  - `ast_refactor(file_path, from_pattern, to_pattern)` → safe AST-based refactoring

#### cap-code-intel
- **Owns**: code_structure_cache, dependents_index
- **Tools**:
  - `code_structure(workspace)` → parse imports/exports, build module graph
  - `code_dependents(symbol, workspace)` → find all files that reference symbol
  - `code_trace(file_path, symbol)` → follow calls/imports upstream

#### cap-diagram
- **Owns**: none (read-only)
- **Tools**:
  - `diagram_render(mermaid_src)` → render Mermaid to SVG/PNG
  - `diagram_mermaid_to_md(description)` → auto-generate Mermaid flowchart from English

---

### Orchestration Layer (`src/cap/orchestration/`)

**router.py** — Complexity scoring and workflow selection

Heuristic scoring: keyword matching for known patterns (terraform plan, kubectl apply, database migration → high complexity). Learned thresholds updated from `routing_decisions` table (self-tuning).

Returns: (score 0-100, workflow_type: "inline", "lightweight", or "full")

- Inline: execute directly in Claude Code
- Lightweight: spawn 1-2 agents sequentially
- Full: multi-agent DAG orchestration with review loops

**dag.py** — Task decomposition and dependency graph

```python
class TaskDAG:
    def __init__(self, task_description: str):
        self.nodes: list[TaskStep] = []  # ordered steps
        self.edges: dict[int, list[int]] = {}  # step_id → [dependent_ids]
        self.critical_path: list[int] = []  # longest path
        self.estimated_cost_usd: float = 0.0
        
    def detect_cycles(self) -> bool:
        """DFS 3-color (white/gray/black) cycle detection."""
        
    def ready_steps(self) -> list[TaskStep]:
        """Steps with all dependencies satisfied."""
        
    def critical_path_length(self) -> float:
        """Max (tokens + time) to completion."""
```

State transitions per step: PENDING → READY → RUNNING → COMPLETED (or FAILED/SKIPPED).

**review_loop.py** — Automated code review integration

Before any push/PR, spawns code-review agent on diff. Blocks if CRITICAL/HIGH found; warns if LOW/MEDIUM. Injects findings into agent context for next iteration.

---

### Storage Layer

All state is consolidated into SQLite at `~/.cap/` with WAL mode:

#### `cap.db` (Writer: orchestrator MCP)

| Table Group | Purpose |
|-------------|---------|
| `workflows`, `workflow_steps` | Workflow execution state, budget tracking |
| `concurrency_slots` | API rate limiting, model-specific semaphores |
| `budget_ledger` | Monthly token/cost per workspace/model |
| `fleet_servers`, `fleet_events` | MCP server registry, health logs |
| `inbox` | Cross-server messages (workflow state updates) |

#### `knowledge.db` (Writer: knowledge MCP)

| Table Group | Purpose |
|-------------|---------|
| `knowledge_entries`, `knowledge_fts` | Content + full-text index (porter tokenizer) |
| `knowledge_graph_nodes`, `knowledge_graph_edges` | Entity graph (repos, services, teams, files) |
| `business_knowledge` | Team ownership, conventions, incidents |
| `sync_state` | Last sync timestamp, commit SHA, file count |
| `embedding_queue` | Entries pending Bedrock embedding, retry count |

#### `sessions.db` (Writer: session MCP)

| Table Group | Purpose |
|-------------|---------|
| `sessions`, `session_events` | Session lifecycle, all events (decisions, corrections) |
| `learnings` | Distilled knowledge (user preferences, patterns) |
| `decisions` | Important decisions with alternatives considered |
| `corrections` | User feedback, highest-priority learnings |
| `checkpoints` | Mid-session save points for crash recovery |

#### `knowledge_vectors/` (LanceDB)

- Vector store: one row per knowledge entry
- Schema: id, vector (1024-dim Titan V2), workspace, content_type, title, source_path
- Index: IVF_PQ after 10k+ vectors
- Cosine similarity search

---

## Data Flow

### Request Lifecycle

```
1. User sends task to Claude Code
                ↓
2. CLAUDE.md auto-orchestration rule fires
                ↓
3. Orchestrator agent called
                ↓
4. cap_route(task) → complexity score + workflow type
                ↓
5. If inline: execute directly
   If lightweight: spawn 1-2 agents in sequence
   If full: proceed to DAG planning
                ↓
6. cap_plan(task) → TaskDAG with estimated cost
                ↓
7. Show plan to user (or auto-approve if < threshold)
                ↓
8. CoordinationEngine resolves ready steps
                ↓
9. For each ready step:
   a. Allocate concurrency slot (model-specific semaphore)
   b. Spawn agent with context (previous outputs + learnings)
   c. LLM invocation via Bedrock
   d. Cost tracked (input_tokens, output_tokens, embedding_tokens)
   e. Result collected and stored in shared_state
                ↓
10. Dependent steps receive predecessor outputs as context
                ↓
11. All steps completed → synthesis step (final agent response)
                ↓
12. Budget checked: if exceeded and kill_on_exceed=true, stop
                ↓
13. Return synthesized result to user
```

### Knowledge Search Pipeline

```
User query → knowledge_search(query, strategy="hybrid")
                ↓
┌───────────────────────────────────────────────────────────────┐
│ Parallel execution (3 channels):                              │
├───────────────────────────────────────────────────────────────┤
│ 1. FTS5 keyword search (BM25)                                 │
│ 2. Bedrock Titan V2 semantic (cosine sim)                     │
│ 3. Knowledge graph traversal (BFS depth=2)                    │
└───────────────────────────────────────────────────────────────┘
                ↓
    RRF Ranking (Reciprocal Rank Fusion)
    score = Σ w_channel * (1 / (k + rank))
    weights: keyword=0.3, semantic=0.5, graph=0.2
                ↓
    Post-processing:
    - Dedup by source_path
    - Workspace filter
    - Recency boost (+20% for < 7 days)
    - Content-type boost (docs > code > config)
                ↓
            Top-K results
```

### Embedding Pipeline

```
New knowledge entry → queue to embedding_queue (status: pending)
                ↓
(background daemon wakes every 30s)
                ↓
Fetch up to 100 pending entries
                ↓
Batch into groups of 25
                ↓
For each batch:
  invoke Bedrock Titan V2 with batch
  store vectors in LanceDB
  update embedding_queue (status: done)
                ↓
Exponential backoff on throttle: base 500ms, max 10s, 3 retries
                ↓
If Bedrock fails: graceful degradation
  - Semantic channel returns empty
  - RRF reweights: keyword=0.6, graph=0.4
  - Queue holds; resume when Bedrock recovers
```

### Dependency Auto-Resolution

```
Agent references repo "missing-repo"
                ↓
Is it in knowledge graph?
  Yes → use existing entry
  No  → continue
                ↓
Found locally?
  Yes → trigger sync & index
  No  → continue
                ↓
Auto-clone enabled?
  Yes → continue
  No  → return not_found
                ↓
Exists on GitHub org?
  Yes → continue
  No  → return not_found
                ↓
git clone --depth 1 (SSH)
                ↓
Index into knowledge base
                ↓
Build graph edges (package.json, terraform remote_state, etc.)
                ↓
Update knowledge graph
                ↓
Return to agent
```

---

## Agent Coordination Model

### Context Isolation

Each agent receives only task-relevant context:
- Task description
- Previous step outputs (from shared_state)
- Learnings matching task domain
- Knowledge search results (not entire knowledge base)

Rationale: Agents are cheap per token; bloating context wastes budget.

### Shared State

Coordinated agents within a workflow share a key-value store:

```python
# Agent A writes
shared_state_write("finding.database_schema", {...})

# Agent B reads
prev_findings = shared_state_read("finding.database_schema")
```

State is workflow-scoped; expires when workflow completes.

### Message Bus (AgentBus)

Pub/sub with topic wildcards for async communication:

```python
# Agent A publishes
bus.publish("finding.security_issue", {
    "severity": "high",
    "description": "...",
})

# Agent B subscribes (within same workflow)
findings = bus.subscribe("finding.*")
```

Request/response pattern for synchronous queries:

```python
# Agent A requests
response = bus.request("query.ast_search", {
    "pattern": "def authenticate",
    "language": "python",
}, timeout_sec=30)

# Bus routes to cap-ast MCP server
# Returns search results
```

### Coordination Primitives

**Distributed locks**: `coordination_lock(resource_type, timeout_sec)`
- Used for safe concurrent access to shared resources (e.g., modifying a config file)
- Bedrock-hosted lock service

**Semaphores**: per-model concurrency slots
- Limit concurrent calls to Bedrock by model (e.g., max 3 opus calls)
- Prevents API throttling

**Dependency barriers**: DAG wait points
- Agent blocks until dependent steps complete

---

## Model Tier Distribution

139 specialist agents across 3 model tiers:

| Model | Agents | Use Case |
|-------|--------|----------|
| Opus | 5 | Complex reasoning: architecture design, security review, incident response |
| Sonnet | 14 | Balanced tasks: dev, devops, code review, testing |
| Haiku | 2 | Fast/cheap: status checks, simple lookups, CLI commands |

Cost-aware routing: Orchestrator avoids opus when sonnet suffices. Falls back to haiku for read-only queries.

---

## Budget Enforcement

Monthly Budget (per workspace): `~/.cap/config.toml` → `[budget] monthly_usd = 500`

Tracking:
- Per-agent invocation: track input_tokens, output_tokens, embedding_tokens
- Update budget_ledger: `workspace, period (YYYY-MM), model, cost_usd`
- Check before spawning agent: if exceeded and kill_on_exceed=true, raise error

Cost Formula:
```
input_cost = (input_tokens / 1M) * input_price[model]
output_cost = (output_tokens / 1M) * output_price[model]
embedding_cost = (embedding_tokens / 1M) * 0.02 (Titan V2)
total = input_cost + output_cost + embedding_cost
```

Mitigation Strategies:
- Monitor monthly cost vs. budget cap
- Dial down model tier (opus → sonnet → haiku)
- Reduce workflow parallelism (fewer concurrent agents)
- Increase DAG critical path (serial execution if budget tight)

---

## Reliability & Resilience

### Circuit Breaker (per-agent-type)

Per-agent circuit breaker: CLOSED → OPEN → HALF_OPEN

- CLOSED: normal operation
- OPEN (after 3 failures in 5 min): reject new requests for 2 min cooldown
- HALF_OPEN: single test request; if succeeds, CLOSED; if fails, OPEN

### Dead Letter Queue (DLQ)

Failed tasks enqueued to `dlq` table with full context:
- Workflow ID, agent type, error, input context
- `cap dlq retry <entry_id>` — retry with instrumentation
- `cap dlq mark-processed <entry_id>` — acknowledge, don't retry

### Cascade Failure Prevention

If orchestrator MCP crashes:
1. In-flight workflows marked PAUSED
2. Next orchestrator restart loads from `checkpoints` table
3. Resume from last checkpoint (no replay)

If knowledge MCP crashes:
1. Retrieval gracefully degrades to FTS5 only
2. Semantic channel returns empty
3. Embedding queue persists; resumes on recovery

---

## Security & Audit

### Integrity Witness

Every workflow change logged with witness file:
- Path: `~/.cap/audit.log` (append-only)
- Entry: timestamp, workflow_id, action, state_before, state_after, hash(previous)
- Blockchain-style chain for tamper detection

### Governance & Policy

Policy manifest in `~/.cap/governance.toml`:

```toml
[agent_allowlist]
allowed = ["dev", "devops", "security", "sre"]
denied = ["none"]

[resource_access]
max_agents_concurrent = 5
max_memory_mb = 512
```

PreTool hook (`src/cap/hooks/pretool.py`) enforces:
- Denied agents → exit(2) hard block
- Forbidden bash commands (git push --force, rm -rf) → exit(2)
- Resource limits exceeded → exit(2)

### Secret Redaction

Before logging or storing in DB, redact secrets:
- AWS keys: `AKIA[A-Z0-9]{16}`
- Passwords: `password\s*=\s*"[^"]*"`
- Tokens: `(Bearer|Token)\s+[A-Za-z0-9_-]+`

---

## Installation & Initialization

### `cap init`

1. Create `~/.cap/` directory
2. Initialize SQLite databases: cap.db, knowledge.db, sessions.db
3. Create config: `~/.cap/config.toml` (TOML with defaults)
4. Discover and register MCP servers in `~/.claude.json`
5. Run initial index: scan workspaces, populate knowledge base

### `cap uninstall`

1. Backup state: `cp ~/.cap ~/.cap.backup.$(date +%s)`
2. Deregister MCP servers from `~/.claude.json`
3. Wipe `~/.cap/` (optional: keep backup)

---

## Configuration

Location: `~/.cap/config.toml`

```toml
[indexing]
enabled = true
interval_sec = 3600              # index every hour
max_workers = 4                  # parallel indexing
chunk_size_tokens = 512

[embedding]
model = "amazon.titan-embed-text-v2:0"
batch_size = 25
max_concurrent_batches = 3

[budget]
monthly_usd = 500
kill_on_exceed = false           # pause if over budget
workspace_override = {}          # per-workspace limits

[retrieval]
top_k = 10
semantic_weight = 0.5            # RRF weight for semantic
keyword_weight = 0.3
graph_weight = 0.2

[agents]
model_tier_default = "sonnet"    # opus|sonnet|haiku
timeout_sec = 300

[workspaces]
auto_discover = true
paths = ["/path/to/repo1", "/path/to/repo2"]
```

---

## Observability

### Metrics Exported

- `cap_workflows_total` — total workflows started (labels: status, type)
- `cap_agents_spawned_total` — agents spawned (labels: model, role)
- `cap_tokens_consumed_total` — input/output tokens (labels: model)
- `cap_cost_usd_total` — total cost (labels: workspace, model)
- `cap_budget_remaining_usd` — monthly budget remaining (labels: workspace)
- `cap_knowledge_entries` — indexed entries count (labels: workspace, content_type)
- `cap_embedding_queue_depth` — pending embeddings

### Health Checks

`cap health` returns:

```json
{
  "status": "healthy|degraded|unhealthy",
  "components": {
    "cap.db": "ok",
    "knowledge.db": "ok",
    "sessions.db": "ok",
    "mcp_orchestrator": "ok",
    "mcp_knowledge": "ok",
    "bedrock": "ok|throttled",
    "embedding_queue_depth": 42
  },
  "timestamp": "2026-07-01T18:00:00Z"
}
```

---

## Cross-References

- [Agents](agents.md) — 139 specialist agents, roles, model tier
- [Configuration](configuration.md) — Full config schema with examples
- [Intelligent Indexing](intelligent-indexing.md) — Chunking, dedup, multi-phase pipeline
- [Agent Coordination](agent-coordination.md) — Shared state, message bus, distributed locks
- [Knowledge Graph](adr/ADR-018-knowledge-graph-schema.md) — Graph design, entities, predicates
- [ADR-012: Unified SQLite Database](adr/ADR-012-unified-sqlite.md) — Single-writer concurrency
- [ADR-018: Knowledge Graph Schema](adr/ADR-018-knowledge-graph-schema.md) — Graph modeling

---

## Development & Testing

### Running Locally

```bash
# Install from source
pip install -e .

# Initialize
cap init

# Start indexing daemon (background)
cap daemon &

# Run a workflow
cap orchestrate --task "Deploy service X to prod"

# Watch live
cap orch-status
```

### Unit Test Structure

- `tests/unit/` — Fast tests, no Bedrock/network calls
- `tests/integration/` — Real MCP server spawns, SQLite in-memory
- `tests/e2e/` — Full workflow: Bedrock invocation, knowledge index, multi-agent

### Instrumentation

Enable debug logging:

```bash
RUST_LOG=debug cap orchestrate --task "..."
```

MCP server logs in `~/.cap/logs/server-{name}.log`

---

## Deployment

CAP ships as PyPI package: `pip install claude-agent-platform`

Requires:
- Python 3.10+
- AWS credentials with Bedrock InvokeModel permission
- ~200MB disk (SQLite + vector store)
- Temporary network access during `cap init` (GitHub clone)

---

**Document Type**: Architecture  
**Last Updated**: 2026-07-01  
**Author**: CAP Team

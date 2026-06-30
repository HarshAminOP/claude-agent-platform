```
 ██████╗ █████╗ ██████╗
██╔════╝██╔══██╗██╔══██╗
██║     ███████║██████╔╝
██║     ██╔══██║██╔═══╝
╚██████╗██║  ██║██║
 ╚═════╝╚═╝  ╚═╝╚═╝
Claude Agent Platform
```

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/protocol-MCP%201.0-purple.svg)](https://modelcontextprotocol.io)

---

**Claude Agent Platform (CAP)** is an AI agent orchestration layer that gives Claude Code a persistent brain, hybrid search, budget-controlled workflows, and the illusion of a full engineering team working in parallel.

> Three sentences: CAP augments Claude Code CLI with hybrid knowledge retrieval (keyword + semantic + graph), session memory that persists learnings across conversations, and workflow orchestration that simulates a coordinated engineering team — all exposed via 9 MCP servers that Claude discovers automatically.

---

## Quick Start

```bash
uv tool install claude-agent-platform   # Install globally
cap init                                 # Initialize databases + config
cap status                               # Verify everything is running
```

That's it. Claude Code will auto-discover the MCP servers on next launch.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            Claude Code (Host Process)                            │
│                                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐      │
│   │  Hooks Layer (short-lived, per-tool-call)                            │      │
│   │  pretool.py   exit(2) = HARD BLOCK   [enforcement + delegation]     │      │
│   │  posttool.py  exit(0) always         [sync triggers + state]        │      │
│   └──────────────────────────────┬──────────────────────────────────────┘      │
│                                  │ allowed                                      │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                        │
│   │  cap-        │  │  cap-        │  │  cap-        │                        │
│   │  orchestrator│  │  memory      │  │  code-intel  │                        │
│   │  (MCP)       │  │  (MCP)       │  │  (MCP)       │                        │
│   │              │  │              │  │              │                        │
│   │  routing     │  │  3-tier mem  │  │  AST queries │                        │
│   │  delegation  │  │  scoring     │  │  blast radius│                        │
│   │  learning    │  │  eviction    │  │  graph trav. │                        │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                        │
│          │                  │                  │                                │
│   ┌──────┴──────────────────┴──────────────────┴──────┐                        │
│   │              Module Layer (src/cap/)                │                        │
│   │                                                    │                        │
│   │  orchestration/   memory/        enforcement/      │                        │
│   │   router.py        scorer.py      passthrough.py   │                        │
│   │   context.py       manager.py                      │                        │
│   │   scratchpad.py    eviction.py   learning/         │                        │
│   │                    consolidation  engine.py        │                        │
│   │  cost/            runtime/        integrity/       │                        │
│   │   tracker.py       offline.py     witness.py       │                        │
│   │                                                    │                        │
│   │  db.py (unified SQLite, WAL mode)                  │                        │
│   └────────────────────────┬───────────────────────────┘                        │
│                            │                                                    │
│   ┌────────────────────────┴───────────────────────────────────────┐            │
│   │                      Storage Layer                              │            │
│   │                                                                 │            │
│   │  ┌─────────────────────────────────────────────────────────┐   │            │
│   │  │  ~/.cap/cap.db  (single unified SQLite, WAL mode)       │   │            │
│   │  │                                                         │   │            │
│   │  │  memory_active + memory_fts (FTS5)  │  routing_decisions│   │            │
│   │  │  memory_archive (zstd compressed)   │  cost_ledger      │   │            │
│   │  │  memory_working (per session)       │  sessions         │   │            │
│   │  │  enforcement_edits + violations     │  passthrough      │   │            │
│   │  │  agent_contexts                     │                   │   │            │
│   │  └─────────────────────────────────────────────────────────┘   │            │
│   └─────────────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Features

### 1. Team Simulation

Workflows render as a live engineering team discussion — like watching a Slack channel where architects debate, security raises concerns, and devops implements.

```
╭─── Workflow: new-service-deployment ─── Budget: $2.40 / $5.00 ─── Agents: 4/12 ───╮
│                                                                                      │
│  🏗️  architect     Analysing requirements... SQS → Lambda → DynamoDB pattern.        │
│                    Recommending event-driven with DLQ for poison pills.               │
│                                                                                      │
│  🔒 security      Concern: Lambda execution role has s3:* — narrowing to             │
│                    s3:GetObject on the specific bucket ARN.                           │
│                                                                                      │
│  ⚙️  devops        Implementing Terraform module. Using aws_lambda_function           │
│                    with reserved_concurrent_executions = 10.                          │
│                                                                                      │
│  ✅ security      Approved. IAM policy follows least privilege.                      │
│                                                                                      │
│  📊 sre           Adding CloudWatch alarms: error rate > 1%, duration p99 > 3s,      │
│                    DLQ depth > 0.                                                    │
│                                                                                      │
╰────────────────────────────────── Phase 3/5: Implementation ─────────────────────────╯
```

### 2. Hybrid Retrieval Engine

Three independent search channels merged with Reciprocal Rank Fusion. The `workspace` parameter is optional — when omitted or set to `"all"`, search spans all indexed workspaces, making knowledge accessible from any directory.

| Channel | Technology | What It Finds | Fallback Behavior |
|---------|-----------|---------------|-------------------|
| **Keyword** | SQLite FTS5 (BM25) | Exact terms, code symbols, file paths | Always available |
| **Semantic** | Titan V2 + LanceDB | Conceptually similar content | Skipped if Bedrock unavailable |
| **Graph** | BFS on knowledge graph | Related entities, transitive connections | Skipped if no entities match |

```bash
$ cap knowledge search "how does ArgoCD sync work"

Strategy: hybrid (keyword + semantic + graph)
Channels active: 3/3 | Fusion: RRF (k=60)

 #  Score   Source                          Title
 1  0.847   repos/argocd-platform           ArgoCD sync waves and hooks
 2  0.791   domains/gitops                  GitOps reconciliation loop
 3  0.734   repos/k8s-infra                 ApplicationSet generator patterns
 4  0.702   tasks/PLAT-892                  Migrated sync policy to automated
 5  0.688   domains/argocd                  Health checks and degraded states

Latency: 142ms (keyword: 8ms, semantic: 89ms, graph: 12ms, fusion: 33ms)
```

<details>
<summary><strong>Graceful Degradation Matrix</strong></summary>

| Bedrock Available | Graph Hit | Channels Used | Weight Distribution |
|:-:|:-:|---|---|
| Yes | Yes | keyword + semantic + graph | 0.35 / 0.40 / 0.25 |
| Yes | No | keyword + semantic | 0.40 / 0.60 |
| No | Yes | keyword + graph | 0.60 / 0.40 |
| No | No | keyword only | 1.00 |

The system never fails — it gracefully drops channels and redistributes weights.

</details>

### 3. Session Memory

CAP remembers learnings, corrections, and decisions across conversations with confidence scoring and reinforcement:

```bash
$ cap session learnings

Category       Learning                                        Confidence  Reinforced
─────────────────────────────────────────────────────────────────────────────────────
correction     Never use -uall flag with git status            0.95        3x
preference     SSH-only repo URLs, never HTTPS                 0.90        5x
decision       DLQ on all async Lambda invocations             0.85        2x
architecture   ArgoCD app-of-apps for platform services        0.80        4x
correction     Use worktrees for parallel agent writes         0.92        2x
```

> **How reinforcement works:** When the same pattern is observed again, confidence increases. When contradicted, it decreases. Stale learnings decay over time.

### 4. Budget Controls

Per-workflow token budgets with hard caps, automatic kill-on-exceed, and cost tracking:

```bash
$ cap budget status

Monthly Budget
──────────────
  Used:      $18.42 / $50.00 (36.8%)
  Remaining: $31.58
  Forecast:  $47.20 (on track)

Active Workflows
────────────────
  wf-a3f8c1d2e4   new-service-deployment    $2.40 / $5.00   ██████░░░░  48%
  wf-b7e9f0a1c3   security-hardening        $0.89 / $3.00   ███░░░░░░░  30%

Cost by Model (this month)
──────────────────────────
  opus      $12.40  (67%)  ████████████████████░░░░░░░░░░
  sonnet     $5.20  (28%)  ████████░░░░░░░░░░░░░░░░░░░░░░
  haiku      $0.82  ( 5%)  █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
```

### 5. MCP Servers + Hooks

Three MCP servers (long-lived) and two hook scripts (short-lived, per-tool-call) that integrate seamlessly:

| Component | Type | Database | Capabilities |
|-----------|------|----------|-------------|
| `cap-orchestrator` | MCP Server | `~/.cap/cap.db` | Routing, delegation, checkpoint, learning |
| `cap-memory` | MCP Server | `~/.cap/cap.db` | 3-tier memory CRUD, search, scoring, eviction |
| `cap-code-intel` | MCP Server | `~/.cap/cap.db` | AST queries, graph traversal, blast radius |
| `pretool.py` | PreToolUse Hook | `~/.cap/cap.db` | Hard enforcement (exit 2), delegation tracking |
| `posttool.py` | PostToolUse Hook | `~/.cap/cap.db` | Sync triggers, state updates |

### 6. Evaluation Framework

Built-in eval suites ensure quality doesn't regress:

```bash
$ cap eval run --suite retrieval

Retrieval Quality (knowledge.db: 1,247 entries)
────────────────────────────────────────────────
  Recall@5:        0.89  (target: 0.85) ✓
  Precision@5:     0.74  (target: 0.70) ✓
  MRR:             0.91  (target: 0.85) ✓
  Latency p50:     48ms  (target: <100ms) ✓
  Latency p99:    187ms  (target: <500ms) ✓
  Degradation:     PASS  (keyword-only recall: 0.72)

$ cap eval run --suite security

Security Boundary Tests
───────────────────────
  Path traversal blocked:     12/12 ✓
  SQL injection blocked:       8/8  ✓
  Command injection blocked:   6/6  ✓
  Budget bypass blocked:       4/4  ✓
  Unauthorized MCP rejected:   5/5  ✓
```

---

## Before / After

| Dimension | Without CAP | With CAP |
|-----------|-------------|----------|
| **Enforcement** | Advisory CLAUDE.md (~60% compliance) | Hard blocking via PreToolUse exit(2) (100% compliance) |
| **Memory** | Flat tables, unbounded, no lifecycle | 3-tier scored memory with eviction + consolidation |
| **Routing** | Binary (trivial or full orchestration) | 3-tier adaptive (INLINE/LIGHTWEIGHT/FULL) with learning |
| **Database** | 4 separate DBs, inbox pattern | Single unified cap.db with WAL concurrent reads |
| **Cost** | Unbounded token usage, no visibility | Per-workflow budgets, monthly caps, kill switch |
| **Knowledge** | Re-reads files every session | Persistent hybrid index, sub-200ms retrieval |
| **Search** | `grep` / `find` on raw files | 3-channel fusion with graph traversal |
| **Security** | Hope for the best | Eval suite, enforcement audit trail, input validation |
| **Observability** | None | Live workflow progress, cost dashboards, routing analytics |

---

## Performance

| Metric | Target | Actual |
|--------|--------|--------|
| Hybrid search latency (p50) | < 100ms | ~50ms |
| Hybrid search latency (p99) | < 500ms | ~190ms |
| Embedding cost per query | < $0.001 | ~$0.0004 |
| Knowledge base capacity | 10K entries | Tested to 15K |
| Workflow budget accuracy | +/- 5% | +/- 3% |
| MCP server cold start | < 2s | ~1.2s |
| Session recall latency | < 50ms | ~25ms |

---

## CLI Reference

<details>
<summary><strong><code>cap status</code></strong> — Platform health overview</summary>

```bash
$ cap status

Claude Agent Platform v1.0.0
────────────────────────────
  Config:    ~/.cap/config.toml
  Database:  ~/.cap/cap.db

  Database (unified, WAL mode)
  ────────────────────────────
    cap.db         ✓  12.8 MB  (WAL: 1.2 MB)
    memory_active:   247 entries (composite_score avg: 0.62)
    memory_archive:   89 entries (compressed)
    enforcement:     12 violations this session
    routing:        156 decisions (learning: active, accuracy: 84%)

  MCP Servers
  ───────────
    cap-orchestrator  ● running   pid:48201   uptime: 2h 14m
    cap-memory        ● running   pid:48202   uptime: 2h 14m
    cap-code-intel    ● running   pid:48203   uptime: 2h 14m

  Hooks
  ─────
    pretool.py        ✓ registered   (enforcement: enabled)
    posttool.py       ✓ registered   (sync: enabled)

  Enforcement
  ───────────
    Mode: active   Passthrough: inactive   Violations today: 3
```

</details>

<details>
<summary><strong><code>cap knowledge</code></strong> — Search and manage the knowledge base</summary>

```bash
cap knowledge search "deployment pipeline"     # Hybrid search
cap knowledge search -s keyword "argocd"       # Keyword-only
cap knowledge search -s semantic "cost waste"  # Vector-only
cap knowledge add domain gitops "Sync waves execute in order 0→N"
cap knowledge status                           # Index health + stats
```

</details>

<details>
<summary><strong><code>cap session</code></strong> — View and manage session memory</summary>

```bash
cap session list                      # Recent sessions
cap session recall "lambda timeout"   # Find relevant past learnings
cap session learnings                 # All stored learnings
cap session learnings --category correction  # Just corrections
```

</details>

<details>
<summary><strong><code>cap workflow</code></strong> — Workflow lifecycle management</summary>

```bash
cap workflow list                      # Active + recent workflows
cap workflow status wf-a3f8c1d2e4     # Detailed status + cost
cap workflow watch                     # Live tail (team simulation view)
cap workflow kill wf-a3f8c1d2e4       # Emergency stop
cap workflow demo                      # Run a demo workflow
```

</details>

<details>
<summary><strong><code>cap fleet</code></strong> — MCP server fleet management</summary>

```bash
cap fleet status                      # All servers with health
cap fleet health-check                # Run health probes now
cap fleet discover                    # Find new MCP servers to manage
```

</details>

<details>
<summary><strong><code>cap budget</code></strong> — Cost tracking and limits</summary>

```bash
cap budget status                     # Monthly usage + forecasts
```

</details>

<details>
<summary><strong><code>cap doctor</code></strong> — Diagnose and fix platform issues</summary>

```bash
cap doctor                            # Check everything
cap doctor --fix                      # Auto-fix what's possible
cap doctor --db knowledge             # Check specific database
```

</details>

---

## Project Structure

```
claude-agent-platform/
├── src/cap/
│   ├── __init__.py
│   ├── py.typed                # PEP 561 type marker
│   ├── db.py                   # Unified SQLite (WAL mode), migrations, get_db()
│   ├── hooks/                  # Claude Code hook scripts (short-lived per tool call)
│   │   ├── __init__.py
│   │   ├── pretool.py         # PreToolUse: exit(2) = HARD BLOCK, enforcement logic
│   │   └── posttool.py        # PostToolUse: sync triggers, state updates
│   ├── enforcement/            # Enforcement bypass and state management
│   │   ├── __init__.py
│   │   └── passthrough.py     # Temporary bypass (5min TTL, max 3/hr, fully logged)
│   ├── memory/                 # 3-tier memory system (Working/Active/Archive)
│   │   ├── __init__.py
│   │   ├── scorer.py          # 4-weight composite scoring (recency/importance/relevance/frequency)
│   │   ├── manager.py         # Store/recall/search memory entries across tiers
│   │   ├── eviction.py        # Background eviction daemon (score < 0.15 → archive)
│   │   └── consolidation.py   # Cross-session dedup, cluster merging
│   ├── orchestration/          # Complexity routing and multi-agent delegation
│   │   ├── __init__.py
│   │   ├── router.py          # 3-tier adaptive routing (INLINE/LIGHTWEIGHT/FULL)
│   │   ├── context.py         # Inter-agent context passing protocol (ContextFrame)
│   │   └── scratchpad.py      # Inter-agent artifact sharing (temp files + refs)
│   ├── learning/               # Self-improvement from routing outcomes
│   │   ├── __init__.py
│   │   └── engine.py          # Record outcomes, recalculate thresholds, adapt
│   ├── cost/                   # Budget enforcement and tracking
│   │   ├── __init__.py
│   │   └── tracker.py         # Token usage, cost estimates, budget checks
│   ├── runtime/                # Environment detection and mode switching
│   │   ├── __init__.py
│   │   └── offline.py         # Network/budget state detection, graceful degradation
│   ├── integrity/              # Audit and verification
│   │   ├── __init__.py
│   │   └── witness.py         # Cryptographic audit trail for enforcement actions
│   ├── mcp/                    # MCP servers (stdio JSON-RPC, long-lived)
│   │   ├── __init__.py
│   │   ├── orchestrator.py    # Orchestration tools (route, delegate, checkpoint)
│   │   ├── memory.py          # Memory tools (store, recall, search, evict)
│   │   └── code_intel.py      # Code tools (structure, dependents, blast radius)
│   ├── cli/                    # Click + Rich CLI application
│   │   ├── __init__.py
│   │   ├── main.py            # All cap commands
│   │   └── init.py            # cap init (DB setup, hook generation, MCP registration)
│   ├── eval/                   # Evaluation framework
│   │   ├── framework.py       # EvalSuite, metrics, scoring, reports
│   │   ├── cli.py             # cap eval commands
│   │   └── suites/            # retrieval, session, security, workflow
│   └── data/                   # Bundled in wheel (installed by cap init)
│       ├── agents/            # 21 specialist agent definitions (.md)
│       └── config.toml.default
├── tests/
│   ├── test_retrieval.py       # Retrieval quality + degradation tests
│   ├── test_graph.py           # Knowledge graph traversal tests
│   ├── test_enforcement.py     # Hook enforcement + passthrough tests
│   ├── test_memory.py          # 3-tier memory scoring + eviction tests
│   └── test_routing.py         # Adaptive routing decision tests
├── docs/
│   ├── INSTALL.md              # Installation & credentials
│   ├── USAGE.md                # Usage scenarios
│   ├── CONFIGURATION.md        # Config reference
│   ├── DISTRIBUTION.md         # Build & share
│   ├── TECHNICAL.md            # This file
│   ├── ARCHITECTURE.md         # System design
│   └── adr/                    # Architecture Decision Records (ADR-001 through ADR-012)
├── pyproject.toml              # Build config (hatchling)
├── LICENSE                     # MIT
└── .gitignore
```

---

## Installation

> **Detailed instructions:** See [INSTALL.md](INSTALL.md) or use the quick start below.

### Requirements

- Python 3.11+
- AWS credentials (for Titan V2 embeddings — optional, degrades gracefully)
- Claude Code CLI installed

### From PyPI (recommended)

```bash
uv tool install claude-agent-platform
cap init
```

### From Source

```bash
git clone git@github.com:moia-dev/claude-agent-platform.git
cd claude-agent-platform
uv pip install -e ".[dev]"
cap init
```

### Verify

```bash
cap doctor
```

---

## Configuration

CAP uses `~/.cap/config.toml`:

```toml
[platform]
workspace = "moia-dev"

[database]
path = "~/.cap/cap.db"       # Unified SQLite database
wal_mode = true              # Concurrent reads via WAL
busy_timeout_ms = 5000       # Wait on write contention

[memory]
working_budget_tokens = 15000     # Hard cap for working memory
eviction_threshold = 0.15         # Score below this → archive
stale_days = 90                   # No access → mark stale
consolidation_on_session_end = true

[routing]
inline_threshold = 0.3            # Below this → INLINE tier
full_threshold = 0.65             # Above this → FULL tier
learning_batch_size = 50          # Decisions before recalculation

[enforcement]
enabled = true
max_undelegated_files = 3         # Block at this count
passthrough_ttl_seconds = 300     # 5-minute bypass
passthrough_max_per_hour = 3      # Rate limit

[budget]
monthly_cap_usd = 50.0
default_workflow_budget_tokens = 500000
```

---

## Knowledge Base Sync

The knowledge base starts empty after `cap init`. It populates through sync — an indexing process that scans your workspace, extracts content, generates embeddings, and builds the knowledge graph.

### When Sync Triggers

| Trigger | When it fires | Config key |
|:--------|:--------------|:-----------|
| **First sync** | Manually after install: `cap sync --workspace /path/to/project` | — |
| **Session start** | Each time Claude Code starts a new session | `knowledge.sync.auto_sync_on_session_start` |
| **After git pull** | When files change from a pull/fetch | `knowledge.sync.auto_sync_on_git_pull` |
| **Scheduled** | Every 60 minutes (background) | `knowledge.sync.scheduled_interval_minutes` |
| **Manual** | `cap sync` from CLI at any time | — |

### What Gets Indexed

Sync walks your workspace directory tree and indexes files matching these criteria:

- **Included:** `.md`, `.tf`, `.yaml`, `.yml`, `.json`, `.toml`, `.py`, `.ts`, `.js`, `.sh`, `.hcl`
- **Skipped (by default):** `.git/`, `node_modules/`, `vendor/`, `.terraform/`, `__pycache__/`, binaries, lock files, `.env`
- **Size limit:** Files above 500 KB are skipped (configurable via `knowledge.sync.max_file_size_kb`)

The skip patterns are configurable in `config.toml`:

```toml
[knowledge.sync]
skip_patterns = [
    '\\.git/', 'node_modules/', 'vendor/', '\\.terraform/',
    '__pycache__/', '\\.pyc$', '\\.lock$', '\\.env$',
    '\\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$',
]
```

### What Sync Produces

For each indexed file, sync creates:

1. **A knowledge entry** — title, content, source path, content type, stored in `knowledge.db`
2. **FTS5 index record** — full-text searchable via BM25 keyword matching
3. **Embedding vector** (if Bedrock is configured) — 1024-dim Titan V2 vector stored in LanceDB for semantic search
4. **Graph nodes and edges** — entities (services, modules, resources) and their relationships for graph traversal

### Sync Lifecycle After Installation

```
cap init
  └→ Creates empty knowledge.db (schema only, zero entries)

cap sync --workspace ~/my-project
  └→ First full index:
       1. Walk directory tree (respecting skip_patterns)
       2. Extract content from each file
       3. Generate embeddings via Bedrock Titan V2 (if credentials available)
       4. Build FTS5 index entries
       5. Extract entities → create graph nodes + edges
       6. Record sync state (timestamp, file count, status)

Next Claude Code session
  └→ auto_sync_on_session_start = true
       → Incremental sync: only re-indexes files changed since last sync
       → Uses git diff or filesystem mtime to detect changes
```

### Incremental vs Full Sync

| Mode | Trigger | What it does |
|:-----|:--------|:-------------|
| **Incremental** (default) | Session start, git pull, scheduled | Only processes files modified since last sync timestamp |
| **Full** | `cap sync --full` or `cap init --force` | Re-indexes everything from scratch, rebuilds graph |

### First-Time Setup After Install

After running `cap init`, the knowledge base is empty. To populate it:

```bash
# Index your primary workspace
cap sync --workspace /path/to/your/project

# Verify it worked
cap knowledge status
```

From this point forward, sync runs automatically on session start and after git pulls. You don't need to run `cap sync` manually again unless you want to force a full re-index or add a new workspace.

### Disabling Auto-Sync

```toml
[knowledge.sync]
auto_sync_on_session_start = false
auto_sync_on_git_pull = false
scheduled_interval_minutes = 0   # 0 = disabled
```

With these settings, sync only runs when you manually invoke `cap sync`.

---

## How It Works

### Retrieval Pipeline

```
Query → ┬─→ FTS5 (BM25 scoring)          ─→ ranked list ─┐
        ├─→ Titan V2 embed → LanceDB     ─→ ranked list ─┼─→ RRF Fusion → top-K
        └─→ Entity extraction → BFS graph ─→ ranked list ─┘
```

### Workflow Lifecycle

```
Start → Running ──→ Completed
          │    ↗
          ├─→ Budget Exceeded → Killed
          │
          └─→ Signal (pause/resume/kill)
```

### Memory Reinforcement

```
Observation → Match existing? ─Yes─→ Increase confidence (+0.1, cap 1.0)
                    │
                    No → Create new learning (confidence: 0.6)

Contradiction → Decrease confidence (-0.2)
Time decay → -0.01/week (minimum: 0.3)
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Install dev dependencies: `uv pip install -e ".[dev]"`
4. Run tests: `pytest`
5. Run linting: `ruff check src/ tests/`
6. Submit a PR

### Development

```bash
# Run tests with coverage
pytest --cov=cap --cov-report=term-missing

# Type checking (if adding types)
ruff check src/ tests/

# Run a specific MCP server locally
python -m cap.servers.knowledge_server

# Demo the team renderer
python -m cap.lib.team_renderer
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](ARCHITECTURE.md) | Full system architecture with diagrams |
| [ADRs](adr/) | Architecture Decision Records |
| `cap doctor` | Self-diagnosing health checks |
| `cap --help` | CLI reference |

---

## License

MIT - see [LICENSE](../LICENSE) for details.

---

## Related Documentation

| Doc | Link |
|:----|:-----|
| Installation & setup | [INSTALL.md](INSTALL.md) |
| Configuration reference | [CONFIGURATION.md](CONFIGURATION.md) |
| Usage guide | [USAGE.md](USAGE.md) |
| Build & distribution | [DISTRIBUTION.md](DISTRIBUTION.md) |
| System architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Architecture decisions | [adr/](adr/) |

---

*Back to [README](../README.md)*

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
[![Version](https://img.shields.io/badge/version-0.5.0-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/protocol-MCP%201.0-purple.svg)](https://modelcontextprotocol.io)

---

**Claude Agent Platform (CAP)** is an AI agent orchestration layer that gives Claude Code a persistent brain, hybrid search, budget-controlled workflows, and the illusion of a full engineering team working in parallel.

> Three sentences: CAP augments Claude Code CLI with hybrid knowledge retrieval (keyword + semantic + graph), session memory that persists learnings across conversations, and workflow orchestration that simulates a coordinated engineering team — all exposed via 4 MCP servers that Claude discovers automatically.

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
│                            Claude Code CLI (Host)                                │
│                                                                                 │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│   │  Workflow     │  │  Knowledge   │  │   Session    │  │    Fleet     │      │
│   │  Engine      │  │   Server     │  │   Server     │  │   Manager    │      │
│   │              │  │              │  │              │  │              │      │
│   │  budget ctl  │  │  hybrid srch │  │  memory      │  │  health mon  │      │
│   │  team sim    │  │  FTS5+vec+   │  │  learnings   │  │  auto-restart│      │
│   │  kill/signal │  │  graph+RRF   │  │  corrections │  │  discovery   │      │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│          │                  │                  │                  │              │
│   ┌──────┴──────────────────┴──────────────────┴──────────────────┴──────┐      │
│   │                         cap.lib (shared)                             │      │
│   │                                                                      │      │
│   │  retrieval.py ─ embeddings.py ─ graph.py ─ models.py ─ security.py  │      │
│   │  team_renderer.py ─ api_gateway.py ─ workflow_hooks.py ─ config.py  │      │
│   └──────────────────────────────┬───────────────────────────────────────┘      │
│                                  │                                               │
│   ┌──────────────────────────────┴───────────────────────────────────────┐      │
│   │                         Storage Layer                                 │      │
│   │                                                                       │      │
│   │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐     │      │
│   │  │platform.db │  │knowledge.db│  │sessions.db │  │ fleet.db   │     │      │
│   │  │            │  │            │  │            │  │            │     │      │
│   │  │ workflows  │  │ entries    │  │ learnings  │  │ servers    │     │      │
│   │  │ budgets    │  │ fts5 index │  │ corrections│  │ events     │     │      │
│   │  │ runs       │  │ graph nodes│  │ decisions  │  │ health     │     │      │
│   │  │ cost track │  │ graph edges│  │ confidence │  │ processes  │     │      │
│   │  └────────────┘  └────────────┘  └────────────┘  └────────────┘     │      │
│   │                                                                       │      │
│   │  ┌────────────────────────┐  ┌────────────────────────────────┐      │      │
│   │  │  LanceDB (vectors)     │  │  AWS Bedrock Titan V2          │      │      │
│   │  │  local, serverless     │  │  1024-dim embeddings           │      │      │
│   │  │  cosine similarity     │  │  $0.02 / 1M tokens             │      │      │
│   │  └────────────────────────┘  └────────────────────────────────┘      │      │
│   └───────────────────────────────────────────────────────────────────────┘      │
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

### 5. Zero-Config MCP Servers

Four MCP servers that integrate seamlessly — Claude Code discovers them automatically:

| Server | Database | Capabilities |
|--------|----------|-------------|
| `cap-knowledge` | knowledge.db | Hybrid search, add/update entries, graph queries |
| `cap-session` | sessions.db | Store/recall learnings, corrections, decisions |
| `cap-workflow` | platform.db | Start/stop/signal workflows, budget enforcement |
| `cap-fleet` | fleet.db | Health monitoring, auto-restart, server discovery |

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
| **Knowledge** | Re-reads files every session, no memory | Persistent hybrid index, sub-200ms retrieval |
| **Memory** | Forgets corrections, repeats mistakes | Reinforced learnings with confidence decay |
| **Workflows** | Single-shot prompts, no coordination | Multi-phase pipelines with team simulation |
| **Cost** | Unbounded token usage, no visibility | Per-workflow budgets, monthly caps, kill switch |
| **MCP Servers** | Manual restart on crash | Auto-health-check, restart with backoff |
| **Search** | `grep` / `find` on raw files | 3-channel fusion with graph traversal |
| **Security** | Hope for the best | Eval suite, path traversal blocking, input validation |
| **Observability** | None | Live workflow progress, cost dashboards |

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

Claude Agent Platform v0.5.0
────────────────────────────
  Config:    ~/.claude-platform/config.toml
  Data:      ~/.claude-platform/data/

  Databases
  ─────────
    platform.db    ✓  2.1 MB   (workflows: 47, budget_entries: 312)
    knowledge.db   ✓  8.4 MB   (entries: 1,247, graph_nodes: 892, fts5: synced)
    sessions.db    ✓  1.3 MB   (learnings: 89, corrections: 34, decisions: 21)
    fleet.db       ✓  0.4 MB   (servers: 4, healthy: 4)

  MCP Servers
  ───────────
    cap-knowledge     ● running   pid:48201   uptime: 2h 14m
    cap-session       ● running   pid:48202   uptime: 2h 14m
    cap-workflow      ● running   pid:48203   uptime: 2h 14m
    cap-fleet         ● running   pid:48204   uptime: 2h 14m

  Bedrock
  ───────
    Region: eu-central-1   Model: amazon.titan-embed-text-v2:0
    Status: ✓ authenticated   Cost today: $0.12
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
│   ├── py.typed                # PEP 561 type marker
│   ├── cli/                    # Click + Rich CLI application
│   │   ├── main.py            # All cap commands
│   │   ├── lifecycle.py       # Init, uninstall, backup/restore
│   │   ├── daemon.py          # Background workflow daemon
│   │   └── watch.py           # Live workflow tail
│   ├── lib/                    # Shared library
│   │   ├── retrieval.py       # 3-channel hybrid search + RRF fusion
│   │   ├── embeddings.py      # Bedrock Titan V2 async client
│   │   ├── graph.py           # Knowledge graph (BFS traversal)
│   │   ├── models.py          # Data models + pricing constants
│   │   ├── team_renderer.py   # Rich team simulation renderer
│   │   ├── api_gateway.py     # Concurrency + rate limiting
│   │   ├── hooks.py           # Lifecycle hooks system (correction_injection, tool_restriction, budget_check)
│   │   ├── repo_resolver.py   # GitHub auto-resolution engine
│   │   ├── security.py        # Input validation, path traversal prevention, repo_name validation
│   │   ├── config.py          # TOML config loader
│   │   ├── db_init.py         # Database initialization
│   │   └── db_maintenance.py  # Vacuum, WAL checkpoint, integrity
│   ├── servers/                # MCP servers (stdio JSON-RPC)
│   │   ├── knowledge_server.py
│   │   ├── session_server.py
│   │   ├── workflow_server.py
│   │   └── fleet_server.py
│   ├── eval/                   # Evaluation framework
│   │   ├── framework.py       # EvalSuite, metrics, scoring, reports
│   │   ├── cli.py             # cap eval commands
│   │   └── suites/            # retrieval, session, security, workflow
│   └── data/                   # Bundled in wheel (installed by cap init)
│       ├── agents/            # 21 specialist agent definitions (.md) — 5 Opus, 14 Sonnet, 2 Haiku with output contracts
│       ├── workflows/         # 10 workflow pipelines (.js)
│       └── config.toml.default
├── tests/
│   ├── test_retrieval.py       # Retrieval quality + degradation tests
│   └── test_graph.py           # Knowledge graph traversal tests
├── docs/
│   ├── INSTALL.md              # Installation & credentials
│   ├── USAGE.md                # Usage scenarios
│   ├── CONFIGURATION.md        # Config reference
│   ├── DISTRIBUTION.md         # Build & share
│   ├── TECHNICAL.md            # This file
│   ├── ARCHITECTURE.md         # System design
│   └── adr/                    # Architecture Decision Records
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

CAP uses `~/.claude-platform/config.toml`:

```toml
[platform]
workspace = "moia-dev"

[embeddings]
model_id = "amazon.titan-embed-text-v2:0"
dimensions = 1024
region = "eu-central-1"

[budget]
monthly_cap_usd = 50.0
default_workflow_budget_tokens = 500000

[fleet]
health_check_interval_s = 60
auto_restart = true
max_restart_attempts = 3
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

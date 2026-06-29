# CAP Usage Guide

## How CAP Works (30-Second Overview)

CAP (Claude Agent Platform) runs as **4 MCP servers** alongside Claude Code. They activate automatically during Claude sessions -- no manual triggering required.

```
┌──────────────────────────────────────────────────────────────┐
│                    Your Claude Code Session                    │
│                                                              │
│   cap-knowledge    cap-session    cap-workflow    cap-fleet   │
│   (retrieval)      (memory)       (orchestration) (health)   │
│        │                │               │              │     │
│        └────────────────┴───────────────┴──────────────┘     │
│                              │                               │
│                     ~/.claude-platform/data/                  │
│              knowledge.db  sessions.db  platform.db           │
└──────────────────────────────────────────────────────────────┘
```

**Three things to know:**

1. Knowledge is indexed from your workspace and available to Claude in every session, from any directory
2. Decisions and corrections persist across sessions -- Claude never repeats mistakes
3. Workflows render as team conversations so you can watch your agents collaborate

---

## When Things Trigger

This is the most important section. CAP works because things happen automatically at the right time.

---

### Knowledge Retrieval -- Triggers Automatically

**When you care:** Any time you ask Claude about your codebase, architecture, or domain.

**What happens:** Claude calls `knowledge_search` from the `cap-knowledge` MCP server. It runs hybrid retrieval (keyword + semantic + graph) and returns ranked results grounded in your actual codebase.

**Example interaction:**

```
You: "How does our alerting pipeline work?"

  [Claude internally]
  → calls knowledge_search(query="alerting pipeline")
  → cap-knowledge runs:
      1. FTS5 keyword search (BM25 scoring)
      2. Bedrock Titan V2 semantic search (cosine similarity)
      3. Knowledge graph traversal (BFS from "alerting" node)
  → RRF merges results: keyword(0.3) + semantic(0.5) + graph(0.2)
  → Returns top 10 ranked entries

Claude: "Your alerting pipeline uses Prometheus rules defined in the
         alerting/ repo, routed through Alertmanager to Slack (#platform-alerts)
         and PagerDuty for P1s. The rules are deployed via ArgoCD from
         argocd-platform/..."
```

> **Note:** The `workspace` parameter is optional. When omitted or set to `"all"`, knowledge search returns results from ALL indexed workspaces -- so agents can access the full knowledge base regardless of which directory they're invoked from. Pass a specific workspace path to scope results to that workspace only.

> **Tip:** If Claude's answers seem generic or lack codebase-specific context, check that knowledge has been indexed: `cap knowledge status`

**Graceful degradation:** If Bedrock is unavailable (throttled, network error), semantic search drops out silently. Retrieval continues with keyword + graph only, using rebalanced weights (0.6 keyword, 0.4 graph). You still get results -- just slightly less precise.

---

### Session Memory -- Triggers on Session Start

**When you care:** Every new Claude session. Corrections from past sessions are loaded so Claude does not repeat mistakes. Decisions are loaded so Claude does not re-decide things you already settled.

**What happens:** The `cap-session` MCP server's `session_start` tool loads three categories of memory:

| Category | What it contains | Priority |
|----------|-----------------|----------|
| Corrections | Things Claude got wrong and you corrected | Highest (loaded first) |
| Learnings | Patterns, conventions, preferences Claude discovered | High (confidence-ranked) |
| Decisions | Architecture choices, tool selections, rationale | Medium (recency-weighted) |

**Example of what gets loaded:**

```
Session starts in /workspace/moia-dev →

  Corrections (top 20, most recent first):
    - "Don't use mocks for database tests in this project" (category: technical)
    - "Branch names must start with JIRA ticket" (category: process)

  Learnings (top 30, confidence-ranked):
    - "This team uses ArgoCD app-of-apps pattern" (confidence: 0.95)
    - "Terraform state is in S3 with DynamoDB locking" (confidence: 0.90)
    - "Alerts go to #platform-alerts, not #engineering" (confidence: 0.85)

  Decisions (top 15, active only):
    - domain: architecture, decision: "DynamoDB for payment-service"
      rationale: "Sub-10ms p99 requirement rules out RDS"
    - domain: tooling, decision: "LanceDB for vector storage"
      rationale: "Embedded, no server dependency, Apache license"
```

> **Tip:** To see what Claude will load at next session start, run `cap session recall "topic"` or `cap session learnings`

**How learnings grow stronger:** Every time a learning is reinforced (Claude encounters the same pattern again), its confidence increases by 0.1, up to 1.0. Learnings below 0.5 confidence are not loaded.

---

### Team Simulation -- Triggers on Workflows

**When you care:** When Claude runs a multi-agent workflow (new service deployment, incident response, security audit, etc.).

**What happens:** Workflow events emit to `platform.db`. The `WorkflowObserver` polls the database and the `TeamRenderer` displays agent collaboration as a conversation -- like watching a Slack channel where your engineering team is working.

**Example output:**

```
━━━━━━━━━━━ Workflow: new-service-deployment ━━━━━━━━━━━
  Budget: $5.00 │ Max Agents: 12 │ Status: running

┌─ Phase: Architecture Design ──────────────────────────────────
│
│  [Architect] Starting architecture design for payment-service...
│  [Architect] → Team: "Proposed: EKS deployment, 3 replicas, ALB ingress,
│                        DynamoDB backend. Estimated $180/mo."
│  [Security] reviewing architecture proposal...
│  [Security] → Architect: "⚠ DynamoDB table needs encryption at rest
│                            enabled. IAM role is too broad — needs
│                            resource-level conditions."
│  [Architect] acknowledged, revising IAM scope...
│  [Architect] ✓ Architecture approved (2 revisions)
│
┌─ Phase: Implementation ───────────────────────────────────────
│
│  [Devops] picked up: Terraform modules + Helm chart
│  [Sre] picked up: alerting rules + dashboards
│  [Devops] → Sre: "Helm chart ready. Service exposes :8080/health
│                    and :8080/metrics. Need alerts for p99 > 500ms."
│  [Sre] acknowledged, creating alert rules...
│  [Devops] ✓ Terraform plan: +14 resources, 0 changes, 0 destroys
│  [Sre] ✓ 3 alerts configured: latency, error_rate, saturation
│
┌─ Phase: Review ───────────────────────────────────────────────
│
│  [Code-Review] reviewing all changes...
│  [Code-Review] → Devops: "⚠ Helm values.yaml: resource limits missing
│                            for sidecar container."
│  [Devops] fixing...
│  [Code-Review] ✓ All clear
│  [Security] final security review...
│  [Security] ✓ Approved — no issues remaining
│
└─ Complete ─────────────────────────────────────────────────────
   Duration: 2m 34s │ Cost: $3.42 │ Agents: 5 │ Status: ✓
```

**Three ways to see it:**

| Method | When to use |
|--------|-------------|
| `cap workflow watch` | Attach to an already-running workflow |
| `cap workflow watch <id>` | Watch a specific workflow by ID |
| `cap workflow daemon` | Auto-attach to ANY new workflow that starts |

---

### Budget Controls -- Always Active

**When you care:** Anytime you want to make sure Claude does not burn through your Bedrock budget.

**What happens:** The workflow engine tracks tokens consumed per workflow. If a workflow exceeds its budget cap, it gets killed automatically.

```
Budget enforcement flow:

  Workflow starts → budget_cap_usd set (default: $5.00)
       │
       ├── Each agent call → tokens tracked in budget_ledger
       │
       ├── At 80% of cap → warning event emitted
       │       (visible in team view: "⚡ Budget: $4.00/$5.00 (80%)")
       │
       └── At 100% of cap → workflow KILLED if kill_on_exceed = true
               (visible: "KILLED — budget exceeded")
```

> **Tip:** Check current spend with `cap budget status`. Set the monthly cap with your `config.toml` file (`budget.monthly_cap_usd`).

---

### Fleet Management -- Background Daemon

**When you care:** Rarely, unless an MCP server dies. Fleet Manager runs a health check loop every 30 seconds to keep all servers alive.

**What happens:**

```
Every 30 seconds:
  For each registered MCP server:
    1. Is the PID alive?              → yes: healthy
    2. No? Increment failure counter
    3. Failures >= 3?                 → auto-restart with exponential backoff
    4. Restart count >= 5?            → mark unhealthy, stop trying
```

You will almost never interact with this directly. If something feels broken, `cap fleet status` will show you what is going on.

---

## CLI Commands -- When to Use Each

### Daily Operations

| Command | When to use | What it shows |
|---------|-------------|---------------|
| `cap status` | Starting your day, after sleep, "is it working?" | DBs, servers, knowledge stats, budget |
| `cap doctor` | Something feels off, MCP not responding | Integrity checks, permission issues, WAL size |
| `cap doctor --fix --yes` | Confirmed issues, ready to auto-repair | Applies fixes (checkpoint WAL, fix permissions) |
| `cap sync -w .` | After cloning a new repo, manual refresh | Triggers knowledge indexing for workspace |

### Knowledge Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap knowledge search "topic"` | See what CAP knows before asking Claude | `cap knowledge search "alerting routing"` |
| `cap knowledge status` | Check index health, embedding coverage | Shows entry counts, queue status |
| `cap knowledge add` | Manually record team/ownership/convention | `cap knowledge add -c team -k "sre" -v '{"slack": "#sre"}'` |

### GitHub Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap github config` | Review GitHub org and clone settings | Shows configured org, clone path, SSH setting |
| `cap github resolve` | Manually resolve missing repo dependencies | `cap github resolve` — clones missing repos from org |
| `cap github deps` | See which repos have been auto-cloned | Shows dependency resolution history |

### Session Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap session list` | Review past session history | Shows timestamps, status, summaries |
| `cap session recall "topic"` | See what Claude remembers about a topic | `cap session recall "database choice"` |
| `cap session learnings` | Review all active learnings and confidence | Shows what Claude will apply next session |

### Workflow Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap workflow list` | See recent/active workflows | Shows status, budget usage, agents |
| `cap workflow watch` | Watch the latest workflow live | Renders team conversation in terminal |
| `cap workflow daemon` | Auto-watch ALL workflows (background) | Run in separate terminal tab |
| `cap workflow demo` | See what team rendering looks like | Runs synthetic demo |
| `cap workflow kill <id>` | Emergency stop a runaway workflow | `cap workflow kill abc123 -r "stuck"` |

### Budget Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap budget status` | Monthly cost review, pre-workflow check | Shows spend, cap, per-model breakdown |

### GitHub Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap github config` | Review GitHub org and clone settings | Shows configured org, clone path, SSH setting |
| `cap github resolve` | Manually resolve missing repo dependencies | `cap github resolve` — clones missing repos from org |
| `cap github deps` | See which repos have been auto-cloned | Shows dependency resolution history |

### Fleet Operations

| Command | When to use | Example |
|---------|-------------|---------|
| `cap fleet status` | Check if all MCP servers are running | Shows PID, health, restart count |
| `cap fleet health-check` | Force immediate health probe | Finds dead processes, updates status |
| `cap fleet discover` | After adding new MCP servers to config | Finds unmanaged servers in `.claude.json` |

---

## GitHub Auto-Resolution (NEW in v0.5.0)

Missing repository references are automatically resolved, cloned, and indexed.

**How it works:**

1. Agent references a repo (e.g., "See the alerting-repo for config details")
2. Knowledge graph detects the reference is unresolved
3. CAP checks `[github]` config for org name
4. Runs `gh repo clone org/repo-name --depth=1` over SSH
5. Repo cloned to `clone_base_path/repo-name`
6. Content automatically indexed into knowledge base
7. Next query finds the repo locally

**Configuration:**

```toml
[github]
org = "moia-dev"
clone_base_path = "~/Projects/moia"
use_ssh = true
auto_clone_on_missing_dep = true
max_auto_clones_per_session = 5
clone_depth = 1
```

**Key features:**

- **SSH-only by default** — all clones use SSH for security
- **Shallow clones** — configurable depth limits bandwidth
- **Session rate limit** — `max_auto_clones_per_session` prevents runaway clones
- **Automatic indexing** — cloned repos are immediately available for search
- **Manual override** — `cap github resolve` for explicit resolution

---

## Integration Points

### With Claude Code Workflows

Workflow scripts (`.claude/workflows/*.js`) emit events that CAP renders as team conversations. The integration is automatic:

```javascript
// In .claude/workflows/new-service-deployment.js

export const meta = {
  name: 'new-service-deployment',
  phases: [
    { title: 'Architecture Design', detail: 'Design the service' },
    { title: 'Implementation', detail: 'Build it' },
    { title: 'Review', detail: 'Validate quality and security' },
  ],
}

phase('Architecture Design')
const design = await agent('Design the service architecture', {
  agentType: 'aws-architect',
  model: 'opus',
})
// → Event: phase_start "Architecture Design"
// → Event: agent_start "architect"
// → Events auto-written to platform.db
// → WorkflowObserver picks them up → TeamRenderer displays them

phase('Implementation')
const infra = await agent(`Implement: ${design}`, {
  agentType: 'devops',
  model: 'sonnet',
})
```

### With Git Hooks

CAP can install a `post-merge` hook that triggers incremental knowledge sync after every `git pull`:

```bash
# Installed at .git/hooks/post-merge
#!/bin/sh
cap sync --trigger git_post_pull --workspace "$(pwd)" &
```

This means your knowledge base stays fresh as code changes come in.

### With Claude Code Session Hooks

CAP registers a session start hook in `.claude/settings.json`:

```json
{
  "hooks": {
    "session_start": ["cap sync --trigger session_start --workspace $CWD"]
  }
}
```

Every time you start a Claude session, CAP checks if your workspace knowledge is stale and refreshes it.

### With AWS Bedrock

CAP uses Bedrock for one thing: generating embeddings with Titan V2.

| Aspect | Detail |
|--------|--------|
| Model | `amazon.titan-embed-text-v2:0` |
| Dimensions | 1024 |
| Cost | ~$0.02 per 1M tokens (~$0.001 per knowledge sync) |
| Auth | AWS SSO profile from `config.toml` |
| Failure mode | Graceful degradation -- keyword + graph search still works |
| Processing | Async background queue, batches of 25 texts |

---

## Configuration Tuning

All configuration lives in `~/.claude-platform/config.toml`. Here are the most impactful settings:

### Retrieval Quality

These weights control how results from each search channel are combined:

```toml
[knowledge.retrieval]
default_strategy = "hybrid"
rrf_k = 60                                    # RRF smoothing constant

[knowledge.retrieval.weights]
keyword = 0.3    # FTS5 BM25 — fast, handles exact matches well
semantic = 0.5   # Titan V2 cosine similarity — understands meaning
graph = 0.2      # Knowledge graph BFS — finds structural relationships

# Used automatically when Bedrock is unavailable:
[knowledge.retrieval.fallback_weights]
keyword = 0.6
graph = 0.4
```

> **Tip:** If you find Claude returning too many irrelevant results, increase `semantic` weight. If it misses exact terms, increase `keyword` weight.

### Budget Limits

```toml
[budget]
monthly_cap_usd = 50.00           # Hard monthly spending limit
per_workflow_default_usd = 5.00   # Default cap per workflow run
warning_threshold = 0.8           # Alert at 80% of monthly cap
kill_on_exceed = true             # Auto-kill workflows over budget
```

### Session Memory

```toml
[session]
max_corrections_loaded = 20       # How many corrections to load at session start
max_learnings_loaded = 30         # How many learnings to surface
max_decisions_loaded = 15         # How many active decisions to load
recency_weight = 0.7              # 0.0 = relevance only, 1.0 = recency only
checkpoint_interval_seconds = 300  # Auto-save every 5 minutes
```

### Fleet Health

```toml
[fleet]
health_check_interval_seconds = 30  # How often to check server health
unhealthy_threshold = 3             # Failures before restart
max_restarts = 5                    # Give up after this many restarts
restart_backoff_base = 2.0          # Exponential backoff: 2s, 4s, 8s, 16s, 32s
auto_restart_enabled = true
```

### Knowledge Sync

```toml
[knowledge.sync]
auto_sync_on_session_start = true
auto_sync_on_git_pull = true
scheduled_interval_minutes = 60
max_file_size_kb = 500              # Skip files larger than 500KB
file_watch_enabled = false          # Real-time sync (resource intensive)
```

<details>
<summary><strong>Advanced: Embedding Configuration</strong></summary>

```toml
[bedrock]
region = "eu-central-1"
profile = "moia-platform-readonly"
embedding_model = "amazon.titan-embed-text-v2:0"
embedding_dimensions = 1024
embedding_batch_size = 25           # Texts per Bedrock API call
embedding_max_concurrent = 3        # Parallel Bedrock calls
embedding_max_input_tokens = 8192   # Titan V2 max context

[bedrock.retry]
max_retries = 3
base_delay_ms = 500
max_delay_ms = 10000
backoff_multiplier = 2.0
```

</details>

<details>
<summary><strong>Advanced: Database Maintenance Schedule</strong></summary>

```toml
[maintenance]
wal_checkpoint_threshold_mb = 50    # Checkpoint WAL when it exceeds 50MB
vacuum_growth_threshold_mb = 100    # VACUUM only when DB grew 100MB+
daily_prune_hour = 3                # Prune expired data at 3am
weekly_vacuum_day = 6               # Full VACUUM on Sundays
backup_retention_count = 5          # Keep last 5 backups
```

</details>

---

## Data Flow Diagram

Understanding where data lives and how it moves:

```
┌─────────────────┐         ┌─────────────────┐
│  Your Repos     │         │  Your Sessions  │
│  (git repos)    │         │  (Claude chats) │
└────────┬────────┘         └────────┬────────┘
         │                           │
    git hooks /                session_start /
    manual sync                session_end
         │                           │
         ▼                           ▼
┌─────────────────┐         ┌─────────────────┐
│  knowledge.db   │         │  sessions.db    │
│  ─────────────  │         │  ─────────────  │
│  entries (FTS5) │         │  corrections    │
│  graph nodes    │         │  learnings      │
│  graph edges    │         │  decisions      │
│  business facts │         │  checkpoints    │
└────────┬────────┘         └─────────────────┘
         │
    embed_batch()
         │
         ▼
┌─────────────────┐         ┌─────────────────┐
│  knowledge_     │         │  platform.db    │
│  vectors/       │         │  ─────────────  │
│  (LanceDB)      │         │  workflows      │
│  1024-dim vecs  │         │  budget_ledger  │
└─────────────────┘         │  fleet_servers  │
                            └─────────────────┘
```

---

## Troubleshooting

### "Claude does not seem to know about my codebase"

This means knowledge has not been indexed for your workspace.

```bash
# Step 1: Is the knowledge server running?
cap fleet status

# Step 2: Is there any content indexed?
cap knowledge status

# Step 3: Does search return anything?
cap knowledge search "your topic"

# Step 4: If empty, trigger a sync
cap sync --workspace . --trigger manual

# Step 5: If sync fails, check doctor
cap doctor
```

### "Workflows do not show the team view"

The team conversation view requires either an active watcher or the daemon:

```bash
# Option A: Watch a specific workflow
cap workflow watch

# Option B: Auto-watch all workflows (run in a separate terminal)
cap workflow daemon

# Option C: See what a workflow looks like
cap workflow demo
```

### "Claude keeps repeating the same mistake"

Session memory might not be recording corrections, or sessions might not be starting properly.

```bash
# Check if sessions are being recorded
cap session list

# Check if corrections are stored
cap session recall "the thing it keeps getting wrong"

# Verify session server is running
cap fleet status
cap fleet health-check
```

### "Budget warning / workflow killed"

```bash
# Check current spend
cap budget status

# See which workflows used the most
cap workflow list --status completed

# Adjust the cap in config
# Edit ~/.claude-platform/config.toml → [budget] monthly_cap_usd
```

### "MCP server not responding"

```bash
# Check all servers
cap fleet status

# Run health check (updates dead servers)
cap fleet health-check

# If a specific server is dead, restart it via Claude (fleet_restart tool)
# Or restart all CAP servers:
cap doctor --fix --yes
```

### "Database is getting large"

```bash
# Check sizes
cap status

# Doctor will report oversized WAL and suggest fixes
cap doctor

# Force checkpoint + prune (safe)
cap doctor --fix --yes
```

---

## Typical Day with CAP

```
08:30  Start Claude session
       → cap-session loads corrections, learnings, decisions
       → cap-knowledge checks workspace freshness (auto-sync if stale)

08:31  "How does payment processing work?"
       → Claude calls knowledge_search → gets grounded answer from YOUR code

09:00  "Deploy a new event-processing Lambda"
       → Workflow starts: architect → devops → security → review
       → Team view shows progress (if daemon running)
       → Budget tracked automatically

09:15  Workflow completes, Claude presents the result
       → Session records decisions made
       → Knowledge graph updated with new service relationships

10:00  "Actually, we should use SQS not EventBridge"
       → Claude records correction
       → Next session: correction loaded, Claude will not suggest EventBridge

12:00  Session ends
       → Learnings distilled and persisted
       → Knowledge base enriched with new entries
```

---

## Architecture Quick Reference

| Component | Database | What it does |
|-----------|----------|--------------|
| cap-knowledge | knowledge.db + LanceDB | Hybrid search (keyword + semantic + graph) |
| cap-session | sessions.db | Cross-session memory, corrections, learnings |
| cap-workflow | platform.db | Workflow orchestration, budget enforcement |
| cap-fleet | platform.db (read) | MCP server health monitoring, auto-restart |

| Database | Writer | Readers |
|----------|--------|---------|
| platform.db | workflow server | all servers |
| knowledge.db | knowledge server | all servers |
| sessions.db | session server | all servers |

Cross-server communication uses the **inbox pattern**: if server A needs to write to server B's database, it drops a JSONL file in `~/.claude-platform/inbox/<server-b>/`, which server B polls and processes. This eliminates WAL contention entirely.

---

## Quick Install Recap

```bash
uv tool install claude-agent-platform
cap init
cap status
```

What `cap init` does:
1. Creates `~/.claude-platform/` with correct permissions (0700)
2. Initializes all SQLite databases (0600 permissions)
3. Installs 14 agent definitions and 10 workflow scripts
4. Registers 4 MCP servers with Claude Code
5. Backs up existing configs before any modifications

After install, sync your first workspace:
```bash
cap sync --workspace /path/to/your/project
```

Then just use Claude normally. CAP works in the background.

> **Full install details:** See [INSTALL.md](INSTALL.md) | **Build from source:** See [DISTRIBUTION.md](DISTRIBUTION.md)

---

## Related Documentation

| Doc | Link |
|:----|:-----|
| Installation & setup | [INSTALL.md](INSTALL.md) |
| Configuration reference | [CONFIGURATION.md](CONFIGURATION.md) |
| Build & distribution | [DISTRIBUTION.md](DISTRIBUTION.md) |
| Technical architecture | [TECHNICAL.md](TECHNICAL.md) |
| System architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Architecture decisions | [adr/](adr/) |

---

*Back to [README](../README.md)*

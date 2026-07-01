# CLI Reference

All commands are accessed via `cap <command>`. This reference documents the complete command interface, organized by functional area.

## Getting Started

### `cap init`

Initialize CAP platform on first use or reset existing installation.

```bash
cap init [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--force` | Reinitialize even if already configured |
| `--minimal` | Create databases and config only (skip MCP/agents) |
| `--non-interactive` | Use defaults without prompting |
| `--skip-mcp` | Skip MCP server registration |
| `--skip-fetch` | Skip model capability probe |
| `--workspace PATH` | Set initial workspace to index |

Creates: SQLite databases (knowledge, sessions, budget), configuration files, installed agent definitions, initialized MCP servers.

Example:
```bash
cap init --workspace ~/projects/my-service
```

### `cap uninstall`

Remove CAP completely and restore original Claude Code settings.

```bash
cap uninstall [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--keep-data` | Keep databases (knowledge, sessions) for potential reinstall |
| `--yes` | Skip confirmation prompt |

Example:
```bash
cap uninstall --yes
```

### `cap status`

Display system health and configuration overview.

```bash
cap status
```

Output includes:
- Database health (SQLite file sizes, last sync time)
- MCP server status (registered, online/offline, health check results)
- Knowledge index stats (total indexed repos, file count, last sync)
- Budget status (today's spend, daily cap, % used)
- Indexing status (coverage %, pending items)
- Session memory (active sessions, retention policy)
- Provider and model availability (anthropic-api, bedrock, models available)

Example:
```bash
cap status
Databases: ✓ knowledge (82 MB), ✓ sessions (14 MB), ✓ budget (2 MB)
MCP Servers: 5 online, 0 offline
Knowledge Index: 47 repos, 18,234 files indexed (92% coverage)
Budget: $2.34 / $10.00 (23% used today)
Sessions: 12 active, 30-day retention
Provider: anthropic-api, models: claude-haiku-4, claude-sonnet-4, claude-opus-4
```

---

## Configuration

### `cap config show`

Display all current configuration as formatted output.

```bash
cap config show [--format TABLE|JSON]
```

Output includes all settings from `~/.claude/cap/config.json`, organized by section:
- `provider` — API provider and model assignments
- `bedrock` — AWS region, assume-role, profile
- `budget` — daily limit, pause state, workspace overrides
- `indexing` — strategy (hybrid, keyword, semantic), frequency, concurrency
- `github` — org, clone path, SSH/HTTPS, auto-clone settings
- `mcp` — registered servers, retry policy
- `knowledge` — retention policy, consolidation schedule
- `session` — memory retention, auto-cleanup

Example:
```bash
cap config show --format json | jq '.budget'
```

### `cap config set`

Set configuration values using dot-notation keys.

```bash
cap config set <KEY> <VALUE>
```

Common settings:

| Key | Value | Example |
|:----|:------|:--------|
| `provider` | `anthropic-api` \| `bedrock` | `cap config set provider bedrock` |
| `bedrock.region` | AWS region | `cap config set bedrock.region eu-west-1` |
| `bedrock.profile` | AWS profile name | `cap config set bedrock.profile prod` |
| `daily_budget_usd` | Dollar amount | `cap config set daily_budget_usd 15.0` |
| `indexing.strategy` | `hybrid` \| `keyword` \| `semantic` | `cap config set indexing.strategy semantic` |
| `github.org` | GitHub org name | `cap config set github.org myorg` |
| `github.use_ssh` | `true` \| `false` | `cap config set github.use_ssh true` |
| `mcp.auto_start` | `true` \| `false` | `cap config set mcp.auto_start true` |

Example:
```bash
cap config set daily_budget_usd 20.0
cap config set bedrock.region us-west-2
```

---

## Budget Management

### `cap budget status`

Show current spend vs. budget for today.

```bash
cap budget status [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to specific workspace (default: current) |
| `--raw` | Output raw JSON instead of formatted table |

Output:
```
┌─────────────────────────────────────────┐
│ Budget Status (2026-07-01)              │
├─────────────────────────────────────────┤
│ Daily Budget:     $10.00                │
│ Spent Today:      $3.45                 │
│ Remaining:        $6.55 (65% available) │
│ Status:           Active                │
├─────────────────────────────────────────┤
│ Top 5 Consumers:                        │
│  1. dev agent         $1.23             │
│  2. security agent    $0.89             │
│  3. test agent        $0.76             │
│  4. code-review       $0.42             │
│  5. orchestrator      $0.15             │
└─────────────────────────────────────────┘
```

### `cap budget history`

Show historical spending data.

```bash
cap budget history [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--days N` | Number of days to show (default: 30) |
| `--workspace PATH` | Scope to specific workspace |
| `--csv` | Export as CSV |

Example:
```bash
cap budget history --days 7
Date       | Spend  | Limit | %Used
-----------|--------|-------|-------
2026-07-01 | $3.45  | $10   | 34%
2026-06-30 | $8.23  | $10   | 82%
2026-06-29 | $5.12  | $10   | 51%
2026-06-28 | $9.87  | $10   | 99%
```

### `cap budget pause`

Immediately pause all agent execution and API calls.

```bash
cap budget pause
```

Paused state persists until resumed. Useful for avoiding overspend when approaching daily limit.

### `cap budget resume`

Resume agent execution after pause.

```bash
cap budget resume
```

### `cap budget reset`

Reset today's spend counter to zero.

```bash
cap budget reset [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Reset specific workspace only |
| `--yes` | Skip confirmation prompt |

**Warning**: This does not refund actual API costs; use for testing only.

### `cap budget raise <AMOUNT>`

Permanently increase daily budget cap.

```bash
cap budget raise <AMOUNT>
```

Example:
```bash
cap budget raise 5.0
# New daily budget: $10.00 + $5.00 = $15.00
```

---

## Knowledge Base

### `cap knowledge search`

Query the indexed knowledge base.

```bash
cap knowledge search <QUERY> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to specific workspace |
| `--strategy` | `hybrid` (default), `keyword`, `semantic`, `graph` |
| `--top-k N` | Return top N results (default: 10) |
| `--filter-domain` | Filter by domain (e.g., `kubernetes`, `security`) |
| `--include-code` | Include code snippets in results |

Search strategies:
- `hybrid` — Combines keyword matching + semantic search + graph traversal
- `keyword` — Exact term matching (fastest)
- `semantic` — Vector similarity search (most flexible)
- `graph` — Follow relationships (services, owners, dependencies)

Example:
```bash
cap knowledge search "alerting configuration" --strategy hybrid --top-k 5
cap knowledge search "EKS networking" --filter-domain kubernetes --include-code
```

### `cap knowledge add`

Manually add knowledge entries to the base.

```bash
cap knowledge add --category <CAT> --key <KEY> --value <VALUE> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--category` | `team`, `ownership`, `convention`, `glossary`, `incident` |
| `--key` | Unique identifier (required) |
| `--value` | Content (free text or JSON) |
| `--workspace PATH` | Target workspace |
| `--ttl-days N` | Auto-expire after N days |

Example:
```bash
cap knowledge add \
  --category team \
  --key "platform-team" \
  --value '{"members": ["alice", "bob"], "oncall": "alice"}'

cap knowledge add \
  --category convention \
  --key "terraform-naming" \
  --value "resource names use snake_case with prefix resource_type_"
```

### `cap knowledge status`

Show knowledge index health and statistics.

```bash
cap knowledge status [--workspace PATH]
```

Output:
```
Knowledge Index Status
├─ Indexed Repos: 47
├─ Total Files: 18,234
├─ Index Size: 82 MB
├─ Coverage: 92% (3 repos pending)
├─ Last Sync: 12 minutes ago
├─ Entries: 8,432
│  ├─ Code: 5,123
│  ├─ Docs: 2,104
│  ├─ Config: 945
│  └─ Other: 260
├─ Embedding Queue: 123 pending
└─ Last Errors: none
```

---

## Session Memory

### `cap session list`

List recent sessions and their status.

```bash
cap session list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Filter by workspace |
| `--limit N` | Max sessions to show (default: 20) |
| `--since DURATION` | Show sessions from last N (e.g., `24h`, `7d`) |
| `--status` | Filter: `active`, `completed`, `failed` |

Example:
```bash
cap session list --limit 10 --since 24h
ID       | Workspace        | Status    | Agents | Duration
---------|------------------|-----------|--------|----------
sess_456 | ~/projects/api   | completed | 3      | 4m 23s
sess_455 | ~/projects/infra | active    | 2      | 2m 15s
sess_454 | ~/projects/api   | completed | 1      | 1m 02s
```

### `cap session recall`

Search session memory for learnings and decisions.

```bash
cap session recall <QUERY> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to workspace |
| `--type` | `decision`, `learning`, `discovery`, `correction` |
| `--since DURATION` | Only recent sessions (e.g., `7d`) |

Example:
```bash
cap session recall "EKS node scaling" --type decision --since 7d
Found 2 learnings:
1. Decision: Use Karpenter v1 NodePool instead of cluster autoscaler for mixed workloads
   Reasoning: Better consolidation, native drift detection
   Date: 2026-06-28
   
2. Learning: KARPENTER_DRIFT_ENABLED causes unexpected node replacement
   Context: Had to disable for batch jobs with local state
   Date: 2026-06-25
```

### `cap session learnings`

Show active learnings across sessions.

```bash
cap session learnings [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Filter by workspace |
| `--category CAT` | Filter by type (`decision`, `bug`, `pattern`, `warning`) |
| `--recent-days N` | Only from last N days (default: 30) |

Example:
```bash
cap session learnings --category warning
Active Warnings (last 30 days):
1. Terraform apply to prod requires review gate (prevents accidents)
2. Bedrock model switching mid-task can cause token budget failures
3. ArgoCD sync to default-prod needs manual approval (blast radius)
```

---

## MCP Server Fleet

### `cap fleet status`

Show health status of all registered MCP servers.

```bash
cap fleet status [NAME]
```

Output for all servers:
```
MCP Fleet Status
├─ aws-eks (running) - 3s last ping - healthy
├─ kubernetes (running) - 2s last ping - healthy
├─ aws-docs (running) - 4s last ping - healthy
├─ aws-iam (running) - 2s last ping - healthy
└─ terraform (stopped) - last seen 2h ago
```

Output for specific server:
```bash
cap fleet status kubernetes
Name:           kubernetes
Status:         running
Version:        v1.28.0
Last Ping:      2s ago
Health:         healthy
Capabilities:   kubectl_get, kubectl_exec, kubectl_apply
Resource Use:   142 MB memory, 0.2% CPU
```

### `cap fleet discover`

Auto-discover MCP servers from workspace configurations.

```bash
cap fleet discover [--workspace PATH]
```

Scans workspace for:
- `mcp-servers.json` configuration
- `.claude/mcp-servers.json` local overrides
- Environment variables `MCP_*_URL`
- Docker daemon for containerized MCP servers

Registers discovered servers and performs health check.

### `cap fleet health-check`

Run immediate health check on all fleet servers.

```bash
cap fleet health-check
```

Reports latency, available tools, and any errors. Useful for diagnosing connectivity issues.

---

## Workflow Execution

### `cap workflow list`

List all workflows (active, completed, failed).

```bash
cap workflow list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--status TYPE` | Filter: `running`, `completed`, `failed`, `cancelled` |
| `--limit N` | Max results (default: 20) |
| `--since DURATION` | Show workflows from last N (e.g., `24h`) |

Example:
```bash
cap workflow list --status running
ID        | Name              | Status   | Progress | Started
----------|-------------------|----------|----------|----------
wf_789    | deploy-prod-api   | running  | 5/8      | 2m 14s ago
wf_788    | security-audit    | running  | 1/10     | 8s ago
```

### `cap workflow status`

Get detailed status of a specific workflow.

```bash
cap workflow status <WORKFLOW_ID>
```

Output includes:
- Overall progress (X of N steps completed)
- Current step executing
- All step results (success/failure, duration, cost)
- Remaining steps
- Total estimated time remaining
- Cost breakdown by agent tier

### `cap workflow watch`

Watch workflow execution in real-time.

```bash
cap workflow watch <WORKFLOW_ID> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--poll SECONDS` | Poll interval (default: 2.0) |
| `--tail N` | Show last N log lines (default: 10) |

Interactive display with live updates of step progress, agent assignments, and cost accumulation.

### `cap workflow kill`

Terminate a running workflow.

```bash
cap workflow kill <WORKFLOW_ID> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--reason TEXT` | Reason for killing (logged for audit) |
| `--force` | Force kill without graceful shutdown |

Example:
```bash
cap workflow kill wf_789 --reason "User requested cancellation"
```

### `cap workflow daemon`

Start background daemon to auto-execute pending workflows.

```bash
cap workflow daemon [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--poll SECONDS` | Check interval for new workflows (default: 10) |
| `--bg` | Run in background with process ID output |
| `--max-parallel N` | Max workflows to run simultaneously (default: 2) |
| `--log-file PATH` | Write daemon logs to file |

Example:
```bash
cap workflow daemon --bg --max-parallel 3 --log-file /var/log/cap-workflow.log
Daemon started (PID: 3847)
```

---

## GitHub Configuration

### `cap github config`

Configure GitHub organization and repo resolution behavior.

```bash
cap github config [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--org NAME` | GitHub organization name |
| `--clone-path PATH` | Base directory for cloned repos (e.g., `~/repos`) |
| `--ssh` | Use SSH for cloning (default) |
| `--https` | Use HTTPS for cloning |
| `--auto-clone` | Enable auto-clone on repo references |
| `--no-auto-clone` | Disable auto-clone |
| `--depth N` | Clone depth (0 = full history, default: 1) |
| `--max-clones N` | Max auto-clones per session (default: 5) |
| `--show` | Display current config |

Example:
```bash
cap github config --org myorg --clone-path ~/projects --ssh --auto-clone --max-clones 10
```

### `cap github resolve`

Resolve and clone a specific repository by name.

```bash
cap github resolve <REPO_NAME> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--domain HINT` | Subdirectory hint if repo has multiple domains |
| `--depth N` | Clone depth (default: 1) |

Example:
```bash
cap github resolve my-service
# Clones org/my-service to ~/repos/my-service and indexes into knowledge base
```

### `cap github deps`

Find and resolve unresolved dependencies across workspace.

```bash
cap github deps [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to specific workspace |
| `--auto-clone` | Auto-clone missing repos |
| `--max-clones N` | Limit clones (default: 10) |
| `--report` | Generate dependency report (JSON) |

Scans for:
- Terraform `remote_state` references to unindexed repos
- ArgoCD manifests with unresolved `repoURL`
- Import statements in code referencing internal packages
- Helm chart dependencies

Example:
```bash
cap github deps --auto-clone --report deps.json
Resolved 3 missing repos: platform-infra, shared-libs, schemas
1 repo failed to resolve (rate limit)
Report written to deps.json
```

---

## Indexing

### `cap index run`

Trigger knowledge base indexing pipeline.

```bash
cap index run [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Index specific workspace (default: all) |
| `--budget AMOUNT` | Max spend for this indexing run (USD) |
| `--skip-llm` | Skip LLM-based indexing (use keyword only) |
| `--skip-embeddings` | Skip vector embedding generation |
| `--full` | Force full re-index (not incremental) |
| `--concurrency N` | Parallel jobs (default: 4) |

Example:
```bash
cap index run --workspace ~/projects/api --budget 2.0 --concurrency 8
Indexing ~/projects/api with $2.00 budget
Files to process: 1,234
Progress: [████████░░] 85% (1,048 files, $1.87 spent)
```

### `cap index status`

Show indexing state and progress.

```bash
cap index status
```

Output:
```
Indexing Status
├─ Workspace Coverage: 92% (47 of 51 repos)
├─ Files Indexed: 18,234
├─ Last Sync: 12 minutes ago
├─ Pending: 123 files (queue processing)
├─ Budget Remaining (today): $4.23
├─ Estimated Time (pending): 4m
└─ Repos Pending:
   ├─ platform-infra (234 files)
   ├─ schemas (45 files)
   ├─ docs (19 files)
   └─ tools (8 files)
```

### `cap index deps`

Show resolved dependencies from knowledge graph.

```bash
cap index deps [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--repo REPO_NAME` | Show deps for specific repo |
| `--type` | Filter: `terraform`, `helm`, `argocd`, `python`, `go` |
| `--missing` | Show only unresolved dependencies |
| `--graph` | Export as DOT or Mermaid graph |

Example:
```bash
cap index deps --repo platform-infra --type terraform
Terraform Dependencies:
├─ state://prod-account/vpc → ../modules/vpc
├─ state://prod-account/eks → ../modules/eks
├─ source = "../../shared/iam"
└─ Missing: github.com/my-org/provider-plugin (not indexed)
```

### `cap index graph`

Query and visualize the knowledge graph.

```bash
cap index graph [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--node ENTITY` | Start from entity (service, repo, file) |
| `--connected` | Show all connected nodes (depth 3) |
| `--domain DOMAIN` | Filter by domain (kubernetes, security, data) |
| `--tag TAG` | Filter by tag |
| `--stats` | Show graph statistics |
| `--export FORMAT` | Export as `dot` (GraphViz) or `mermaid` |

Example:
```bash
cap index graph --node eks-cluster --domain kubernetes --export mermaid
graph TD
    eks-cluster[EKS Cluster]
    eks-cluster -->|depends on| vpc[VPC]
    eks-cluster -->|uses| karpenter[Karpenter]
    eks-cluster -->|owns| argocd[ArgoCD]
    vpc -->|contains| subnets[Subnets]
```

### `cap index daemon`

Configure automatic re-indexing daemon.

```bash
cap index daemon [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--interval MINUTES` | Re-index every N minutes (default: 60) |
| `--enable` | Start daemon |
| `--disable` | Stop daemon |
| `--status` | Show daemon status |
| `--log-file PATH` | Write logs to file |

Example:
```bash
cap index daemon --enable --interval 30 --log-file /var/log/indexing.log
Daemon enabled: re-index every 30 minutes
```

---

## System Operations

### `cap doctor`

Comprehensive platform diagnostics and self-repair.

```bash
cap doctor [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--fix` | Auto-fix detected issues |
| `--verbose` | Detailed diagnostics |

Checks:
- Database integrity and corruption
- MCP server connectivity
- Configuration validation
- API credential validity
- Knowledge index consistency
- Orphaned temporary files
- Recommended agent updates

Example:
```bash
cap doctor --fix
Running diagnostics...
✓ Databases: healthy (3 files, 98 MB)
✓ MCP servers: 5 online
! Config: deprecated settings detected
  Fixed: removed legacy auth_token setting
✓ API credentials: valid
✓ Knowledge index: consistent (18,234 files)
✗ Orphaned files: 234 MB in /tmp/cap-*
  Fixed: cleaned up 234 MB
```

### `cap db-doctor`

Database-specific integrity checking and repair.

```bash
cap db-doctor [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--fix` | Attempt to fix issues |
| `--yes` | Apply fixes without confirmation |
| `--db NAME` | Check specific database: `knowledge`, `sessions`, `budget` |

Checks and repairs:
- Foreign key consistency
- Index fragmentation
- Orphaned records
- Duplicate entries
- Transaction log corruption

### `cap backup`

Create a complete backup of CAP data.

```bash
cap backup [--output PATH]
```

| Option | Description |
|:-------|:------------|
| `--output PATH` | Backup destination (default: `~/.claude/backups/cap-YYYY-MM-DD.tar.gz`) |

Backs up:
- All databases (knowledge, sessions, budget)
- Configuration files
- Custom agent definitions
- Session recordings for audit

### `cap restore`

Restore CAP data from backup.

```bash
cap restore [--from PATH]
```

| Option | Description |
|:-------|:------------|
| `--from PATH` | Backup file to restore |

Example:
```bash
cap restore --from ~/.claude/backups/cap-2026-07-01.tar.gz
```

---

## Agent Health and Diagnostics

### `cap health`

Show agent health dashboard.

```bash
cap health [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `-a AGENT_TYPE` | Show details for specific agent type |
| `--raw` | Output raw JSON |

Example:
```bash
cap health -a dev
Agent: dev (model: sonnet)
├─ Status: healthy
├─ Success Rate: 94% (last 100 tasks)
├─ Avg Duration: 2m 15s
├─ Avg Cost: $0.23 per task
├─ Last 5 Tasks:
│  ├─ ✓ API endpoint implementation (1m 45s, $0.18)
│  ├─ ✓ Bug fix in auth module (45s, $0.08)
│  ├─ ✓ Type safety migration (3m 12s, $0.31)
│  ├─ ✓ CLI flag addition (28s, $0.05)
│  └─ ✓ Error handling refactor (2m 23s, $0.22)
└─ Recent Errors: none
```

### `cap dlq list`

Show dead-letter queue of failed tasks.

```bash
cap dlq list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--limit N` | Max items (default: 20) |
| `--since DURATION` | Show from last N (e.g., `24h`) |

### `cap dlq retry`

Retry a failed task from DLQ.

```bash
cap dlq retry <TASK_ID> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--force` | Retry even if max attempts exceeded |

### `cap dlq dismiss`

Permanently dismiss a failed task.

```bash
cap dlq dismiss <TASK_ID>
```

### `cap dlq retry-all`

Retry all tasks in DLQ.

```bash
cap dlq retry-all [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--since DURATION` | Retry tasks from last N (e.g., `24h`) |
| `--limit N` | Max items to retry (default: all) |

### `cap orch-status`

Show orchestration system status and diagnostics.

```bash
cap orch-status
```

Output:
```
Orchestrator Status
├─ Mode: normal
├─ Active Workflows: 2
├─ Queued Tasks: 7
├─ DLQ Size: 1 item
├─ Budget State: active ($6.55 remaining)
├─ Model Availability:
│  ├─ haiku: ✓ available (1,200 TPM)
│  ├─ sonnet: ✓ available (1,800 TPM)
│  └─ opus: ✓ available (800 TPM)
└─ Last Error: none
```

---

## Workflow and Task Management

### `cap backlog list`

List backlog tasks.

```bash
cap backlog list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--status` | Filter: `open`, `in-progress`, `blocked`, `done` |
| `--priority` | Filter: `critical`, `high`, `medium`, `low` |

### `cap backlog stats`

Show backlog statistics.

```bash
cap backlog stats
```

### `cap decisions list`

List decision cards and their status.

```bash
cap decisions list [--status pending|resolved]
```

### `cap conflicts list`

List active conflicts between agents or systems.

```bash
cap conflicts list [--severity blocking|advisory]
```

---

## Infrastructure Validation

### `cap drift check`

Check for Terraform drift between state and actual infrastructure.

```bash
cap drift check [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Check specific workspace |
| `--resource TYPE` | Check specific resource type (e.g., `aws_instance`) |
| `--fix` | Auto-remediate drift (requires approval) |

---

## Daemon Management

### `cap daemon start`

Start the background daemon.

```bash
cap daemon start [--bg]
```

### `cap daemon stop`

Stop the background daemon.

```bash
cap daemon stop
```

### `cap daemon restart`

Restart the daemon.

```bash
cap daemon restart
```

### `cap daemon status`

Show daemon status.

```bash
cap daemon status
```

### `cap daemon logs`

Stream daemon logs.

```bash
cap daemon logs [--tail N]
```

---

## Git and Version Control

### `cap git ingest`

Ingest git history into knowledge base.

```bash
cap git ingest [OPTIONS]
```

| Option | Description |
|:-------|:=========|
| `--workspace PATH` | Ingest specific workspace |
| `--since DATE` | Only commits after DATE |
| `--limit N` | Max commits to process |

Extracts and indexes:
- Commit messages (for decision history)
- PR descriptions (for context)
- Code author information (for ownership)
- Conventional Commit metadata (for automation)

---

## Audit and Witness

### `cap witness`

Show audit trail of CAP operations.

```bash
cap witness [OPTIONS]
```

| Option | Description |
|:-------|:=========|
| `--since DURATION` | Show from last N (e.g., `24h`) |
| `--action TYPE` | Filter: `config_change`, `api_call`, `workflow_execution` |
| `--export FORMAT` | Export as `json` or `csv` |

---

## Evaluation Framework

### `cap eval run`

Run evaluation suites to measure agent quality.

```bash
cap eval run [SUITE_NAME] [OPTIONS]
```

| Option | Description |
|:-------|:=========|
| `--agent AGENT_TYPE` | Test specific agent type |
| `--timeout SECONDS` | Max time per test case |

### `cap eval list`

List available evaluation suites.

```bash
cap eval list
```

### `cap eval report`

Generate evaluation report.

```bash
cap eval report [--format html|json|markdown]
```

---

## Environment and Help

### `cap help`

Show help for CAP or specific command.

```bash
cap help [COMMAND]
```

### `cap version`

Show CAP version.

```bash
cap version
```

### `cap env`

Show CAP environment variables and settings.

```bash
cap env [--json]
```

---

## Cross-References

- [Installation](installation.md) — First-time setup, system requirements
- [Configuration](configuration.md) — Detailed config file format, workspace settings
- [Agents](agents.md) — Agent types, roles, model tier assignments
- [Architecture](architecture.md) — System design, data flows, MCP integration

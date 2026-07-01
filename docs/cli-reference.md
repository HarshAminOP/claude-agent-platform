# CLI Reference

All commands are accessed via `cap <command>`.

## Top-Level Commands

### `cap init`

Initialize CAP platform (databases, config, MCP server registration).

```bash
cap init [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--minimal` | Create databases and config only (no MCP/agents) |
| `--force` | Reinitialize even if already configured |
| `--skip-mcp` | Skip MCP server registration |
| `--workspace PATH` | Set initial workspace to index |
| `--skip-fetch` | Skip model probe (use defaults) |
| `--non-interactive` | Use defaults without prompting |

### `cap uninstall`

Remove CAP and restore original Claude Code settings.

```bash
cap uninstall [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--keep-data` | Keep databases (knowledge, sessions) |
| `--yes` | Skip confirmation prompt |

### `cap status`

Display system health overview.

```bash
cap status
```

Shows: databases, MCP servers, knowledge index stats, budget, provider.

### `cap sync`

Trigger knowledge base sync for a workspace.

```bash
cap sync [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Target workspace (default: cwd) |
| `--trigger TYPE` | Trigger type: `manual`, `session_start`, `git_post_pull` |
| `--full` | Force full re-index (not incremental) |

### `cap doctor`

Diagnose and optionally fix platform issues.

```bash
cap doctor
```

### `cap db-doctor`

Database integrity check and repair.

```bash
cap db-doctor [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--fix` | Attempt to fix issues |
| `--yes` | Apply fixes without confirmation |
| `--db NAME` | Check specific database only |

### `cap backup`

Create a backup of CAP data.

```bash
cap backup
```

### `cap restore`

Restore CAP data from backup.

```bash
cap restore
```

---

## `cap config`

View and modify configuration.

### `cap config show`

Display current configuration.

```bash
cap config show
```

### `cap config set`

Set a configuration value.

```bash
cap config set <KEY> <VALUE>
```

Examples:
```bash
cap config set daily_budget_usd 10.0
cap config set provider anthropic-api
```

---

## `cap knowledge`

Knowledge base operations.

### `cap knowledge search`

Search indexed knowledge.

```bash
cap knowledge search <QUERY> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to workspace |
| `--strategy TYPE` | `hybrid` (default), `keyword`, `semantic`, `graph` |
| `--top-k N` | Number of results (default: 10) |

Example:
```bash
cap knowledge search "alerting configuration" --strategy hybrid --top-k 5
```

### `cap knowledge add`

Add a knowledge entry.

```bash
cap knowledge add --category <CAT> --key <KEY> --value <VALUE> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--category` | `team`, `ownership`, `convention`, `glossary`, `incident` |
| `--key` | Unique identifier |
| `--value` | Content (JSON for structured data) |
| `--workspace PATH` | Target workspace |

### `cap knowledge status`

Show knowledge index health.

```bash
cap knowledge status [--workspace PATH]
```

---

## `cap session`

Session memory management.

### `cap session list`

List recent sessions.

```bash
cap session list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Filter by workspace |
| `--limit N` | Max sessions to show (default: 20) |

### `cap session recall`

Search session memory.

```bash
cap session recall <QUERY> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to workspace |

### `cap session learnings`

Show active learnings.

```bash
cap session learnings [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--category CAT` | Filter by category |
| `--workspace PATH` | Filter by workspace |

---

## `cap fleet`

MCP server fleet management.

### `cap fleet status`

Show health status of managed servers.

```bash
cap fleet status [NAME]
```

### `cap fleet discover`

Auto-discover MCP servers from workspace config.

```bash
cap fleet discover [--workspace PATH]
```

### `cap fleet health-check`

Run immediate health check on all servers.

```bash
cap fleet health-check
```

---

## `cap workflow`

Workflow engine management.

### `cap workflow list`

List workflows.

```bash
cap workflow list [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--status TYPE` | Filter: `running`, `completed`, `failed`, `killed` |

### `cap workflow status`

Get detailed workflow status.

```bash
cap workflow status <RUN_ID>
```

### `cap workflow watch`

Watch workflow progress in real time.

```bash
cap workflow watch [WORKFLOW_ID] [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--poll SECONDS` | Poll interval (default: 2.0) |

### `cap workflow kill`

Kill a running workflow.

```bash
cap workflow kill <RUN_ID> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--reason TEXT` | Reason for killing |

### `cap workflow daemon`

Start the workflow daemon (auto-watches for pending work).

```bash
cap workflow daemon [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--poll SECONDS` | Poll interval |
| `--bg` | Run in background |

---

## `cap budget`

Cost tracking and budget management.

### `cap budget status`

Current spend vs. limits.

```bash
cap budget status [--workspace PATH]
```

### `cap budget history`

Historical spending.

```bash
cap budget history [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--days N` | Number of days (default: 30) |
| `--workspace PATH` | Filter by workspace |

### `cap budget pause`

Pause all spending.

```bash
cap budget pause
```

### `cap budget resume`

Resume spending after pause.

```bash
cap budget resume
```

### `cap budget reset`

Reset budget counters.

```bash
cap budget reset [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Reset specific workspace |
| `--confirm` | Skip confirmation |

### `cap budget raise`

Increase daily budget limit.

```bash
cap budget raise <AMOUNT>
```

---

## `cap github`

GitHub org configuration and repo resolution.

### `cap github config`

Configure GitHub settings.

```bash
cap github config [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--org NAME` | GitHub org name |
| `--clone-path PATH` | Base path for cloned repos |
| `--ssh` | Use SSH protocol |
| `--https` | Use HTTPS protocol |
| `--auto-clone` | Enable auto-clone |
| `--no-auto-clone` | Disable auto-clone |
| `--depth N` | Clone depth (0=full) |
| `--max-clones N` | Max auto-clones per session |
| `--show` | Show current config |

### `cap github resolve`

Resolve a specific repo.

```bash
cap github resolve <REPO_NAME> [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--domain HINT` | Subdirectory domain hint |

### `cap github deps`

Find and resolve unresolved dependencies.

```bash
cap github deps [OPTIONS]
```

| Option | Description |
|:-------|:------------|
| `--workspace PATH` | Scope to workspace |
| `--auto-clone` | Auto-clone missing repos |
| `--max-clones N` | Clone limit |

---

## `cap eval`

Quality evaluation framework.

### `cap eval run`

Run evaluation suites.

```bash
cap eval run [SUITE_NAME]
```

### `cap eval list`

List available evaluation suites.

```bash
cap eval list
```

### `cap eval report`

Generate evaluation report.

```bash
cap eval report
```

---

## `cap backlog`

Task backlog management.

### `cap backlog list`

List backlog tasks.

```bash
cap backlog list
```

### `cap backlog stats`

Show backlog statistics.

```bash
cap backlog stats
```

---

## `cap decisions`

Decision card management.

### `cap decisions list`

List pending and resolved decisions.

```bash
cap decisions list
```

---

## `cap conflicts`

Conflict resolution.

### `cap conflicts list`

List active conflicts.

```bash
cap conflicts list
```

---

## `cap drift`

Terraform drift detection.

### `cap drift check`

Check for infrastructure drift.

```bash
cap drift check
```

---

## `cap daemon`

Background daemon management.

### `cap daemon status`

Show daemon status.

```bash
cap daemon status
```

### `cap daemon start`

Start the background daemon.

```bash
cap daemon start
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

---

## `cap git ingest`

Ingest git history into knowledge base.

```bash
cap git ingest [OPTIONS]
```

---

## `cap witness`

Audit trail and integrity verification.

```bash
cap witness
```

---

## `cap health`

Agent health status.

```bash
cap health
```

---

## `cap dlq`

Dead-letter queue management.

```bash
cap dlq
```

---

## `cap resume`

Resume a failed workflow step.

```bash
cap resume
```

---

## `cap orch-status`

Orchestrator internal status.

```bash
cap orch-status
```

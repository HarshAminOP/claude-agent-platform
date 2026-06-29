# CAP Configuration Reference

All CAP configuration lives in a single TOML file:

```
~/.claude-platform/config.toml
```

This file is created by `cap init` with sensible defaults. You can modify it at any time — changes take effect on the next MCP server restart (or immediately for CLI commands).

---

## Configuration File Structure

```toml
[platform]              # General platform settings
[github]                # GitHub org config and auto-resolution
[bedrock]               # AWS Bedrock embedding configuration
[bedrock.retry]         # Retry/backoff for Bedrock API calls
[models.*]              # Cost per model tier (for budget tracking)
[concurrency]           # Agent concurrency pool sizing
[budget]                # Spending limits and kill switches
[knowledge.retrieval]   # Search strategy, weights, tuning
[knowledge.sync]        # Auto-sync triggers and file filters
[knowledge.graph]       # Graph traversal depth limits
[session]               # Session memory loading behavior
[fleet]                 # MCP server health monitoring
[maintenance]           # Database maintenance schedules
```

---

## Sections

### `[platform]`

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `version` | string | `"0.5.0"` | Config schema version (do not change manually) |
| `log_level` | string | `"INFO"` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

### `[github]`

GitHub organization configuration for automatic repository resolution and dependency management.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `org` | string | _(commented)_ | GitHub organization name (e.g., `moia-dev`) |
| `clone_base_path` | string | `"~/Projects"` | Base directory for auto-cloned repos |
| `use_ssh` | bool | `true` | Use SSH-only for clones (recommended for security) |
| `auto_clone_on_missing_dep` | bool | `true` | Auto-clone missing dependency repos |
| `max_auto_clones_per_session` | int | `5` | Limit auto-clones to prevent runaway |
| `default_branch` | string | `"main"` | Default branch to clone (fallback: detect from repo) |
| `clone_depth` | int | `1` | Shallow clone depth (1 = single commit, 0 = full history) |

**Example: Enable GitHub auto-resolution**

```toml
[github]
org = "moia-dev"
clone_base_path = "~/Projects/moia"
use_ssh = true
auto_clone_on_missing_dep = true
max_auto_clones_per_session = 10
clone_depth = 1
```

**How it works:**

1. Agent references a repository from your GitHub org (e.g., "See alerting-repo for details")
2. Knowledge server detects missing repo via knowledge graph
3. Runs `gh repo clone org/repo --depth=1` (SSH-only)
4. Repo auto-cloned to `clone_base_path/repo-name`
5. Content automatically indexed into knowledge base
6. All subsequent references find the repo locally

---

### `[bedrock]`

Controls the AWS Bedrock connection for generating vector embeddings.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `region` | string | `"eu-central-1"` | AWS region where Bedrock Titan is accessible |
| `profile` | string | _(commented)_ | AWS profile name (SSO or static). Falls back to `AWS_PROFILE` env var |
| `embedding_model` | string | `"amazon.titan-embed-text-v2:0"` | Bedrock model ID for embeddings |
| `embedding_dimensions` | int | `1024` | Vector dimension count (must match model) |
| `embedding_batch_size` | int | `25` | Texts per Bedrock API call |
| `embedding_max_concurrent` | int | `3` | Max parallel embedding API calls |
| `embedding_max_input_tokens` | int | `8192` | Max tokens per embedding request (Titan V2 limit) |

**Example: Switch to a different region**

```toml
[bedrock]
region = "us-east-1"
profile = "my-bedrock-profile"
```

---

### `[bedrock.retry]`

Exponential backoff for Bedrock API throttling.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `max_retries` | int | `3` | Maximum retry attempts before giving up |
| `base_delay_ms` | int | `500` | Initial backoff delay (ms) |
| `max_delay_ms` | int | `10000` | Maximum backoff delay cap (ms) |
| `backoff_multiplier` | float | `2.0` | Delay multiplied by this factor each retry |

---

### `[models.*]`

Per-model cost tracking. Used by the budget system to calculate workflow spend.

| Key | Type | Description |
|:----|:-----|:------------|
| `input_cost` | float | Cost per 1M input tokens (USD) |
| `output_cost` | float | Cost per 1M output tokens (USD) |

**Default tiers:**

```toml
[models.opus]
input_cost = 15.00
output_cost = 75.00

[models.sonnet]
input_cost = 3.00
output_cost = 15.00

[models.haiku]
input_cost = 0.25
output_cost = 1.25

[models.titan_embed_v2]
cost_per_million = 0.02
```

> **Tip:** Update these if Anthropic changes pricing. Budget calculations use these values directly.

---

### `[concurrency]`

Adaptive concurrency pool for parallel agent execution.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `min_slots` | int | `3` | Minimum concurrent agent slots |
| `max_slots` | int | `8` | Maximum concurrent agent slots |
| `initial_slots` | int | `4` | Starting pool size |
| `scale_up_after_seconds` | float | `60.0` | Wait time before expanding the pool |

Each model tier consumes different slot weights:
- **Opus** = 3 slots
- **Sonnet** = 2 slots
- **Haiku** = 1 slot

---

### `[budget]`

Spending limits and enforcement behavior.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `monthly_cap_usd` | float | `50.00` | Monthly spending limit across all workflows |
| `warning_threshold` | float | `0.8` | Alert when spend reaches this fraction of cap (80%) |
| `per_workflow_default_usd` | float | `5.00` | Default budget per workflow if not explicitly set |
| `kill_on_exceed` | bool | `true` | Auto-kill workflows that exceed their budget |

**Example: Increase monthly cap and lower warning threshold**

```toml
[budget]
monthly_cap_usd = 100.00
warning_threshold = 0.7
per_workflow_default_usd = 10.00
kill_on_exceed = true
```

---

### `[knowledge.retrieval]`

Controls the hybrid search engine behavior.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `default_strategy` | string | `"hybrid"` | Search mode: `hybrid`, `keyword`, `semantic`, `graph` |
| `rrf_k` | int | `60` | RRF constant (higher = more even weighting between channels) |
| `default_top_k` | int | `10` | Number of results returned per search |
| `recency_boost_halflife_days` | int | `30` | Newer entries get a boost; this is the half-life |

#### `[knowledge.retrieval.weights]`

Channel weights for hybrid mode (must sum to 1.0):

```toml
[knowledge.retrieval.weights]
keyword = 0.3     # FTS5/BM25 keyword matching
semantic = 0.5    # Vector cosine similarity
graph = 0.2       # Graph BFS traversal
```

#### `[knowledge.retrieval.fallback_weights]`

Used when embeddings are unavailable (no AWS credentials):

```toml
[knowledge.retrieval.fallback_weights]
keyword = 0.6
graph = 0.4
```

---

### `[knowledge.sync]`

Auto-indexing of workspace content.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `auto_sync_on_session_start` | bool | `true` | Re-index on each Claude session start |
| `auto_sync_on_git_pull` | bool | `true` | Re-index after git pull events |
| `scheduled_interval_minutes` | int | `60` | Background re-index interval |
| `max_file_size_kb` | int | `500` | Skip files larger than this |
| `skip_patterns` | array | _(see below)_ | Regex patterns to exclude from indexing |

**Default skip patterns:**

```toml
skip_patterns = [
    '\\.git/', 'node_modules/', 'vendor/', '\\.terraform/',
    '__pycache__/', '\\.pyc$', '\\.lock$', '\\.env$',
    '\\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$',
]
```

**Example: Add custom exclusions**

```toml
[knowledge.sync]
skip_patterns = [
    '\\.git/', 'node_modules/', 'vendor/', '\\.terraform/',
    '__pycache__/', '\\.pyc$', '\\.lock$', '\\.env$',
    '\\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$',
    'dist/', 'build/', '\\.min\\.js$',
]
```

---

### `[knowledge.graph]`

Graph traversal settings for entity-based search.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `max_traversal_depth` | int | `3` | Hard ceiling on BFS hops |
| `default_depth` | int | `2` | Default BFS depth when not specified |

---

### `[session]`

Controls how much context is loaded from session memory.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `checkpoint_interval_seconds` | int | `300` | Auto-save session state interval (5 min) |
| `max_corrections_loaded` | int | `20` | Max corrections loaded per session start |
| `max_learnings_loaded` | int | `30` | Max learnings loaded per session start |
| `max_decisions_loaded` | int | `15` | Max decisions loaded per session start |
| `recency_weight` | float | `0.7` | Bias toward recent entries (0.0 = all equal, 1.0 = most recent only) |

---

### `[fleet]`

MCP server health monitoring and auto-restart.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `health_check_interval_seconds` | int | `30` | How often to ping servers |
| `health_check_timeout_seconds` | int | `5` | Timeout before marking a check as failed |
| `unhealthy_threshold` | int | `3` | Consecutive failures before marking unhealthy |
| `max_restarts` | int | `5` | Maximum auto-restart attempts before giving up |
| `restart_backoff_base` | float | `2.0` | Exponential backoff base for restarts |
| `auto_restart_enabled` | bool | `true` | Toggle auto-restart behavior |

---

### `[maintenance]`

Database housekeeping schedules.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `wal_checkpoint_threshold_mb` | float | `50.0` | WAL checkpoint when log exceeds this size |
| `vacuum_growth_threshold_mb` | float | `100.0` | VACUUM when DB exceeds this free-space ratio |
| `daily_prune_hour` | int | `3` | Hour (local time) to run daily pruning |
| `weekly_vacuum_day` | int | `6` | Day of week for VACUUM (0=Mon, 6=Sun) |
| `backup_retention_count` | int | `5` | Number of config backups to retain |

---

## Common Modifications

### Change search strategy to keyword-only

```toml
[knowledge.retrieval]
default_strategy = "keyword"
```

### Disable auto-sync (manual control only)

```toml
[knowledge.sync]
auto_sync_on_session_start = false
auto_sync_on_git_pull = false
scheduled_interval_minutes = 0
```

### Lower budget for experimentation

```toml
[budget]
monthly_cap_usd = 10.00
per_workflow_default_usd = 1.00
kill_on_exceed = true
```

### Increase concurrency for powerful machines

```toml
[concurrency]
min_slots = 4
max_slots = 12
initial_slots = 6
```

### Disable auto-restart (for debugging)

```toml
[fleet]
auto_restart_enabled = false
```

---

## Applying Changes

Most configuration changes take effect immediately on the next operation:
- **CLI commands** (`cap status`, `cap eval`) read config on each invocation
- **MCP servers** read config at startup; they restart automatically on next Claude Code session

To force MCP servers to refresh config immediately, close and reopen Claude Code:

```bash
# Check if servers are running
cap fleet status

# Verify health after changes
cap fleet health-check
```

---

## Resetting to Defaults

```bash
# Overwrite with factory defaults
cap init --force
```

Or manually copy the default:

```bash
cp $(python -c "import importlib.resources; print(importlib.resources.files('cap.data') / 'config.toml.default')") ~/.claude-platform/config.toml
```

---

## Related Documentation

| Doc | Link |
|:----|:-----|
| Installation & setup | [INSTALL.md](INSTALL.md) |
| Usage scenarios & CLI | [USAGE.md](USAGE.md) |
| Technical architecture | [TECHNICAL.md](TECHNICAL.md) |
| System architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Build & distribution | [DISTRIBUTION.md](DISTRIBUTION.md) |
| Architecture decisions | [adr/](adr/) |

---

*Back to [README](../README.md)*

# CAP v2 Configuration Reference

CAP configuration is split across two files:

- **`~/.claude-platform/config.toml`** — Platform settings (TOML format)
- **`~/.claude-platform/harness-config.json`** — Provider, models, budget (JSON format)

Both are created by `cap init` with sensible defaults. Modify via `cap config set <key> <value>` or edit files directly. Changes take effect on the next MCP server restart (or immediately for CLI commands).

---

## Quick Start: Modifying Configuration

Via CLI:
```bash
cap config set bedrock.region eu-west-1
cap config set budget.monthly_cap_usd 100
cap config set github.org my-org
```

Via file:
```bash
# Edit platform config
$EDITOR ~/.claude-platform/config.toml

# Edit harness config
$EDITOR ~/.claude-platform/harness-config.json
```

---

## Platform Configuration (`config.toml`)

### `[platform]`

General platform settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| log_level | string | "INFO" | Log verbosity: DEBUG, INFO, WARNING, ERROR |

**Example:**
```toml
[platform]
log_level = "DEBUG"
```

---

### `[bedrock]`

AWS Bedrock connection for vector embeddings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| region | string | "us-east-1" | AWS region for Bedrock API calls |
| profile | string | null | AWS profile name (for SSO). Falls back to AWS_PROFILE env var |
| embedding_model | string | "amazon.titan-embed-text-v2:0" | Bedrock model ID for embeddings |
| embedding_dimensions | int | 1024 | Vector dimensions (must match model) |
| embedding_batch_size | int | 25 | Texts per Bedrock API call |
| embedding_max_concurrent | int | 3 | Max parallel embedding API calls |
| embedding_max_input_tokens | int | 8192 | Max tokens per embedding request |

**Example:**
```toml
[bedrock]
region = "eu-central-1"
profile = "my-bedrock-profile"
embedding_max_concurrent = 5
```

---

### `[bedrock.retry]`

Exponential backoff for Bedrock API throttling.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| max_retries | int | 3 | Max retry attempts before giving up |
| base_delay_ms | int | 500 | Initial backoff delay (milliseconds) |
| max_delay_ms | int | 10000 | Maximum backoff delay cap (milliseconds) |
| backoff_multiplier | float | 2.0 | Delay multiplied by this each retry |

**Example:**
```toml
[bedrock.retry]
max_retries = 5
base_delay_ms = 1000
max_delay_ms = 30000
backoff_multiplier = 2.0
```

---

### `[concurrency]`

Adaptive concurrency pool for parallel agent execution.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| min_slots | int | 3 | Minimum concurrent agent slots |
| max_slots | int | 8 | Maximum concurrent agent slots |
| initial_slots | int | 4 | Starting pool size |
| scale_up_after_seconds | float | 60.0 | Seconds before scaling up |

Slot weights by model tier:
- Opus = 3 slots
- Sonnet = 2 slots
- Haiku = 1 slot

**Example:**
```toml
[concurrency]
min_slots = 2
max_slots = 12
initial_slots = 6
scale_up_after_seconds = 30.0
```

---

### `[budget]`

Spending limits and enforcement behavior.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| monthly_cap_usd | float | 50.0 | Monthly spending cap (all workflows) |
| warning_threshold | float | 0.8 | Alert at this % of cap (0.8 = 80%) |
| per_workflow_default_usd | float | 5.0 | Default per-workflow budget |
| kill_on_exceed | bool | true | Kill workflows exceeding budget |

**Example:**
```toml
[budget]
monthly_cap_usd = 100.0
warning_threshold = 0.7
per_workflow_default_usd = 10.0
kill_on_exceed = true
```

---

### `[knowledge.retrieval]`

Hybrid search engine configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| default_strategy | string | "hybrid" | Search mode: hybrid, keyword, semantic, graph |
| rrf_k | int | 60 | Reciprocal Rank Fusion constant |
| default_top_k | int | 10 | Number of results returned per search |
| recency_boost_halflife_days | int | 30 | Halflife for recency boost (newer = higher) |

#### `[knowledge.retrieval.weights]`

Channel weights for hybrid mode (must sum to 1.0):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| keyword | float | 0.3 | FTS5 keyword matching weight |
| semantic | float | 0.5 | Vector cosine similarity weight |
| graph | float | 0.2 | Graph traversal weight |

**Example:**
```toml
[knowledge.retrieval]
default_strategy = "hybrid"
rrf_k = 60
default_top_k = 15

[knowledge.retrieval.weights]
keyword = 0.3
semantic = 0.5
graph = 0.2
```

#### `[knowledge.retrieval.fallback_weights]`

Used when embeddings unavailable (no AWS credentials):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| keyword | float | 0.6 | Keyword weight without embeddings |
| graph | float | 0.4 | Graph weight without embeddings |

**Example:**
```toml
[knowledge.retrieval.fallback_weights]
keyword = 0.6
graph = 0.4
```

---

### `[knowledge.sync]`

Automatic workspace indexing.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| auto_sync_on_session_start | bool | true | Re-index on each Claude session start |
| auto_sync_on_git_pull | bool | true | Re-index after git pull events |
| scheduled_interval_minutes | int | 60 | Background re-index interval (0 = disabled) |
| max_file_size_kb | int | 500 | Skip files larger than this |
| skip_patterns | list[string] | (see below) | Regex patterns to exclude from indexing |

**Default skip patterns:**
```toml
[knowledge.sync]
skip_patterns = [
  '\.git/',
  'node_modules/',
  'vendor/',
  '\.terraform/',
  '__pycache__/',
  '\.pyc$',
  '\.lock$',
  '\.env$',
  '\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$'
]
```

**Example: Add custom exclusions**
```toml
[knowledge.sync]
auto_sync_on_session_start = true
scheduled_interval_minutes = 120
skip_patterns = [
  '\.git/',
  'node_modules/',
  'vendor/',
  '\.terraform/',
  '__pycache__/',
  '\.pyc$',
  '\.lock$',
  '\.env$',
  '\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$',
  'dist/',
  'build/',
  '\.min\.js$'
]
```

---

### `[knowledge.graph]`

Graph traversal settings for entity-based search.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| max_traversal_depth | int | 3 | Hard ceiling on BFS hops |
| default_depth | int | 2 | Default BFS depth when not specified |

**Example:**
```toml
[knowledge.graph]
max_traversal_depth = 4
default_depth = 2
```

---

### `[session]`

Session memory context loading.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| checkpoint_interval_seconds | int | 300 | Auto-save session state interval |
| max_corrections_loaded | int | 20 | Max corrections loaded per session start |
| max_learnings_loaded | int | 30 | Max learnings loaded per session start |
| max_decisions_loaded | int | 15 | Max decisions loaded per session start |
| recency_weight | float | 0.7 | Bias toward recent entries (0.0 = all equal, 1.0 = most recent only) |

**Example:**
```toml
[session]
checkpoint_interval_seconds = 600
max_corrections_loaded = 30
max_learnings_loaded = 50
recency_weight = 0.8
```

---

### `[github]`

GitHub organization configuration for auto-resolution and dependency management.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| org | string | "" | GitHub organization name (e.g., "moia-dev") |
| clone_base_path | string | "~/Projects" | Base directory for auto-cloned repos |
| use_ssh | bool | true | Use SSH-only for clones (required) |
| auto_clone_on_missing_dep | bool | true | Auto-clone missing dependency repos |
| max_auto_clones_per_session | int | 10 | Safety limit on auto-clones |
| default_branch | string | "main" | Default branch to clone (fallback: detect from repo) |
| clone_depth | int | 1 | Shallow clone depth (1 = single commit, 0 = full history) |

**Example:**
```toml
[github]
org = "moia-dev"
clone_base_path = "~/Projects/moia"
use_ssh = true
auto_clone_on_missing_dep = true
max_auto_clones_per_session = 15
clone_depth = 1
```

How it works:
1. Agent references a repo from your GitHub org
2. Knowledge server detects missing repo via knowledge graph
3. Clones repo (SSH, depth=1) to `clone_base_path/repo-name`
4. Content automatically indexed into knowledge base
5. Subsequent references find the repo locally

---

### `[fleet]`

MCP server health monitoring and auto-restart.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| health_check_interval_seconds | int | 30 | How often to ping servers |
| health_check_timeout_seconds | int | 5 | Timeout before marking check as failed |
| unhealthy_threshold | int | 3 | Consecutive failures before unhealthy |
| max_restarts | int | 5 | Maximum auto-restart attempts |
| restart_backoff_base | float | 2.0 | Exponential backoff base for restarts |
| auto_restart_enabled | bool | true | Toggle auto-restart behavior |

**Example:**
```toml
[fleet]
health_check_interval_seconds = 60
health_check_timeout_seconds = 10
unhealthy_threshold = 5
max_restarts = 3
auto_restart_enabled = true
```

---

### `[maintenance]`

Database housekeeping schedules.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| wal_checkpoint_threshold_mb | float | 50.0 | Checkpoint WAL at this size |
| vacuum_growth_threshold_mb | float | 100.0 | VACUUM when DB exceeds this free-space |
| daily_prune_hour | int | 3 | Hour (UTC) for daily pruning |
| weekly_vacuum_day | int | 6 | Day of week for vacuum (0=Mon, 6=Sun) |
| backup_retention_count | int | 5 | Number of config backups to keep |

**Example:**
```toml
[maintenance]
wal_checkpoint_threshold_mb = 100.0
vacuum_growth_threshold_mb = 200.0
daily_prune_hour = 2
weekly_vacuum_day = 0
backup_retention_count = 10
```

---

## Harness Configuration (`harness-config.json`)

The harness configuration controls provider selection, model IDs, budgets, and remote indexing.

**Location:** `~/.claude-platform/harness-config.json`

### Full Structure

```json
{
  "provider": "aws-bedrock",
  "aws": {
    "region": "eu-central-1",
    "auth_method": "sso-profile",
    "profile": "<your-sso-profile>"
  },
  "models": {
    "haiku": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "eu.anthropic.claude-sonnet-4-5-20241022-v1:0",
    "opus": "us.anthropic.claude-opus-4-5-20250514-v1:0"
  },
  "budget": {
    "daily_limit_usd": 5.0,
    "per_project": false,
    "agent_caps": {
      "opus": 2.0,
      "sonnet": 1.0,
      "haiku": 0.5
    }
  },
  "indexing": {
    "local_paths": [
      "/path/to/workspace/repo1",
      "/path/to/workspace/repo2"
    ],
    "exclude_patterns": [
      "test-*",
      "archived-*",
      "vendor/",
      "node_modules/"
    ],
    "auto_index_interval_minutes": 60
  },
  "remotes": [
    {
      "url": "git@github.com:<your-org>",
      "type": "github",
      "ssh": true
    }
  ],
  "embeddings": {
    "model_id": "amazon.titan-embed-text-v2:0",
    "dimensions": 1024,
    "fallback": "sentence-transformers",
    "region": "us-east-1",
    "profile": "<your-profile>"
  }
}
```

### Section Breakdown

#### `provider`
- **Type:** string
- **Value:** "aws-bedrock"
- The model provider. Currently only Bedrock is supported.

#### `aws`
- **region** (string) — AWS region for Bedrock API calls
- **auth_method** (string) — "sso-profile" (SSO) or "static" (static credentials)
- **profile** (string) — AWS SSO profile name (for auth_method="sso-profile")

#### `models`
Maps model tier names to Bedrock model IDs. Must include haiku, sonnet, opus.

**Available models in Bedrock regions:**

US regions (us-east-1, us-west-2):
```json
"haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
"sonnet": "us.anthropic.claude-sonnet-4-5-20241022-v1:0",
"opus": "us.anthropic.claude-opus-4-5-20250514-v1:0"
```

EU regions (eu-central-1):
```json
"haiku": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
"sonnet": "eu.anthropic.claude-sonnet-4-5-20241022-v1:0",
"opus": "us.anthropic.claude-opus-4-5-20250514-v1:0"
```

#### `budget`
- **daily_limit_usd** (float) — Daily spending cap
- **per_project** (bool) — Whether to apply limits per project (vs globally)
- **agent_caps** (object) — Per-model spending limits

#### `indexing`
- **local_paths** (array[string]) — Workspace paths to index
- **exclude_patterns** (array[string]) — Regex patterns to skip
- **auto_index_interval_minutes** (int) — Background indexing frequency

#### `remotes`
Array of git remotes to auto-clone from. Each object:
- **url** (string) — Git remote URL (e.g., git@github.com:org)
- **type** (string) — "github", "gitlab", or "gitea"
- **ssh** (bool) — Use SSH (required)

#### `embeddings`
- **model_id** (string) — Bedrock model ID for embeddings
- **dimensions** (int) — Vector dimensions (1024 for Titan V2)
- **fallback** (string) — Fallback when Bedrock unavailable (e.g., "sentence-transformers")
- **region** (string) — AWS region for embeddings API
- **profile** (string) — AWS profile for embeddings (may differ from main profile)

---

## Environment Variables

Override configuration via env vars:

| Variable | Default | Description |
|----------|---------|-------------|
| CAP_HOME | ~/.claude-platform | CAP home directory |
| CAP_ORCHESTRATOR_DB | $CAP_HOME/orchestrator.db | Orchestrator database path |
| CAP_LOG_LEVEL | INFO | Log verbosity |
| AWS_PROFILE | (none) | AWS profile (used if bedrock.profile not set) |
| AWS_REGION | (none) | AWS region (used if bedrock.region not set) |

**Example:**
```bash
export CAP_HOME=/opt/cap
export CAP_LOG_LEVEL=DEBUG
cap status
```

---

## Configuration Profiles

Create separate config files for different environments:

```bash
# Production config
cp ~/.claude-platform/config.toml ~/.claude-platform/config.prod.toml
# Edit for production settings
$EDITOR ~/.claude-platform/config.prod.toml

# Use with env var
CAP_HOME=~/.claude-platform-prod cap status
```

---

## Common Modifications

### Switch to keyword-only search (no embeddings)
```toml
[knowledge.retrieval]
default_strategy = "keyword"
```

### Disable auto-sync
```toml
[knowledge.sync]
auto_sync_on_session_start = false
auto_sync_on_git_pull = false
scheduled_interval_minutes = 0
```

### Lower budget for experimentation
```toml
[budget]
monthly_cap_usd = 10.0
per_workflow_default_usd = 1.0
kill_on_exceed = true
```

### Increase concurrency
```toml
[concurrency]
min_slots = 4
max_slots = 16
initial_slots = 8
```

### Disable auto-restart (debugging)
```toml
[fleet]
auto_restart_enabled = false
```

### Use local region for lower latency
```toml
[bedrock]
region = "eu-central-1"
profile = "my-eu-profile"
```

---

## What's Configurable vs Hardcoded

**Configurable:**
- All settings listed above in `config.toml` and `harness-config.json`
- Log levels and debug output
- Concurrency and budget limits
- Search strategy and weights
- Auto-sync behavior
- Health check intervals

**Hardcoded (not configurable):**
- Database file names (platform.db, knowledge.db, sessions.db, fleet.db)
- MCP server names (cap-knowledge, cap-session, cap-harness, etc.)
- Circuit breaker thresholds (3 failures in 5min, 2min cooldown)
- Chunk size for knowledge sync (512 tokens, 64 overlap)
- Agent definition file format (.md files)
- Protocol versions and API contracts

---

## Validating Configuration

Check configuration validity:
```bash
cap config validate
```

Show current active config:
```bash
cap config show
```

Show config with defaults:
```bash
cap config defaults
```

---

## Troubleshooting

**MCP servers not picking up changes:**
Close and reopen Claude Code. MCP servers read config at startup.

**Config file corrupted:**
```bash
cap init --force
```

**Finding your config files:**
```bash
echo $CAP_HOME
ls -la ~/.claude-platform/
```

---

## Cross-References

- [Installation](installation.md) — Getting started with CAP
- [CLI Reference](cli-reference.md) — Command-line tool reference
- [Bedrock Provider](providers/bedrock.md) — Model details and authentication
- [USAGE.md](USAGE.md) — Usage patterns and workflows
- [TECHNICAL.md](TECHNICAL.md) — Technical deep-dive

---

*Back to [README](../README.md)*

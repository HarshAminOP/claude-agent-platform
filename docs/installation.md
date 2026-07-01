# Installation

## Prerequisites

- **Python 3.11+** — CAP runs on Python 3.11, 3.12, or 3.13
  ```bash
  python3 --version
  ```

- **pip or uv** — pip is built-in; uv is faster for installs
  ```bash
  # macOS
  brew install uv
  
  # Linux / macOS (standalone)
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **Optional: sentence-transformers** — for local embeddings fallback (installed automatically if needed)

- **Optional: AWS CLI v2** — only required if using Bedrock provider with SSO profile auth
  ```bash
  aws --version
  ```

## Installation

### From PyPI

```bash
pip install claude-agent-platform
```

Or with uv (faster):
```bash
uv tool install claude-agent-platform
uv tool update-shell && source ~/.zshrc  # one-time PATH update
```

### From source

```bash
git clone git@github.com:<your-org>/claude-agent-platform.git
cd claude-agent-platform
pip install -e .
```

Or with uv:
```bash
uv tool install .
```

### Verify installation

```bash
cap --version
```

## First-Time Setup: `cap init`

The init wizard creates `~/.claude-platform/`, registers MCP servers, and installs agent definitions.

### Interactive setup (default)

```bash
cap init
```

The wizard walks through these options in order:

#### 1. Provider selection

Choose which LLM provider to use:
- `aws-bedrock` (default) — Bedrock models via ChatBedrockConverse, cost-tracked per workflow
- `anthropic-api` — Direct Anthropic API, requires ANTHROPIC_API_KEY env var
- `local` — Ollama, no auth required, local inference only

#### 2. AWS configuration (if bedrock selected)

- **Region** — default: `us-east-1` (must support Bedrock in your account)
- **Auth method:**
  - `sso-profile` — SSO profile name (recommended for AWS orgs)
  - `env-vars` — reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`
  - `instance-role` — EC2/ECS task role credentials
  - `static-credentials` — explicit key/secret pair (not recommended)
- **Profile name** — if sso-profile selected, provide SSO profile name

#### 3. Multi-path workspace configuration

Specify workspace root(s) to index for code search:
- Default: current working directory
- Multiple workspaces: enter paths separated by newlines
- Examples:
  ```
  /home/user/repos/core-services
  /home/user/repos/data-platform
  /home/user/repos/observability
  ```

CAP indexes READMEs, package.json, Terraform, Kubernetes manifests, and source code structure.

#### 4. Remote git endpoints

Configure where to clone missing dependencies:
- **Git provider URL** — GitHub: `https://github.com/<your-org>`, Bitbucket: `https://bitbucket.org/<your-org>`
- **Auth method** — SSH only (default); uses `~/.ssh/id_rsa`
- **Clone base path** — where to cache cloned repos (default: `~/code`)

SSH must be set up with provider (GitHub/Bitbucket) before this step:
```bash
ssh -T git@github.com  # verify GitHub SSH works
```

#### 5. Embedding model selection

CAP auto-probes for embeddings capability:
- **Primary:** Titan Embed V2 (`amazon.titan-embed-text-v2:0`, 1024 dimensions)
  - Must be available in your Bedrock account's region
- **Fallback:** `sentence-transformers/all-MiniLM-L6-v2` (local, no cloud dependency)
  - Automatically used if Titan Embed unavailable

Wizard displays probe results. Accept default or choose fallback.

#### 6. Budget configuration

Set spending guardrails per day, month, and workflow:
- **Daily limit** — default: $5.00, hard stop at midnight UTC
- **Monthly cap** — default: $50.00, cumulative
- **Per-workflow default** — default: $5.00, override via `cap run --budget $10`

Budget tracking includes model usage + knowledge base indexing costs.

### Non-interactive setup

```bash
cap init --non-interactive --workspace /path/to/code
```

Uses defaults:
- Provider: `aws-bedrock`
- Region: `us-east-1`
- Auth: env-vars (reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- Embedding: auto-probe with fallback
- Budget: $5.00/day, $50.00/month
- Workspace: current directory if not specified

### Minimal setup (databases only)

```bash
cap init --minimal
```

Creates SQLite databases and config.toml, skips MCP server registration and agent install. Useful for CI/CD containers or testing.

### Force reinitialize

```bash
cap init --force
```

Backs up existing config and reinitializes from scratch. Preserves `~/.claude-platform/data/` (databases).

## Cold Start Phases

Init runs through these phases and prints progress:

| Phase | Duration | Action |
|-------|----------|--------|
| **0: Setup** | 0s | Create `~/.claude-platform/` directories: data/, logs/, backups/, locks/, inbox/ |
| **1: Databases** | 0-2s | Create SQLite databases (platform.db, knowledge.db, sessions.db, fleet.db), run migrations, write config.toml |
| **2: Hooks** | 2-3s | Generate hook scripts for `session_start`, `session_end`, credential refresh |
| **3: Claude Code integration** | 3-5s | Update `~/.claude/settings.json` with tool permissions, back up original first |
| **4: Quick index** | 5-10s | Index current workspace (README, package.json, manifest files, top-level source structure) |
| **5: MCP servers** | 10-15s | Register 9 MCP servers in `~/.claude.json`: knowledge-base, kubernetes, aws-iam, aws-ec2, aws-rds, aws-s3, terraform, bedrock-models, prometheus |
| **6: Background index** | Background | Queue full workspace index for `session_start` hook |
| **7: Health check** | 15s+ | Validate DB integrity, hook syntax, MCP config, provider auth |
| **8: Success summary** | - | Print config summary, sample commands, next steps |

Example output:
```
CAP v2.0.0 initialized in 18s

  Workspace:       /home/user/repos/core-services
  Provider:        aws-bedrock (us-east-1, sso-profile: dev)
  Budget:          $5.00/day, $50.00/month
  Embedding:       amazon.titan-embed-text-v2:0 (1024 dims)
  Databases:       platform.db, knowledge.db, sessions.db, fleet.db
  MCP Servers:     9/9 registered
  Agents:          139 definitions installed
  
  Next steps:
    cap status              # Platform health
    cap config show         # View configuration
    cap knowledge status    # Knowledge base status
    cap run 'what is my biggest repo?'  # First query
```

## Post-Install Verification

```bash
# View all configuration
cap config show

# Platform health and version
cap status

# Check budget tracking (daily, monthly, today's usage)
cap budget status

# Knowledge base indexing status
cap knowledge status

# Test LLM provider connection
cap doctor

# List all installed agents
cap agents list
```

Expected output for `cap status`:
```
CAP Status: HEALTHY

  Platform:       v2.0.0 (eu.anthropic.claude-haiku-4-5-20251001-v1:0)
  Workspace:      /home/user/repos/core-services (indexed: 312 files, 891 edges)
  Databases:      OK (4 SQLite files, 284ms queries)
  MCP Servers:    9/9 registered
  Provider:       aws-bedrock (us-east-1, sso-profile: dev)
  Budget Today:   $0.47 / $5.00
  Budget Month:   $12.33 / $50.00
  Sessions:       3 active, 24 completed today
  Last Index:     2 hours ago (background: queued)
```

## What Gets Created

### `~/.claude-platform/`

CAP home directory structure:

```
~/.claude-platform/
├── config.toml                          # Platform config (provider, region, budget)
├── harness-config.json                  # Model/provider/LLM settings
├── data/
│   ├── platform.db                      # Platform metadata, sessions, agents, workflows
│   ├── knowledge.db                     # Knowledge base: RAG embeddings, file index
│   ├── sessions.db                      # Session history, decisions, feedback
│   └── fleet.db                         # Agent fleet state, logs, telemetry
├── logs/
│   ├── platform.log                     # CAP system logs
│   ├── knowledge-index.log              # Indexing progress
│   └── agent-*.log                      # Per-agent execution logs
├── backups/
│   ├── config.toml.2024-07-01T14-32-45 # Timestamped config snapshots
│   └── ~/.claude.json.backup            # Claude Code MCP config before CAP init
└── locks/
    └── platform.lock                    # Active instance lock (prevents concurrent writes)
```

### `~/.claude/agents/`

139 agent definitions installed as `.md` files in Markdown frontmatter format:

```
~/.claude/agents/
├── dev.md                        # Software engineer agent
├── devops.md                     # Infrastructure & Kubernetes
├── security.md                   # Security audits & IAM
├── sre.md                        # Observability & incidents
├── docs.md                       # Documentation & runbooks
├── test.md                       # Test engineering
├── code-review.md                # Code review & quality
├── orchestrator.md               # Multi-agent coordinator
└── ... (131 more)
```

Agents are referenced by name in workflows:
```bash
cap run --agent dev 'Fix the N+1 query bug in user_service'
```

### `~/.claude.json`

MCP server registrations (backed up before modification):

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "python",
      "args": ["-m", "cap.mcp.knowledge_base"],
      "env": {"WORKSPACE": "/home/user/repos/core-services"}
    },
    "kubernetes": {...},
    "aws-iam": {...},
    "aws-ec2": {...},
    "aws-rds": {...},
    "aws-s3": {...},
    "terraform": {...},
    "bedrock-models": {...},
    "prometheus": {...}
  }
}
```

MCP servers are automatically available to all agents.

### `~/.claude/settings.json`

Tool permissions updated to allow CAP agents access (original backed up):

```json
{
  "permissions": {
    "allowList": [
      "bash:git:*",
      "bash:terraform:*",
      "bash:kubectl:*",
      "bash:aws:*",
      "read:*",
      "write:*.md",
      "write:*.tf"
    ]
  }
}
```

Original settings restored on `cap uninstall`.

## Non-Interactive Install (CI/CD)

For automated deployments (GitHub Actions, GitLab CI, etc.):

```bash
# Non-interactive with env vars
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-1"
cap init --non-interactive --workspace /app/code

# Or minimal (skip MCP servers)
cap init --minimal --workspace /app/code
```

In CI pipelines, use `cap init --minimal` to avoid unnecessary MCP registration and speed up container init.

## Uninstalling

### Full removal

```bash
cap uninstall
```

Removes:
- `~/.claude-platform/` directory (all databases, logs, config)
- Agent definitions from `~/.claude/agents/`
- MCP server registrations from `~/.claude.json`
- Tool permissions from `~/.claude/settings.json`

Restores original `~/.claude.json` and `~/.claude/settings.json` from backups.

### Keep databases

```bash
cap uninstall --keep-data
```

Removes config and agents, but preserves:
- `~/.claude-platform/data/` (SQLite databases)
- Knowledge base embeddings
- Session history

Useful for reinstalling or migrating to a new machine.

### Remove from path

```bash
# If installed via PyPI/uv
pip uninstall claude-agent-platform

# If installed from source
cd claude-agent-platform && pip uninstall -e .
```

## Troubleshooting

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| `cap: command not found` | Installation didn't add CAP to PATH | `uv tool update-shell && source ~/.zshrc` (uv installs) or verify pip in $PATH |
| MCP servers fail to register | `.claude.json` has syntax errors or wrong permissions | Run `cap doctor`, check `~/.claude/logs/platform.log` |
| Database errors on startup | SQLite file corruption or permission denied | `cap doctor --fix` to repair, or `rm ~/.claude-platform/data/*.db` to reset |
| Provider auth fails (Bedrock) | AWS credentials not available or region unavailable | `aws sts get-caller-identity` to check auth, `aws bedrock list-foundation-models --region us-east-1` to verify region access |
| Embeddings not available | Titan Embed V2 not enabled in account or region | Switch to fallback: edit `config.toml`, set `embedding_model: sentence-transformers/all-MiniLM-L6-v2` |
| Knowledge base not indexing | Workspace path invalid or no indexable files | Verify path exists: `ls -la /path/to/workspace`, check `~/.claude-platform/logs/knowledge-index.log` |

Run `cap doctor` for detailed diagnostics:

```bash
cap doctor
```

Output:
```
CAP Diagnostics

  Configuration:    OK (config.toml valid, 127 keys loaded)
  Databases:        OK (4 files, 8.2MB, no corruption)
  MCP Servers:      8/9 registered (prometheus offline)
  Provider:         OK (aws-bedrock, us-east-1, sso-profile: dev)
  Provider Auth:    OK (credentials valid, expires in 14d)
  Bedrock Models:   OK (claude-3-sonnet, claude-3-haiku)
  Embeddings:       OK (amazon.titan-embed-text-v2:0, available)
  Workspace:        OK (/home/user/repos/core-services, 312 files)
  Knowledge Base:   OK (embedded: 8,234 documents, 918 edges)
  Permissions:      OK (24 tool permissions granted)
  
  Issues:
    WARNING: prometheus server offline (see logs)
```

## Related Documentation

- [Configuration](configuration.md) — Detailed config.toml options, budgets, embeddings
- [Architecture](architecture.md) — CAP platform design, agent coordination, MCP servers
- [CLI Reference](cli-reference.md) — `cap` command reference with all subcommands
- [Provider Setup](providers/) — Provider-specific auth and configuration
- [Agents](agents.md) — Agent registry, capabilities, selection guide
- [Troubleshooting](troubleshooting.md) — Common issues and solutions

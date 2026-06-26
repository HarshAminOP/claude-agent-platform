# Claude Agent Platform (CAP) — Installation & Uninstallation Guide

## Prerequisites

Before installing CAP, ensure you have the following:

### Python 3.11+

CAP requires Python 3.11, 3.12, or 3.13. Check your version:

```sh
python3 --version
```

### `uv` — Python Package Manager

CAP is distributed as a Python package installed via [`uv`](https://docs.astral.sh/uv/).

<details>
<summary><strong>Install uv on macOS</strong></summary>

```sh
# Homebrew (recommended)
brew install uv

# Or standalone installer
curl -LsSf https://astral.sh/uv/install.sh | sh
```

</details>

<details>
<summary><strong>Install uv on Linux</strong></summary>

```sh
# Standalone installer
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or via pip
pip install uv
```

After installing, ensure `uv` is on your PATH:

```sh
uv --version
```

</details>

### Claude Code CLI

CAP registers MCP servers with Claude Code. Install it first:

- **Install:** [https://docs.anthropic.com/en/docs/claude-code/overview](https://docs.anthropic.com/en/docs/claude-code/overview)
- Verify: `claude --version`

### AWS Credentials (for Bedrock Embeddings)

CAP uses **Amazon Titan Text Embeddings V2** to generate vector embeddings for the knowledge graph. You need:

- An AWS account with **Amazon Bedrock** access enabled
- The **Titan Text Embeddings V2** model enabled in your region (default: `us-east-1`)
- An AWS profile configured with credentials (SSO or static keys)

> **Why embeddings?** CAP indexes knowledge entries (repo summaries, architecture docs, task records) as vectors. This powers semantic search — finding relevant context even when exact keywords don't match. Without embeddings, CAP gracefully degrades to keyword + graph-based search.

---

## Installation

### Quick Install

```sh
# 1. Install the package
uv tool install claude-agent-platform

# 2. Ensure uv tools are on your PATH (one-time, if not already done)
uv tool update-shell && source ~/.zshrc   # or ~/.bashrc

# 3. Initialize (creates databases, agents, workflows, registers MCP servers)
cap init

# 4. Index your workspace (populates the knowledge base)
cap sync --workspace /path/to/your/project

# 5. Verify everything is healthy
cap status
```

After this, Claude Code will auto-discover CAP on next launch, and the knowledge base will auto-sync going forward.

---

### What `cap init` Does

Running `cap init` performs the following steps:

#### 1. Creates the directory structure

```
~/.claude-platform/
├── data/           # SQLite databases
├── config.toml    # Platform configuration
├── backups/       # Config backups (timestamped)
└── logs/          # Server logs
```

#### 2. Initializes 4 SQLite databases

| Database | Purpose |
|----------|---------|
| `platform.db` | Workflows, budget tracking, events |
| `knowledge.db` | Knowledge entries, graph edges, embeddings |
| `sessions.db` | Session learnings, corrections, decisions |
| `fleet.db` | MCP server registry, health events |

All databases are created with:
- **WAL mode** (Write-Ahead Logging) for concurrent read/write performance
- **`0600` permissions** — only the owning user can read/write

#### 3. Copies default `config.toml`

A default configuration file is placed at `~/.claude-platform/config.toml`. It contains sensible defaults for:
- AWS region and profile
- Embedding model selection
- Budget limits and rate limiting
- Server fleet configuration

#### 4. Installs 14 agent definitions

Agent markdown files are installed to `~/.claude/agents/`:

```
aws-architect.md  cicd.md     code-review.md  dev.md
devops.md         docs.md     optimization.md orchestrator.md
security.md       sre.md      system.md       teacher.md
test.md           workflow.md
```

These appear in Claude Code's agent picker menu.

#### 5. Installs 10 workflow scripts

Multi-agent workflow orchestration scripts are installed to `~/.claude/workflows/`:

```
architecture-explainer.js   cost-optimization.js
cross-repo-impact.js        incident-response.js
new-service-deployment.js   repo-health-check.js
repo-sync-clean.js          security-hardening.js
session-observe.js          system-evolve.js
```

#### 6. Registers 4 MCP servers with Claude Code

CAP registers its MCP servers via `claude mcp add`:

| Server | Function |
|--------|----------|
| `cap-platform` | Workflow engine, budget tracking, event bus |
| `cap-knowledge` | Knowledge graph, semantic search, embeddings |
| `cap-sessions` | Session memory, learnings, corrections |
| `cap-fleet` | Server health, registry, diagnostics |

#### 7. Backs up existing configuration

> **Important:** Before modifying `~/.claude.json` or `~/.claude/settings.json`, CAP creates timestamped backups. See [Configuration Backup](#configuration-backup) below.

---

### Configuration Backup

CAP never destructively modifies your existing Claude Code configuration.

**Before any modification**, `cap init`:
1. Reads the current `~/.claude.json` and `~/.claude/settings.json`
2. Creates timestamped copies in `~/.claude-platform/backups/`
3. Only then merges CAP's MCP server registrations

**Backup format:**

```
~/.claude-platform/backups/
├── claude.json.backup.2024-01-15T10:30:00
└── settings.json.backup.2024-01-15T10:30:00
```

**Restoration:** `cap uninstall` automatically restores the original configs from the most recent backup. You can also manually copy a backup file back if needed.

> **Note:** If you run `cap init` multiple times, each run creates a new backup. Only the pre-CAP originals are used during uninstall restoration.

---

### Initial Knowledge Base Sync

After `cap init`, your knowledge base is **empty** — it has the database schema but no indexed content. You need to run the first sync to populate it with your workspace content.

#### Run the first sync

```sh
cap sync --workspace /path/to/your/project
```

This scans your workspace and indexes everything into the knowledge base:

1. **Walks the directory tree** — finds all relevant files (`.md`, `.tf`, `.yaml`, `.py`, `.ts`, `.js`, `.hcl`, etc.)
2. **Skips noise** — ignores `.git/`, `node_modules/`, `vendor/`, `.terraform/`, binaries, lock files, `.env`
3. **Indexes content** — creates FTS5 full-text search entries for keyword matching
4. **Generates embeddings** — calls Bedrock Titan V2 to create vectors for semantic search (if AWS credentials are configured)
5. **Builds knowledge graph** — extracts entities (services, modules, resources) and their relationships

#### Verify the sync

```sh
cap knowledge status
```

You should see entry counts, graph node counts, and embedding status.

#### What happens after the first sync

Once the initial sync is complete, **you don't need to run it manually again**. CAP auto-syncs:

| Trigger | When | What it does |
|:--------|:-----|:-------------|
| Session start | Each time Claude Code opens | Incremental sync (only changed files) |
| After git pull | When files change from remote | Re-indexes modified files |
| Scheduled | Every 60 minutes | Background incremental sync |

To force a full re-index at any time:

```sh
cap sync --workspace /path/to/your/project --full
```

#### Without AWS credentials

If you haven't configured Bedrock credentials yet, the first sync still works — it indexes everything for keyword search and knowledge graph traversal. Semantic search (vector similarity) will be unavailable until you set up AWS credentials. You can add credentials later and re-sync:

```sh
# After configuring [bedrock] in config.toml
cap sync --workspace /path/to/your/project --full
```

#### Multiple workspaces

You can index multiple workspaces:

```sh
cap sync --workspace ~/projects/infra-repo
cap sync --workspace ~/projects/app-repo
cap sync --workspace ~/projects/docs-repo
```

All content is searchable from a single unified knowledge base.

---

## Credential Setup

### AWS Bedrock (Embeddings)

#### What's needed

- AWS account with Bedrock access in your target region
- Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) model enabled
- An IAM role/user with `bedrock:InvokeModel` permission for the Titan model

#### Configuration

**Option 1: Set profile in `config.toml`** (recommended)

```toml
[aws]
profile = "your-sso-profile"
region = "us-east-1"
```

**Option 2: Environment variable**

```sh
export AWS_PROFILE=your-sso-profile
```

#### SSO Setup Flow

```sh
# Configure SSO profile (one-time)
aws configure sso --profile your-sso-profile

# Login before using CAP
aws sso login --profile your-sso-profile

# Verify access
aws sts get-caller-identity --profile your-sso-profile
```

#### What happens without credentials

CAP **does not fail** without AWS credentials. Behavior:

| Feature | With credentials | Without credentials |
|---------|-----------------|-------------------|
| Semantic search | Vector similarity (high quality) | Keyword + graph traversal (still functional) |
| Knowledge indexing | Full embeddings generated | Entries stored without vectors |
| Performance | Sub-100ms search | Slightly slower for large graphs |

> **Note:** You can install and use CAP without Bedrock access. Embeddings enhance search quality but are not required for core functionality.

#### Cost

Titan Text Embeddings V2 pricing: **~$0.02 per 1 million tokens**.

For typical platform engineering usage (indexing ~50 repos, daily knowledge updates), expect **< $0.10/month**.

---

### Claude Code CLI

CAP requires Claude Code to be installed and authenticated:

```sh
# Verify Claude Code is installed
claude --version

# Authenticate if needed
claude auth login
```

CAP registers its MCP servers using `claude mcp add`. If Claude Code is not installed, `cap init` will exit with an error and clear instructions.

---

## Storage

### What's stored where

| Path | Contents | Typical Size | Permissions |
|------|----------|:------------:|:-----------:|
| `~/.claude-platform/data/platform.db` | Workflows, budget, events | 50-200 KB | `0600` |
| `~/.claude-platform/data/knowledge.db` | Knowledge entries, graph, embeddings | 100 KB - 50 MB | `0600` |
| `~/.claude-platform/data/sessions.db` | Learnings, corrections, decisions | 100 KB - 5 MB | `0600` |
| `~/.claude-platform/data/fleet.db` | Server registry, health events | 20-50 KB | `0600` |
| `~/.claude-platform/config.toml` | Platform configuration | ~2 KB | `0644` |
| `~/.claude-platform/backups/` | Config backups | varies | `0600` |
| `~/.claude-platform/logs/` | Server logs | varies | `0600` |
| `~/.claude/agents/*.md` | Agent definitions (14 files) | ~2 KB each | `0644` |
| `~/.claude/workflows/*.js` | Workflow scripts (10 files) | ~5 KB each | `0644` |

### Data growth

- **Knowledge DB** grows as you index content. Each entry is ~1 KB of text + ~6 KB for the embedding vector. A workspace with 50 repos and full documentation typically stays under 50 MB.
- **Session DB** grows slowly. Learnings are deduplicated, and old sessions are pruned automatically after 90 days.
- **Platform DB** has automatic maintenance:
  - WAL checkpointing runs on each server start
  - `VACUUM` runs weekly via `cap doctor`
  - Old workflow events are pruned after 30 days
- **Logs** are rotated daily and retained for 7 days.

Run `cap doctor` periodically to monitor database health and disk usage.

---

## Uninstallation

### Clean Removal

```sh
cap uninstall --yes
uv tool uninstall claude-agent-platform
```

This completely removes CAP and restores your original Claude Code configuration.

---

### What `cap uninstall` Does

| Step | Action | Effect |
|------|--------|--------|
| 1 | Deregisters MCP servers | Runs `claude mcp remove` for all 4 CAP servers |
| 2 | Removes agent definitions | Deletes CAP-installed files from `~/.claude/agents/` |
| 3 | Removes workflow scripts | Deletes CAP-installed files from `~/.claude/workflows/` |
| 4 | Restores original configs | Copies backup `~/.claude.json` and `~/.claude/settings.json` back |
| 5 | Removes `~/.claude-platform/` | Deletes all data, config, logs, and backups |

> **Note:** Step 4 restores the exact `~/.claude.json` and `~/.claude/settings.json` that existed before `cap init` was first run. Any manual changes you made to MCP server config after install will be lost — re-add them after uninstall if needed.

---

### Partial Removal (Keep Data)

```sh
cap uninstall --keep-data --yes
uv tool uninstall claude-agent-platform
```

This removes:
- MCP server registrations
- Agent definitions
- Workflow scripts
- Restores original configs

But **preserves**:
- `~/.claude-platform/data/` (all databases)
- `~/.claude-platform/config.toml`

This is useful for:
- **Upgrading** — uninstall old version, install new, run `cap init` (databases are preserved)
- **Temporary removal** — keep your indexed knowledge for later reinstall
- **Debugging** — remove the servers but keep data for inspection

---

## Troubleshooting

### Common Issues

#### "MCP server failed to start"

```sh
# Check which Python uv is using
uv tool run --from claude-agent-platform python --version

# Verify the tool is installed correctly
uv tool list | grep claude-agent-platform

# Check fleet health
cap fleet health-check

# View server logs
ls ~/.claude-platform/logs/
```

> **Tip:** If the Python version is wrong, reinstall with: `uv tool install claude-agent-platform --python 3.12`

#### "Embeddings unavailable"

```sh
# Verify AWS credentials
aws sts get-caller-identity --profile your-profile

# Check Bedrock model access
aws bedrock list-foundation-models --profile your-profile \
  --query "modelSummaries[?modelId=='amazon.titan-embed-text-v2:0']"

# Test SSO session
aws sso login --profile your-profile
```

> **Note:** If credentials are expired or unavailable, CAP continues working with degraded search (keyword + graph). Fix credentials when convenient — no urgency.

#### "Permission denied on database"

```sh
# Diagnose and fix permissions
cap doctor --fix --yes

# Or manually fix
chmod 600 ~/.claude-platform/data/*.db
```

#### "Claude CLI not found"

CAP requires Claude Code to be installed and on your PATH:

```sh
# Check if claude is available
which claude

# If not found, install Claude Code first:
# https://docs.anthropic.com/en/docs/claude-code/overview
```

#### "cap: command not found" after install

The `uv tool install` binary directory may not be on your PATH:

```sh
# Add uv tool bin to PATH (add to ~/.zshrc or ~/.bashrc)
export PATH="$HOME/.local/bin:$PATH"

# Or check where uv installed it
uv tool dir
```

---

### Logs

| Log source | Location | How to access |
|------------|----------|---------------|
| MCP server logs | `~/.claude-platform/logs/` | `cat ~/.claude-platform/logs/cap-*.log` |
| Database diagnostics | — | `cap doctor` |
| Fleet health | — | `cap fleet status` |
| Init/uninstall output | stdout | Visible during command execution |

---

### Reset Without Reinstall

If something is corrupted or you want a fresh start without uninstalling the package:

```sh
cap uninstall --yes
cap init --force
```

The `--force` flag:
- Recreates all databases from scratch
- Overwrites config with defaults
- Reinstalls all agents and workflows
- Re-registers MCP servers

> **Warning:** This destroys all indexed knowledge, session learnings, and workflow history. Use `--keep-data` on the uninstall step if you want to preserve databases.

---

## System Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| **OS** | macOS 12+ / Linux (Ubuntu 20.04+, Debian 11+) | macOS 14+ / Ubuntu 22.04+ |
| **Python** | 3.11 | 3.12 or 3.13 |
| **Disk space** | 100 MB | 500 MB (with knowledge indexing) |
| **RAM** | — | No significant memory overhead |
| **Internet** | Required for AWS Bedrock only | — |
| **Claude Code** | Any version with MCP support | Latest |

> **Note:** CAP runs entirely locally except for embedding generation (AWS Bedrock API calls). All databases, knowledge, and session data remain on your machine. No data is sent to third parties beyond the Bedrock embedding requests.

---

## Upgrade Path

To upgrade CAP to a newer version:

```sh
# Upgrade the package
uv tool upgrade claude-agent-platform

# Re-initialize (preserves databases, updates agents/workflows/servers)
cap init --force
```

This updates agent definitions, workflow scripts, and MCP server registrations while preserving your existing databases and knowledge.

---

## Quick Reference

| Task | Command |
|------|---------|
| Install | `uv tool install claude-agent-platform` |
| Initialize | `cap init` |
| Check health | `cap status` |
| Database diagnostics | `cap doctor` |
| Fleet status | `cap fleet status` |
| Fix permissions | `cap doctor --fix --yes` |
| Uninstall (full) | `cap uninstall --yes && uv tool uninstall claude-agent-platform` |
| Uninstall (keep data) | `cap uninstall --keep-data --yes && uv tool uninstall claude-agent-platform` |
| Reset | `cap uninstall --yes && cap init --force` |
| Upgrade | `uv tool upgrade claude-agent-platform && cap init --force` |

---

## Related Documentation

| Doc | Link |
|:----|:-----|
| Configuration reference | [CONFIGURATION.md](CONFIGURATION.md) |
| Usage guide | [USAGE.md](USAGE.md) |
| Build & distribution | [DISTRIBUTION.md](DISTRIBUTION.md) |
| Technical architecture | [TECHNICAL.md](TECHNICAL.md) |
| System architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Architecture decisions | [adr/](adr/) |

---

*Back to [README](../README.md)*

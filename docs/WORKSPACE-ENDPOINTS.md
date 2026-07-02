# Workspace and Endpoint Configuration Guide

CAP's workspace registry and endpoint system manages multiple local development environments and remote Git repositories. This guide covers registration, configuration, synchronization, and best practices.

## Quick Start

### 1. Auto-Registration

Workspaces are auto-registered when you first use CAP in a directory:

```bash
cd /Users/dev/my-project
# First Claude Code session in this directory automatically registers it
```

### 2. Manual Registration

```bash
cap config workspaces add /Users/dev/my-project
cap sync  # Initial index
```

### 3. Configure Remote Endpoints

```bash
cap config endpoints add \
  --type github \
  --org my-github-org \
  --ssh-endpoint git@github.com \
  --auto-clone
```

### 4. Verify Setup

```bash
cap config workspaces list
cap config endpoints list
cap knowledge status  # Shows indexed workspaces
```

---

## Workspace Registration

A workspace is any directory containing code, configuration, or documentation that you want indexed into the knowledge base.

### Auto-Registration

When `session_start` is called with a path not in the registry, CAP automatically adds it:

```python
# When this is called from /Users/dev/my-project
mcp__cap-session__session_start({
  "workspace": "/Users/dev/my-project"
})
# Result: workspace auto-added with default config
```

**Default auto-added workspace config:**
```json
{
  "path": "/Users/dev/my-project",
  "sync_frequency": "5m",
  "include_patterns": [
    "*.py", "*.ts", "*.tsx", "*.go", "*.java",
    "*.tf", "*.yaml", "*.yml", "*.json",
    "*.md", "*.adoc", "*.rst",
    "Dockerfile", "docker-compose.yaml"
  ],
  "exclude_patterns": [
    ".git/", "node_modules/", "vendor/",
    ".terraform/", "__pycache__/", ".venv/",
    "dist/", "build/", "target/",
    ".pyc$", ".o$", ".so$", ".dylib$",
    ".lock$", ".env$"
  ],
  "depth": null,
  "last_synced": "2026-07-02T16:19:00Z",
  "auto_added": true
}
```

### Manual Registration with Custom Config

```bash
cap config workspaces add /path/to/project \
  --sync-frequency 1h \
  --include '*.ts,*.tsx,*.md' \
  --exclude 'node_modules,dist'
```

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | — | Absolute path to workspace root |
| `sync_frequency` | string | "5m" | Sync interval: "5m", "1h", "6h", "never" |
| `include_patterns` | array | standard | File glob patterns to index |
| `exclude_patterns` | array | standard | Regex patterns to skip |
| `depth` | int \| null | null | Max directory traversal depth (null = unlimited) |
| `last_synced` | string | — | ISO 8601 timestamp of last index |
| `auto_added` | bool | — | Whether auto-registered (informational) |

### Include/Exclude Patterns

**Include patterns** use glob-style matching (*, ?, []):
```json
"include_patterns": [
  "*.py",          // All Python files
  "*.tf",          // Terraform
  "Dockerfile",    // Exact name
  "**/*.md"        // Markdown anywhere
]
```

**Exclude patterns** use regex matching:
```json
"exclude_patterns": [
  ".git/",         // Hidden .git directory
  "node_modules/", // Node modules
  "__pycache__/",  // Python cache
  "\.lock$",       // Any .lock file
  "test_.*\.py$"   // Python test files
]
```

### Traversal Depth

Controls how deep CAP indexes into directory hierarchies:

```json
// Unlimited depth (default)
"depth": null

// Only root level files
"depth": 0

// Root + one subdirectory level
"depth": 1

// Root + two levels (typical for monorepos)
"depth": 2
```

### Sync Frequency

Background daemon respects these intervals:

| Frequency | Description |
|-----------|-------------|
| "5m" | Sync every 5 minutes (default) |
| "15m" | Sync every 15 minutes |
| "1h" | Sync every hour |
| "6h" | Sync every 6 hours |
| "never" | Only sync on explicit `cap sync` call |

Synchronization is idempotent: unchanged files are skipped to minimize embedding API calls.

### Multiple Workspaces Example

```bash
# Monorepo with multiple services
cap config workspaces add /Users/dev/moia-platform \
  --sync-frequency 15m \
  --depth 2

# Individual service repository
cap config workspaces add /Users/dev/moia-payment-service \
  --sync-frequency 5m

# Documentation repository
cap config workspaces add /Users/dev/moia-docs \
  --sync-frequency 1h \
  --include '*.md'

# Legacy system (archived, sync rarely)
cap config workspaces add /Users/dev/legacy-system \
  --sync-frequency 6h
```

---

## Endpoint Configuration

Endpoints define remote Git repositories for auto-clone and dependency resolution.

### Supported Endpoint Types

- **GitHub** — `type: "github"`
- **GitLab** — `type: "gitlab"`
- **Gitea** — `type: "gitea"`

### Adding Endpoints

**GitHub:**
```bash
cap config endpoints add \
  --type github \
  --org my-org \
  --ssh-endpoint git@github.com \
  --auto-clone
```

**GitLab:**
```bash
cap config endpoints add \
  --type gitlab \
  --org my-group \
  --ssh-endpoint git@gitlab.com \
  --auto-clone
```

**Self-Hosted Gitea:**
```bash
cap config endpoints add \
  --type gitea \
  --org platform \
  --ssh-endpoint git@gitea.internal.company \
  --auto-clone
```

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | — | "github", "gitlab", or "gitea" |
| `org` | string | — | Organization or group name |
| `ssh_endpoint` | string | — | SSH host for cloning (e.g., "git@github.com") |
| `auto_clone` | bool | false | Enable auto-clone on missing dependencies |
| `clone_base_path` | string | "~/Projects" | Where to clone repos |
| `discovery_frequency` | string | "1h" | How often to scan for new repos |

### Auto-Clone Workflow

When a service references an unknown repository:

1. **Detection** — Knowledge server encounters missing repo in Terraform/ArgoCD manifest
2. **Lookup** — Searches registered endpoints for matching `org`
3. **Clone** — `git clone --depth=1 <repo-url>` to `clone_base_path/<repo-name>`
4. **Index** — Cloned repo automatically added to workspace registry
5. **Sync** — Content indexed into knowledge base within 5 minutes

Example:
```hcl
# In Terraform
data "terraform_remote_state" "auth_service" {
  backend = "s3"
  config = {
    bucket = "tf-state"
    key    = "services/auth"
  }
}

# Causes lookup in endpoints for "auth-service" repo
# If not found locally, clones from endpoint matching GitHub org
```

### Multiple Endpoints

```bash
# Public GitHub organization
cap config endpoints add \
  --type github \
  --org moia-oss \
  --ssh-endpoint git@github.com \
  --auto-clone

# Internal GitHub Enterprise
cap config endpoints add \
  --type github \
  --org moia-internal \
  --ssh-endpoint git@github.company.com \
  --auto-clone

# GitLab self-hosted
cap config endpoints add \
  --type gitlab \
  --org platform-team \
  --ssh-endpoint git@gitlab.internal \
  --auto-clone
```

---

## Synchronization

### Manual Sync

**Sync all registered workspaces:**
```bash
cap sync
```

**Sync a specific workspace:**
```bash
cap sync -w /Users/dev/my-project
```

**Force re-sync (ignore frequency limits):**
```bash
cap sync --force
```

**Sync with detailed output:**
```bash
cap sync -v
# Shows: files indexed, embeddings queued, conflicts resolved
```

### Automatic Background Sync

The knowledge server daemon automatically syncs workspaces according to their `sync_frequency`:

1. On startup: Syncs any workspace not synced in its frequency window
2. Every minute: Checks for workspaces due for sync
3. Skips unchanged files (uses mtime + size hash)
4. Queues embeddings asynchronously (non-blocking)

**Daemon behavior:**
```
Start → Load workspace registry
        ↓
        For each workspace:
          if (now - last_synced) > sync_frequency:
            Scan files matching include/exclude patterns
            For each new/modified file:
              Add to knowledge base
              Queue embedding (async)
        ↓
        Every 6h: Run VACUUM on knowledge.db
        ↓
        Sleep 1 minute, repeat
```

### Checking Sync Status

```bash
# Show index health and staleness
cap knowledge status

# Output:
# Knowledge Base Status
# ├─ Total documents: 2,847
# ├─ Embeddings pending: 3
# ├─ Workspaces indexed: 3
# │  ├─ /Users/dev/platform (synced 2m ago)
# │  ├─ /Users/dev/payment-service (synced 45m ago)
# │  └─ /Users/dev/docs (synced 6h ago)
# └─ Database size: 342 MB
```

---

## Configuration Files

### File Locations

All configuration is stored in `~/.claude-platform/`:

```
~/.claude-platform/
├── harness-config.json          # Provider, models, workspaces, endpoints
├── config.toml                   # Platform settings
├── knowledge.db                  # Indexed content + embeddings
├── sessions.db                   # Session memory
└── fleet.db                      # MCP server health
```

### harness-config.json Structure

```json
{
  "provider": "aws-bedrock",
  "workspaces": [
    {
      "path": "/Users/dev/moia-platform",
      "sync_frequency": "5m",
      "include_patterns": ["*.py", "*.tf", "*.md"],
      "exclude_patterns": [".git", "node_modules", "__pycache__"],
      "depth": null,
      "last_synced": "2026-07-02T16:19:00Z",
      "auto_added": false
    }
  ],
  "endpoints": [
    {
      "type": "github",
      "org": "moia-dev",
      "ssh_endpoint": "git@github.com",
      "auto_clone": true,
      "clone_base_path": "/Users/dev/Projects",
      "discovery_frequency": "1h"
    }
  ],
  "models": { /* ... */ },
  "budget": { /* ... */ }
}
```

### Editing Configuration

**Via CLI (recommended):**
```bash
cap config workspaces list
cap config workspaces add /path/to/project
cap config endpoints list
cap config endpoints add --type github --org myorg --ssh-endpoint git@github.com
```

**Via file (direct edit):**
```bash
$EDITOR ~/.claude-platform/harness-config.json
```

After editing, changes take effect on next:
- `cap sync`
- `session_start` call
- MCP server restart

---

## Best Practices

### 1. Organize by Team/Service

```bash
# Services
cap config workspaces add /Users/dev/payment-service
cap config workspaces add /Users/dev/auth-service
cap config workspaces add /Users/dev/notification-service

# Infrastructure
cap config workspaces add /Users/dev/platform-infrastructure

# Documentation
cap config workspaces add /Users/dev/platform-docs
```

### 2. Use Appropriate Sync Frequencies

```bash
# Active development: sync frequently
cap config workspaces add /Users/dev/active-service --sync-frequency 5m

# Stable/reference: sync less often
cap config workspaces add /Users/dev/shared-lib --sync-frequency 1h

# Archived: sync rarely
cap config workspaces add /Users/dev/legacy --sync-frequency 6h
```

### 3. Optimize Include/Exclude Patterns

```bash
# Monorepo: include only relevant code
cap config workspaces add /Users/dev/monorepo \
  --include '*.go,*.yaml,*.md' \
  --exclude 'vendor,node_modules,dist'

# Frontend: include TypeScript and assets
cap config workspaces add /Users/dev/web \
  --include '*.tsx,*.ts,*.css,*.md' \
  --exclude 'node_modules,.next,dist'
```

### 4. Use Depth Limiting for Large Repos

```bash
# Monorepo: limit depth to relevant levels
cap config workspaces add /Users/dev/moia-mono \
  --depth 2  # src/services/*, terraform/*, docs/*

# Avoid indexing unnecessarily deep structures
# Reduces embedding calls and keeps knowledge base focused
```

### 5. Configure Endpoints Before Use

```bash
# Add all your organization endpoints upfront
cap config endpoints add --type github --org moia-dev --ssh-endpoint git@github.com --auto-clone
cap config endpoints add --type gitlab --org internal --ssh-endpoint git@gitlab.internal --auto-clone

# Then references to those orgs auto-clone seamlessly
```

### 6. Use SSH Endpoints (Required)

Always use SSH endpoints, never HTTPS:

```bash
# Correct
--ssh-endpoint git@github.com

# Incorrect (will not work)
--ssh-endpoint https://github.com
```

SSH avoids credential prompts and works with agent forwarding.

### 7. Batch Workspaces for Monorepos

```bash
# For large monorepos, consider a single workspace entry
# with careful include/exclude patterns rather than multiple entries

# Good for monorepo
cap config workspaces add /Users/dev/monorepo \
  --include '*.tf,*.yaml,*.go' \
  --exclude 'vendor,node_modules' \
  --depth 2

# Instead of (slower, redundant):
# cap config workspaces add /Users/dev/monorepo/services/auth
# cap config workspaces add /Users/dev/monorepo/services/payment
```

---

## Troubleshooting

### Workspace Not Syncing

**Check workspace is registered:**
```bash
cap config workspaces list
# Look for your workspace path
```

**Check sync frequency:**
```bash
cap knowledge status
# Shows last_synced timestamp for each workspace
```

**Force sync:**
```bash
cap sync --force -w /path/to/workspace
```

### Auto-Clone Not Working

**Verify endpoint is configured:**
```bash
cap config endpoints list
# Ensure org name matches repo organization
```

**Check SSH connectivity:**
```bash
ssh -T git@github.com
# Should succeed without prompting for password
```

**Enable debug logging:**
```bash
CAP_LOG_LEVEL=DEBUG cap knowledge search "query"
# Look for "clone" messages in output
```

### Files Not Being Indexed

**Verify patterns match:**
```bash
# Check file types in workspace
cd /path/to/workspace
find . -type f | head -20

# Check if include/exclude patterns match
cap config workspaces list -w /path/to/workspace
# Review include_patterns and exclude_patterns
```

**Verify workspace registered:**
```bash
cap knowledge status
# Workspace should appear in list
```

**Check file size limit:**
Files larger than 500 KB are skipped by default. Edit `config.toml` to increase:
```toml
[knowledge.sync]
max_file_size_kb = 1000  # Index files up to 1 MB
```

### High Memory Usage

**Reduce concurrent syncs:**
```toml
[bedrock]
embedding_max_concurrent = 1  # Process one at a time
```

**Reduce workspace frequency:**
```bash
cap config workspaces update /path --sync-frequency 1h
```

### Stale Knowledge Base

**Manual compaction:**
```bash
cap knowledge vacuum
# Removes stale entries for deleted files
```

**Reset and re-index:**
```bash
rm ~/.claude-platform/knowledge.db
cap sync --force
```

---

## Examples

### Example 1: Single Service Development

```bash
# Clone service repository
git clone git@github.com:moia-dev/payment-service.git ~/dev/payment

# Register workspace
cap config workspaces add ~/dev/payment --sync-frequency 5m

# Add GitHub endpoint for dependencies
cap config endpoints add \
  --type github \
  --org moia-dev \
  --ssh-endpoint git@github.com \
  --auto-clone

# Initial sync
cap sync

# Now when working in Claude Code, all code and docs are searchable
# Any referenced repos auto-clone and index
```

### Example 2: Platform Engineering Setup

```bash
# Multiple services + infrastructure
cap config workspaces add /Users/dev/moia-auth --sync-frequency 5m
cap config workspaces add /Users/dev/moia-payment --sync-frequency 5m
cap config workspaces add /Users/dev/moia-infra --sync-frequency 15m
cap config workspaces add /Users/dev/moia-docs --sync-frequency 1h

# GitHub endpoints
cap config endpoints add --type github --org moia-dev --ssh-endpoint git@github.com --auto-clone
cap config endpoints add --type github --org moia-platform --ssh-endpoint git@github.com --auto-clone

# GitLab for internal tools
cap config endpoints add --type gitlab --org platform --ssh-endpoint git@gitlab.internal --auto-clone

# Sync everything
cap sync

# Check status
cap knowledge status
```

### Example 3: Monorepo Development

```bash
# Register monorepo with smart patterns
cap config workspaces add /Users/dev/moia-mono \
  --sync-frequency 15m \
  --include '*.go,*.tf,*.yaml,*.md' \
  --exclude 'vendor,node_modules,dist,build' \
  --depth 2

# Register endpoints for cross-org references
cap config endpoints add --type github --org moia-dev --ssh-endpoint git@github.com --auto-clone
cap config endpoints add --type github --org moia-platform --ssh-endpoint git@github.com --auto-clone

# Initial index
cap sync

# Now search across the entire monorepo
cap knowledge search "database migration strategy"
```

---

## Related Documentation

- [CLI Reference](cli-reference.md) — Complete command reference
- [CONFIGURATION.md](CONFIGURATION.md) — Platform and provider configuration
- [Knowledge Base](intelligent-indexing.md) — Indexing and search details
- [Installation](installation.md) — Setup and initialization

---

*Back to [README](../README.md)*

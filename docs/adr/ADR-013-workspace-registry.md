# ADR-013: Workspace & Endpoint Registry in harness-config.json

**Status:** Accepted  
**Date:** 2026-07-02  
**Context:** Version 2 (CAP System Design v1)

## Context

CAP's knowledge daemon needs two things to operate autonomously: (1) which local filesystem paths to index, and (2) which remote endpoints to discover and clone repositories from. Before this ADR, both were ad-hoc:

- Workspaces were hardcoded in `~/.claude/config.toml` with no consistent schema
- GitHub/GitLab endpoints were added manually per-engineer with no shared format
- The `session_start` handler had no mechanism to register the current working directory automatically
- There was no CLI surface for inspecting or modifying the workspace list at runtime
- `ssh_url_template` appeared in some configs, `ssh_endpoint` in others — tooling broke depending on which key was present

The result: engineers frequently ran `cap sync` only to find no workspaces registered, or discovered the daemon had been silently skipping repos because an endpoint entry was missing.

**Key constraints:**
- Configuration must be human-readable and diff-friendly (CI change review)
- Schema must support both local (filesystem) and remote (GitHub/GitLab) sources
- Auto-registration must be safe to call repeatedly (idempotent)
- CLI management must be discoverable without reading source code
- The daemon must be able to derive its full work list from a single file without additional environment variables

## Decision

**`harness-config.json` is the single source of truth for all workspace and endpoint configuration, structured under `"workspaces"` and `"endpoints"` top-level keys.**

### File Location

```
~/.claude/harness-config.json
```

The harness server reads this file on startup and on every sync cycle. Hot-reload is supported via file mtime check — no restart required for workspace additions.

### Workspace Entry Schema

```json
{
  "workspaces": [
    {
      "path":            "/Users/eng/repos/my-service",
      "sync_frequency":  "15m",
      "include":         ["**/*.py", "**/*.tf", "**/*.yaml", "**/*.md"],
      "exclude":         [".git/**", "node_modules/**", "__pycache__/**", "*.pyc"],
      "depth":           5,
      "last_synced":     "2026-07-02T14:30:00Z",
      "auto_added":      false
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | ✅ | Absolute filesystem path to workspace root |
| `sync_frequency` | string | ✅ | Duration string: `5m`, `15m`, `1h`, `6h` |
| `include` | string[] | ✅ | Glob patterns for files to index |
| `exclude` | string[] | ✅ | Glob patterns for files to skip |
| `depth` | integer | ✅ | Max directory traversal depth (default: 5) |
| `last_synced` | ISO-8601 string | — | Written by daemon after each successful sync; null on first registration |
| `auto_added` | boolean | ✅ | `true` if registered by `session_start` handler; `false` if added manually via CLI |

### Endpoint Entry Schema

```json
{
  "endpoints": [
    {
      "type":                "github",
      "org":                 "moia-dev",
      "ssh_endpoint":        "git@github.com",
      "auto_clone":          true,
      "clone_base_path":     "/Users/eng/repos",
      "discovery_frequency": "6h"
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"github"` \| `"gitlab"` | ✅ | Provider type |
| `org` | string | ✅ | GitHub org name or GitLab group path |
| `ssh_endpoint` | string | ✅ | SSH host (e.g. `git@github.com`). **Not** `ssh_url_template` |
| `auto_clone` | boolean | ✅ | If `true`, daemon clones newly-discovered repos automatically |
| `clone_base_path` | string | ✅ | Parent directory for auto-cloned repos |
| `discovery_frequency` | string | ✅ | How often to query the endpoint for new repos |

> **Naming note:** The field is `ssh_endpoint`, not `ssh_url_template`. The full clone URL is constructed at runtime as `<ssh_endpoint>:<org>/<repo>.git`. Any config using `ssh_url_template` must be migrated.

### Auto-Registration (`session_start` Handler)

When Claude Code opens a new session, the `session_start` hook fires `cap register-workspace`. This call:

1. Reads `$PWD` (current working directory at session open time)
2. Checks `harness-config.json` workspaces for an entry with `path == $PWD`
3. If **not found**: appends a new workspace entry with defaults (`sync_frequency: "15m"`, standard include/exclude globs, `auto_added: true`, `last_synced: null`)
4. If **found**: no-op (idempotent)
5. Writes the updated file atomically (write to `.harness-config.json.tmp`, then rename)

Auto-added workspaces use conservative defaults. Engineers can refine them via `cap config workspaces edit`.

### CLI Interface

**Workspaces:**

```bash
# List all registered workspaces
cap config workspaces list

# Add a workspace manually
cap config workspaces add /path/to/repo --sync-frequency 15m

# Remove a workspace (does not delete files)
cap config workspaces remove /path/to/repo

# Show last sync time and status for all workspaces
cap config workspaces status
```

**Endpoints:**

```bash
# List configured remote endpoints
cap config endpoints list

# Add a GitHub org endpoint
cap config endpoints add \
  --type github \
  --org moia-dev \
  --ssh-endpoint git@github.com \
  --auto-clone \
  --clone-base-path /Users/eng/repos

# Add a GitLab group endpoint
cap config endpoints add \
  --type gitlab \
  --org platform/infra \
  --ssh-endpoint git@gitlab.company.com \
  --no-auto-clone \
  --clone-base-path /Users/eng/gitlab

# Remove an endpoint by org
cap config endpoints remove --type github --org moia-dev
```

All commands print a diff of the config change before writing, and require no confirmation flag (changes are immediately visible in `list`).

### Daemon Sync Behaviour

The CAP daemon (`cap daemon start`) reads `harness-config.json` on each tick and:

1. Iterates `workspaces`, syncing any entry whose `last_synced` is null or older than `sync_frequency`
2. Iterates `endpoints`, running discovery on the configured `discovery_frequency`
3. For auto-clone endpoints, clones newly-discovered repos into `clone_base_path` and appends them to `workspaces` with `auto_added: true`
4. Compacts `cap.db` (SQLite `VACUUM`) every 6 hours — tracked via a separate `last_compacted` entry in the daemon state file

The daemon re-reads `harness-config.json` on every sync iteration (mtime-checked), so workspace additions take effect on the next tick without restart.

### Example Full Config

```json
{
  "workspaces": [
    {
      "path":            "/Users/eng/repos/claude-agent-platform",
      "sync_frequency":  "15m",
      "include":         ["**/*.py", "**/*.tf", "**/*.yaml", "**/*.md", "**/*.json"],
      "exclude":         [".git/**", "node_modules/**", "__pycache__/**", "*.pyc", ".venv/**"],
      "depth":           6,
      "last_synced":     "2026-07-02T14:30:00Z",
      "auto_added":      false
    },
    {
      "path":            "/Users/eng/repos/platform-infra",
      "sync_frequency":  "1h",
      "include":         ["**/*.tf", "**/*.yaml", "**/*.md"],
      "exclude":         [".git/**", ".terraform/**", "*.tfstate*"],
      "depth":           4,
      "last_synced":     null,
      "auto_added":      true
    }
  ],
  "endpoints": [
    {
      "type":                "github",
      "org":                 "moia-dev",
      "ssh_endpoint":        "git@github.com",
      "auto_clone":          true,
      "clone_base_path":     "/Users/eng/repos",
      "discovery_frequency": "6h"
    }
  ]
}
```

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **`~/.claude/config.toml` (existing approach)** | Already present; TOML is human-friendly | No shared schema; `ssh_url_template` naming inconsistency; no CLI surface; no auto-registration support | Rejected |
| **Environment variables per workspace** | Zero config file overhead | Does not survive shell restarts; unshareable; daemon cannot read env vars from a different process context | Rejected |
| **SQLite-only storage (no JSON file)** | Single source; directly queryable | Not human-readable; not diff-friendly in code review; requires `cap` installed to inspect | Rejected |
| **Separate `workspaces.json` and `endpoints.json`** | Smaller files, single concern per file | Two files to keep in sync; `cap daemon start` must read and merge both; atomic cross-file updates are complex | Rejected |
| **YAML format** | More expressive than JSON for comments | Requires PyYAML dependency in harness paths; JSON is already a project dependency; YAML indentation errors are silent | Rejected |
| **Auto-register on every `cap` command (not just `session_start`)** | More aggressive coverage | Creates spurious workspace entries for repos the engineer is just querying, not actively working in | Rejected |

## Consequences

### Positive
- **Single source of truth:** One file, one schema — no reconciliation between config.toml, env vars, and ad-hoc CLI flags
- **Discoverable:** `cap config workspaces list` and `cap config endpoints list` give an instant view of the full sync surface; no documentation required
- **Auto-registration:** Engineers opening a new repo in Claude Code get it indexed within 15 minutes without any manual setup
- **CLI-manageable:** All mutations go through the CLI, which performs schema validation before writing — malformed entries cannot enter the config
- **Daemon self-contained:** The daemon can be started on any machine and derives its complete work list from the single config file, with no environment variable prerequisites
- **Diff-friendly:** JSON diffs in pull requests clearly show workspace additions/removals, making config changes auditable in code review

### Negative
- **File growth with workspace count:** Each workspace entry is ~200 bytes of JSON; at 20 workspaces this is ~4KB — negligible in practice, but the file is not designed for hundreds of entries
- **Auto-added entries accumulate:** Workspaces auto-added from `session_start` are never automatically removed; engineers must run `cap config workspaces remove` for repos they no longer use (mitigated: `cap config workspaces status` surfaces stale entries by `last_synced` age)
- **JSON has no comments:** Engineers cannot annotate workspace entries inline; mitigation is `cap config workspaces list` which shows human-readable status, and the README for this file
- **`ssh_endpoint` migration required:** Existing configs using `ssh_url_template` must be migrated; `cap config migrate` command handles this automatically in v2.2.0

## Related ADRs

- [ADR-007: Incremental Git-Diff Based Ingestion](ADR-007-ingestion-strategy.md) — Ingestion strategy that the daemon executes per registered workspace
- [ADR-012: KB Enforcement Hooks](ADR-012-kb-enforcement-hooks.md) — Uses workspace path as partition key for `kb_search_flags`
- [ADR-018: Automatic Knowledge Freshness](ADR-018-auto-sync.md) — 5 sync triggers that complement the daemon's frequency-based sync

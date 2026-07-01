# Intelligent Indexing — CAP Knowledge Base Construction

Intelligent indexing powers CAP's searchable knowledge base and dependency graph. It combines file-level indexing (for grep-like precision) with repo-level semantic analysis (for understanding what services do and how they connect).

## Overview

The indexing system:
- Scans configured workspace roots to discover repositories
- Tracks changes via git SHA and file content hashing
- Indexes file contents with sentence-aware chunking and full-text search
- Generates semantic summaries via LLM (Bedrock Claude)
- Builds a queryable dependency graph
- Generates embeddings for semantic search
- Operates within configurable budget constraints

All data is stored in `~/.claude-platform/data/knowledge.db` (SQLite with FTS5).

## Pipeline Phases

### Phase 1: Discovery

Scans `indexing.local_paths` from configuration for git repositories.

Detection markers:
- `.git` — git repository
- `go.mod` — Go module
- `pyproject.toml` — Python project
- `Chart.yaml` — Helm chart
- `Dockerfile` — container image
- `package.json` — Node.js/JavaScript project

Output: Total repos discovered and paths for next phase.

### Phase 2: Incremental Check

Compares each repository's current HEAD SHA against the last-indexed SHA in the `sync_state` table.

- **Unchanged repos:** Skipped entirely
- **Changed repos:** Proceed to Phase 3
- **Force re-index:** Use `--full` flag to bypass SHA comparison

Enables fast incremental runs: unchanged workspaces complete in <100ms.

### Phase 3: Repo-Level Extraction

`repo_extractor.extract_and_index_repos` processes each changed repository:

1. Read README (if present)
2. Detect technology stack from markers (go.mod, pyproject.toml, Chart.yaml, package.json, Dockerfile)
3. List top-level dependencies
4. Identify key entry point files:
   - Go: `main.go`, `cmd/main.go`
   - Python: `main.py`, `app.py`, `__main__.py`
   - TypeScript: `index.ts`, `main.ts`

Output: One structured knowledge entry per repository (summary prose) inserted into `knowledge_entries` table.

### Phase 4: File-Level Sync

`sync_engine.sync_workspace` walks all files in the workspace.

**Filtering:**
- Includes: `.go`, `.py`, `.ts`, `.tsx`, `.java`, `.rs`, `.sh`, `.yaml`, `.toml`, `.json`, `.md`
- Skips: `node_modules/`, `.git/`, `vendor/`, `__pycache__/`, `build/`, `dist/`, `.venv/`, `.env*`, `*.pyc`

**Deduplication via SHA-256:**
- Hash file contents
- Skip files with matching previous hash
- On change: re-chunk and re-embed

**Secret Scanning:**
- Reject files containing patterns: `AKIA` (AWS keys), `-----BEGIN PRIVATE KEY`, `sk_live_`, `ghp_`
- Log violations to `rejection_log` table

**Sentence-Aware Chunking:**
- Target chunk size: 512 tokens
- Overlap window: 64 tokens
- Splits on sentence boundaries to preserve context

**Graph Edges:**
- File imports (detected via regex parse for `import`, `require`, `from`, `use`)
- Cross-repo dependencies (namespace matching)

### Phase 5: LLM Semantic Analysis (optional)

Optional LLM-powered semantic summaries. Skip with `--skip-llm` flag.

**`CodeUnderstanding` module:**

1. **Entry Files:** Reads primary source files
   - Go: `main.go`, `cmd/main.go`
   - Python: `main.py`, `app.py`, `server.py`
   - TypeScript: `index.ts`, `main.ts`, `server.ts`

2. **Manifest Files:** Reads configuration and dependency declarations
   - `Chart.yaml` (Helm)
   - `go.mod` (Go dependencies)
   - `pyproject.toml` (Python dependencies)
   - `package.json` (Node.js dependencies)

3. **Semantic Summary:** Calls Bedrock Claude to produce human-readable descriptions
   - What does this service/repo do?
   - What is its primary responsibility?
   - What are the main components?

4. **Budget Awareness:**
   - Per-run budget limit (default: $2.00, configurable via `--budget`)
   - Stops gracefully with `BudgetExceeded` exception when limit reached
   - File sync and graph construction continue (they are free)

5. **Concurrency:** Default 3 parallel LLM calls (configurable)

### Phase 6: Dependency Resolution (optional)

Optional dependency graph construction. Extracts cross-repo dependencies.

**Sources scanned:**
- Go imports in `go.mod` → dependencies on external Go modules
- Python imports in `pyproject.toml` → dependencies on PyPI packages
- Helm charts in `Chart.yaml` → dependencies on other Helm repos or services
- Terraform remote state references → cross-workspace state dependencies
- ArgoCD `repoURL` values → deployment dependencies

**Dependency edge types:**
- `depends-on` — A depends on B
- `deploys-to` — A deployment manifest targets cluster B
- `owned-by` — Service A is owned by team B
- `contains` — Monorepo A contains submodule B
- `imports` — A imports B

### Phase 7: Knowledge Graph Construction

`KnowledgeGraph` builds queryable nodes and edges.

**Node Types:**
- `repo` — Git repository
- `service` — Deployable service (Helm chart, Docker image, Lambda)
- `module` — Go module, Python package, or npm package
- `helm-chart` — Helm chart package
- `terraform-module` — Terraform module

**Edge Predicates:**
- `depends-on(A, B)` — A depends on B (runtime or build)
- `deploys-to(A, B)` — A deployment deploys to B (cluster, function, queue)
- `owned-by(A, B)` — A is owned by team B
- `contains(A, B)` — A is a superset/monorepo containing B
- `imports(A, B)` — A imports/requires B (code-level)

**Queryable via:**
- `cap index graph --stats` — Node and edge counts
- `cap index graph --node my-service --connected` — Show connected nodes
- `cap index graph --export mermaid` — Export as Mermaid diagram
- MCP tools: `knowledge_graph_query`, `knowledge_graph_traverse`

### Phase 8: Embedding Generation (optional)

Optional vector embeddings for semantic search. Skip with `--skip-embeddings` flag.

**Process:**
1. New/updated entries queued to `embedding_queue` table
2. `EmbeddingClient` batches texts (25 per API call)
3. Embeds into vector database for semantic similarity search

**Models:**
- Primary: `amazon.titan-embed-text-v2:0` (1024 dimensions, high quality)
- Fallback: `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions, local)

**Used for:**
- Semantic search in MCP tools (`knowledge_search` with natural language queries)
- Retrieval-augmented generation (agent context injection)

## Multi-Path Workspace Scanning

Configure multiple workspace roots to scan separate directory trees:

```json
{
  "indexing": {
    "local_paths": [
      "/home/user/projects/platform",
      "/home/user/projects/services",
      "/home/user/projects/infra"
    ],
    "exclude_patterns": ["archived-*", "test-*"],
    "max_file_size_mb": 10,
    "max_repo_age_days": 90
  }
}
```

Each path is scanned independently. Repositories across paths can reference each other in the dependency graph via namespace matching or explicit cross-repo imports.

## Remote Git Auto-Clone

When indexing or querying the knowledge graph discovers references to repositories not available locally:

1. Check knowledge graph for the repo name
2. If not found locally, check GitHub org configuration
3. Auto-clone via SSH: `git clone --depth 1 git@github.com:<org>/<repo>.git`
4. Index the cloned repo into the knowledge base
5. Update graph edges with new dependencies

**Configure via:**
```bash
cap github config --org myorg --clone-path /path/to/clones --ssh
```

**Safety limits:**
- `max_auto_clones_per_session` (default: 10)
- Fails safely if org is not configured
- SSH keys required (no HTTPS credentials)

## Incremental Updates

Indexing is designed for efficiency in CI/CD and periodic runs.

**Tracking:**
- `sync_state` table: (repo_path, HEAD_sha, last_indexed_at)
- File-level deduplication: SHA-256 hashing per file
- Only changed files are re-chunked and re-embedded

**Change Detection:**
- `git diff --name-status` for file-level changes
- SHA-256 comparison for content drift
- `--full` flag forces complete re-index, ignoring SHA tracking

**Incremental vs Full:**
- Incremental (default): 100ms–5 seconds for unchanged workspaces
- Full: Varies by size (1000 repos ≈ 5–10 minutes with LLM enabled)

## Budget-Aware Operation

Indexing respects per-run and cumulative budget limits for LLM calls (Bedrock Claude).

**Configuration:**
```bash
cap index run --budget 2.0          # Per-run limit: $2.00
cap index status --cumulative       # Show total cost this month
```

**Behavior:**
- Tracks cost of each LLM API call during semantic analysis phase
- File sync and graph construction are free (local operations)
- Stops gracefully when budget exceeded (`BudgetExceeded` exception)
- Completed entries are preserved; incomplete entries are skipped
- Cumulative cost tracked in `~/.claude-platform/data/indexer_state.json`

**Default limits:**
- Per-run: $2.00
- Monthly cumulative: $50.00 (configurable)

## Daemon Mode

Periodic background re-indexing of workspaces.

**Enable daemon:**
```bash
cap index daemon --enable --interval 60
```

**Check status:**
```bash
cap index daemon --status
```

**Disable:**
```bash
cap index daemon --disable
```

When enabled, the CAP harness triggers incremental indexing at the configured interval (in minutes). Daemon state is stored in `~/.claude-platform/data/indexer_daemon.json`.

Daemon runs are logged to `~/.claude-platform/logs/indexer-daemon.log`.

## CLI Commands

### Full Indexing Run

```bash
cap index run --workspace /path --budget 2.0 --concurrency 3
```

Trigger a complete indexing run:
- `--workspace PATH` — Target workspace (default: current directory)
- `--budget FLOAT` — Per-run LLM budget in USD (default: 2.0)
- `--concurrency INT` — Parallel LLM calls (default: 3)

### Skip Expensive Phases

```bash
cap index run --skip-llm --skip-embeddings
```

- `--skip-llm` — Skip semantic analysis (Phase 5). File indexing proceeds.
- `--skip-embeddings` — Skip embedding generation (Phase 8). Semantic search unavailable.

### Force Full Re-Index

```bash
cap index run --full
```

Ignore SHA tracking; re-index all files and repos.

### Check Indexer Status

```bash
cap index status
```

Shows:
- Total entries indexed
- Repos discovered
- Last indexing run timestamp
- Current budget usage
- Embedding queue status

### View Dependencies

```bash
cap index deps --repo my-service --type terraform
```

List dependencies for a specific repository:
- `--repo NAME` — Repository name or path
- `--type TYPE` — Filter by type: `go`, `python`, `node`, `terraform`, `helm`

### Explore Knowledge Graph

```bash
cap index graph --stats
```

Graph statistics: total nodes, edges, connected components.

```bash
cap index graph --node my-service --connected
```

Show all nodes connected to `my-service` (dependencies and dependents).

```bash
cap index graph --export mermaid
```

Export entire graph as Mermaid diagram (markdown).

## Storage Schema

All indexing data lives in `~/.claude-platform/data/knowledge.db` (SQLite):

### `knowledge_entries` table

```sql
CREATE TABLE knowledge_entries (
  id TEXT PRIMARY KEY,           -- repo/service identifier
  type TEXT NOT NULL,             -- 'repo', 'service', 'module', etc.
  content TEXT NOT NULL,          -- indexed text (full content)
  summary TEXT,                   -- LLM semantic summary
  metadata JSONB,                 -- repo_path, tech_stack, maintainers
  created_at DATETIME,
  updated_at DATETIME,
  UNIQUE(id)
);
```

Full-text search (FTS5) index on `content` for grep-like queries.

### `embedding_queue` table

```sql
CREATE TABLE embedding_queue (
  id INTEGER PRIMARY KEY,
  entry_id TEXT,                  -- foreign key to knowledge_entries
  text_chunk TEXT,
  status TEXT,                    -- 'pending', 'completed', 'failed'
  embedding BLOB,                 -- 1024-dim vector (Titan) or 384-dim (MiniLM)
  error_message TEXT,
  created_at DATETIME,
  indexed_at DATETIME
);
```

### `knowledge_graph_nodes` table

```sql
CREATE TABLE knowledge_graph_nodes (
  id TEXT PRIMARY KEY,            -- node identifier
  type TEXT NOT NULL,             -- 'repo', 'service', 'module', etc.
  label TEXT,                     -- human-readable name
  metadata JSONB,                 -- tech_stack, version, url
  created_at DATETIME
);
```

### `knowledge_graph_edges` table

```sql
CREATE TABLE knowledge_graph_edges (
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  predicate TEXT NOT NULL,        -- 'depends-on', 'deploys-to', etc.
  metadata JSONB,                 -- version constraints, conditionals
  created_at DATETIME,
  UNIQUE(source_id, target_id, predicate)
);
```

### `sync_state` table

```sql
CREATE TABLE sync_state (
  repo_path TEXT PRIMARY KEY,
  head_sha TEXT NOT NULL,         -- current git HEAD SHA
  last_indexed_at DATETIME,
  file_count INT,
  chunk_count INT,
  updated_at DATETIME
);
```

Tracks incremental sync progress per repository.

### `rejection_log` table

```sql
CREATE TABLE rejection_log (
  id INTEGER PRIMARY KEY,
  file_path TEXT,
  rejection_reason TEXT,          -- 'secret_detected', 'size_exceeded', etc.
  details TEXT,
  created_at DATETIME
);
```

Records files skipped due to secrets or policy violations.

## State File

`~/.claude-platform/data/indexer_state.json` tracks persistent state:

```json
{
  "last_run_at": "2026-07-01T18:34:00Z",
  "last_run_status": "success",
  "total_cost_usd": 1.42,
  "repos_indexed": 147,
  "entries_created": 892,
  "entries_updated": 156,
  "errors": []
}
```

Used to:
- Detect daemon interval expiry
- Report cumulative LLM cost
- Identify failed runs for retry

## Error Handling

**File sync failures:**
- Logs to `rejection_log` table
- Continues with next file
- Does not block completion

**LLM budget exceeded:**
- Stops semantic analysis phase
- Returns `BudgetExceeded` exception
- File sync and graph construction complete normally

**Secret detected:**
- Rejects file from indexing
- Logs path and reason to `rejection_log`
- Continues with next file

**Embedding queue failures:**
- Retries up to 3 times with exponential backoff
- Moves to `failed` status after max retries
- Does not block knowledge search (falls back to FTS5)

## Cross-references

- [ARCHITECTURE.md](ARCHITECTURE.md) — System architecture overview
- [CONFIGURATION.md](CONFIGURATION.md) — Configuration reference for `indexing.*` keys
- [cli-reference.md](cli-reference.md) — Complete `cap index` CLI documentation
- [ADR-015: Knowledge Graph Design](adr/adr-015-knowledge-graph.md)
- [ADR-008: Multi-Workspace Indexing](adr/adr-008-workspace-scanning.md)

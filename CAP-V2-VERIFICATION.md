Now I have sufficient context from the actual codebase. Let me produce the verification report.

## Verification Report: CAP v2 Plan vs Ruflo Implementation

---

### Fully Covered (matches or exceeds Ruflo)

**1. Chunking**
- Ruflo: sentence-aware, 512 chars, 50 char overlap
- CAP Plan: sentence/paragraph/character strategies, 1024 chars, 100 char overlap, mandatory CHARACTER fallback for oversized chunks
- Verdict: CAP is MORE robust. The recursive fallback guarantee (no chunk ever exceeds max_size) is stronger than Ruflo's fixed strategy. Larger chunk size (1024 vs 512) is appropriate for the Titan V2 model's 8192-token context.

**2. Content Hash Dedup**
- Ruflo: SHA-256 content hash, pre-fetch existing hashes
- CAP Plan: SHA-256 (truncated to 32 chars), batch pre-fetch via `_get_existing_hashes()` (already exists in sync_engine.py line 121)
- Verdict: Equivalent. The existing `content_hash` column and `idx_ke_content_hash` index in knowledge.db schema confirm infrastructure is in place.

**3. Retry Logic**
- Ruflo: exponential backoff, 3 retries, 2^n * 100ms
- CAP Current: exponential backoff, 3 retries, 0.5s base, 2x multiplier, 10s cap (embeddings.py lines 126-183)
- Verdict: Already implemented in current code. Parameters are slightly more conservative (500ms base vs 100ms) which is appropriate for Bedrock rate limits.

**4. FTS5 Fallback**
- Ruflo: keyword/FTS5 when embedder fails
- CAP Current: Already implemented. `hybrid_search` in retrieval.py supports FTS5-only mode. knowledge_server.py falls back when `embedding_client.is_available is False`.
- Verdict: Equivalent, already working.

**5. Persistent Embedding Cache**
- Ruflo: persistent SQLite LRU, 1000 entries, 7-day TTL
- CAP Plan: persistent SQLite BLOB LRU, 1000 entries, 7-day TTL
- Verdict: Exact match. CAP's BLOB storage (struct.pack) is more efficient than typical JSON serialization.

**6. Consolidation**
- Ruflo: sweep expired, dedup, compact every 6h
- CAP Plan: WAL checkpoint, sweep expired, dedup by content_hash, requeue transient failures, purge orphans. Triggered every 50th search if >6h since last run.
- Verdict: CAP plan is MORE comprehensive (5 phases vs 3). The opportunistic trigger design is honest about Claude Code constraints.

**7. Workflow Scripts Calling agent()**
- Ruflo: Workflow scripts spawn real subagents via agent() with pipeline/parallel/phase primitives
- CAP Current: Already implemented. `cost-optimization.js` (verified) uses `phase()`, `parallel()`, `agent()` with labels, models, and agentTypes. 10 workflow scripts exist.
- Verdict: Already working. The plan's 5 new workflows are incremental additions to an existing functional pattern.

**8. Agent Definitions**
- Ruflo: 100+ YAML agents with structured capabilities
- CAP Current: 21 markdown-based agent definitions exist at `src/cap/data/agents/`. Plan adds 7 YAML versions.
- Verdict: CAP has fewer agents (21 vs 100+) but covers the critical roles. Markdown format is already functional as Claude Code natively reads these.

---

### Partially Covered (gaps identified)

**1. Per-Entry Metadata (Ruflo: index, startPos, endPos, length, tokenCount, contentHash)**
- CAP Plan: chunk_index, start_pos, end_pos, content_hash in Chunk dataclass
- GAP: `tokenCount` is missing from the plan's schema migration. The `ALTER TABLE knowledge_entries ADD COLUMN chunk_index` is there, but `start_pos`, `end_pos`, and `token_count` per chunk are only in the Python Chunk dataclass -- not persisted to the database. This means chunk-level attribution is available at search time only if the search returns the full chunk text; the positional metadata for highlighting/pinpointing is lost.
- Recommendation: Add `token_count` column to knowledge_entries migration. Consider storing `start_pos`/`end_pos` in the existing `metadata` JSON column rather than adding more columns.

**2. Health Events (Ruflo: explicit events per component -- embedder/storage/index)**
- CAP Plan: `cap doctor` shows component health, `cap status` extended with embedder_health field
- GAP: No structured health_events table or health event recording mechanism. The plan checks component status on demand but does not record health transitions over time. Ruflo's event-driven health model enables trend analysis ("embedder was degraded 3x this week").
- Recommendation: Low priority for v2 -- the on-demand check is sufficient for a single-user tool. Add health event recording in a future phase if observability becomes an issue.

**3. Versioning per Entry (Ruflo: version, createdAt, updatedAt, accessCount, expiresAt)**
- CAP Current: knowledge_entries has `created_at`, `updated_at`, `expires_at` but NO `version` or `access_count` fields.
- CAP Plan: Does not add `version` or `access_count` to knowledge_entries.
- GAP: No entry versioning. When content is updated, the old version is overwritten (upsert). Ruflo can track how many times content has been accessed and which version is current.
- Recommendation: Low impact for v2. `access_count` could be useful for cache eviction decisions. Add in Phase 4 if needed.

**4. Orchestration Topologies (Ruflo: Raft, Byzantine, GOAP)**
- CAP Plan: Linear phases, parallel within phases, no consensus algorithms
- GAP: The plan explicitly removes consensus.py, quorum.py. This is appropriate given Claude Code constraints (single-user, single-machine), but it means multi-agent conflict resolution falls to the human PO.
- Recommendation: Justified omission. Raft/Byzantine are designed for distributed systems; CAP is single-node. The `conflict_raise` MCP tool in cap-backlog is the correct PO-in-the-loop replacement.

**5. Background Workers (Ruflo: 12 auto-triggered background workers)**
- CAP Plan: Zero background workers. Replaced with opportunistic triggers.
- GAP: This is a fundamental architectural constraint of Claude Code (MCP servers die on idle). The plan acknowledges this and proposes reasonable workarounds (every-50th-search, explicit CLI commands).
- Recommendation: Justified tradeoff. The plan is honest about this limitation. The alternative (a persistent daemon) would be a separate tool entirely.

**6. Budget Gate Granularity**
- Ruflo: Per-workflow cost tracking with phase-level granularity
- CAP Plan: Budget gate at workflow start + per-phase ceiling ($2/agent)
- GAP: The plan specifies budget checking but the actual `budget_ledger` table in platform.db only tracks by workspace/period/model. There is no mechanism to correlate a specific workflow execution with its cost entries. The workflow scripts have no way to atomically reserve budget.
- Recommendation: Add a `workflow_id` column to `budget_ledger` or create a separate `workflow_costs` table. Without this, the per-phase ceiling is unenforceable -- the script can only check total remaining budget, not what this specific workflow has consumed.

---

### Missing (not in plan)

**1. HNSW Binary Snapshots (Ruflo: atomic HNSW binary + metadata JSON every Nth store)**
- Plan explicitly removes this (Feasibility Finding 8) and proposes `table.compact_files()` instead.
- Impact: If LanceDB corrupts, recovery requires full re-embedding from knowledge_entries. With 7,234 entries at current scale, this takes ~30 minutes. At 3-year projected 200K entries, it would take hours.
- Recommendation: Accepted tradeoff for v2. At current scale, re-embedding is fast enough. Add periodic LanceDB directory backup (file-level copy with write-pause) in a later phase when vector count exceeds 50K.

**2. Plugin Marketplace / Extensibility**
- Ruflo: Plugin system for custom agents and tools
- CAP: No plugin system. Agents are defined as markdown/yaml files in `src/cap/data/agents/`.
- Impact: Low. Single-user tool with a known domain (MOIA platform engineering).
- Recommendation: Not needed for v2. The existing agent definition pattern is sufficient.

**3. Web UI for Workflow Visibility**
- Ruflo: Web dashboard for real-time workflow monitoring
- CAP Plan: stderr logging from workflow scripts, `cap status` CLI command
- Impact: Users see workflow progress only if watching the terminal.
- Recommendation: Acceptable for v2. Claude Code's TUI already shows agent activity. Adding a web UI would be significant scope creep.

**4. Auto-Learning Loop (Ruflo: 1.9-4.7x improvement reported)**
- Ruflo: Automated closed-loop learning with measurable improvement metrics
- CAP Plan: Threshold adaptation after 50 samples, trust decay, hard ceilings
- GAP: No automated A/B testing or improvement measurement. CAP's learning is threshold-only (which tier to route to), while Ruflo reportedly improves actual agent output quality.
- Recommendation: The plan's conservative approach (bounded thresholds, hard ceilings) is safer for a production tool. Measuring "improvement" without automated quality evaluation is hand-waving anyway. Keep as-is.

**5. Zero-Config Install (Ruflo: `npx wizard install`, 2 min)**
- CAP Plan: Phase 4 includes `cap init` improvements, auto-detect AWS profile
- GAP: `cap init` still requires CAP to be installed first. There is no `npx`-style one-shot install.
- Recommendation: Low priority. CAP is installed via pip (`pip install -e .`) for the developer who maintains it. A zero-config public distribution is out of scope.

---

### CAP Advantages Validated

1. **Classified Failed Item Retry**: Ruflo retries all failed items blindly. CAP's plan distinguishes transient (Throttl/Timeout) from permanent (ValidationException) failures. This prevents infinite retry loops on permanently invalid content. Genuinely better.

2. **Secret Scanner as Ingest Gate**: Ruflo has no documented pre-ingest secret scanning. CAP's `reject_if_secrets()` running BEFORE content enters the DB is a real security improvement. The entropy-based detection for novel patterns is a strong addition.

3. **Hard Trust Ceilings with Decay**: Ruflo's trust systems (if any) are not bounded. CAP's 0.85 ceiling + lazy decay toward 0.5 prevents runaway trust escalation and ensures human oversight is never fully removed. Appropriate for a platform engineering context where mistakes are costly.

4. **SQL Portability**: CAP fixes the `FILTER` clause issue (SQLite 3.30+ requirement). Currently broken in `engine.py` lines 148-154 and 207-220. The `SUM(CASE WHEN...)` replacement is a real improvement for cross-platform compatibility.

5. **WAL Checkpoint over VACUUM**: The plan correctly identifies that VACUUM blocks concurrent readers. `PRAGMA wal_checkpoint(TRUNCATE)` is the right choice for a tool where search must remain available during maintenance.

6. **Provenance Tracking**: The `source_agent` + `verified` fields enable KB content filtering without complex ACLs. Pragmatic for a single-user tool that still needs to distinguish machine-generated from human-verified knowledge.

---

### Final Recommendations (changes needed before implementation)

**Critical (must fix before starting Phase 1):**

1. **EmbeddingClient constructor crash** (file: `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/servers/knowledge_server.py` line 50-58): The knowledge_server.py creates `EmbeddingClient()` at module import time. If AWS credentials are invalid, `boto3.Session().client()` raises immediately, crashing the entire MCP server. The plan identifies this as "Bug 7" but it is blocking -- until this is fixed, `cap knowledge` cannot start without valid AWS creds. Fix this FIRST in Phase 1 before any other work.

2. **Orchestrator server imports dead code** (file: `/Users/harsh/VWITS/MOIA/moia-dev-master/claude-agent-platform/src/cap/servers/orchestrator_server.py` lines 34-41): The orchestrator imports `executor`, `context`, `planner`, `checkpoint` which are marked for deletion in Phase 2. These imports will crash after deletion. The deletion and import cleanup must be an atomic operation -- plan specifies this but the phase ordering (Phase 2) means the orchestrator server is broken during Phase 1 testing.
   - Recommendation: Move import cleanup to early Phase 2, or gate the imports with try/except in Phase 1 so the server does not crash.

3. **Budget gate has no enforcement mechanism**: The plan says workflows check `cap_status` for budget. But `budget_ledger` in platform.db is never populated by anything in the current code. There is no cost-tracking write path. The budget gate will always return "full budget remaining" because nothing decrements it.
   - Recommendation: Phase 2 must implement the cost recording side (workflow scripts must call `mcp__cap-orchestrator__cap_status` to RECORD costs, not just read them). Otherwise the budget gate is theater.

**High (fix before Phase 2):**

4. **Existing workflows have no budget gate or outcome recording**: The 10 existing workflow scripts (cost-optimization.js, incident-response.js, etc.) do not record outcomes or check budget. The plan only creates 5 new workflows with these features.
   - Recommendation: Retrofit the existing 10 workflows with the budget gate + outcome recording pattern during Phase 2, not just the 5 new ones. Otherwise, most actual workflow executions will never contribute to learning.

5. **Router `workflow_name` extraction is brittle**: The proposed `TASK_KEYWORD_PATTERNS` uses substring matching (`"fix" in prompt_lower`). This will false-positive on strings like "suffix", "prefix", "fixture". 
   - Recommendation: Use word-boundary matching (`re.search(r'\bfix\b', prompt_lower)`) or at minimum filter out common false positives.

6. **LanceDB `to_pandas()` for duplicate check is expensive**: The plan proposes `to_pandas().query(f"id == '{uuid}'")` which loads the entire table into memory. At 6,440 vectors this is fine; at 50K it will OOM.
   - Recommendation: Use LanceDB's native `table.search().where(f"id = '{uuid}'").limit(1)` which pushes the filter to the engine. Or maintain a UUID set in SQLite (already have `knowledge_entries.uuid` column -- just query that instead of LanceDB).

**Medium (can address during implementation):**

7. **Chunk overlap handling for FTS5 triggers**: When chunked entries are inserted into knowledge_entries, the FTS5 trigger fires per chunk. This means searching for a term that spans a chunk boundary will miss it. The 100-char overlap mitigates this but does not eliminate it.
   - Recommendation: Document as known limitation. Consider storing the full content as a separate FTS5 entry alongside chunks (one "full" entry for FTS5 + N chunk entries for vector search).

8. **Schema migration atomicity**: The plan's `ALTER TABLE` statements cannot be rolled back in SQLite (DDL is auto-committed). If migration fails partway (e.g., disk full after adding `chunk_index` but before `source_agent`), the schema is in an inconsistent state.
   - Recommendation: Add a `migration_in_progress` flag to `user_version` (e.g., use version 1.5 during migration, set to 2 only on completion). Check for this on startup and re-run if detected.

9. **Plan claims ~1,400 lines deleted but actual count is 2,060**: The dead code files total 2,060 lines (verified via wc -l). The plan underestimates by ~600 lines. Not a correctness issue but the Phase 2 "done criteria" should use the accurate number.

10. **Agent YAML vs existing Markdown**: The plan proposes 7 new YAML agent definitions, but 21 markdown agents already exist. The plan does not specify whether YAML replaces markdown or coexists. The existing workflow scripts reference agents by label string (e.g., `agentType: 'optimization'`) not by file path.
    - Recommendation: Keep markdown format (it already works). YAML adds a format migration burden with zero functional benefit. Claude Code's `agent()` consumes the prompt text, not structured YAML fields.
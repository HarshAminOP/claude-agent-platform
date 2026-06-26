# ADR-008: Delivery Phases

## Status
Accepted (revised — replaces original phased-delivery ADR)

## Context
The platform has 4 MCP servers, shared libraries, a CLI, and maintenance tooling. 
Building everything at once is risky. Building in phases with PO review gates ensures 
each component is solid before building on top of it.

## Decision
Five phases, each ending with: implement → internal review → PO review → next phase.

## Phases

### Phase 1 — Workflow Engine + API Gateway ✅ DONE
- Adaptive concurrency pool (3-8 slots, cost-weighted)
- Budget enforcement + kill switch
- Workflow lifecycle MCP server (6 tools)
- 8 integration tests passing

### Phase 2 — Knowledge Server (hybrid retrieval) ✅ IN PROGRESS / MOSTLY COMPLETE
- SQLite schema: entities, edges, embeddings metadata (implemented)
- FTS5 full-text index with BM25 ranking (implemented)
- Bedrock Titan V2 embedding client (implemented via ADR-005)
- LanceDB vector storage (implemented)
- Graph adjacency table + BFS traversal (implemented)
- Reciprocal Rank Fusion merger (implemented in hybrid_search)
- Entity extraction from code files (TF, K8s, Helm, ArgoCD)
- MCP server with 5 tools (implemented)
- Incremental sync engine (implemented via ADR-007)

### Phase 3 — Session Server (memory)
- Session lifecycle (start, checkpoint, end)
- Learning extraction and storage
- Semantic recall over past sessions
- User profile persistence
- Auto-correction detection
- Cross-linking to knowledge entities

### Phase 4 — Fleet Manager + CLI + Maintenance
- Fleet health monitoring (ping, restart, register)
- `cap` CLI with all commands
- Database maintenance (WAL checkpoint, vacuum, prune, backup)
- `cap doctor` with dry-run and --fix
- Retention policy enforcement

### Phase 5 — Integration + Installer
- End-to-end integration tests
- `install.sh` (single command, creates venv, registers servers)
- `upgrade.sh` (backup, migrate, restart)
- Cross-server inbox pattern validation
- Load testing (concurrent workflows + ingestion)

## Consequences
- Each phase can be validated independently
- PO reviews between phases catch requirement drift early
- Phase 2 is the largest (knowledge is the core value prop)
- Phases 3-4 can partially parallelize if Phase 2 is stable

# Architecture Decision Records — Knowledge & Retrieval System

This directory contains Architecture Decision Records (ADRs) documenting key design decisions for the Knowledge & Retrieval System, an MCP-based knowledge server for Claude Code agents.

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [ADR-001](ADR-001-search-engine.md) | Use SQLite FTS5 for full-text search (with hybrid vector search in v2) | Accepted | 2026-06-25 |
| [ADR-002](ADR-002-graph-storage.md) | Use SQLite adjacency table + in-memory Python dict for graph | Accepted | 2026-06-25 |
| [ADR-003](ADR-003-mcp-transport.md) | Use stdio transport via Python MCP SDK | Accepted | 2026-06-25 |
| [ADR-004](ADR-004-security-model.md) | Pre-index skip-based security (no inline redaction) | Accepted | 2026-06-25 |
| [ADR-005](ADR-005-bedrock-embeddings.md) | Use Bedrock Titan V2 for embeddings (supersedes ADR-001 deferral) | Accepted | 2026-06-25 |
| [ADR-006](ADR-006-no-caching.md) | No application-level cache for v1 | Accepted | 2026-06-25 |
| [ADR-007](ADR-007-ingestion-strategy.md) | Incremental git-diff based ingestion with per-repo atomic writes | Accepted | 2026-06-25 |
| [ADR-008](ADR-008-delivery-phases.md) | Three-phase delivery: FTS MVP → Hardening → Vector Search | Accepted | 2026-06-25 |

## Decision Principles

These ADRs follow MADR (Markdown Architecture Decision Records) format with:
- **Status**: Accepted, Pending, or Rejected
- **Context**: The problem and constraints
- **Decision**: What was chosen and why
- **Alternatives**: Other options considered with pros/cons
- **Consequences**: Positive and negative impacts
- **Related ADRs**: Cross-references to related decisions

## Quick Reference

**For Agents Implementing This System:**
- Start with ADR-001 (search engine choice) — foundational
- ADR-002 (graph storage) — complementary to search
- ADR-003 (MCP transport) — deployment mechanism
- ADR-004 (security) — non-negotiable pre-requisite
- ADR-005 + ADR-006 — explain why v1 is simpler than alternatives
- ADR-007 (ingestion) — operational strategy
- ADR-008 (phased delivery) — implementation roadmap

## Architecture Overview

The system indexes 41+ platform engineering repositories into typed entities with inferred relationships, enabling sub-200ms structured queries via MCP tools.

**Key Numbers:**
- ~3,000 entities (Terraform modules, K8s resources, ARgoCD apps, decisions, etc.)
- ~12,000 edges (inferred relationships)
- <2ms point lookups via SQLite
- <50ms FTS5 search queries
- 5 MCP tools (search, get entity, find related, record, system operations)
- 3 Python dependencies (mcp, python-hcl2, pyyaml)
- ~800 lines of code (~1,210 total with utilities)
- ~15MB install size

## Related Documents

- [Architecture Overview](../ARCHITECTURE.md) — Full system architecture
- [Technical Reference](../TECHNICAL.md) — API surface, internals, diagrams
- [Configuration Reference](../CONFIGURATION.md) — All config.toml options

*Back to [docs/](../)*

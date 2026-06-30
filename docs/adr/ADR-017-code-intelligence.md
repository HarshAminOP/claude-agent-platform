# ADR-017: Tree-sitter Based Code Graph

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Week 3 — Code Intelligence Engine

## Context

The code intelligence subsystem needs to extract symbols (functions, classes, imports, exports, variables) from source files across 41+ repositories to build a structural code graph. This graph powers blast-radius analysis, dead-code detection, and cross-repo dependency navigation.

Two fundamental approaches exist for symbol extraction:

1. **Regex-based parsing** — hand-written patterns per language (e.g., `/def (\w+)\(/` for Python functions)
2. **AST-based parsing** — full parse trees via tree-sitter grammars, queried with structural patterns

The platform supports Python, TypeScript, JavaScript, Go, Rust, HCL/Terraform, and YAML. Each language has distinct syntax that regex handles poorly (nested generics, decorators, multi-line signatures, etc.).

**Key constraints:**
- Must handle incomplete/invalid files gracefully (tree-sitter produces partial trees; regex fails entirely or silently misparses)
- Must support metavariable queries (`$X`, `$$$`) for structural search (the `ast_search` MCP tool)
- Must be deterministic — same file always produces same symbol set
- HCL grammar (tree-sitter-hcl) is not yet stable upstream; frequent breaking changes in grammar definition

## Decision

**Use ast-grep (which wraps tree-sitter) for symbol extraction and structural queries. Exclude HCL from AST-based extraction until the tree-sitter-hcl grammar reaches stable release (v1.0+).**

### Implementation

```python
# Symbol extraction via ast-grep CLI
SUPPORTED_LANGUAGES = {
    "python": [".py"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx"],
    "go": [".go"],
    "rust": [".rs"],
    "yaml": [".yaml", ".yml"],
}

# HCL explicitly excluded — uses regex fallback
HCL_FALLBACK_PATTERNS = {
    "resource": r'resource\s+"(\w+)"\s+"(\w+)"',
    "module": r'module\s+"(\w+)"',
    "variable": r'variable\s+"(\w+)"',
    "output": r'output\s+"(\w+)"',
    "data": r'data\s+"(\w+)"\s+"(\w+)"',
}
```

### ast-grep Integration

The existing `ast_server.py` (Section 19.8 of ARCHITECTURE.md) already wraps ast-grep for search/match/refactor. The code intelligence layer reuses the same `sg` binary for symbol extraction:

```python
async def extract_symbols(file_path: str, language: str) -> list[Symbol]:
    """Extract symbols from a file using ast-grep."""
    rules = LANGUAGE_RULES[language]
    result = subprocess.run(
        ["sg", "scan", "--rule", rules, "--json", file_path],
        capture_output=True, text=True, timeout=30
    )
    return parse_sg_output(result.stdout)
```

### Graph Schema Extension

Symbols are stored as knowledge graph nodes with `entity_type = "symbol"`:

```sql
-- Example nodes created by code intelligence
INSERT INTO knowledge_graph_nodes (uuid, entity_name, entity_type, workspace, metadata)
VALUES (
    'sym-abc123',
    'src/cap/memory/scorer.py::compute_score',
    'symbol',
    '/path/to/workspace',
    '{"kind": "function", "language": "python", "line": 42, "params": ["entry", "context"]}'
);
```

Edges connect symbols to their containing files, imports, and call targets:

- `defines` — file defines symbol
- `imports` — file imports symbol
- `calls` — symbol calls another symbol
- `inherits` — class inherits from another class

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Regex per language** | No binary dependency, simple | Fragile (nested parens, generics, decorators break it), no partial-parse recovery, no metavariable support, exponential maintenance per language | Rejected |
| **Python `ast` module + per-language equivalents** | Standard library for Python | Each language needs its own parser library, no unified query interface, heavy dependencies | Rejected |
| **tree-sitter directly (via py-tree-sitter)** | Full control, Python bindings | Must manage grammar compilation, no structural query syntax (must write tree traversal code), larger binary per grammar | Rejected (ast-grep wraps this better) |
| **ast-grep for all languages including HCL** | Uniform approach | tree-sitter-hcl grammar breaks on complex HCL (nested dynamic blocks, provider aliases), upstream has no v1.0 release, would cause extraction failures on ~30% of Terraform files | Rejected for HCL specifically |
| **Language Server Protocol (LSP)** | Most accurate, handles edge cases | Requires running a server per language, heavy resource usage, startup latency, not designed for batch extraction | Rejected |

## Consequences

### Positive
- **Accuracy:** AST-based extraction handles edge cases that regex cannot (multi-line signatures, nested structures, string interpolation)
- **Unified query interface:** `sg` metavariables (`$X`, `$$$`) work across all supported languages
- **Partial-parse tolerance:** tree-sitter produces valid partial trees for files with syntax errors
- **Performance:** ast-grep is Rust-native, processes files in <10ms each
- **Reuse:** Same `sg` binary already deployed for `ast_search`/`ast_match`/`ast_refactor` MCP tools
- **Deterministic:** Same file + same grammar version = same symbol set (no non-determinism)

### Negative
- **Binary dependency:** Requires `sg` (ast-grep) installed — already present for AST MCP server
- **HCL gap:** Terraform files use regex fallback until grammar stabilizes — lower accuracy for HCL symbols
- **Grammar updates:** tree-sitter grammar updates may change extraction results (mitigated by pinning grammar versions)
- **YAML limitation:** YAML is not truly a programming language; symbol extraction is limited to key paths (useful for K8s manifests, ArgoCD apps)

### HCL Migration Plan

When tree-sitter-hcl reaches v1.0:
1. Add HCL to `SUPPORTED_LANGUAGES`
2. Remove `HCL_FALLBACK_PATTERNS`
3. Re-index all `.tf` files through AST pipeline
4. Validate accuracy against regex baseline (expect improvement on nested blocks)

## Related ADRs

- [ADR-002: Graph Storage](ADR-002-graph-storage.md) — SQLite adjacency table stores the code graph
- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — Incremental sync triggers re-extraction on file change
- [ADR-019: Degree-Aware Graph Traversal](ADR-019-degree-aware-graph.md) — Hub-aware BFS for high-degree symbol nodes

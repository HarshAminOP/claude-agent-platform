# ADR-003: Use stdio Transport via Python MCP SDK

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

The system must integrate with Claude Code's MCP (Model Context Protocol) server system to expose knowledge queries as tools available to agents.

**Constraints:**
- Claude Code has native MCP support; integration must use official protocols
- Agents query via standard MCP tool invocations (no custom HTTP/websocket required)
- Server must support concurrent queries from multiple agent threads
- Tool discovery must be automatic (agents see available tools without configuration)
- Transport must be robust; protocol errors should not break agent execution

## Decision

**Use Python MCP SDK 1.26.0 with stdio transport. Server runs as subprocess; reads JSON-RPC messages from stdin, writes responses to stdout.**

**Rationale:**
- Standard Claude Code MCP transport; no custom negotiation needed
- Official Anthropic SDK handles JSON-RPC marshaling and tool registration
- Subprocess model provides isolation; server crash doesn't crash Claude Code
- Stdin/stdout is reliable, debuggable, and works across platforms
- Tool discovery via MCP protocol — agents automatically discover all 5 tools

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **HTTP/SSE server (custom)** | Flexibility, can add webhooks later | Requires HTTP client/server complexity, port management, Claude Code integration work | Rejected |
| **gRPC** | High performance, modern protocol | Requires Protocol Buffers, adds 20MB+ to install, overkill for local communication | Rejected |
| **File-based IPC (fifo/sockets)** | Platform-native, low overhead | Complex sync, error handling, less robust than subprocess | Rejected |
| **Direct Python import (no MCP)** | Simplest code, fastest execution | Breaks Claude Code integration, agents can't use tools, not practical | Rejected |
| **Custom JSON-RPC over pipes** | Lightweight, full control | Reinvents MCP protocol, loses SDK benefits, error-prone | Rejected |

## Consequences

### Positive
- **Official integration:** Uses Claude Code's native MCP support
- **Automatic tool discovery:** Agents see tools without manual registration
- **Subprocess isolation:** Server crash is recoverable; doesn't break Claude Code
- **Standard protocol:** Future Claude Code features (batching, streaming) inherit for free
- **Debuggable:** Stdin/stdout can be logged/captured for troubleshooting
- **Cross-platform:** stdio works on macOS, Linux, Windows

### Negative
- **Stdout protection critical:** Any rogue print/log to stdout breaks MCP protocol (mitigated by redirecting stdout to stderr at module load time)
- **Process overhead:** Subprocess startup takes ~100ms (acceptable, amortized across usage)
- **Blocking tool calls:** Long-running queries block the agent thread until response (acceptable with non-blocking ingestion)
- **Error propagation:** Server crashes require Claude Code to restart MCP server (rare, handled by health checks)

## Required Architecture

```
Claude Code Agent
    │
    └─> MCP Client (built-in)
            │ (stdin/stdout)
            ▼
    Python MCP Server (subprocess)
    server.py ──┬─> Query Engine (FTS5)
                ├─> Graph Index (in-memory)
                └─> Ingestion Manager (background thread)
                        │
                        ▼
                    SQLite Database
```

## Stdout Protection (Critical)

The server **MUST** redirect stdout to stderr before any imports. A single rogue print() breaks MCP protocol:

```python
#!/usr/bin/env python3
import sys
import os

# CRITICAL: Reserve stdout exclusively for MCP transport
_mcp_stdout = sys.stdout
sys.stdout = sys.stderr
os.environ["PYTHONWARNINGS"] = "ignore"

# Now safe to import (no module can print to stdout)
import asyncio
from mcp import Server
```

## Registration in Claude Code

The installer registers the server in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "KNOWLEDGE_DATA_DIR": "/path/to/.claude/knowledge-db",
        "KNOWLEDGE_WORKSPACE": "/path/to/repos"
      }
    }
  }
}
```

Claude Code starts the server on demand, communicates via stdio, and terminates it when done.

## Tool Exposure

All 5 MCP tools are exposed via standard tool registration:

```python
@app.call_tool()
async def handle_knowledge_search(query: str, entity_types: list[str] = None, ...) -> str:
    """Tool handler for MCP."""
    result = engine.search(query, entity_types, ...)
    return json.dumps({"success": True, "data": result})
```

Agents invoke tools naturally:
```
Agent calls: knowledge_search(query="EKS cluster deployment")
MCP marshals: JSON-RPC to server
Server executes: FTS5 search
Response: {"success": true, "data": [...]}
```

## Related ADRs

- [ADR-001: Search Engine](ADR-001-search-engine.md) — Implemented via MCP tool
- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — Ingestion triggered via `knowledge_system` tool

## Implementation Notes

**Dependencies:**
- `mcp==1.26.0` (official Anthropic SDK)
- Handles JSON-RPC marshaling, tool discovery, error handling

**No custom network logic needed:** SDK abstracts stdin/stdout complexity.

**SLO:** Tool invocation round-trip <100ms (excluding query execution time).

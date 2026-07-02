#!/usr/bin/env python3
"""Code Intelligence MCP Server — AST-aware code structure and dependency queries.

Provides tools for navigating code structure, finding dependents, tracing call
chains, computing blast radius, and searching symbols across indexed workspaces.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from cap.db import get_db, migrate
from cap.code_intel.queries import (
    code_structure as _code_structure,
    code_dependents as _code_dependents,
    code_trace as _code_trace,
    blast_radius as _blast_radius_simple,
)
from cap.code_intel.blast_radius import blast_radius as _blast_radius_full
from cap.code_intel.indexer import index_file as _index_file

logger = logging.getLogger("cap.code_intel")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Database setup
from cap.config import get_cap_home, get_platform_db_path
CAP_HOME = get_cap_home()
DB_PATH = get_platform_db_path()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

db = get_db(str(DB_PATH))
migrate(db)

server = Server("cap-code-intel")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="code_structure",
            description="Get all symbols and their hierarchy for a file. Returns functions, classes, methods, interfaces, imports, and relationships.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the source file to analyze.",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="code_dependents",
            description="Find all symbols and files that depend on (reference, import, call) a given symbol.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": "Name of the symbol to find dependents for (e.g., 'MyClass', 'handle_request').",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional file path to scope the search. If provided, only returns dependents that reference symbols from this file.",
                    },
                },
                "required": ["symbol_name"],
            },
        ),
        Tool(
            name="code_trace",
            description="Trace the call chain between two files/symbols. Uses BFS over the relationship graph to find how one symbol reaches another.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_file": {
                        "type": "string",
                        "description": "Starting file path or symbol name.",
                    },
                    "to_file": {
                        "type": "string",
                        "description": "Target file path or symbol name.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum traversal depth for BFS.",
                    },
                },
                "required": ["from_file", "to_file"],
            },
        ),
        Tool(
            name="blast_radius",
            description="Compute impact analysis for a file. Returns direct dependents, transitive dependents, affected tests, and risk score.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file being changed.",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="code_search",
            description="Search symbols by name pattern across the indexed codebase. Supports partial matches.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Symbol name or pattern to search for (supports SQL LIKE patterns with %).",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "typescript", "javascript", "go", "rust"],
                        "description": "Optional language filter.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["function", "class", "method", "struct", "interface", "trait", "type", "import"],
                        "description": "Optional symbol kind filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="reindex",
            description="Force re-index of a specific file. Removes existing entries and re-extracts symbols and relationships.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to re-index.",
                    },
                },
                "required": ["file_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "code_structure":
            return await _handle_code_structure(arguments)
        elif name == "code_dependents":
            return await _handle_code_dependents(arguments)
        elif name == "code_trace":
            return await _handle_code_trace(arguments)
        elif name == "blast_radius":
            return await _handle_blast_radius(arguments)
        elif name == "code_search":
            return await _handle_code_search(arguments)
        elif name == "reindex":
            return await _handle_reindex(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_code_structure(args: dict):
    file_path = os.path.abspath(args["file_path"])
    result = _code_structure(file_path, db)
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_code_dependents(args: dict):
    symbol_name = args["symbol_name"]
    file_path = args.get("file_path")

    dependents = _code_dependents(symbol_name, db)

    # If file_path is provided, filter to only show dependents that reference
    # symbols from that specific file
    if file_path:
        file_path = os.path.abspath(file_path)
        # Get symbols defined in the target file
        file_symbols = db.execute(
            "SELECT name, qualified_name FROM code_symbols WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        file_symbol_names = set()
        for row in file_symbols:
            file_symbol_names.add(row[0])
            file_symbol_names.add(row[1])

        # Only keep dependents where the target is actually from this file
        if file_symbol_names:
            dependents = [
                d for d in dependents
                if d.get("file_path") != file_path
            ]

    return [TextContent(type="text", text=json.dumps({
        "symbol": symbol_name,
        "dependents": dependents,
        "count": len(dependents),
    }))]


async def _handle_code_trace(args: dict):
    from_file = args["from_file"]
    to_file = args["to_file"]
    max_depth = args.get("max_depth", 5)

    # If paths are provided (contain /), resolve to module names for trace
    from_symbol = Path(from_file).stem if "/" in from_file else from_file
    to_symbol = Path(to_file).stem if "/" in to_file else to_file

    trace = _code_trace(from_symbol, to_symbol, db, max_depth=max_depth)

    if trace is None:
        return [TextContent(type="text", text=json.dumps({
            "from": from_file,
            "to": to_file,
            "path": None,
            "message": "No call chain found between these symbols.",
        }))]

    return [TextContent(type="text", text=json.dumps({
        "from": from_file,
        "to": to_file,
        "path": trace,
        "hops": len(trace),
    }))]


async def _handle_blast_radius(args: dict):
    file_path = os.path.abspath(args["file_path"])

    # Try the full blast radius (with knowledge graph integration) first,
    # fall back to simple if knowledge graph tables are not available
    try:
        result = _blast_radius_full(file_path, db)
    except Exception:
        result = _blast_radius_simple(file_path, db)

    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_code_search(args: dict):
    query = args["query"]
    language = args.get("language")
    kind = args.get("kind")
    limit = args.get("limit", 20)

    # Build SQL query with filters
    conditions = []
    params = []

    # Use LIKE for pattern matching
    if "%" in query:
        conditions.append("s.name LIKE ?")
        params.append(query)
    else:
        # Match as prefix, suffix, or exact
        conditions.append("(s.name LIKE ? OR s.qualified_name LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    # Language filter via JOIN on code_files
    if language:
        conditions.append("f.language = ?")
        params.append(language)

    # Kind filter
    if kind:
        conditions.append("s.kind = ?")
        params.append(kind)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    rows = db.execute(
        f"""SELECT s.qualified_name, s.name, s.kind, s.file_path,
                   s.line_start, s.line_end, s.signature, s.visibility, f.language
            FROM code_symbols s
            JOIN code_files f ON s.file_path = f.path
            WHERE {where_clause}
            ORDER BY s.name
            LIMIT ?""",
        params,
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "qualified_name": row[0],
            "name": row[1],
            "kind": row[2],
            "file_path": row[3],
            "start_line": row[4],
            "end_line": row[5],
            "signature": row[6],
            "visibility": row[7],
            "language": row[8],
        })

    return [TextContent(type="text", text=json.dumps({
        "query": query,
        "results": results,
        "count": len(results),
        "language_filter": language,
        "kind_filter": kind,
    }))]


async def _handle_reindex(args: dict):
    file_path = os.path.abspath(args["file_path"])

    if not os.path.isfile(file_path):
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "message": f"File not found: {file_path}",
        }))]

    success = _index_file(file_path, db)

    if success:
        # Get updated stats
        sym_count = db.execute(
            "SELECT COUNT(*) FROM code_symbols WHERE file_path = ?",
            (file_path,),
        ).fetchone()[0]
        rel_count = db.execute(
            "SELECT COUNT(*) FROM code_relationships WHERE file_path = ?",
            (file_path,),
        ).fetchone()[0]

        return [TextContent(type="text", text=json.dumps({
            "status": "reindexed",
            "file_path": file_path,
            "symbols_extracted": sym_count,
            "relationships_extracted": rel_count,
        }))]
    else:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "message": f"Failed to index {file_path}. File may be in an unsupported language.",
        }))]


async def _async_main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for the cap-code-intel-server console script."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

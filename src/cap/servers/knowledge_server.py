#!/usr/bin/env python3
"""Knowledge Server MCP — hybrid retrieval engine.

Owner of knowledge.db + knowledge_vectors/ (LanceDB).
Provides: search, ingest, graph, sync, status tools.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config
from lib.db_init import init_knowledge_db
from lib.embeddings import EmbeddingClient, EmbeddingConfig
from lib.retrieval import hybrid_search, SearchResult
from lib.graph import find_entities, bfs_traverse, get_related_entries, add_edge, get_node_context
from lib.security import sanitize_content, validate_path
from lib.inbox import poll_inbox, ack_message, nack_message
from lib.repo_resolver import resolve_repo, resolve_multiple, find_unresolved_dependencies, reset_session_counter

logger = logging.getLogger("cap.knowledge")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = load_config()
DATA_DIR = config.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = init_knowledge_db(DATA_DIR)
VECTORS_DIR = DATA_DIR / "knowledge_vectors"
VECTORS_DIR.mkdir(parents=True, exist_ok=True)

embedding_client = EmbeddingClient(EmbeddingConfig(
    region=config.bedrock.region,
    profile=config.bedrock.profile,
    dimensions=config.bedrock.embedding_dimensions,
    max_concurrent=config.bedrock.embedding_max_concurrent,
    max_retries=config.bedrock.max_retries,
    base_delay_s=config.bedrock.base_delay_ms / 1000.0,
    max_delay_s=config.bedrock.max_delay_ms / 1000.0,
))

vectors_table = None
try:
    import lancedb
    lance_db = lancedb.connect(str(VECTORS_DIR))
    try:
        vectors_table = lance_db.open_table("knowledge_vectors")
    except Exception:
        vectors_table = None
except ImportError:
    logger.warning("LanceDB not available — semantic search disabled")
    lance_db = None

server = Server("cap-knowledge")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# --- Search result cache (LRU-style, TTL-based) ---
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_CACHE_TTL = 300  # seconds
_SEARCH_CACHE_MAX_SIZE = 100


def _cache_key(query: str, workspace: str | None, scope: str, strategy: str, top_k: int) -> str:
    """Generate a deterministic cache key from search parameters."""
    raw = json.dumps([query, workspace, scope, strategy, top_k], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> list | None:
    """Return cached results if present and fresh, else None."""
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        return None
    ts, results = entry
    if time.monotonic() - ts > _SEARCH_CACHE_TTL:
        del _SEARCH_CACHE[key]
        return None
    return results


def _cache_put(key: str, results: list) -> None:
    """Store results in cache, evicting oldest entry on overflow."""
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX_SIZE:
        oldest_key = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        del _SEARCH_CACHE[oldest_key]
    _SEARCH_CACHE[key] = (time.monotonic(), results)


def _cache_clear() -> None:
    """Invalidate all cached search results."""
    _SEARCH_CACHE.clear()


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="knowledge_search",
            description="Search knowledge base using hybrid retrieval (keyword + semantic + graph). Returns ranked results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "workspace": {"type": "string", "description": "Workspace path to scope search. If omitted or set to 'all', searches across all indexed workspaces."},
                    "scope": {
                        "type": "string",
                        "enum": ["all", "code", "config", "doc", "decision", "convention", "glossary", "incident"],
                        "default": "all",
                    },
                    "top_k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "strategy": {
                        "type": "string",
                        "enum": ["hybrid", "keyword", "semantic", "graph"],
                        "default": "hybrid",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="knowledge_ingest",
            description="Ingest a file or text snippet into the knowledge base.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "File path or inline content"},
                    "workspace": {"type": "string", "description": "Workspace this belongs to"},
                    "content_type": {
                        "type": "string",
                        "enum": ["code", "config", "doc", "decision", "convention", "glossary", "incident"],
                    },
                    "title": {"type": "string", "description": "Human-readable title"},
                    "source_type": {
                        "type": "string",
                        "enum": ["file", "snippet", "agent_recorded", "manual"],
                        "default": "file",
                    },
                    "metadata": {"type": "object", "description": "Optional metadata"},
                },
                "required": ["source", "workspace", "content_type"],
            },
        ),
        Tool(
            name="knowledge_record",
            description="Record business knowledge (team, ownership, convention, deadline, glossary, incident).",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["team", "ownership", "convention", "deadline", "glossary", "incident"],
                    },
                    "key": {"type": "string", "description": "Unique key for this knowledge"},
                    "value": {"type": "string", "description": "Content (plain text or JSON)"},
                    "workspace": {"type": "string"},
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 3,
                            "maxItems": 3,
                        },
                        "description": "Graph relations as [subject, predicate, object] triples",
                    },
                },
                "required": ["category", "key", "value", "workspace"],
            },
        ),
        Tool(
            name="knowledge_graph_query",
            description="Traverse the knowledge graph from an entity. Returns related entities and their connections.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity name to start from"},
                    "relation_type": {"type": "string", "description": "Filter by relation type"},
                    "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 4},
                    "workspace": {"type": "string", "description": "Workspace path to scope traversal. If omitted or set to 'all', traverses across all indexed workspaces."},
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="knowledge_graph_add",
            description="Add a relationship to the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string", "default": "concept"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": "string", "default": "concept"},
                    "workspace": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["subject", "predicate", "object", "workspace"],
            },
        ),
        Tool(
            name="knowledge_sync",
            description="Trigger workspace knowledge sync (incremental by default).",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "description": "Workspace path to sync"},
                    "trigger": {
                        "type": "string",
                        "enum": ["session_start", "git_post_pull", "workspace_change", "scheduled", "manual"],
                        "default": "manual",
                    },
                    "full": {"type": "boolean", "default": False, "description": "Force full re-sync"},
                },
                "required": ["workspace"],
            },
        ),
        Tool(
            name="knowledge_status",
            description="Get knowledge base health: index size, staleness, embedding coverage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "description": "Filter to workspace (optional)"},
                },
            },
        ),
        Tool(
            name="knowledge_resolve_repo",
            description="Resolve a dependent repo: finds it locally or auto-clones from the configured GitHub org, then indexes it into the knowledge base.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_name": {"type": "string", "description": "Repository name to resolve (e.g., 'alerting', 'fleet-connector')"},
                    "domain_hint": {"type": "string", "description": "Optional domain/group directory hint (e.g., 'Observability-Alerting')"},
                },
                "required": ["repo_name"],
            },
        ),
        Tool(
            name="knowledge_resolve_deps",
            description="Find all unresolved dependencies in the knowledge graph and auto-clone missing repos from the configured GitHub org.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "description": "Scope to workspace (optional, searches all if omitted)"},
                    "auto_clone": {"type": "boolean", "default": True, "description": "Whether to auto-clone missing repos"},
                    "max_clones": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20, "description": "Max repos to clone in one call"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "knowledge_search":
            return await _handle_search(arguments)
        elif name == "knowledge_ingest":
            return await _handle_ingest(arguments)
        elif name == "knowledge_record":
            return await _handle_record(arguments)
        elif name == "knowledge_graph_query":
            return await _handle_graph_query(arguments)
        elif name == "knowledge_graph_add":
            return await _handle_graph_add(arguments)
        elif name == "knowledge_sync":
            return await _handle_sync(arguments)
        elif name == "knowledge_status":
            return await _handle_status(arguments)
        elif name == "knowledge_resolve_repo":
            return await _handle_resolve_repo(arguments)
        elif name == "knowledge_resolve_deps":
            return await _handle_resolve_deps(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_search(args: dict):
    query = args["query"]
    workspace = args.get("workspace")
    if workspace == "all":
        workspace = None
    top_k = args.get("top_k", 10)
    strategy = args.get("strategy", "hybrid")
    scope = args.get("scope", "all")

    # Check cache first
    ck = _cache_key(query, workspace, scope, strategy, top_k)
    cached = _cache_get(ck)
    if cached is not None:
        logger.debug("Search cache hit for query=%r", query)
        return cached

    query_vector = None
    if strategy in ("hybrid", "semantic") and embedding_client.is_available is not False:
        query_vector = await embedding_client.embed_single(query)

    results = hybrid_search(
        conn=db,
        vectors_table=vectors_table,
        query=query,
        query_vector=query_vector,
        workspace=workspace,
        strategy=strategy,
        top_k=top_k,
        scope=scope if scope != "all" else None,
    )

    response = [TextContent(type="text", text=json.dumps({
        "results": [
            {
                "title": r.title,
                "content_preview": r.content_preview,
                "source_path": r.source_path,
                "content_type": r.content_type,
                "score": round(r.score, 4),
                "channels": r.channels,
            }
            for r in results
        ],
        "count": len(results),
        "strategy": strategy,
        "semantic_available": query_vector is not None,
    }))]

    _cache_put(ck, response)
    return response


async def _handle_ingest(args: dict):
    workspace = args["workspace"]
    source = args["source"]
    content_type = args["content_type"]
    source_type = args.get("source_type", "file")
    title = args.get("title")
    metadata = args.get("metadata", {})

    if source_type == "file":
        resolved = validate_path(source, workspace)
        with open(resolved) as f:
            content = f.read()
        if not title:
            title = os.path.basename(resolved)
    else:
        content = source
        if not title:
            title = content[:80]

    content = sanitize_content(content)
    chash = _content_hash(content)

    existing = db.execute(
        "SELECT id FROM knowledge_entries WHERE content_hash = ? AND workspace = ?",
        (chash, workspace)
    ).fetchone()
    if existing:
        return [TextContent(type="text", text=json.dumps({"status": "duplicate", "entry_id": existing[0]}))]

    entry_uuid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (entry_uuid, workspace, source if source_type == "file" else None,
         source_type, content_type, title, content, chash, json.dumps(metadata))
    )

    entry_id = db.execute("SELECT id FROM knowledge_entries WHERE uuid = ?", (entry_uuid,)).fetchone()[0]

    db.execute(
        "INSERT INTO embedding_queue (entry_id) VALUES (?)",
        (entry_id,)
    )
    db.commit()

    # Invalidate search cache — new content may affect results
    _cache_clear()

    return [TextContent(type="text", text=json.dumps({
        "status": "ingested",
        "entry_id": entry_id,
        "uuid": entry_uuid,
        "title": title,
        "content_type": content_type,
    }))]


async def _handle_record(args: dict):
    workspace = args["workspace"]
    category = args["category"]
    key = args["key"]
    value = sanitize_content(args["value"])
    relations = args.get("relations", [])

    bk_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO business_knowledge (id, workspace, category, key, value, source)
           VALUES (?, ?, ?, ?, ?, 'agent')
           ON CONFLICT(workspace, category, key) DO UPDATE SET
               value = excluded.value,
               updated_at = datetime('now')""",
        (bk_id, workspace, category, key, value)
    )

    for rel in relations:
        if len(rel) == 3:
            add_edge(db, rel[0], category, rel[2], "concept", rel[1], workspace)

    db.commit()

    return [TextContent(type="text", text=json.dumps({
        "status": "recorded",
        "category": category,
        "key": key,
    }))]


async def _handle_graph_query(args: dict):
    entity = args["entity"]
    workspace = args.get("workspace")
    if workspace == "all":
        workspace = None
    depth = args.get("depth", 2)

    node_ids = find_entities(db, entity, workspace)
    if not node_ids:
        return [TextContent(type="text", text=json.dumps({"results": [], "message": "No matching entities found"}))]

    traversal = bfs_traverse(db, node_ids, max_depth=depth, workspace=workspace)
    context = get_node_context(db, entity, workspace)

    return [TextContent(type="text", text=json.dumps({
        "entity": entity,
        "context": context,
        "traversal": [{"node_id": nid, "distance": dist} for nid, dist in traversal[:20]],
    }))]


async def _handle_graph_add(args: dict):
    workspace = args["workspace"]
    add_edge(
        db,
        source_name=args["subject"],
        source_type=args.get("subject_type", "concept"),
        target_name=args["object"],
        target_type=args.get("object_type", "concept"),
        predicate=args["predicate"],
        workspace=workspace,
        metadata=args.get("metadata"),
    )
    db.commit()

    return [TextContent(type="text", text=json.dumps({"status": "added", "edge": f"{args['subject']} --{args['predicate']}--> {args['object']}"}))]


async def _handle_sync(args: dict):
    workspace = args["workspace"]
    trigger = args.get("trigger", "manual")
    full = args.get("full", False)

    from cap.lib.sync_engine import sync_workspace

    stats = sync_workspace(db, workspace, full=full)

    # Invalidate search cache — synced content may affect results
    _cache_clear()

    return [TextContent(type="text", text=json.dumps({
        "status": "complete" if not stats.errors else "complete_with_errors",
        "workspace": workspace,
        "trigger": trigger,
        "full": full,
        "files_scanned": stats.files_scanned,
        "files_indexed": stats.files_indexed,
        "files_updated": stats.files_updated,
        "files_unchanged": stats.files_unchanged,
        "graph_edges": stats.graph_edges_created,
        "embeddings_queued": stats.embeddings_queued,
        "errors": stats.errors[:5] if stats.errors else [],
    }))]


async def _handle_status(args: dict):
    workspace = args.get("workspace")

    where = "WHERE workspace = ?" if workspace else ""
    params = (workspace,) if workspace else ()

    total = db.execute(f"SELECT COUNT(*) FROM knowledge_entries {where}", params).fetchone()[0]
    embedded = db.execute(
        f"SELECT COUNT(*) FROM knowledge_entries {where} {'AND' if workspace else 'WHERE'} embedding_status = 'embedded'",
        params
    ).fetchone()[0]
    pending = db.execute(
        f"SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'"
    ).fetchone()[0]
    failed = db.execute(
        f"SELECT COUNT(*) FROM embedding_queue WHERE status = 'failed'"
    ).fetchone()[0]

    graph_nodes = db.execute(f"SELECT COUNT(*) FROM knowledge_graph_nodes {where}", params).fetchone()[0]
    graph_edges = db.execute(f"SELECT COUNT(*) FROM knowledge_graph_edges {where}", params).fetchone()[0]

    bk_count = db.execute(f"SELECT COUNT(*) FROM business_knowledge {where}", params).fetchone()[0]

    return [TextContent(type="text", text=json.dumps({
        "total_entries": total,
        "embedded": embedded,
        "embedding_coverage_pct": round(embedded / max(total, 1) * 100, 1),
        "embedding_queue_pending": pending,
        "embedding_queue_failed": failed,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "business_knowledge_entries": bk_count,
        "semantic_search_available": embedding_client.is_available is not False,
        "lancedb_available": vectors_table is not None,
    }))]


async def _handle_resolve_repo(args: dict):
    repo_name = args["repo_name"]
    domain_hint = args.get("domain_hint")

    result = resolve_repo(
        repo_name=repo_name,
        db=db,
        config=config.github,
        domain_hint=domain_hint,
    )

    if result.get("cloned"):
        _cache_clear()

    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_resolve_deps(args: dict):
    workspace = args.get("workspace")
    auto_clone = args.get("auto_clone", True)
    max_clones = args.get("max_clones", 5)

    unresolved = find_unresolved_dependencies(db, workspace)

    if not unresolved:
        return [TextContent(type="text", text=json.dumps({
            "status": "all_resolved",
            "unresolved_count": 0,
            "message": "All dependencies are available locally.",
        }))]

    clone_results = []
    if auto_clone:
        to_clone = unresolved[:max_clones]
        clone_results = resolve_multiple(
            [dep["repo_name"] for dep in to_clone],
            db=db,
            config=config.github,
        )
        _cache_clear()

    return [TextContent(type="text", text=json.dumps({
        "status": "resolved",
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:20],
        "clone_results": clone_results,
        "cloned_count": sum(1 for r in clone_results if r.get("cloned")),
    }))]


async def _process_embedding_queue():
    """Background task: process pending embeddings."""
    while True:
        try:
            pending = db.execute(
                """SELECT eq.id, eq.entry_id, ke.content, ke.uuid, eq.attempts
                   FROM embedding_queue eq
                   JOIN knowledge_entries ke ON ke.id = eq.entry_id
                   WHERE eq.status = 'pending' AND eq.attempts < eq.max_attempts
                   ORDER BY eq.created_at LIMIT 25""",
            ).fetchall()

            if not pending:
                await asyncio.sleep(30)
                continue

            texts = [row[2][:config.bedrock.embedding_max_input_tokens * 4] for row in pending]
            vectors = await embedding_client.embed_batch(texts)

            for row, vector in zip(pending, vectors):
                eq_id, entry_id, _, entry_uuid, attempts = row
                if vector is not None:
                    if vectors_table is not None:
                        import pyarrow as pa
                        entry = db.execute(
                            "SELECT content_type, title, source_path, workspace FROM knowledge_entries WHERE id = ?",
                            (entry_id,)
                        ).fetchone()
                        vectors_table.add([{
                            "id": entry_uuid,
                            "vector": vector,
                            "workspace": entry[3],
                            "content_type": entry[0],
                            "title": entry[1],
                            "source_path": entry[2] or "",
                            "chunk_index": 0,
                            "created_at": _now(),
                        }])
                    db.execute("UPDATE embedding_queue SET status = 'done', processed_at = ? WHERE id = ?", (_now(), eq_id))
                    db.execute("UPDATE knowledge_entries SET embedding_status = 'embedded' WHERE id = ?", (entry_id,))
                else:
                    new_attempts = attempts + 1
                    status = "failed" if new_attempts >= 3 else "pending"
                    db.execute(
                        "UPDATE embedding_queue SET attempts = ?, status = ?, last_error = 'embedding_failed' WHERE id = ?",
                        (new_attempts, status, eq_id)
                    )

            db.commit()

        except Exception as e:
            logger.error("Embedding queue processing error: %s", e, exc_info=True)

        await asyncio.sleep(10)


async def main():
    embedding_task = asyncio.create_task(_process_embedding_queue())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        embedding_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

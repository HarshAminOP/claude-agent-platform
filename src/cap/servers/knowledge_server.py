#!/usr/bin/env python3
"""Knowledge Server MCP — hybrid retrieval engine.

Owner of knowledge.db + knowledge_vectors/ (LanceDB).
Provides: search, ingest, graph, sync, status, resolve tools.

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

# --- Logging setup (stderr + file handler) ---
logger = logging.getLogger("cap.knowledge")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from cap.config import get_logs_dir
_log_dir = get_logs_dir()
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(_log_dir / "knowledge-server.log")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_file_handler)
except Exception as _log_setup_err:
    logger.warning("Could not set up file logging: %s", _log_setup_err)

# --- MCP imports (stdlib only above this point — crash risk starts here) ---
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Ensure lib/ directory is importable (sibling of this file's parent)
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Module-level globals: populated lazily by _ensure_initialized() ---
_initialized: bool = False
_init_error: str | None = None
_start_time: float = time.monotonic()

config = None
db = None
embedding_client = None
_embed_cache = None
vectors_table = None
lance_db = None
_consolidate = None
_auto_index_available: bool = False

# Search cache (LRU-style, TTL-based)
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_CACHE_TTL = 300  # seconds
_SEARCH_CACHE_MAX_SIZE = 100

# Consolidation state
_search_count = 0
_last_consolidation = 0.0

# Workspaces auto-indexed this session
_auto_indexed_this_session: set[str] = set()


def _ensure_initialized() -> None:
    """Lazily initialize all subsystems. Idempotent — safe to call on every request.

    Design intent: the MCP process must start and accept stdio connections
    immediately. Heavy init (DB open, AWS auth, LanceDB) is deferred to the
    first tool call so a cold environment (no DB yet, no AWS creds) doesn't
    kill the process before it has a chance to serve any request.

    If initialization fails the error is stored in _init_error and each tool
    handler returns a structured error message rather than raising.
    """
    global _initialized, _init_error
    global config, db, embedding_client, _embed_cache
    global vectors_table, lance_db, _consolidate, _auto_index_available

    if _initialized:
        return

    errors: list[str] = []

    # 1. Config
    try:
        from lib.config import load_config
        config = load_config()
        DATA_DIR: Path = config.data_dir
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Config loaded — DATA_DIR=%s", DATA_DIR)
    except Exception as _cfg_err:
        logger.error("load_config() failed: %s", _cfg_err)
        errors.append(f"config: {_cfg_err}")
        _init_error = "; ".join(errors)
        # Cannot proceed without config — mark failed but allow retry
        return

    DATA_DIR = config.data_dir

    # 2. SQLite knowledge DB
    try:
        from lib.db_init import init_knowledge_db
        db = init_knowledge_db(DATA_DIR)
        logger.info("Knowledge DB opened at %s", DATA_DIR)
    except Exception as _db_err:
        logger.error("init_knowledge_db() failed: %s", _db_err)
        errors.append(f"db: {_db_err}")
        # Continue — tools that need db will return errors gracefully

    # 3. Embedding client
    try:
        from lib.embeddings import EmbeddingClient, EmbeddingConfig
        embedding_client = EmbeddingClient(EmbeddingConfig(
            region=config.bedrock.region,
            profile=config.bedrock.profile,
            dimensions=config.bedrock.embedding_dimensions,
            max_concurrent=config.bedrock.embedding_max_concurrent,
            max_retries=config.bedrock.max_retries,
            base_delay_s=config.bedrock.base_delay_ms / 1000.0,
            max_delay_s=config.bedrock.max_delay_ms / 1000.0,
        ))
        logger.info("EmbeddingClient initialized")
    except Exception as _emb_init_err:
        logger.warning("EmbeddingClient init failed — semantic search disabled: %s", _emb_init_err)
        embedding_client = None

    # 4. Persistent embedding cache
    try:
        from lib.embed_cache import PersistentEmbedCache
        import sqlite3 as _sqlite3
        _embed_cache_conn = _sqlite3.connect(str(DATA_DIR / "embed_cache.db"), timeout=5.0)
        _embed_cache_conn.execute("PRAGMA journal_mode=WAL")
        _embed_cache_conn.execute("PRAGMA busy_timeout=5000")
        _embed_cache = PersistentEmbedCache(_embed_cache_conn)
        logger.info("PersistentEmbedCache initialized")
    except Exception as _cache_err:
        logger.warning("PersistentEmbedCache not available — embedding cache disabled: %s", _cache_err)
        _embed_cache = None

    # 5. LanceDB vector store
    VECTORS_DIR: Path = DATA_DIR / "knowledge_vectors"
    try:
        VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        import lancedb as _lancedb
        lance_db = _lancedb.connect(str(VECTORS_DIR))
        try:
            vectors_table = lance_db.open_table("knowledge_vectors")
        except Exception:
            vectors_table = None
        logger.info("LanceDB connected — vectors_table=%s", "open" if vectors_table is not None else "missing")
    except ImportError:
        logger.warning("LanceDB not available — semantic search disabled")
        lance_db = None

    # 6. Consolidator
    try:
        from lib.consolidator import consolidate as _consolidate
        logger.info("Consolidator available")
    except Exception as _cons_import_err:
        logger.warning("consolidator module not available — auto-consolidation disabled: %s", _cons_import_err)
        _consolidate = None

    # 7. Auto-index support
    try:
        from lib.harness_config import get_knowledge_config, is_path_under_indexed_root, add_indexed_root  # noqa: F401
        from lib.recursive_indexer import index_directory_tree  # noqa: F401
        _auto_index_available = True
        logger.info("Auto-index subsystem available")
    except Exception:
        logger.warning("recursive_indexer or harness_config not available — auto-index disabled")
        _auto_index_available = False

    if errors:
        _init_error = "; ".join(errors)
        logger.warning("Initialization completed with errors: %s", _init_error)
    else:
        _init_error = None

    _initialized = True
    logger.info("Knowledge server initialization complete (errors=%s)", bool(errors))


# --- Server instance ---
server = Server("cap-knowledge")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


_EXTENSION_TO_CONTENT_TYPE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".tf": "terraform",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".sh": "shell",
    ".bash": "shell",
}


def _detect_content_type(source_path: str) -> str:
    """Auto-detect content type from file extension."""
    ext = Path(source_path).suffix.lower()
    return _EXTENSION_TO_CONTENT_TYPE.get(ext, "text")


def _cache_key(query: str, workspace: str | None, scope: str, strategy: str, top_k: int) -> str:
    raw = json.dumps([query, workspace, scope, strategy, top_k], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> list | None:
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        return None
    ts, results = entry
    if time.monotonic() - ts > _SEARCH_CACHE_TTL:
        del _SEARCH_CACHE[key]
        return None
    return results


def _cache_put(key: str, results: list) -> None:
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX_SIZE:
        oldest_key = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        del _SEARCH_CACHE[oldest_key]
    _SEARCH_CACHE[key] = (time.monotonic(), results)


def _cache_clear() -> None:
    _SEARCH_CACHE.clear()


def _db_required(tool_name: str) -> list | None:
    """Return an error TextContent list if db is not available, else None."""
    if db is None:
        msg = (
            f"Tool '{tool_name}' requires the knowledge database but it is not available. "
            f"Init error: {_init_error or 'unknown'}. "
            "Run 'cap init' to initialise the knowledge base."
        )
        return [TextContent(type="text", text=json.dumps({"error": msg}))]
    return None


def _maybe_auto_index_workspace(workspace: str | None) -> None:
    """Trigger background indexing for workspaces not yet under an indexed root."""
    if not _auto_index_available or not workspace or workspace == "all":
        return
    if workspace in _auto_indexed_this_session:
        return

    try:
        from lib.harness_config import get_knowledge_config, is_path_under_indexed_root, add_indexed_root
        from lib.recursive_indexer import index_directory_tree

        knowledge_cfg = get_knowledge_config()
        if not knowledge_cfg.get("auto_index_new_workspaces", True):
            return
        if is_path_under_indexed_root(workspace):
            return

        logger.info("Auto-detecting new workspace: %s — triggering background indexing", workspace)
        _auto_indexed_this_session.add(workspace)

        recursive_config = {
            "data_dir": str(config.data_dir),
            "extensions": set(knowledge_cfg.get("file_extensions", [])),
            "exclude_dirs": set(knowledge_cfg.get("exclude_patterns", [])),
            "max_file_size_kb": knowledge_cfg.get("max_file_size_kb", 500),
            "batch_size": 100,
            "workspace": workspace,
        }

        stats = index_directory_tree(root=workspace, config=recursive_config)
        add_indexed_root(workspace)
        logger.info(
            "Auto-indexed new workspace %s: %d files, %d repos detected",
            workspace, stats.get("files_indexed", 0), stats.get("repos_detected", 0),
        )
    except Exception as exc:
        logger.warning("Auto-index of workspace %s failed (non-fatal): %s", workspace, exc)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

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
                    "workspace": {
                        "type": "string",
                        "description": "Workspace path to scope search. If omitted or set to 'all', searches across all indexed workspaces.",
                    },
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
                "required": ["source", "workspace"],
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
                    "workspace": {
                        "type": "string",
                        "description": "Workspace path to scope traversal. If omitted or set to 'all', traverses across all indexed workspaces.",
                    },
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="knowledge_graph_add",
            description=(
                "Add a relationship (triple) to the knowledge graph. "
                "Format: subject --predicate--> object. "
                "Examples: ('eks-cluster', 'depends_on', 'vpc'), "
                "('alerting-service', 'owned_by', 'observability-team'), "
                "('terraform-module', 'deploys', 'lambda-function'). "
                "subject_type and object_type classify the node (service, team, resource, concept, repo)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Source entity name (e.g., 'eks-cluster', 'alerting-service')"},
                    "subject_type": {
                        "type": "string",
                        "default": "concept",
                        "description": "Node type for subject: service, team, resource, concept, repo, component",
                    },
                    "predicate": {"type": "string", "description": "Relationship type (e.g., 'depends_on', 'owned_by', 'deploys', 'uses', 'contains')"},
                    "object": {"type": "string", "description": "Target entity name (e.g., 'vpc', 'observability-team')"},
                    "object_type": {
                        "type": "string",
                        "default": "concept",
                        "description": "Node type for object: service, team, resource, concept, repo, component",
                    },
                    "workspace": {"type": "string", "description": "Workspace scope (defaults to CAP_HOME if omitted)"},
                    "metadata": {"type": "object", "description": "Optional metadata dict to attach to the edge"},
                },
                "required": ["subject", "predicate", "object"],
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
                    "repo_name": {
                        "type": "string",
                        "description": "Repository name to resolve (e.g., 'alerting', 'fleet-connector')",
                    },
                    "domain_hint": {
                        "type": "string",
                        "description": "Optional domain/group directory hint (e.g., 'Observability-Alerting')",
                    },
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
                    "workspace": {
                        "type": "string",
                        "description": "Scope to workspace (optional, searches all if omitted)",
                    },
                    "auto_clone": {"type": "boolean", "default": True, "description": "Whether to auto-clone missing repos"},
                    "max_clones": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Max repos to clone in one call",
                    },
                },
            },
        ),
        Tool(
            name="knowledge_health",
            description="Check knowledge server health: DB connectivity, embedding client status, and process uptime.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        _ensure_initialized()

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
        elif name == "knowledge_health":
            return await _handle_health(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def _handle_search(args: dict):
    """Execute a hybrid knowledge search and return ranked results."""
    err = _db_required("knowledge_search")
    if err:
        return err

    global _search_count, _last_consolidation
    _search_count += 1
    if _search_count % 50 == 0 and _consolidate is not None:
        if time.time() - _last_consolidation > 6 * 3600:
            try:
                consolidation_result = _consolidate(db)
                _last_consolidation = time.time()
                logger.info("Auto-consolidation triggered at search #%d: %s", _search_count, consolidation_result)
            except Exception as _cons_err:
                logger.warning("Auto-consolidation failed (non-fatal): %s", _cons_err)

    from lib.retrieval import hybrid_search

    query = args.get("query")
    if not query:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: query"}))]
    workspace = args.get("workspace")
    if workspace == "all":
        workspace = None

    _maybe_auto_index_workspace(workspace)
    top_k = args.get("top_k", 10)
    strategy = args.get("strategy", "hybrid")
    scope = args.get("scope", "all")

    ck = _cache_key(query, workspace, scope, strategy, top_k)
    cached = _cache_get(ck)
    if cached is not None:
        logger.debug("Search cache hit for query=%r", query)
        return cached

    query_vector = None
    if strategy in ("hybrid", "semantic") and embedding_client is not None and embedding_client.is_available is not False:
        if _embed_cache is not None:
            try:
                query_vector = _embed_cache.get(query)
            except Exception:
                pass
        if query_vector is None:
            query_vector = await embedding_client.embed_single(query)
            if query_vector is not None and _embed_cache is not None:
                try:
                    _embed_cache.put(query, query_vector)
                except (ValueError, Exception):
                    pass  # dimension mismatch or other cache error — non-fatal

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
    """Ingest a file or text snippet into the knowledge base."""
    err = _db_required("knowledge_ingest")
    if err:
        return err

    from lib.security import sanitize_content, validate_path

    workspace = args.get("workspace")
    source = args.get("source")
    if not workspace or not source:
        missing = [k for k in ("workspace", "source") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({"error": f"Missing required parameter(s): {', '.join(missing)}"}))]
    source_type = args.get("source_type", "file")
    content_type = args.get("content_type") or _detect_content_type(source)
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

    source_agent = metadata.get("source_agent") if metadata else None
    if source_agent:
        metadata = dict(metadata)
        metadata.setdefault("source_agent", source_agent)

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

    _cache_clear()

    return [TextContent(type="text", text=json.dumps({
        "status": "ingested",
        "entry_id": entry_id,
        "uuid": entry_uuid,
        "title": title,
        "content_type": content_type,
    }))]


async def _handle_record(args: dict):
    """Record business knowledge (team, ownership, convention, etc.)."""
    err = _db_required("knowledge_record")
    if err:
        return err

    from lib.security import sanitize_content
    from lib.graph import add_edge

    workspace = args.get("workspace")
    category = args.get("category")
    key = args.get("key")
    value_raw = args.get("value")
    if not workspace or not category or not key or not value_raw:
        missing = [k for k in ("workspace", "category", "key", "value") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({"error": f"Missing required parameter(s): {', '.join(missing)}"}))]
    value = sanitize_content(value_raw)
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
    """Traverse the knowledge graph from a named entity."""
    err = _db_required("knowledge_graph_query")
    if err:
        return err

    from lib.graph import find_entities, bfs_traverse, get_node_context

    entity = args.get("entity")
    if not entity:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: entity"}))]
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
    """Add a relationship edge to the knowledge graph."""
    err = _db_required("knowledge_graph_add")
    if err:
        return err

    from lib.graph import add_edge
    from cap.config import get_cap_home

    subject = args.get("subject")
    predicate = args.get("predicate")
    obj = args.get("object")

    if not subject or not predicate or not obj:
        missing = [k for k in ("subject", "predicate", "object") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({
            "error": f"Missing required parameter(s) for knowledge_graph_add: {', '.join(missing)}",
        }))]

    workspace = args.get("workspace") or str(get_cap_home())

    add_edge(
        db,
        source_name=subject,
        source_type=args.get("subject_type", "concept"),
        target_name=obj,
        target_type=args.get("object_type", "concept"),
        predicate=predicate,
        workspace=workspace,
        metadata=args.get("metadata"),
    )
    db.commit()

    return [TextContent(type="text", text=json.dumps({
        "status": "added",
        "edge": f"{subject} --{predicate}--> {obj}",
    }))]


async def _handle_sync(args: dict):
    """Trigger a workspace knowledge sync."""
    err = _db_required("knowledge_sync")
    if err:
        return err

    from lib.sync_engine import sync_workspace

    workspace = args.get("workspace")
    if not workspace:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: workspace"}))]
    trigger = args.get("trigger", "manual")
    full = args.get("full", False)

    stats = sync_workspace(db, workspace, full=full)

    _cache_clear()

    embeddings_processed = 0
    if stats.embeddings_queued and stats.embeddings_queued > 0 and embedding_client is not None:
        try:
            pending = db.execute(
                """SELECT eq.id, eq.entry_id, ke.content, ke.uuid, eq.attempts
                   FROM embedding_queue eq
                   JOIN knowledge_entries ke ON ke.id = eq.entry_id
                   WHERE eq.status = 'pending' AND eq.attempts < eq.max_attempts
                   ORDER BY eq.created_at LIMIT 25"""
            ).fetchall()

            if pending:
                texts = [row[2][:config.bedrock.embedding_max_input_tokens * 4] for row in pending]
                vectors = await embedding_client.embed_batch(texts)

                for row, vector in zip(pending, vectors):
                    eq_id, entry_id, _, entry_uuid, attempts = row
                    if vector is not None:
                        if vectors_table is not None:
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
                        db.execute(
                            "UPDATE embedding_queue SET status = 'done', processed_at = ? WHERE id = ?",
                            (_now(), eq_id)
                        )
                        db.execute(
                            "UPDATE knowledge_entries SET embedding_status = 'embedded' WHERE id = ?",
                            (entry_id,)
                        )
                        embeddings_processed += 1
                    else:
                        new_attempts = attempts + 1
                        status = "failed" if new_attempts >= 3 else "pending"
                        db.execute(
                            "UPDATE embedding_queue SET attempts = ?, status = ? WHERE id = ?",
                            (new_attempts, status, eq_id)
                        )

                db.commit()
                logger.info("Post-sync embedding: processed %d/%d items", embeddings_processed, len(pending))
        except Exception as e:
            logger.warning("Post-sync embedding failed (non-fatal, queue retained): %s", e)

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
        "embeddings_processed": embeddings_processed,
        "errors": stats.errors[:5] if stats.errors else [],
    }))]


async def _handle_status(args: dict):
    """Return knowledge base health metrics."""
    err = _db_required("knowledge_status")
    if err:
        return err

    workspace = args.get("workspace")

    where = "WHERE workspace = ?" if workspace else ""
    params = (workspace,) if workspace else ()

    total = db.execute(f"SELECT COUNT(*) FROM knowledge_entries {where}", params).fetchone()[0]
    embedded = db.execute(
        f"SELECT COUNT(*) FROM knowledge_entries {where} {'AND' if workspace else 'WHERE'} embedding_status = 'embedded'",
        params
    ).fetchone()[0]
    pending = db.execute("SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'").fetchone()[0]
    failed = db.execute("SELECT COUNT(*) FROM embedding_queue WHERE status = 'failed'").fetchone()[0]

    graph_nodes = db.execute(f"SELECT COUNT(*) FROM knowledge_graph_nodes {where}", params).fetchone()[0]
    graph_edges = db.execute(f"SELECT COUNT(*) FROM knowledge_graph_edges {where}", params).fetchone()[0]

    bk_count = db.execute(f"SELECT COUNT(*) FROM business_knowledge {where}", params).fetchone()[0]

    if embedding_client is None:
        embedder_health = "degraded"
    elif embedding_client.is_available is False:
        embedder_health = "degraded"
    else:
        embedder_health = "available"

    search_path = (
        "hybrid"
        if (embedding_client is not None and embedding_client.is_available is not False and vectors_table is not None)
        else "keyword_only"
    )

    last_consolidation_iso = (
        datetime.fromtimestamp(_last_consolidation, tz=timezone.utc).isoformat()
        if _last_consolidation > 0.0 else None
    )

    return [TextContent(type="text", text=json.dumps({
        "total_entries": total,
        "embedded": embedded,
        "embedding_coverage_pct": round(embedded / max(total, 1) * 100, 1),
        "embedding_queue_pending": pending,
        "embedding_queue_failed": failed,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "business_knowledge_entries": bk_count,
        "semantic_search_available": embedding_client is not None and embedding_client.is_available is not False,
        "lancedb_available": vectors_table is not None,
        "last_consolidation": last_consolidation_iso,
        "embedder_health": embedder_health,
        "search_path": search_path,
    }))]


async def _handle_resolve_repo(args: dict):
    """Resolve a dependent repo, cloning it from GitHub if necessary."""
    err = _db_required("knowledge_resolve_repo")
    if err:
        return err

    from lib.repo_resolver import resolve_repo

    repo_name = args.get("repo_name")
    if not repo_name:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: repo_name"}))]
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
    """Find and resolve all unresolved repo dependencies."""
    err = _db_required("knowledge_resolve_deps")
    if err:
        return err

    from lib.repo_resolver import find_unresolved_dependencies, resolve_multiple

    workspace = args.get("workspace")
    auto_clone = args.get("auto_clone", True)
    max_clones = min(max(1, args.get("max_clones", 5)), 20)

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


async def _handle_health(args: dict):  # noqa: ARG001
    """Return server health: DB connectivity, embedding status, uptime."""
    uptime_s = round(time.monotonic() - _start_time, 1)

    db_ok = False
    db_detail = "not initialized"
    if db is not None:
        try:
            db.execute("SELECT 1").fetchone()
            db_ok = True
            db_detail = "ok"
        except Exception as _db_ping_err:
            db_detail = str(_db_ping_err)

    embedding_status = "disabled"
    if embedding_client is not None:
        if embedding_client.is_available is False:
            embedding_status = "unavailable"
        else:
            embedding_status = "available"

    return [TextContent(type="text", text=json.dumps({
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": uptime_s,
        "db_ok": db_ok,
        "db_detail": db_detail,
        "init_error": _init_error,
        "embedding_client": embedding_status,
        "embed_cache": "available" if _embed_cache is not None else "disabled",
        "lancedb": "available" if vectors_table is not None else "disabled",
        "consolidator": "available" if _consolidate is not None else "disabled",
        "auto_index": _auto_index_available,
        "search_cache_entries": len(_SEARCH_CACHE),
    }))]


# ---------------------------------------------------------------------------
# Background embedding queue processor
# ---------------------------------------------------------------------------

async def _process_embedding_queue():
    """Background task: process pending embeddings every 10–30 seconds."""
    while True:
        try:
            if db is None or embedding_client is None:
                await asyncio.sleep(30)
                continue

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

            vectors: list = []
            cache_misses_indices: list[int] = []
            for i, text in enumerate(texts):
                cached_vec = None
                if _embed_cache is not None:
                    try:
                        cached_vec = _embed_cache.get(text)
                    except Exception:
                        pass
                vectors.append(cached_vec)
                if cached_vec is None:
                    cache_misses_indices.append(i)

            if cache_misses_indices:
                miss_texts = [texts[i] for i in cache_misses_indices]
                fresh_vectors = await embedding_client.embed_batch(miss_texts)
                for list_pos, orig_idx in enumerate(cache_misses_indices):
                    vec = fresh_vectors[list_pos]
                    vectors[orig_idx] = vec
                    if vec is not None and _embed_cache is not None:
                        try:
                            _embed_cache.put(texts[orig_idx], vec)
                        except (ValueError, Exception):
                            pass  # dimension mismatch — non-fatal

            for row, vector in zip(pending, vectors):
                eq_id, entry_id, _, entry_uuid, attempts = row
                if vector is not None:
                    already_embedded = db.execute(
                        "SELECT 1 FROM knowledge_entries WHERE id = ? AND embedding_status = 'embedded'",
                        (entry_id,)
                    ).fetchone()
                    if already_embedded:
                        db.execute("UPDATE embedding_queue SET status = 'done', processed_at = ? WHERE id = ?", (_now(), eq_id))
                        continue

                    if vectors_table is not None:
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _async_main():
    """Start the MCP stdio server and background embedding processor."""
    embedding_task = asyncio.create_task(_process_embedding_queue())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        embedding_task.cancel()


def main():
    """Entry point for the cap-knowledge-server console script."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

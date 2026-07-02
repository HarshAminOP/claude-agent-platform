#!/usr/bin/env python3
"""Session Server MCP — cross-session memory and adaptive learning.

Owner of sessions.db.
Provides: start, checkpoint, record, recall, end, feedback, history tools.

Data directory is resolved from the ``CAP_HOME`` environment variable at
start-up time (not at import time) so that the entry-point binary honours
the value set by ``cap init``.  Fallback: ``~/.claude-platform``.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from cap.lib.security import sanitize_content
from cap.lib.repo_resolver import reset_session_counter

logger = logging.getLogger("cap.session")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Module-level globals — populated by _ensure_initialized() on first tool call
# ---------------------------------------------------------------------------

_initialized: bool = False
_init_error: str | None = None

config = None
db = None
server = Server("cap-session")


def _cap_data_dir() -> Path:
    """Resolve the data directory from CAP_HOME env var at call time."""
    from cap.config import get_data_dir
    return get_data_dir()


def _ensure_initialized() -> None:
    """Lazily load config and open sessions.db on first use.

    Deferring import and DB open to here (rather than module level) means
    the CAP_HOME environment variable that ``claude mcp add ... -e CAP_HOME=...``
    injects is already present when we resolve the data directory.
    """
    global _initialized, _init_error, config, db

    if _initialized:
        return

    try:
        from cap.lib.config import load_config
        config = load_config()
        data_dir = config.data_dir
    except Exception as cfg_err:
        logger.warning(
            "_ensure_initialized: load_config() failed (%s); falling back to CAP_HOME", cfg_err
        )
        data_dir = _cap_data_dir()

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        from cap.lib.db_init import init_sessions_db
        db = init_sessions_db(data_dir)
        logger.info("Session DB opened at %s", data_dir)
    except Exception as db_err:
        _init_error = str(db_err)
        logger.error("init_sessions_db() failed: %s", db_err)

    _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_or_create_session(workspace: str) -> str:
    """Return the active session id for workspace (last 4 hours), or create one.

    This is the core of auto-session: any tool that needs a session but wasn't
    given one will call this instead of failing.
    """
    row = db.execute(
        """SELECT id FROM sessions
           WHERE workspace = ?
             AND status != 'ended'
             AND started_at >= datetime('now', '-4 hours')
           ORDER BY started_at DESC LIMIT 1""",
        (workspace,)
    ).fetchone()

    if row:
        logger.debug("Auto-session: reusing existing session %s for workspace=%s", row[0], workspace)
        return row[0]

    session_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO sessions (id, workspace, started_at) VALUES (?, ?, ?)",
        (session_id, workspace, now)
    )
    db.commit()
    logger.info("Auto-session: created new session %s for workspace=%s", session_id, workspace)
    return session_id


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="session_start",
            description="Start a new session. Loads relevant corrections, learnings, and recent decisions for the workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "description": "Current workspace path"},
                    "context": {"type": "object", "description": "Optional initial context"},
                },
                "required": ["workspace"],
            },
        ),
        Tool(
            name="session_checkpoint",
            description="Save mid-session progress for crash recovery. Record decisions and learnings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "domain": {"type": "string"},
                                "decision": {"type": "string"},
                                "rationale": {"type": "string"},
                            },
                            "required": ["domain", "decision"],
                        },
                    },
                    "learnings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["category", "key", "value"],
                        },
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="session_record",
            description=(
                "Record a session event (correction, preference, discovery, decision, error). "
                "session_id is optional — if omitted, an active session for the workspace is "
                "reused or a new one is auto-created."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Optional — auto-created if absent"},
                    "workspace": {"type": "string", "description": "Required when session_id is not provided"},
                    "event_type": {
                        "type": "string",
                        "enum": ["decision", "correction", "preference", "discovery", "error", "milestone"],
                    },
                    "category": {"type": "string", "description": "Domain category"},
                    "content": {"type": "string", "description": "Human-readable description"},
                    "data": {"type": "object", "description": "Structured data"},
                },
                "required": ["event_type", "content"],
            },
        ),
        Tool(
            name="session_recall",
            description="Recall relevant past decisions and learnings by searching session memory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to recall"},
                    "workspace": {"type": "string"},
                    "session_id": {"type": "string", "description": "Current session (for context)"},
                    "recency_weight": {"type": "number", "default": 0.7, "minimum": 0, "maximum": 1},
                },
                "required": ["query", "workspace"],
            },
        ),
        Tool(
            name="session_end",
            description="Close a session. Persist final summary and learnings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "summary": {"type": "string", "description": "Session summary"},
                    "learnings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="session_feedback",
            description=(
                "Record a user correction or preference (highest-priority learning). "
                "session_id is optional — if omitted, an active session for the workspace is "
                "reused or a new one is auto-created."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Optional — auto-created if absent"},
                    "category": {"type": "string", "enum": ["factual", "style", "process", "technical"]},
                    "what_was_wrong": {"type": "string"},
                    "what_is_correct": {"type": "string"},
                    "workspace": {"type": "string", "description": "Used for auto-session when session_id is absent"},
                },
                "required": ["what_was_wrong", "what_is_correct"],
            },
        ),
        Tool(
            name="session_history",
            description="List past sessions with summaries.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    _ensure_initialized()
    try:
        if name == "session_start":
            return await _handle_start(arguments)
        elif name == "session_checkpoint":
            return await _handle_checkpoint(arguments)
        elif name == "session_record":
            return await _handle_record(arguments)
        elif name == "session_recall":
            return await _handle_recall(arguments)
        elif name == "session_end":
            return await _handle_end(arguments)
        elif name == "session_feedback":
            return await _handle_feedback(arguments)
        elif name == "session_history":
            return await _handle_history(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_start(args: dict):
    workspace = args.get("workspace")
    if not workspace:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: workspace"}))]
    context = args.get("context")
    now = _now()

    # Idempotent: if an active session exists within the last 4 hours, reuse it
    existing = db.execute(
        """SELECT id FROM sessions
           WHERE workspace = ?
             AND status != 'ended'
             AND started_at >= datetime('now', '-4 hours')
           ORDER BY started_at DESC LIMIT 1""",
        (workspace,)
    ).fetchone()

    if existing:
        session_id = existing[0]
        is_new = False
        logger.info("Session start (reused existing): %s workspace=%s", session_id, workspace)
    else:
        session_id = str(uuid.uuid4())
        is_new = True
        db.execute(
            "INSERT INTO sessions (id, workspace, started_at, context) VALUES (?, ?, ?, ?)",
            (session_id, workspace, now, json.dumps(context) if context else None)
        )
        db.commit()
        reset_session_counter()
        logger.info("Session started (new): %s workspace=%s", session_id, workspace)

    corrections = db.execute(
        """SELECT what_was_wrong, what_is_correct, category
           FROM corrections
           WHERE workspace = ? OR workspace IS NULL
           ORDER BY created_at DESC LIMIT ?""",
        (workspace, config.session.max_corrections_loaded)
    ).fetchall()

    learnings = db.execute(
        """SELECT category, key, value FROM learnings
           WHERE (workspace = ? OR workspace IS NULL) AND confidence > 0.5
           ORDER BY confidence DESC, last_applied_at DESC LIMIT ?""",
        (workspace, config.session.max_learnings_loaded)
    ).fetchall()

    decisions = db.execute(
        """SELECT domain, decision, rationale FROM decisions
           WHERE workspace = ? AND (outcome IS NULL OR outcome != 'superseded')
           ORDER BY created_at DESC LIMIT ?""",
        (workspace, config.session.max_decisions_loaded)
    ).fetchall()

    # Auto-register new workspaces in the workspace registry so the daemon
    # picks them up for periodic sync.  This is non-blocking and fails silently.
    if is_new:
        try:
            from cap.lib.workspace_registry import add_workspace, get_workspace
            if get_workspace(workspace) is None:
                add_workspace(workspace, auto_added=True)
                logger.info("session_start: auto-registered workspace %s", workspace)
        except Exception as _reg_exc:
            logger.warning(
                "session_start: workspace auto-registration failed (non-fatal): %s",
                _reg_exc,
            )

    # Trigger sync on session start so agents never work with stale data.
    # Only run for new sessions to avoid duplicate work on session reuse.
    sync_result = {}
    if is_new:
        try:
            from cap.sync.engine import SyncEngine
            sync_result = SyncEngine(workspace, db).on_session_start(workspace)
            logger.info(
                "Session sync complete: fetched=%s behind=%d reindexed=%d",
                sync_result.get("fetched"),
                sync_result.get("behind_count", 0),
                sync_result.get("files_reindexed", 0),
            )
        except Exception as e:
            logger.warning("Session sync failed (non-fatal): %s", e)

    return [TextContent(type="text", text=json.dumps({
        "session_id": session_id,
        "is_new": is_new,
        "corrections": [{"what_was_wrong": c[0], "what_is_correct": c[1], "category": c[2]} for c in corrections],
        "learnings": [{"category": l[0], "key": l[1], "value": l[2]} for l in learnings],
        "active_decisions": [{"domain": d[0], "decision": d[1], "rationale": d[2]} for d in decisions],
        "loaded_counts": {
            "corrections": len(corrections),
            "learnings": len(learnings),
            "decisions": len(decisions),
        },
        "sync": sync_result,
    }))]


async def _handle_checkpoint(args: dict):
    session_id = args.get("session_id")
    if not session_id:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: session_id"}))]
    decisions = args.get("decisions", [])
    learnings = args.get("learnings", [])

    workspace = db.execute("SELECT workspace FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not workspace:
        return [TextContent(type="text", text=json.dumps({"error": "Session not found"}))]
    workspace = workspace[0]

    state = {"decisions": decisions, "learnings": learnings}
    db.execute(
        "INSERT INTO checkpoints (session_id, state) VALUES (?, ?)",
        (session_id, json.dumps(state))
    )

    for d in decisions:
        db.execute(
            """INSERT OR IGNORE INTO decisions (id, session_id, workspace, domain, decision, rationale)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_id, workspace, d["domain"], d["decision"], d.get("rationale"))
        )

    for l in learnings:
        db.execute(
            """INSERT INTO learnings (id, workspace, category, key, value, source_session_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace, category, key) DO UPDATE SET
                   confidence = MIN(confidence + 0.1, 1.0),
                   times_reinforced = times_reinforced + 1,
                   last_applied_at = datetime('now')""",
            (str(uuid.uuid4()), l.get("workspace", workspace), l["category"], l["key"], l["value"], session_id)
        )

    db.commit()
    return [TextContent(type="text", text=json.dumps({"status": "checkpointed", "decisions": len(decisions), "learnings": len(learnings)}))]


async def _handle_record(args: dict):
    session_id = args.get("session_id")
    event_type = args.get("event_type")
    content_raw = args.get("content")
    if not event_type or not content_raw:
        missing = [k for k in ("event_type", "content") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({"error": f"Missing required parameter(s): {', '.join(missing)}"}))]
    content = sanitize_content(content_raw)
    category = args.get("category")
    data = args.get("data")

    # Auto-session: if no session_id provided, find or create one for the workspace
    if not session_id:
        workspace = args.get("workspace", "default")
        session_id = _get_or_create_session(workspace)

    db.execute(
        """INSERT INTO session_events (session_id, event_type, category, content, data)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, event_type, category, content, json.dumps(data) if data else None)
    )
    db.commit()

    return [TextContent(type="text", text=json.dumps({"status": "recorded", "session_id": session_id, "event_type": event_type}))]


async def _handle_recall(args: dict):
    query = args.get("query")
    workspace = args.get("workspace")
    if not query or not workspace:
        missing = [k for k in ("query", "workspace") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({"error": f"Missing required parameter(s): {', '.join(missing)}"}))]

    # Sanitize for FTS5 — hyphens between words become spaces, remove special chars,
    # then join with OR for broader recall (implicit AND requires ALL terms present)
    import re
    fts_query = re.sub(r'(\w)-(\w)', r'\1 \2', query)
    fts_query = re.sub(r'[{}()\[\]^~*]', ' ', fts_query)
    terms = [t for t in fts_query.split() if t]
    if len(terms) > 1:
        fts_query = ' OR '.join(terms)
    elif terms:
        fts_query = terms[0]
    else:
        fts_query = query

    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_pattern = f"%{escaped_query}%"

    # FTS5 search on decisions
    try:
        fts_results = db.execute(
            """SELECT d.id, d.domain, d.decision, d.rationale, d.created_at
               FROM decisions d
               JOIN decisions_fts df ON d.rowid = df.rowid
               WHERE decisions_fts MATCH ? AND d.workspace = ?
               ORDER BY rank LIMIT 10""",
            (fts_query, workspace)
        ).fetchall()
    except Exception:
        fts_results = db.execute(
            """SELECT id, domain, decision, rationale, created_at
               FROM decisions WHERE workspace = ? AND decision LIKE ? ESCAPE '\\'
               ORDER BY created_at DESC LIMIT 10""",
            (workspace, like_pattern)
        ).fetchall()

    # Also search learnings
    learning_results = db.execute(
        """SELECT category, key, value, confidence
           FROM learnings
           WHERE (workspace = ? OR workspace IS NULL) AND (key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')
           ORDER BY confidence DESC LIMIT 10""",
        (workspace, like_pattern, like_pattern)
    ).fetchall()

    # Search corrections
    correction_results = db.execute(
        """SELECT what_was_wrong, what_is_correct, category
           FROM corrections
           WHERE (workspace = ? OR workspace IS NULL) AND (what_was_wrong LIKE ? ESCAPE '\\' OR what_is_correct LIKE ? ESCAPE '\\')
           ORDER BY created_at DESC LIMIT 5""",
        (workspace, like_pattern, like_pattern)
    ).fetchall()

    # Search session_events table (where session_record writes)
    # Split query into terms and match any term against content or event_type
    query_terms = [t for t in query.split() if t]
    if query_terms:
        term_clauses = " OR ".join(
            ["(content LIKE '%' || ? || '%' OR event_type LIKE '%' || ? || '%')"] * len(query_terms)
        )
        term_params = []
        for t in query_terms:
            term_params.extend([t, t])
        event_results = db.execute(
            f"""SELECT * FROM session_events
               WHERE {term_clauses}
               ORDER BY created_at DESC LIMIT 10""",
            term_params
        ).fetchall()
    else:
        event_results = []

    # Get column names for session_events to build dicts
    event_columns = [desc[0] for desc in db.execute("SELECT * FROM session_events LIMIT 0").description]

    return [TextContent(type="text", text=json.dumps({
        "decisions": [
            {"id": r[0], "domain": r[1], "decision": r[2], "rationale": r[3], "created_at": r[4]}
            for r in fts_results
        ],
        "learnings": [
            {"category": r[0], "key": r[1], "value": r[2], "confidence": r[3]}
            for r in learning_results
        ],
        "corrections": [
            {"what_was_wrong": r[0], "what_is_correct": r[1], "category": r[2]}
            for r in correction_results
        ],
        "events": [
            dict(zip(event_columns, r))
            for r in event_results
        ],
    }))]


async def _handle_end(args: dict):
    session_id = args.get("session_id")
    if not session_id:
        return [TextContent(type="text", text=json.dumps({"error": "Missing required parameter: session_id"}))]
    summary = args.get("summary")
    learnings = args.get("learnings", [])
    now = _now()

    workspace = db.execute("SELECT workspace FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not workspace:
        return [TextContent(type="text", text=json.dumps({"error": "Session not found"}))]
    workspace = workspace[0]

    db.execute(
        "UPDATE sessions SET ended_at = ?, status = 'ended', summary = ? WHERE id = ?",
        (now, summary, session_id)
    )

    for l in learnings:
        db.execute(
            """INSERT INTO learnings (id, workspace, category, key, value, source_session_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace, category, key) DO UPDATE SET
                   confidence = MIN(confidence + 0.1, 1.0),
                   times_reinforced = times_reinforced + 1,
                   last_applied_at = datetime('now')""",
            (str(uuid.uuid4()), l.get("workspace", workspace), l["category"], l["key"], l["value"], session_id)
        )

    db.commit()
    logger.info("Session ended: %s", session_id)

    return [TextContent(type="text", text=json.dumps({"status": "ended", "session_id": session_id}))]


async def _handle_feedback(args: dict):
    session_id = args.get("session_id")
    raw_wrong = args.get("what_was_wrong")
    raw_correct = args.get("what_is_correct")
    if not raw_wrong or not raw_correct:
        missing = [k for k in ("what_was_wrong", "what_is_correct") if not args.get(k)]
        return [TextContent(type="text", text=json.dumps({"error": f"Missing required parameter(s): {', '.join(missing)}"}))]
    what_was_wrong = sanitize_content(raw_wrong)
    what_is_correct = sanitize_content(raw_correct)
    category = args.get("category")
    workspace = args.get("workspace")

    # Resolve workspace: explicit arg > session lookup > auto-create
    if not workspace and session_id:
        row = db.execute("SELECT workspace FROM sessions WHERE id = ?", (session_id,)).fetchone()
        workspace = row[0] if row else None

    if not session_id:
        # Auto-session: resolve workspace first (fall back to "default"), then get/create session
        effective_workspace = workspace or "default"
        session_id = _get_or_create_session(effective_workspace)
        if not workspace:
            workspace = effective_workspace

    db.execute(
        """INSERT INTO corrections (session_id, workspace, what_was_wrong, what_is_correct, category)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, workspace, what_was_wrong, what_is_correct, category)
    )
    db.commit()

    logger.info("Correction recorded: session=%s category=%s", session_id, category)
    return [TextContent(type="text", text=json.dumps({"status": "correction_recorded", "session_id": session_id, "category": category}))]


async def _handle_history(args: dict):
    workspace = args.get("workspace")
    limit = args.get("limit", 20)

    if workspace:
        rows = db.execute(
            """SELECT id, workspace, started_at, ended_at, status, summary
               FROM sessions WHERE workspace = ? ORDER BY started_at DESC LIMIT ?""",
            (workspace, limit)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT id, workspace, started_at, ended_at, status, summary
               FROM sessions ORDER BY started_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()

    return [TextContent(type="text", text=json.dumps({
        "sessions": [
            {"id": r[0], "workspace": r[1], "started_at": r[2], "ended_at": r[3], "status": r[4], "summary": r[5]}
            for r in rows
        ],
        "count": len(rows),
    }))]


async def _async_main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for the cap-session-server console script."""
    import asyncio
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

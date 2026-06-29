#!/usr/bin/env python3
"""Session Server MCP — cross-session memory and adaptive learning.

Owner of sessions.db.
Provides: start, checkpoint, record, recall, end, feedback, history tools.

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

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config
from lib.db_init import init_sessions_db
from lib.security import sanitize_content
from lib.repo_resolver import reset_session_counter

logger = logging.getLogger("cap.session")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = load_config()
DATA_DIR = config.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = init_sessions_db(DATA_DIR)
server = Server("cap-session")


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
    workspace = args["workspace"]
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
    }))]


async def _handle_checkpoint(args: dict):
    session_id = args["session_id"]
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
    event_type = args["event_type"]
    content = sanitize_content(args["content"])
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
    query = args["query"]
    workspace = args["workspace"]

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
    }))]


async def _handle_end(args: dict):
    session_id = args["session_id"]
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
    what_was_wrong = sanitize_content(args["what_was_wrong"])
    what_is_correct = sanitize_content(args["what_is_correct"])
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


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

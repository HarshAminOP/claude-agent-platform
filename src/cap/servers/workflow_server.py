#!/usr/bin/env python3
"""Workflow Engine MCP Server.

Provides workflow lifecycle management: start, status, kill, list, estimate, report.
Enforces budget caps and provides progress visibility.

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
from lib.models import (
    ModelTier,
    init_database,
)
from lib.api_gateway import APIGateway, ConcurrencyConfig

logger = logging.getLogger("platform.workflow")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DATA_DIR = Path(os.environ.get("PLATFORM_DATA_DIR", str(Path.home() / ".claude-platform" / "data")))
DB_PATH = DATA_DIR / "platform.db"

db = init_database(DB_PATH)
gateway = APIGateway(DB_PATH, ConcurrencyConfig())

server = Server("workflow-engine")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="workflow_start",
            description=(
                "Start a new workflow with budget controls. Returns workflow_id for tracking. "
                "Set budget_tokens (default 500K) and max_agents (default 15) to control cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workflow name/description"},
                    "budget_tokens": {
                        "type": "integer", "default": 500000,
                        "description": "Max tokens this workflow can consume. Hard limit."
                    },
                    "max_agents": {
                        "type": "integer", "default": 15,
                        "description": "Max agents this workflow can spawn."
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata (trigger, workspace, etc.)"
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="workflow_status",
            description=(
                "Get real-time status of a workflow: phase, tokens spent, agents active, "
                "budget remaining, recent events. Use for progress visibility."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="workflow_signal",
            description=(
                "Signal a workflow event: phase transition, agent completion, failure. "
                "Used by orchestrator to report progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": [
                            "phase_start", "phase_end",
                            "agent_start", "agent_end", "agent_fail",
                            "agent_message", "agent_concern", "agent_handoff", "agent_acknowledge",
                            "workflow_complete", "error",
                        ],
                    },
                    "phase": {"type": "string", "description": "Current phase name"},
                    "agent_id": {"type": "string", "description": "Agent identifier"},
                    "message": {"type": "string", "description": "Event details"},
                    "tokens_delta": {"type": "integer", "description": "Tokens consumed in this event"},
                },
                "required": ["workflow_id", "event_type"],
            },
        ),
        Tool(
            name="workflow_kill",
            description=(
                "Immediately kill a workflow. All new API calls for this workflow will be rejected. "
                "Running agents complete their current generation but get no more tokens."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "reason": {"type": "string", "default": "User requested kill"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="workflow_list",
            description="List active and recent workflows with summary stats.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["running", "completed", "failed", "killed", "all"],
                        "default": "all",
                    },
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
            },
        ),
        Tool(
            name="workflow_estimate",
            description=(
                "Estimate cost for a workflow before launching. Based on agent count, "
                "model mix, and historical averages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_count": {"type": "integer", "description": "Planned number of agents"},
                    "model_mix": {
                        "type": "object",
                        "description": "Model distribution, e.g. {\"opus\": 2, \"sonnet\": 5, \"haiku\": 3}",
                    },
                    "avg_tokens_per_agent": {
                        "type": "integer", "default": 50000,
                        "description": "Estimated tokens per agent (default 50K)"
                    },
                },
                "required": ["agent_count"],
            },
        ),
        Tool(
            name="workflow_report",
            description="Generate post-run report: duration, cost breakdown by model, agent outcomes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "workflow_start":
            return await _handle_start(arguments)
        elif name == "workflow_status":
            return await _handle_status(arguments)
        elif name == "workflow_signal":
            return await _handle_signal(arguments)
        elif name == "workflow_kill":
            return await _handle_kill(arguments)
        elif name == "workflow_list":
            return await _handle_list(arguments)
        elif name == "workflow_estimate":
            return await _handle_estimate(arguments)
        elif name == "workflow_report":
            return await _handle_report(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_start(args: dict):
    wf_id = _generate_id()
    budget = args.get("budget_tokens", 500_000)
    max_agents = args.get("max_agents", 15)
    name = args["name"]
    metadata = json.dumps(args.get("metadata", {}))
    now = _now()

    db.execute(
        """INSERT INTO workflows (id, name, status, budget_tokens, max_agents, started_at, metadata)
           VALUES (?, ?, 'running', ?, ?, ?, ?)""",
        (wf_id, name, budget, max_agents, now, metadata)
    )
    db.commit()

    logger.info("Workflow started: %s (%s) budget=%d max_agents=%d", wf_id, name, budget, max_agents)

    return [TextContent(type="text", text=json.dumps({
        "workflow_id": wf_id,
        "name": name,
        "status": "running",
        "budget_tokens": budget,
        "max_agents": max_agents,
        "started_at": now,
        "pool_status": gateway.pool_status(),
    }))]


async def _handle_status(args: dict):
    wf_id = args["workflow_id"]

    row = db.execute(
        """SELECT id, name, status, budget_tokens, max_agents, tokens_used,
                  agents_spawned, killed, started_at, completed_at, error
           FROM workflows WHERE id = ?""",
        (wf_id,)
    ).fetchone()

    if not row:
        return [TextContent(type="text", text=json.dumps({"error": f"Workflow {wf_id} not found"}))]

    wf = dict(zip(
        ["id", "name", "status", "budget_tokens", "max_agents", "tokens_used",
         "agents_spawned", "killed", "started_at", "completed_at", "error"],
        row
    ))

    # Recent events
    events = db.execute(
        """SELECT event_type, phase, agent_id, message, tokens_delta, timestamp
           FROM workflow_events WHERE workflow_id = ? ORDER BY timestamp DESC LIMIT 10""",
        (wf_id,)
    ).fetchall()

    wf["recent_events"] = [
        {"event_type": e[0], "phase": e[1], "agent_id": e[2],
         "message": e[3], "tokens_delta": e[4], "timestamp": e[5]}
        for e in events
    ]
    wf["budget_remaining"] = wf["budget_tokens"] - wf["tokens_used"]
    wf["budget_pct_used"] = round(wf["tokens_used"] / max(wf["budget_tokens"], 1) * 100, 1)
    wf["pool_status"] = gateway.pool_status()

    # Usage breakdown
    wf["usage_by_model"] = gateway.get_usage_by_model(wf_id)

    return [TextContent(type="text", text=json.dumps(wf))]


async def _handle_signal(args: dict):
    wf_id = args["workflow_id"]
    event_type = args["event_type"]
    now = _now()

    # Check workflow exists and isn't killed
    row = db.execute("SELECT killed, status FROM workflows WHERE id = ?", (wf_id,)).fetchone()
    if not row:
        return [TextContent(type="text", text=json.dumps({"error": "Workflow not found"}))]
    if row[0] or row[1] in ("killed", "failed"):
        return [TextContent(type="text", text=json.dumps({"error": "Workflow is dead", "killed": True}))]

    # Record event
    db.execute(
        """INSERT INTO workflow_events (workflow_id, event_type, agent_id, phase, message, tokens_delta, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (wf_id, event_type, args.get("agent_id"), args.get("phase"),
         args.get("message"), args.get("tokens_delta", 0), now)
    )

    # Update agent count if new agent started
    if event_type == "agent_start":
        db.execute("UPDATE workflows SET agents_spawned = agents_spawned + 1 WHERE id = ?", (wf_id,))
        # Check max_agents
        row2 = db.execute("SELECT agents_spawned, max_agents FROM workflows WHERE id = ?", (wf_id,)).fetchone()
        if row2 and row2[0] > row2[1]:
            logger.warning("Workflow %s exceeded max_agents (%d > %d)", wf_id, row2[0], row2[1])

    # Update tokens if delta provided
    if args.get("tokens_delta", 0) > 0:
        db.execute(
            "UPDATE workflows SET tokens_used = tokens_used + ? WHERE id = ?",
            (args["tokens_delta"], wf_id)
        )

        # Check budget enforcement
        budget_row = db.execute(
            "SELECT tokens_used, budget_tokens FROM workflows WHERE id = ?", (wf_id,)
        ).fetchone()
        if budget_row and budget_row[0] >= budget_row[1]:
            db.execute(
                "UPDATE workflows SET killed = 1, status = 'killed', completed_at = ?, error = ? WHERE id = ?",
                (now, f"Budget exhausted: {budget_row[0]}/{budget_row[1]} tokens", wf_id)
            )
            db.execute(
                """INSERT INTO workflow_events (workflow_id, event_type, message, timestamp)
                   VALUES (?, 'killed', ?, ?)""",
                (wf_id, f"Auto-killed: budget exhausted ({budget_row[0]}/{budget_row[1]} tokens)", now)
            )
            db.commit()
            logger.warning("Workflow %s auto-killed: budget exhausted %d/%d", wf_id, budget_row[0], budget_row[1])
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "event_type": event_type,
                "budget_exceeded": True, "killed": True,
                "tokens_used": budget_row[0], "budget_tokens": budget_row[1],
            }))]

    db.commit()
    return [TextContent(type="text", text=json.dumps({"ok": True, "event_type": event_type}))]


async def _handle_kill(args: dict):
    wf_id = args["workflow_id"]
    reason = args.get("reason", "User requested kill")
    now = _now()

    db.execute(
        "UPDATE workflows SET killed = 1, status = 'killed', completed_at = ?, error = ? WHERE id = ?",
        (now, reason, wf_id)
    )
    db.execute(
        """INSERT INTO workflow_events (workflow_id, event_type, message, timestamp)
           VALUES (?, 'killed', ?, ?)""",
        (wf_id, reason, now)
    )
    db.commit()

    logger.warning("Workflow KILLED: %s — %s", wf_id, reason)
    return [TextContent(type="text", text=json.dumps({"killed": True, "workflow_id": wf_id, "reason": reason}))]


async def _handle_list(args: dict):
    status_filter = args.get("status_filter", "all")
    limit = args.get("limit", 10)

    if status_filter == "all":
        rows = db.execute(
            """SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, started_at, completed_at
               FROM workflows ORDER BY started_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, started_at, completed_at
               FROM workflows WHERE status = ? ORDER BY started_at DESC LIMIT ?""",
            (status_filter, limit)
        ).fetchall()

    workflows = [
        {
            "id": r[0], "name": r[1], "status": r[2],
            "budget_tokens": r[3], "tokens_used": r[4], "agents_spawned": r[5],
            "started_at": r[6], "completed_at": r[7],
            "budget_pct_used": round(r[4] / max(r[3], 1) * 100, 1),
        }
        for r in rows
    ]

    return [TextContent(type="text", text=json.dumps({"workflows": workflows, "count": len(workflows)}))]


async def _handle_estimate(args: dict):
    agent_count = args["agent_count"]
    avg_tokens = args.get("avg_tokens_per_agent", 50_000)
    model_mix = args.get("model_mix", {})

    if not model_mix:
        # Default mix based on optimized assignments
        model_mix = {
            "opus": max(1, agent_count // 6),
            "sonnet": agent_count // 2,
            "haiku": agent_count - (agent_count // 6) - (agent_count // 2),
        }

    total_cost = 0.0
    total_tokens = 0
    breakdown = {}

    for model, count in model_mix.items():
        tier = ModelTier(model)
        tokens = count * avg_tokens
        cost = gateway.estimate_cost(tier, tokens)
        total_cost += cost
        total_tokens += tokens
        breakdown[model] = {"agents": count, "tokens": tokens, "cost_usd": round(cost, 4)}

    # Time estimate based on pool capacity
    pool = gateway.pool_status()
    avg_agent_time_s = 60  # rough average
    serial_time = agent_count * avg_agent_time_s
    parallel_factor = min(agent_count, pool["current_slots"])
    estimated_time_s = serial_time / max(parallel_factor, 1)

    return [TextContent(type="text", text=json.dumps({
        "estimated_cost_usd": round(total_cost, 4),
        "estimated_tokens": total_tokens,
        "estimated_time_minutes": round(estimated_time_s / 60, 1),
        "model_breakdown": breakdown,
        "recommended_budget": int(total_tokens * 1.3),  # 30% buffer
        "pool_capacity": pool,
    }))]


async def _handle_report(args: dict):
    wf_id = args["workflow_id"]

    row = db.execute(
        """SELECT id, name, status, budget_tokens, tokens_used, agents_spawned,
                  started_at, completed_at, error
           FROM workflows WHERE id = ?""",
        (wf_id,)
    ).fetchone()

    if not row:
        return [TextContent(type="text", text=json.dumps({"error": "Workflow not found"}))]

    wf = dict(zip(
        ["id", "name", "status", "budget_tokens", "tokens_used", "agents_spawned",
         "started_at", "completed_at", "error"],
        row
    ))

    # Duration
    if wf["started_at"] and wf["completed_at"]:
        start = datetime.fromisoformat(wf["started_at"])
        end = datetime.fromisoformat(wf["completed_at"])
        wf["duration_seconds"] = (end - start).total_seconds()
        wf["duration_human"] = f"{wf['duration_seconds']/60:.1f} minutes"

    # Model breakdown
    wf["usage_by_model"] = gateway.get_usage_by_model(wf_id)
    wf["total_usage"] = gateway.get_usage(wf_id)

    # Event timeline
    events = db.execute(
        """SELECT event_type, phase, agent_id, message, tokens_delta, timestamp
           FROM workflow_events WHERE workflow_id = ? ORDER BY timestamp""",
        (wf_id,)
    ).fetchall()

    wf["timeline"] = [
        {"event_type": e[0], "phase": e[1], "agent_id": e[2],
         "message": e[3], "tokens_delta": e[4], "timestamp": e[5]}
        for e in events
    ]

    # Cost summary
    wf["cost_usd"] = wf["total_usage"]["cost_usd"]
    wf["budget_efficiency"] = f"{wf['tokens_used']}/{wf['budget_tokens']} ({round(wf['tokens_used']/max(wf['budget_tokens'],1)*100,1)}%)"

    return [TextContent(type="text", text=json.dumps(wf))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

#!/usr/bin/env python3
"""Orchestrator MCP Server — exposes planning and routing tools.

Components:
- Router: 3-tier complexity routing
- DAG: TaskDAG plan generation
- Reliability: circuit breakers, dead-letter queue
- Health: agent health monitoring

Execution is handled externally by workflow scripts — not by this server.

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cap.db import get_db, migrate
from cap.orchestration.router import route, RoutingDecision, Tier
from cap.orchestration.dag import TaskDAG
from cap.reliability.circuit_breaker import CircuitBreaker
from cap.reliability.dlq import list_dlq, retry_task, dismiss_task
from cap.health.monitor import AgentHealthMonitor, HealthState

logger = logging.getLogger("cap.orchestrator")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Database setup
DB_PATH = os.environ.get(
    "CAP_ORCHESTRATOR_DB",
    os.path.expanduser("~/.cap/cap.db"),
)

db = get_db(DB_PATH)
migrate(db)

server = Server("cap-orchestrator")

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _generate_workflow_id() -> str:
    return f"orch-{uuid.uuid4().hex[:12]}"


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="cap_route",
            description="Route a task to the appropriate orchestration tier (inline/lightweight/full) based on complexity scoring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "The task to route",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Current session identifier",
                        "default": "unknown",
                    },
                },
                "required": ["task_description"],
            },
        ),
        Tool(
            name="cap_plan",
            description="Generate a TaskDAG execution plan from a task description. Decomposes into steps with dependencies and agent assignments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "The task to plan",
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional context (workspace, affected_files, agent_overrides)",
                    },
                },
                "required": ["task_description"],
            },
        ),
        Tool(
            name="cap_status",
            description="Get the current status of a workflow: step states, progress, and completion info.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to query",
                    },
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="cap_dlq_list",
            description="List tasks in the dead-letter queue (failed tasks that exhausted retries).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="cap_health",
            description="Get agent health summary: per-agent-type health states, circuit breaker status, and failure predictions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "description": "Optional: filter to specific agent type",
                    },
                },
            },
        ),
        Tool(
            name="cap_execute",
            description="Execute a task on a specialist agent via AWS Bedrock. Spawns an agent, runs multi-turn conversation with tool use, records cost and learning outcomes. Full end-to-end execution.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "description": "Agent role: dev, devops, security, sre, code-review, test, docs, optimization, aws-architect",
                    },
                    "task": {
                        "type": "string",
                        "description": "The task prompt to send to the agent.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override (sonnet, opus, haiku). Auto-selected from agent_type when omitted.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to prepend (e.g., from knowledge search or prior agent output).",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max output tokens per turn (default 8192).",
                        "default": 8192,
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Working directory for tool execution (file_read, bash_exec).",
                    },
                },
                "required": ["agent_type", "task"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "cap_route":
            return await _handle_route(arguments)
        elif name == "cap_plan":
            return await _handle_plan(arguments)
        elif name == "cap_status":
            return await _handle_status(arguments)
        elif name == "cap_dlq_list":
            return await _handle_dlq_list(arguments)
        elif name == "cap_health":
            return await _handle_health(arguments)
        elif name == "cap_execute":
            return await _handle_execute(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_route(args: dict):
    """Route a task to the appropriate tier."""
    task_description = args["task_description"]
    session_id = args.get("session_id", "unknown")

    decision = route(task_description, db, session_id)

    return [TextContent(type="text", text=json.dumps({
        "tier": decision.tier.value,
        "reasoning": decision.reasoning,
        "estimated_agents": decision.estimated_agents,
        "estimated_cost": decision.estimated_cost,
        "complexity_score": decision.complexity_score,
        "decision_id": decision.decision_id,
    }))]


async def _handle_plan(args: dict):
    """Generate a TaskDAG plan from a task description."""
    return [TextContent(type="text", text=json.dumps({
        "error": "cap_plan is not available: planner module removed",
    }))]


async def _handle_status(args: dict):
    """Get workflow status. Execution is handled by external workflow scripts."""
    workflow_id = args["workflow_id"]
    return [TextContent(type="text", text=json.dumps({
        "error": "cap_status: workflow execution is handled by external workflow scripts. Query the workflow engine directly.",
        "workflow_id": workflow_id,
    }))]


async def _handle_dlq_list(args: dict):
    """List dead-letter queue contents."""
    items = list_dlq(db)

    return [TextContent(type="text", text=json.dumps({
        "count": len(items),
        "items": items,
    }))]


async def _handle_health(args: dict):
    """Get agent health summary."""
    monitor = AgentHealthMonitor(db)
    agent_type_filter = args.get("agent_type")

    # Known agent types
    agent_types = ["dev", "devops", "security", "sre", "code-review", "test", "docs", "explore"]

    if agent_type_filter:
        agent_types = [agent_type_filter]

    health_data = []
    for agent_type in agent_types:
        health_state = monitor.infer_health(agent_type)
        cb = CircuitBreaker(agent_type, db)
        cb_state = cb.get_state()
        can_dispatch, reason = cb.can_dispatch()

        entry = {
            "agent_type": agent_type,
            "health": health_state.value,
            "circuit_breaker": cb_state,
            "can_dispatch": can_dispatch,
        }
        if reason:
            entry["dispatch_reason"] = reason

        # Get baseline if available
        baseline = db.execute(
            "SELECT failure_rate, sample_count, avg_duration FROM agent_health_baselines WHERE agent_type = ?",
            (agent_type,),
        ).fetchone()
        if baseline:
            entry["failure_rate"] = baseline[0]
            entry["sample_count"] = baseline[1]
            entry["avg_duration_ms"] = baseline[2]

        health_data.append(entry)

    return [TextContent(type="text", text=json.dumps({
        "agents": health_data,
        "timestamp": _now(),
    }))]


async def _handle_execute(args: dict):
    """Full agent execution: spawn -> execute -> record -> learn."""
    import os
    from cap.harness.converse_executor import ConverseExecutor
    from cap.harness.agent_store import spawn_agent, record_execution as store_record_execution
    from cap.harness.hooks import hooks_post_task
    import cap.harness.cost_meter as cost_meter

    agent_type = args["agent_type"]
    task = args["task"]
    model_override = args.get("model")
    context = args.get("context")
    max_tokens = int(args.get("max_tokens", 8192))

    # Spawn agent record
    try:
        record = spawn_agent(agent_type, model=None)
    except ValueError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    # Create executor
    executor = ConverseExecutor(
        profile=os.environ.get("AWS_PROFILE"),
        region=os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"),
        budget_limit_usd=float(os.environ.get("CAP_DAILY_LIMIT_USD", "5.0")),
    )

    # Execute
    result = executor.execute(
        agent_id=record.agent_id,
        agent_type=agent_type,
        prompt=task,
        model=model_override,
        max_tokens=max_tokens,
        context=context,
    )

    # Record to cost meter
    try:
        exec_result = result.to_execution_result()
        cost_meter.record_execution(exec_result, agent_type=agent_type)
    except Exception as exc:
        logger.warning("cap_execute: cost_meter.record failed: %s", exc)

    # Record to agent store
    try:
        store_record_execution(
            agent_id=record.agent_id,
            input_tokens=result.total_input_tokens,
            output_tokens=result.total_output_tokens,
            cost_usd=result.total_cost_usd,
            result=result.response,
            error=result.error,
        )
    except Exception as exc:
        logger.warning("cap_execute: store_record failed: %s", exc)

    # Post-task hook for learning
    try:
        hooks_post_task(
            agent_id=record.agent_id,
            execution_id=record.agent_id,
            success=result.error is None,
            output_summary=(result.response or "")[:500] if result.response else None,
        )
    except Exception as exc:
        logger.warning("cap_execute: hooks_post_task failed: %s", exc)

    # Build response
    payload = {
        "agent_id": record.agent_id,
        "agent_type": agent_type,
        "model": result.model,
        "response": result.response,
        "error": result.error,
        "total_input_tokens": result.total_input_tokens,
        "total_output_tokens": result.total_output_tokens,
        "cost_usd": result.total_cost_usd,
        "duration_ms": result.duration_ms,
        "turns": result.turns,
        "tool_calls": result.tool_calls[:20],  # Cap to avoid huge payloads
    }

    return [TextContent(type="text", text=json.dumps(payload))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

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
            description="Get current orchestrator status: active agents, budget, top spenders.",
            inputSchema={
                "type": "object",
                "properties": {},
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
                        "description": "Agent role: dev, devops, security, sre, code-review, test, docs, optimization, aws-architect, explore, cicd",
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
        Tool(
            name="cap_orchestrate",
            description="Route a task to the best specialist agent and execute it end-to-end via AWS Bedrock. Combines routing + execution in a single call. Returns the agent's full response with cost tracking.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to execute.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to prepend (knowledge base results, prior agent output).",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Working directory for file/bash operations.",
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="cap_resume",
            description="Resume a failed agent execution by re-running the agent's last task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent ID to resume (from a previous cap_execute or cap_orchestrate result).",
                    },
                },
                "required": ["agent_id"],
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
        elif name == "cap_orchestrate":
            return await _handle_orchestrate(arguments)
        elif name == "cap_resume":
            return await _handle_resume(arguments)
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
    """Generate a TaskDAG plan using Bedrock to decompose the task."""
    import os
    from cap.harness.converse_executor import ConverseExecutor

    task_description = args["task_description"]

    # Use haiku for planning (fast + cheap)
    executor = ConverseExecutor(
        profile=os.environ.get("AWS_PROFILE"),
        region=os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"),
        budget_limit_usd=float(os.environ.get("CAP_DAILY_LIMIT_USD", "5.0")),
    )

    plan_prompt = f"""Decompose this task into a sequence of sub-tasks for specialist agents.

Available agent types: dev, devops, security, sre, code-review, test, docs, explore, aws-architect, optimization, cicd

Task: {task_description}

Return a JSON object with this exact structure:
{{
  "steps": [
    {{"id": "step-1", "agent_type": "agent-name", "task": "specific sub-task description", "depends_on": []}},
    {{"id": "step-2", "agent_type": "agent-name", "task": "specific sub-task description", "depends_on": ["step-1"]}}
  ],
  "parallel_groups": [["step-1"], ["step-2"]],
  "estimated_cost_usd": 0.05
}}

Return ONLY the JSON. No explanation."""

    result = executor.execute(
        agent_id=f"planner-{uuid.uuid4().hex[:8]}",
        agent_type="dev",
        prompt=plan_prompt,
        model="haiku",
        max_tokens=4096,
        temperature=0.3,
    )

    if result.error:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Planning failed: {result.error}",
        }))]

    # Extract JSON from response (handle markdown code blocks)
    response_text = result.response or ""
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0]
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0]

    try:
        plan = json.loads(response_text.strip())
        plan["workflow_id"] = _generate_workflow_id()
        plan["planning_cost_usd"] = result.total_cost_usd
        plan["planning_model"] = result.model
        return [TextContent(type="text", text=json.dumps(plan))]
    except (json.JSONDecodeError, IndexError) as exc:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Failed to parse plan: {exc}",
            "raw_response": (result.response or "")[:1000],
        }))]


async def _handle_status(args: dict):
    """Get active agent and execution status."""
    from cap.harness.agent_store import list_agents
    import cap.harness.cost_meter as cost_meter

    try:
        active = list_agents(status="active")
    except Exception:
        active = []

    try:
        remaining = cost_meter.budget_remaining()
    except Exception:
        remaining = None

    try:
        spenders = cost_meter.top_spenders(n=5)
    except Exception:
        spenders = []

    return [TextContent(type="text", text=json.dumps({
        "active_agents": [
            {"agent_id": a.agent_id, "agent_type": a.agent_type, "status": a.status, "model": a.model}
            for a in active
        ],
        "active_count": len(active),
        "budget_remaining_usd": remaining,
        "top_spenders": spenders[:5] if spenders else [],
        "timestamp": _now(),
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


def _load_harness_config() -> dict:
    """Load .harness/config.json if it exists, else return defaults."""
    config_path = Path.cwd() / ".harness" / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "data" / "harness" / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


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

    # Load config
    config = _load_harness_config()
    aws_config = config.get("aws", {})
    budget_config = config.get("budget", {})

    profile = os.environ.get("AWS_PROFILE") or aws_config.get("profile")
    region = os.environ.get("AWS_DEFAULT_REGION") or aws_config.get("region", "eu-central-1")
    budget_limit = float(os.environ.get("CAP_DAILY_LIMIT_USD", budget_config.get("daily_limit_usd", 5.0)))

    # Spawn agent record
    try:
        record = spawn_agent(agent_type, model=None)
    except ValueError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    # Create executor
    executor = ConverseExecutor(
        profile=profile,
        region=region,
        budget_limit_usd=budget_limit,
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
            agent_type=agent_type,
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


async def _handle_orchestrate(args: dict):
    """Route a task to the best specialist agent and execute it end-to-end.

    This is the Ruflo-equivalent "just do it" entrypoint:
    1. Determine best agent_type via agentdb_semantic_route (keyword + history)
    2. Determine best model via hooks_route (embedding + tier)
    3. Execute via ConverseExecutor (real Bedrock call)
    4. Record cost + update trust
    5. Return result with full routing metadata
    """
    from cap.harness.hooks import hooks_route
    from cap.harness.agentdb import agentdb_semantic_route

    task = args["task"]
    context = args.get("context")
    workspace = args.get("workspace")

    # Step 1: Determine agent_type (who handles this task)
    agent_routing = agentdb_semantic_route(task)
    agent_type = agent_routing.get("recommended_agent_type", "dev")
    agent_confidence = agent_routing.get("confidence", 0.3)

    # Step 2: Determine model — prefer agent's configured default, then hooks_route
    config = _load_harness_config()
    agent_defaults = config.get("agent_defaults", {})
    agent_default_model = agent_defaults.get(agent_type)

    if agent_default_model:
        short_model = agent_default_model
    else:
        model_routing = hooks_route(task, agent_type=agent_type)
        recommended_model = model_routing.get("recommended_model", "claude-sonnet-4-6")
        _full_to_short: dict[str, str] = {
            "claude-haiku-4-5": "haiku",
            "claude-sonnet-4-6": "sonnet",
            "claude-sonnet-4-5": "sonnet",
            "claude-opus-4-6": "opus",
            "haiku": "haiku",
            "sonnet": "sonnet",
            "opus": "opus",
        }
        short_model = _full_to_short.get(recommended_model, "sonnet")

    model_routing = hooks_route(task, agent_type=agent_type)
    tier = model_routing.get("tier", "lightweight")

    # Step 3: Execute via cap_execute
    execute_args = {
        "agent_type": agent_type,
        "task": task,
        "model": short_model,
    }
    if context:
        execute_args["context"] = context
    if workspace:
        execute_args["workspace"] = workspace

    result_contents = await _handle_execute(execute_args)

    # Step 4: Enrich result with routing metadata
    try:
        result_payload = json.loads(result_contents[0].text)
        result_payload["routing"] = {
            "agent_type": agent_type,
            "agent_confidence": agent_confidence,
            "agent_alternatives": agent_routing.get("alternatives", []),
            "based_on_patterns": agent_routing.get("based_on_patterns", 0),
            "model_alias": short_model,
            "tier": tier,
            "model_confidence": model_routing.get("confidence"),
            "model_reason": model_routing.get("reason"),
            "routing_method": model_routing.get("routing_method", "keyword"),
        }
        return [TextContent(type="text", text=json.dumps(result_payload))]
    except (json.JSONDecodeError, IndexError, AttributeError):
        return result_contents


async def _handle_resume(args: dict):
    """Resume a failed agent execution by re-running the same task."""
    from cap.harness.agent_store import get_agent

    agent_id = args.get("agent_id") or args.get("workflow_id")
    if not agent_id:
        return [TextContent(type="text", text=json.dumps({"error": "agent_id or workflow_id required"}))]

    try:
        agent = get_agent(agent_id)
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"agent not found: {exc}"}))]

    if agent is None:
        return [TextContent(type="text", text=json.dumps({"error": f"no agent with id {agent_id}"}))]

    last_task = agent.get("last_task") or agent.get("task_description", "")
    if not last_task:
        return [TextContent(type="text", text=json.dumps({"error": "no task recorded for this agent — cannot resume"}))]

    execute_args = {
        "agent_type": agent.get("agent_type", "dev"),
        "task": last_task,
        "model": agent.get("model"),
    }
    return await _handle_execute(execute_args)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

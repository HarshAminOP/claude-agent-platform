#!/usr/bin/env python3
"""Orchestrator MCP Server — exposes DAG orchestration tools.

Integrates Week 2 components:
- Router: 3-tier complexity routing
- Planner: TaskDAG generation from task descriptions
- Executor: parallel DAG execution with dependency tracking
- Checkpoint: save/resume workflow state
- Reliability: circuit breakers, dead-letter queue
- Health: agent health monitoring

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
from cap.orchestration.dag import TaskDAG, TaskStep, StepState
from cap.orchestration.executor import DAGExecutor
from cap.orchestration.context import ContextThread
from cap.orchestration.planner import generate_plan
from cap.orchestration.checkpoint import (
    save_checkpoint,
    save_initial_checkpoint,
    resume_from_checkpoint,
    list_checkpoints,
)
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

# Active workflows tracked in memory
_active_workflows: dict[str, dict] = {}
_MAX_ACTIVE_WORKFLOWS = 50  # Evict oldest completed workflows beyond this


def _evict_completed_workflows() -> None:
    """Remove completed/failed workflows from memory when cache exceeds limit."""
    if len(_active_workflows) <= _MAX_ACTIVE_WORKFLOWS:
        return
    # Evict completed/failed workflows, oldest first
    completed = sorted(
        ((wf_id, wf) for wf_id, wf in _active_workflows.items()
         if wf.get("status") in ("completed", "failed")),
        key=lambda x: x[1].get("created_at", 0),
    )
    to_remove = len(_active_workflows) - _MAX_ACTIVE_WORKFLOWS
    for wf_id, _ in completed[:to_remove]:
        del _active_workflows[wf_id]


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
            name="cap_execute",
            description="Execute a workflow DAG by ID. Runs all steps with maximum parallelism respecting dependencies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to execute (from cap_plan)",
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "description": "Max parallel steps",
                        "default": 5,
                    },
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="cap_resume",
            description="Resume a workflow from its last checkpoint. Steps in RUNNING state are reset to PENDING for re-execution.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to resume",
                    },
                },
                "required": ["workflow_id"],
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "cap_route":
            return await _handle_route(arguments)
        elif name == "cap_plan":
            return await _handle_plan(arguments)
        elif name == "cap_execute":
            return await _handle_execute(arguments)
        elif name == "cap_resume":
            return await _handle_resume(arguments)
        elif name == "cap_status":
            return await _handle_status(arguments)
        elif name == "cap_dlq_list":
            return await _handle_dlq_list(arguments)
        elif name == "cap_health":
            return await _handle_health(arguments)
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
    task_description = args["task_description"]
    context = args.get("context", {})

    # Generate the plan
    dag = generate_plan(task_description, context=context, db=db)

    # Create a workflow ID and save initial checkpoint
    workflow_id = _generate_workflow_id()
    save_initial_checkpoint(workflow_id, dag, db)

    # Track in memory (with eviction of old completed workflows)
    _evict_completed_workflows()
    _active_workflows[workflow_id] = {
        "dag": dag,
        "task_description": task_description,
        "created_at": time.time(),
        "status": "planned",
    }

    # Compute plan metadata
    critical_path = dag.critical_path()
    parallelism = dag.parallelism_factor()

    return [TextContent(type="text", text=json.dumps({
        "workflow_id": workflow_id,
        "status": "planned",
        "steps": dag.to_dict()["steps"],
        "step_count": len(dag.steps),
        "critical_path": critical_path,
        "critical_path_length": len(critical_path),
        "parallelism_factor": round(parallelism, 2),
        "agent_types": list(set(s.agent_type for s in dag.steps.values())),
    }))]


async def _handle_execute(args: dict):
    """Execute a workflow DAG."""
    workflow_id = args["workflow_id"]
    max_concurrency = args.get("max_concurrency", 5)

    # Retrieve DAG from memory or checkpoint
    if workflow_id in _active_workflows:
        dag = _active_workflows[workflow_id]["dag"]
        context_thread = ContextThread(orchestration_id=workflow_id)
    else:
        try:
            dag, context_thread = resume_from_checkpoint(workflow_id, db)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({
                "error": str(e),
                "workflow_id": workflow_id,
            }))]

    # Check circuit breakers for all agent types in the DAG
    blocked_agents = []
    for step in dag.steps.values():
        if step.state == StepState.PENDING:
            cb = CircuitBreaker(step.agent_type, db)
            can_dispatch, reason = cb.can_dispatch()
            if not can_dispatch:
                blocked_agents.append({"agent_type": step.agent_type, "reason": reason})

    if blocked_agents:
        return [TextContent(type="text", text=json.dumps({
            "status": "blocked",
            "workflow_id": workflow_id,
            "blocked_agents": blocked_agents,
            "message": "Circuit breaker(s) open. Wait for cooldown or resolve failures.",
        }))]

    # Define the dispatch function (stub for now -- in production this calls Claude)
    async def dispatch_step(step: TaskStep, dep_context: dict) -> dict:
        """Stub dispatch function. In production, this invokes Claude agents."""
        # Simulate agent execution
        await asyncio.sleep(0.01)  # Minimal delay for async cooperation
        return {
            "status": "success",
            "summary": f"Step {step.id} ({step.agent_type}) completed: {step.description[:100]}",
            "output": f"Executed by {step.agent_type} agent",
            "artifacts": [],
        }

    # Create executor
    def on_step_complete(step: TaskStep, result: dict):
        """Checkpoint after each step completion."""
        try:
            save_checkpoint(workflow_id, dag, context_thread, db, phase="running")
        except Exception as e:
            logger.warning("Checkpoint save failed: %s", e)

    executor = DAGExecutor(
        dag=dag,
        dispatch_fn=dispatch_step,
        context_thread=context_thread,
        max_concurrency=max_concurrency,
        on_step_complete=on_step_complete,
    )

    # Update status
    if workflow_id in _active_workflows:
        _active_workflows[workflow_id]["status"] = "running"

    # Execute
    results = await executor.execute()

    # Determine final phase
    failed_steps = [s for s in dag.steps.values() if s.state == StepState.FAILED]
    skipped_steps = [s for s in dag.steps.values() if s.state == StepState.SKIPPED]
    completed_steps = [s for s in dag.steps.values() if s.state == StepState.COMPLETED]

    if failed_steps:
        phase = "failed"
    elif skipped_steps and not completed_steps:
        phase = "failed"
    else:
        phase = "completed"

    # Save final checkpoint
    save_checkpoint(workflow_id, dag, context_thread, db, phase=phase)

    # Update in-memory status
    if workflow_id in _active_workflows:
        _active_workflows[workflow_id]["status"] = phase

    return [TextContent(type="text", text=json.dumps({
        "workflow_id": workflow_id,
        "status": phase,
        "completed": len(completed_steps),
        "failed": len(failed_steps),
        "skipped": len(skipped_steps),
        "total_steps": len(dag.steps),
        "results": {
            step_id: {
                "status": result.get("status", "unknown"),
                "summary": result.get("summary", ""),
            }
            for step_id, result in results.items()
        },
    }))]


async def _handle_resume(args: dict):
    """Resume a workflow from checkpoint."""
    workflow_id = args["workflow_id"]

    try:
        dag, context_thread = resume_from_checkpoint(workflow_id, db)
    except ValueError as e:
        return [TextContent(type="text", text=json.dumps({
            "error": str(e),
            "workflow_id": workflow_id,
        }))]

    # Compute current state
    states = {}
    for step in dag.steps.values():
        state_name = step.state.value
        states[state_name] = states.get(state_name, 0) + 1

    # Store back in memory
    _active_workflows[workflow_id] = {
        "dag": dag,
        "context_thread": context_thread,
        "status": "resumed",
        "resumed_at": time.time(),
    }

    return [TextContent(type="text", text=json.dumps({
        "workflow_id": workflow_id,
        "status": "resumed",
        "step_states": states,
        "total_steps": len(dag.steps),
        "message": f"Workflow resumed. {states.get('pending', 0)} steps pending, {states.get('completed', 0)} already completed.",
    }))]


async def _handle_status(args: dict):
    """Get workflow status."""
    workflow_id = args["workflow_id"]

    # Check in-memory first
    if workflow_id in _active_workflows:
        wf = _active_workflows[workflow_id]
        dag = wf["dag"]
        steps_info = {}
        for step_id, step in dag.steps.items():
            steps_info[step_id] = {
                "description": step.description,
                "agent_type": step.agent_type,
                "state": step.state.value,
                "depends_on": step.depends_on,
            }
            if step.result:
                steps_info[step_id]["result_summary"] = step.result.get("summary", "")

        states = {}
        for step in dag.steps.values():
            state_name = step.state.value
            states[state_name] = states.get(state_name, 0) + 1

        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": wf.get("status", "unknown"),
            "step_states": states,
            "steps": steps_info,
            "total_steps": len(dag.steps),
        }))]

    # Try loading from checkpoint
    try:
        dag, _ = resume_from_checkpoint(workflow_id, db)
        steps_info = {}
        for step_id, step in dag.steps.items():
            steps_info[step_id] = {
                "description": step.description,
                "agent_type": step.agent_type,
                "state": step.state.value,
                "depends_on": step.depends_on,
            }

        states = {}
        for step in dag.steps.values():
            state_name = step.state.value
            states[state_name] = states.get(state_name, 0) + 1

        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": "checkpointed",
            "step_states": states,
            "steps": steps_info,
            "total_steps": len(dag.steps),
        }))]
    except ValueError:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Workflow {workflow_id} not found",
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


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

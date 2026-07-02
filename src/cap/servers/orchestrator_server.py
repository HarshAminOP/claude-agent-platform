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
import dataclasses
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
from cap.lib.workflow_store import save_workflow, update_heartbeat, load_workflow, mark_failed_stale, list_active_workflows

logger = logging.getLogger("cap.orchestrator")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Database setup — centralized path resolution
from cap.config import get_platform_db_path, get_knowledge_db_path
DB_PATH = os.environ.get("CAP_ORCHESTRATOR_DB", str(get_platform_db_path()))

db = get_db(DB_PATH)
migrate(db)

server = Server("cap-orchestrator")

# Module-level singleton for embedding-based routing (lazy-initialized)
_embedding_router = None

# Module-level singleton for task decomposer (lazy-initialized)
_task_decomposer = None

# Module-level singleton for knowledge graph (lazy-initialized)
_knowledge_graph = None

# ── Background workflow tracking ──────────────────────────────────────────────
_running_workflows: dict[str, dict] = {}  # workflow_id -> {task, status, result, error, ...}

# Thresholds for determining default blocking behavior
_SIMPLE_COMPLEXITIES = {"simple"}
_NONBLOCKING_COMPLEXITIES = {"moderate", "complex"}

# TTL for completed/failed workflows (1 hour)
_WORKFLOW_TTL_SECONDS = 3600


def _get_decomposer():
    """Return the module-level TaskDecomposer, creating it lazily on first call."""
    global _task_decomposer
    if _task_decomposer is None:
        from cap.lib.task_decomposer import TaskDecomposer
        _task_decomposer = TaskDecomposer()
    return _task_decomposer


def _get_knowledge_graph():
    """Return the module-level KnowledgeGraph, creating it lazily on first call.

    Returns None when the knowledge_graph module is unavailable or the graph
    has not been populated yet.  Callers must handle None gracefully.
    """
    global _knowledge_graph
    if _knowledge_graph is not None:
        return _knowledge_graph
    try:
        from cap.lib.knowledge_graph import KnowledgeGraph
        # CRITICAL FIX: KnowledgeGraph uses knowledge.db, NOT the orchestrator/platform DB.
        knowledge_db = str(get_knowledge_db_path())
        _knowledge_graph = KnowledgeGraph(knowledge_db)
        return _knowledge_graph
    except Exception as exc:
        logger.debug("KnowledgeGraph init failed (will retry next call): %s", exc)
        return None


def _query_graph_context(task: str, workspace: str = "") -> str:
    """Query the knowledge graph for nodes/services mentioned in *task*.

    Extracts entity names from the task string via a simple token scan,
    queries the graph for matching nodes, and returns a compact context
    string that can be prepended to agent prompts.  The whole operation
    is bounded to < 200 ms: if the graph is empty or unavailable the
    function returns "" in sub-millisecond time.

    Args:
        task: Natural-language task description.
        workspace: Optional workspace path to scope graph queries.

    Returns:
        A formatted context string (may be empty when graph is empty or
        the query finds nothing relevant).
    """
    import time
    t0 = time.monotonic()

    try:
        kg = _get_knowledge_graph()
        if kg is None:
            return ""

        # Set the workspace on the graph instance so scoped queries work.
        # KnowledgeGraph._default_workspace is the scope key for all queries.
        if workspace:
            kg._default_workspace = workspace
        elif not kg._default_workspace:
            # No workspace configured — graph queries would raise RuntimeError.
            return ""

        # Quick check: if the graph is empty, skip token extraction.
        try:
            stats = kg.get_stats()
            if stats.get("total_nodes", 0) == 0:
                return ""
        except Exception:
            return ""

        # Extract candidate entity names from the task using a simple
        # token scan: words of 3+ chars that look like identifiers.
        import re
        tokens = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b", task)
        # Deduplicate while preserving order; limit to first 20 to stay fast.
        seen: set[str] = set()
        candidates: list[str] = []
        for tok in tokens:
            lower = tok.lower()
            if lower not in seen:
                seen.add(lower)
                candidates.append(tok)
            if len(candidates) >= 20:
                break

        matched_nodes: list[dict] = []
        for candidate in candidates:
            results = kg.search(candidate, limit=3)
            for node in results:
                if node not in matched_nodes:
                    matched_nodes.append(node)
            if len(matched_nodes) >= 10:
                break

        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > 200:
            logger.warning(
                "_query_graph_context: graph query took %.0f ms (>200 ms budget)", elapsed_ms
            )

        if not matched_nodes:
            return ""

        # Build a compact context block for the agent prompt.
        lines: list[str] = ["### Knowledge Graph Context"]
        for node in matched_nodes[:10]:
            import json as _json
            name = node.get("entity_name", "")
            ntype = node.get("entity_type", "")
            try:
                meta = _json.loads(node.get("metadata") or "{}")
            except (_json.JSONDecodeError, TypeError):
                meta = {}
            summary = meta.get("summary", "")
            line = f"- **{name}** ({ntype})"
            if summary:
                line += f": {summary[:120]}"
            lines.append(line)

        return "\n".join(lines)

    except Exception as exc:
        logger.debug("_query_graph_context failed: %s", exc)
        return ""


def _default_region() -> str:
    """Return the default AWS region from harness config."""
    from cap.lib.harness_config import DEFAULT_AWS_REGION
    return DEFAULT_AWS_REGION


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _generate_workflow_id() -> str:
    return f"orch-{uuid.uuid4().hex[:12]}"


def _prune_stale_workflows() -> None:
    """Remove workflows older than _WORKFLOW_TTL_SECONDS from _running_workflows."""
    now = time.time()
    stale_ids = [
        wf_id
        for wf_id, wf in _running_workflows.items()
        if wf.get("completed_at") and (now - wf["completed_at"]) > _WORKFLOW_TTL_SECONDS
    ]
    for wf_id in stale_ids:
        del _running_workflows[wf_id]


async def _run_orchestration_bg(workflow_id: str, args: dict) -> None:
    """Background coroutine that executes the full orchestration and updates _running_workflows.

    This function mirrors the logic in _handle_orchestrate but updates step-level
    progress in _running_workflows[workflow_id] as it executes.
    """
    wf = _running_workflows[workflow_id]
    t0 = time.time()

    async def _heartbeat(wf_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                update_heartbeat(wf_id)
            except Exception:
                pass

    hb_task = asyncio.create_task(_heartbeat(workflow_id))

    try:
        wf["current_step"] = {"phase": "routing", "description": "Determining agent type and complexity"}
        wf["steps_completed"] = 0
        try:
            save_workflow(
                workflow_id,
                status=wf["status"],
                steps_completed=wf["steps_completed"],
                steps_total=wf["steps_total"],
                current_step=wf.get("current_step"),
                args=args,
            )
        except Exception as _pe:
            logger.debug("_run_orchestration_bg: save_workflow (init) failed: %s", _pe)

        from cap.harness.hooks import hooks_route
        from cap.harness.agentdb import agentdb_semantic_route

        task = args["task"]
        context = args.get("context")
        workspace = args.get("workspace")

        # Check complexity to decide execution path.
        try:
            decomposer = _get_decomposer()
            complexity = decomposer._classify_complexity(task)
        except Exception as exc:
            logger.warning("_run_orchestration_bg: complexity check failed (%s); defaulting to simple", exc)
            complexity = "simple"

        wf["complexity"] = complexity
        wf["steps_completed"] = 1
        wf["current_step"] = {"phase": "execution", "description": "Executing task"}

        if complexity in ("moderate", "complex"):
            try:
                logger.info(
                    "_run_orchestration_bg: complexity=%s — using CoordinationEngine for workflow %s",
                    complexity,
                    workflow_id,
                )
                wf["steps_total"] = 4  # route -> decompose -> execute steps -> synthesize
                wf["current_step"] = {"phase": "coordination", "description": "Running multi-step coordination"}

                # Progress callback: updates the parent workflow's progress as
                # the coordination engine completes each internal step.
                def _on_coord_step_complete(steps_done: int, total_steps: int, step_desc: str) -> None:
                    # +1 offset because routing was step 0 (already counted)
                    wf["steps_completed"] = steps_done + 1
                    # +2 for routing (before) and finalization (after)
                    wf["steps_total"] = total_steps + 2
                    wf["current_step"] = {"phase": "coordination", "description": step_desc}
                    try:
                        save_workflow(
                            workflow_id,
                            status="running",
                            steps_completed=wf["steps_completed"],
                            steps_total=wf["steps_total"],
                            current_step=wf["current_step"],
                        )
                    except Exception:
                        pass

                result_contents = await _run_coordination(
                    task, context or "", workspace or "",
                    on_step_complete=_on_coord_step_complete,
                )
                wf["steps_completed"] = wf["steps_total"]
                wf["current_step"] = {"phase": "done", "description": "Completed"}
                wf["status"] = "completed"
                wf["result"] = json.loads(result_contents[0].text)
                wf["duration_ms"] = int((time.time() - t0) * 1000)
                wf["completed_at"] = time.time()
                try:
                    save_workflow(
                        workflow_id,
                        status=wf["status"],
                        steps_completed=wf["steps_completed"],
                        steps_total=wf["steps_total"],
                        current_step=wf.get("current_step"),
                        result=wf.get("result"),
                    )
                except Exception as _pe:
                    logger.debug("_run_orchestration_bg: save_workflow (coord complete) failed: %s", _pe)
                return
            except Exception as exc:
                logger.warning(
                    "_run_orchestration_bg: CoordinationEngine failed (%s); falling back to single-agent path",
                    exc,
                )

        # Single-agent path
        wf["steps_total"] = 3  # route -> execute -> record
        wf["current_step"] = {"phase": "routing", "description": "Selecting agent via embedding/keyword routing"}

        global _embedding_router
        routing_method = "keyword"
        embed_result = None
        agent_routing = {}

        try:
            if _embedding_router is None:
                from cap.harness.embed_router import EmbeddingRouter
                _embedding_router = EmbeddingRouter()
            embed_result = _embedding_router.route(task)
        except Exception as exc:
            logger.debug("EmbeddingRouter failed in _run_orchestration_bg: %s", exc)
            embed_result = None

        if embed_result and embed_result.get("confidence", 0) > 0.5:
            agent_type = embed_result["recommended_agent_type"]
            agent_confidence = embed_result["confidence"]
            short_model = embed_result.get("model", "sonnet")
            routing_method = "embedding"
            agent_routing = embed_result
        else:
            agent_routing = agentdb_semantic_route(task)
            agent_type = agent_routing.get("recommended_agent_type", "dev")
            agent_confidence = agent_routing.get("confidence", 0.3)
            short_model = None

        # Determine model
        if short_model is None or routing_method != "embedding":
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

        wf["steps_completed"] = 2
        wf["current_step"] = {"phase": "execution", "description": f"Executing via {agent_type} agent ({short_model})"}
        try:
            save_workflow(
                workflow_id,
                status=wf["status"],
                steps_completed=wf["steps_completed"],
                steps_total=wf["steps_total"],
                current_step=wf.get("current_step"),
            )
        except Exception as _pe:
            logger.debug("_run_orchestration_bg: save_workflow (step 2) failed: %s", _pe)

        # Execute via cap_execute
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

        # Store pattern embedding for future routing
        try:
            from cap.harness.vector_patterns import PatternEmbedder
            _pattern_embedder = PatternEmbedder()
            if _pattern_embedder.is_available:
                import hashlib
                pattern_id = hashlib.sha256(task[:500].encode()).hexdigest()[:32]
                _pattern_embedder.embed_pattern(pattern_id, task[:500])
        except Exception as exc:
            logger.debug("Pattern embedding storage failed: %s", exc)

        # Enrich result with routing metadata
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
                "routing_method": routing_method,
            }
            wf["result"] = result_payload
        except (json.JSONDecodeError, IndexError, AttributeError):
            try:
                wf["result"] = json.loads(result_contents[0].text) if result_contents else None
            except Exception:
                wf["result"] = {"raw": str(result_contents)} if result_contents else None

        wf["steps_completed"] = wf["steps_total"]
        wf["current_step"] = {"phase": "done", "description": "Completed"}
        wf["status"] = "completed"
        wf["duration_ms"] = int((time.time() - t0) * 1000)
        wf["cost_usd"] = wf["result"].get("cost_usd") if wf["result"] else None
        wf["completed_at"] = time.time()
        try:
            save_workflow(
                workflow_id,
                status=wf["status"],
                steps_completed=wf["steps_completed"],
                steps_total=wf["steps_total"],
                current_step=wf.get("current_step"),
                result=wf.get("result"),
            )
        except Exception as _pe:
            logger.debug("_run_orchestration_bg: save_workflow (single complete) failed: %s", _pe)

    except Exception as exc:
        logger.error("_run_orchestration_bg workflow %s failed: %s", workflow_id, exc, exc_info=True)
        wf["status"] = "failed"
        wf["error"] = str(exc)
        wf["duration_ms"] = int((time.time() - t0) * 1000)
        wf["completed_at"] = time.time()
        # Preserve any partial results already stored
        if "result" not in wf:
            wf["result"] = None
        try:
            save_workflow(
                workflow_id,
                status=wf["status"],
                steps_completed=wf.get("steps_completed", 0),
                steps_total=wf.get("steps_total", 0),
                current_step=wf.get("current_step"),
                error=wf.get("error"),
            )
        except Exception as _pe:
            logger.debug("_run_orchestration_bg: save_workflow (failed) failed: %s", _pe)

    finally:
        hb_task.cancel()


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
            description="Route a task to the best specialist agent and execute it end-to-end via AWS Bedrock. Combines routing + execution in a single call. When blocking=false (default for moderate/complex tasks), returns immediately with a workflow_id for polling via cap_result.",
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
                    "blocking": {
                        "type": "boolean",
                        "description": "If true, wait for completion and return full result inline. If false, return immediately with workflow_id. Default: true for simple tasks, false for moderate/complex.",
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="cap_result",
            description="Get the result or progress of a background workflow started by cap_orchestrate (non-blocking mode).",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "The workflow_id returned by cap_orchestrate.",
                    },
                },
                "required": ["workflow_id"],
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
        Tool(
            name="cap_coordinate",
            description=(
                "Explicitly run multi-step coordination: decompose a task into a TaskDAG "
                "and execute all steps with full dependency ordering, parallelism, and shared state. "
                "Use this when you want guaranteed multi-agent coordination even for tasks that "
                "cap_orchestrate might route as single-agent. Returns per-step results plus a "
                "synthesised final response."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to decompose and execute.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to include during decomposition and execution.",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Working directory for file/bash operations.",
                    },
                },
                "required": ["task"],
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
        elif name == "cap_result":
            return await _handle_result(arguments)
        elif name == "cap_resume":
            return await _handle_resume(arguments)
        elif name == "cap_coordinate":
            return await _handle_coordinate(arguments)
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_route(args: dict):
    """Route a task to the appropriate tier, using EmbeddingRouter first."""
    task_description = args["task_description"]
    session_id = args.get("session_id", "unknown")

    # Try embedding-based routing first for agent_type recommendation
    global _embedding_router
    routing_method = "keyword"
    embed_recommendation = None

    try:
        if _embedding_router is None:
            from cap.harness.embed_router import EmbeddingRouter
            _embedding_router = EmbeddingRouter()
        embed_recommendation = _embedding_router.route(task_description)
    except Exception as exc:
        logger.debug("EmbeddingRouter failed in _handle_route: %s", exc)
        embed_recommendation = None

    if embed_recommendation and embed_recommendation.get("confidence", 0) > 0.5:
        routing_method = "embedding"

    # Standard tier routing (complexity-based)
    decision = route(task_description, db, session_id)

    response = {
        "tier": decision.tier.value,
        "reasoning": decision.reasoning,
        "estimated_agents": decision.estimated_agents,
        "estimated_cost": decision.estimated_cost,
        "complexity_score": decision.complexity_score,
        "decision_id": decision.decision_id,
        "routing_method": routing_method,
    }

    # If embedding routing succeeded, include its recommendation
    if embed_recommendation and routing_method == "embedding":
        response["embedding_recommendation"] = {
            "recommended_agent_type": embed_recommendation["recommended_agent_type"],
            "confidence": embed_recommendation["confidence"],
            "model": embed_recommendation.get("model", "sonnet"),
            "alternatives": embed_recommendation.get("alternatives", []),
            "based_on_patterns": embed_recommendation.get("based_on_patterns", 0),
        }

    # Include graph context for relevant nodes/services mentioned in the task.
    graph_context = _query_graph_context(task_description)
    if graph_context:
        response["graph_context"] = graph_context

    return [TextContent(type="text", text=json.dumps(response))]


async def _handle_plan(args: dict):
    """Generate a TaskDAG plan using TaskDecomposer (heuristic + LLM fallback)."""
    task_description = args["task_description"]
    context = args.get("context", {})
    context_str = json.dumps(context) if isinstance(context, dict) else str(context)

    try:
        decomposer = _get_decomposer()
        plan = await decomposer.decompose(
            task=task_description,
            context=context_str,
        )
    except Exception as exc:
        logger.error("_handle_plan: TaskDecomposer failed: %s", exc, exc_info=True)
        return [TextContent(type="text", text=json.dumps({
            "error": f"Planning failed: {exc}",
        }))]

    return [TextContent(type="text", text=json.dumps({
        "workflow_id": plan.workflow_id,
        "steps": [
            {
                "id": s.id,
                "agent_type": s.agent_type,
                "task": s.task,
                "depends_on": s.depends_on,
            }
            for s in plan.steps
        ],
        "parallel_groups": plan.parallel_groups,
        "complexity": plan.complexity,
        "estimated_cost_usd": plan.estimated_cost_usd,
        "planning_cost_usd": plan.planning_cost_usd,
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

    # Check embedding router availability
    embedding_router_available = False
    try:
        global _embedding_router
        if _embedding_router is None:
            from cap.harness.embed_router import EmbeddingRouter
            _embedding_router = EmbeddingRouter()
        # Check if the underlying embedder is functional
        from cap.harness.vector_patterns import PatternEmbedder
        pe = PatternEmbedder()
        embedding_router_available = pe.is_available
    except Exception:
        embedding_router_available = False

    # Background workflow summary
    running_wfs = [
        {
            "workflow_id": wf_id,
            "status": wf["status"],
            "complexity": wf.get("complexity"),
            "steps_completed": wf.get("steps_completed", 0),
            "steps_total": wf.get("steps_total", 0),
            "elapsed_ms": int((time.time() - wf["started_at"]) * 1000) if wf["status"] == "running" else wf.get("duration_ms"),
        }
        for wf_id, wf in _running_workflows.items()
    ]

    # Count persisted active workflows in SQLite
    persisted_active_count = 0
    try:
        persisted_active_count = len(list_active_workflows())
    except Exception as _se:
        logger.debug("_handle_status: list_active_workflows failed: %s", _se)

    return [TextContent(type="text", text=json.dumps({
        "active_agents": [
            {"agent_id": a.agent_id, "agent_type": a.agent_type, "status": a.status, "model": a.model}
            for a in active
        ],
        "active_count": len(active),
        "budget_remaining_usd": remaining,
        "top_spenders": [dataclasses.asdict(s) if dataclasses.is_dataclass(s) else s for s in spenders[:5]] if spenders else [],
        "routing": {
            "primary": "embedding" if embedding_router_available else "keyword",
            "fallback": "keyword",
            "embedding_router_available": embedding_router_available,
        },
        "background_workflows": {
            "total": len(running_wfs),
            "running": sum(1 for w in running_wfs if w["status"] == "running"),
            "completed": sum(1 for w in running_wfs if w["status"] == "completed"),
            "failed": sum(1 for w in running_wfs if w["status"] == "failed"),
            "persisted_active": persisted_active_count,
            "workflows": running_wfs,
        },
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
    """Load harness config from the canonical location (~/.claude-platform/harness-config.json).

    Falls back to .harness/config.json in CWD for backward compat.
    """
    from cap.lib.harness_config import load_harness_config
    try:
        return load_harness_config()
    except Exception:
        pass
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
    """Full agent execution: spawn -> execute -> record -> learn.

    When the caller does not provide explicit context and the knowledge graph
    contains relevant nodes for the task, graph context is automatically
    prepended so the agent has structural knowledge about affected services.
    """
    import os
    from cap.harness.converse_executor import ConverseExecutor
    from cap.harness.agent_store import spawn_agent, record_execution as store_record_execution
    from cap.harness.hooks import hooks_post_task
    from cap.lib.agent_context import SharedState, create_agent_context
    from cap.lib.agent_bus import AgentBus
    import cap.harness.cost_meter as cost_meter

    agent_type = args["agent_type"]
    task = args["task"]
    model_override = args.get("model")
    context = args.get("context")
    max_tokens = int(args.get("max_tokens", 8192))
    workspace = args.get("workspace", "")
    # coordination_session_id is set when this execute is called as part of a
    # coordinated plan (via CoordinationEngine).  When present, the agent gets
    # an AgentContext wired to the shared session.
    coordination_session_id: str | None = args.get("_coordination_session_id")

    # Enrich context with knowledge graph data (< 200 ms, graceful fallback).
    graph_ctx = _query_graph_context(task, workspace=workspace)
    if graph_ctx:
        if context:
            context = f"{graph_ctx}\n\n{context}"
        else:
            context = graph_ctx

    # Load config
    config = _load_harness_config()
    aws_config = config.get("aws", {})
    budget_config = config.get("budget", {})

    profile = os.environ.get("AWS_PROFILE") or aws_config.get("profile")
    region = os.environ.get("AWS_DEFAULT_REGION") or aws_config.get("region", _default_region())
    budget_limit = float(os.environ.get("CAP_DAILY_LIMIT_USD", budget_config.get("daily_limit_usd", 5.0)))

    # Spawn agent record
    try:
        record = spawn_agent(agent_type, model=None)
    except ValueError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    agent_id = record.agent_id

    # When part of a coordinated plan, create a per-agent AgentContext and
    # wire it to the shared session bus so downstream agents can read results.
    agent_ctx = None
    shared_state = None
    bus = None
    if coordination_session_id:
        try:
            shared_state = SharedState(session_id=coordination_session_id)
            bus = AgentBus(session_id=coordination_session_id)
            agent_ctx = create_agent_context(
                agent_id=agent_id,
                agent_type=agent_type,
                task=task,
                workspace=workspace or ".",
                session_id=coordination_session_id,
                shared_state=shared_state,
            )
        except Exception as exc:
            logger.debug("cap_execute: AgentContext init failed (non-fatal): %s", exc)

    # Create executor
    executor = ConverseExecutor(
        profile=profile,
        region=region,
        budget_limit_usd=budget_limit,
        config=config,
    )

    # Execute
    result = executor.execute(
        agent_id=agent_id,
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
            agent_id=agent_id,
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
            agent_id=agent_id,
            execution_id=agent_id,
            success=result.error is None,
            output_summary=(result.response or "")[:500] if result.response else None,
            agent_type=agent_type,
        )
    except Exception as exc:
        logger.warning("cap_execute: hooks_post_task failed: %s", exc)

    # Publish result to shared bus so downstream agents in a coordinated plan
    # can access this agent's output.
    if agent_ctx is not None and shared_state is not None:
        try:
            output_summary = (result.response or "")[:500]
            await agent_ctx.publish(
                topic="result",
                payload={
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "output_summary": output_summary,
                    "cost_usd": result.total_cost_usd,
                    "success": result.error is None,
                },
            )
        except Exception as exc:
            logger.debug("cap_execute: AgentContext publish failed (non-fatal): %s", exc)

    # Build response
    payload = {
        "agent_id": agent_id,
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


def _build_dag_from_decomposer_plan(plan) -> "TaskDAG":
    """Convert a TaskDecomposer TaskPlan into a CoordinationEngine-compatible TaskDAG.

    The two ``TaskStep`` types differ:
    - ``cap.lib.task_decomposer.TaskStep`` has a ``task`` field (the prompt text).
    - ``cap.orchestration.dag.TaskStep`` has a ``description`` field used as the prompt.

    Args:
        plan: A ``cap.lib.task_decomposer.TaskPlan`` instance.

    Returns:
        A ``cap.orchestration.dag.TaskDAG`` with all steps mapped.
    """
    from cap.orchestration.dag import TaskDAG, TaskStep as DAGStep

    dag = TaskDAG()
    for s in plan.steps:
        dag.steps[s.id] = DAGStep(
            id=s.id,
            description=s.task,
            agent_type=s.agent_type,
            depends_on=list(s.depends_on),
        )
    return dag


async def _run_coordination(task: str, context: str, workspace: str, on_step_complete=None) -> list:
    """Decompose *task* and execute via CoordinationEngine.

    Used by both ``_handle_orchestrate`` (for moderate/complex tasks) and
    ``_handle_coordinate`` (explicit multi-step call).

    Args:
        task: Natural-language task description.
        context: Optional context string.
        workspace: Working directory for agent tool calls.
        on_step_complete: Optional callback invoked after each coordination step
            finishes. Signature: ``(steps_done: int, total_steps: int, step_desc: str) -> None``.

    Returns:
        A list containing a single ``TextContent`` with the coordination result JSON.
    """
    import os
    from cap.harness.converse_executor import ConverseExecutor
    from cap.lib.agent_context import SharedState
    from cap.lib.agent_bus import AgentBus
    from cap.lib.coordination_engine import CoordinationEngine

    config = _load_harness_config()
    aws_config = config.get("aws", {})
    budget_config = config.get("budget", {})

    profile = os.environ.get("AWS_PROFILE") or aws_config.get("profile")
    region = os.environ.get("AWS_DEFAULT_REGION") or aws_config.get("region", _default_region())
    budget_limit = float(os.environ.get("CAP_DAILY_LIMIT_USD", budget_config.get("daily_limit_usd", 5.0)))

    decomposer = _get_decomposer()
    plan = await decomposer.decompose(task=task, context=context or "", workspace=workspace or "")

    if plan.step_count <= 1:
        # Decomposer collapsed this to a single step — fall back to single-agent path.
        logger.debug(
            "_run_coordination: decomposer returned %d step(s); delegating to single-agent path",
            plan.step_count,
        )
        single_step = plan.steps[0]
        return await _handle_execute({
            "agent_type": single_step.agent_type,
            "task": single_step.task,
            "context": context,
            "workspace": workspace,
            "_coordination_session_id": plan.workflow_id,
        })

    dag = _build_dag_from_decomposer_plan(plan)
    session_id = plan.workflow_id

    executor = ConverseExecutor(
        profile=profile,
        region=region,
        budget_limit_usd=budget_limit,
        config=config,
    )
    shared = SharedState(session_id=session_id)
    bus = AgentBus(session_id=session_id)
    engine = CoordinationEngine(executor=executor, bus=bus, shared=shared)

    result = await engine.execute_plan(dag, workspace=workspace or "", on_step_complete=on_step_complete)

    payload = {
        "workflow_id": plan.workflow_id,
        "status": result.status,
        "response": result.final_response,
        "steps": [
            {
                "step_id": sr.step_id,
                "agent_type": sr.agent_type,
                "status": sr.status,
                "cost_usd": sr.cost_usd,
                "duration_ms": sr.duration_ms,
                "error": sr.error,
            }
            for sr in result.steps
        ],
        "total_cost_usd": result.total_cost_usd,
        "total_duration_ms": result.total_duration_ms,
        "errors": result.errors,
        "routing": {
            "method": "coordinated",
            "complexity": plan.complexity,
            "step_count": plan.step_count,
        },
    }
    return [TextContent(type="text", text=json.dumps(payload))]


async def _handle_orchestrate(args: dict):
    """Route a task to the best specialist agent and execute it end-to-end.

    Supports both blocking and non-blocking execution modes:
    - blocking=true (explicit or default for simple tasks): awaits inline, returns full result.
    - blocking=false (explicit or default for moderate/complex): spawns background task,
      returns immediately with workflow_id for polling via cap_result.

    Routing tiers:
    - simple complexity  -> single-agent path (unchanged; EmbeddingRouter + keyword)
    - moderate/complex   -> multi-step CoordinationEngine path
    If the new modules are unavailable, falls back to the single-agent path.

    Original single-agent steps (blocking path):
    1. Try EmbeddingRouter (vector-based) FIRST, fall back to keyword routing
    2. Determine best model via hooks_route (embedding + tier)
    3. Execute via ConverseExecutor (real Bedrock call)
    4. Record cost + update trust + store pattern embedding
    5. Return result with full routing metadata
    """
    task = args["task"]
    blocking_param = args.get("blocking")  # None means auto-detect

    # Prune stale workflows on each call (cheap O(n) scan)
    _prune_stale_workflows()

    # Determine complexity to decide default blocking behavior
    try:
        decomposer = _get_decomposer()
        complexity = decomposer._classify_complexity(task)
    except Exception as exc:
        logger.warning("_handle_orchestrate: complexity check failed (%s); defaulting to simple", exc)
        complexity = "simple"

    # Determine effective blocking mode
    if blocking_param is not None:
        blocking = bool(blocking_param)
    else:
        # Auto: simple -> blocking, moderate/complex -> non-blocking
        blocking = complexity in _SIMPLE_COMPLEXITIES

    if blocking:
        # ── BLOCKING PATH (original behavior, unchanged) ──
        return await _handle_orchestrate_blocking(args, complexity)
    else:
        # ── NON-BLOCKING PATH ──
        workflow_id = _generate_workflow_id()

        # Build initial plan summary for immediate response
        plan_summary = {
            "complexity": complexity,
            "execution_mode": "non-blocking",
        }

        # Store workflow metadata
        _running_workflows[workflow_id] = {
            "status": "running",
            "task": task,
            "complexity": complexity,
            "started_at": time.time(),
            "completed_at": None,
            "steps_completed": 0,
            "steps_total": 3 if complexity == "simple" else 4,
            "current_step": {"phase": "initializing", "description": "Starting background execution"},
            "result": None,
            "error": None,
            "duration_ms": None,
            "cost_usd": None,
            "task_ref": None,  # Will hold asyncio.Task reference
        }

        # Spawn background task
        bg_task = asyncio.create_task(_run_orchestration_bg(workflow_id, args))
        _running_workflows[workflow_id]["task_ref"] = bg_task

        # Add a done callback to handle unexpected errors
        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                wf = _running_workflows.get(workflow_id)
                if wf and wf["status"] == "running":
                    wf["status"] = "failed"
                    wf["error"] = "Task was cancelled"
                    wf["completed_at"] = time.time()
            elif t.exception():
                wf = _running_workflows.get(workflow_id)
                if wf and wf["status"] == "running":
                    wf["status"] = "failed"
                    wf["error"] = str(t.exception())
                    wf["completed_at"] = time.time()

        bg_task.add_done_callback(_on_done)

        logger.info(
            "_handle_orchestrate: non-blocking dispatch workflow_id=%s complexity=%s task=%.80s",
            workflow_id,
            complexity,
            task,
        )

        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": "running",
            "plan": plan_summary,
            "message": "Workflow started in background. Poll with cap_result to get progress/results.",
        }))]


async def _handle_orchestrate_blocking(args: dict, complexity: str):
    """Blocking execution path for cap_orchestrate (original behavior preserved)."""
    from cap.harness.hooks import hooks_route
    from cap.harness.agentdb import agentdb_semantic_route

    task = args["task"]
    context = args.get("context")
    workspace = args.get("workspace")

    if complexity in ("moderate", "complex"):
        try:
            logger.info(
                "_handle_orchestrate: complexity=%s — using CoordinationEngine for task: %.80s",
                complexity,
                task,
            )
            return await _run_coordination(task, context or "", workspace or "")
        except Exception as exc:
            logger.warning(
                "_handle_orchestrate: CoordinationEngine failed (%s); falling back to single-agent path",
                exc,
            )

    # Step 1: Determine agent_type — EmbeddingRouter first, keyword fallback
    global _embedding_router
    routing_method = "keyword"
    embed_result = None
    agent_routing = {}

    try:
        if _embedding_router is None:
            from cap.harness.embed_router import EmbeddingRouter
            _embedding_router = EmbeddingRouter()
        embed_result = _embedding_router.route(task)
    except Exception as exc:
        logger.debug("EmbeddingRouter failed in _handle_orchestrate: %s", exc)
        embed_result = None

    if embed_result and embed_result.get("confidence", 0) > 0.5:
        agent_type = embed_result["recommended_agent_type"]
        agent_confidence = embed_result["confidence"]
        short_model = embed_result.get("model", "sonnet")
        routing_method = "embedding"
        agent_routing = embed_result
    else:
        # Fall back to keyword routing
        agent_routing = agentdb_semantic_route(task)
        agent_type = agent_routing.get("recommended_agent_type", "dev")
        agent_confidence = agent_routing.get("confidence", 0.3)
        short_model = None  # will be determined below

    # Step 2: Determine model — if embedding already chose one, prefer it;
    # otherwise use agent's configured default, then hooks_route
    if short_model is None or routing_method != "embedding":
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

    # Step 4: Store pattern embedding for future routing (continuous learning)
    try:
        from cap.harness.vector_patterns import PatternEmbedder
        _pattern_embedder = PatternEmbedder()
        if _pattern_embedder.is_available:
            import hashlib
            pattern_id = hashlib.sha256(task[:500].encode()).hexdigest()[:32]
            _pattern_embedder.embed_pattern(pattern_id, task[:500])
    except Exception as exc:
        logger.debug("Pattern embedding storage failed: %s", exc)

    # Step 5: Enrich result with routing metadata
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
            "routing_method": routing_method,
        }
        return [TextContent(type="text", text=json.dumps(result_payload))]
    except (json.JSONDecodeError, IndexError, AttributeError):
        return result_contents


async def _handle_result(args: dict):
    """Get the result or progress of a background workflow.

    Returns:
    - If running: status, steps_completed, steps_total, current_step
    - If completed: status, result, cost_usd, duration_ms
    - If failed: status, error, partial_results
    - If not found: error message
    """
    workflow_id = args.get("workflow_id")
    if not workflow_id:
        return [TextContent(type="text", text=json.dumps({"error": "workflow_id is required"}))]

    wf = _running_workflows.get(workflow_id)
    if wf is None:
        # Try SQLite fallback before returning not_found
        try:
            mark_failed_stale()
            persisted = load_workflow(workflow_id)
        except Exception as _se:
            logger.debug("_handle_result: SQLite fallback failed: %s", _se)
            persisted = None

        if persisted is None:
            return [TextContent(type="text", text=json.dumps({
                "error": "not_found",
                "message": f"No workflow with id '{workflow_id}'. It may have been pruned (TTL: 1 hour) or never existed.",
            }))]

        # Build response from persisted state
        p_status = persisted["status"]
        try:
            p_result = json.loads(persisted["result_json"]) if persisted.get("result_json") else None
        except (json.JSONDecodeError, TypeError):
            p_result = None
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": p_status,
            "result": p_result,
            "error": persisted.get("error"),
            "steps_completed": persisted.get("steps_completed", 0),
            "steps_total": persisted.get("steps_total", 0),
            "source": "persisted",
        }))]

    status = wf["status"]

    if status == "running":
        elapsed_ms = int((time.time() - wf["started_at"]) * 1000)
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": "running",
            "steps_completed": wf.get("steps_completed", 0),
            "steps_total": wf.get("steps_total", 0),
            "current_step": wf.get("current_step"),
            "complexity": wf.get("complexity"),
            "elapsed_ms": elapsed_ms,
        }))]

    elif status == "completed":
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": "completed",
            "result": wf.get("result"),
            "cost_usd": wf.get("cost_usd"),
            "duration_ms": wf.get("duration_ms"),
            "complexity": wf.get("complexity"),
        }))]

    elif status == "failed":
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": "failed",
            "error": wf.get("error"),
            "partial_results": wf.get("result"),
            "duration_ms": wf.get("duration_ms"),
            "steps_completed": wf.get("steps_completed", 0),
            "steps_total": wf.get("steps_total", 0),
        }))]

    else:
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": workflow_id,
            "status": status,
            "message": "Unknown workflow state",
        }))]


async def _handle_coordinate(args: dict):
    """Explicitly run multi-step coordination for a task.

    Unlike ``cap_orchestrate``, this always goes through the TaskDecomposer and
    CoordinationEngine regardless of detected complexity. Use when the caller
    wants guaranteed multi-agent execution with full DAG coordination.

    Args:
        args: Must contain ``task``. Optional ``context`` and ``workspace``.

    Returns:
        TextContent with coordination result JSON including per-step outcomes
        and a synthesised final_response.
    """
    task = args.get("task")
    if not task:
        return [TextContent(type="text", text=json.dumps({"error": "task is required"}))]

    context = args.get("context", "")
    workspace = args.get("workspace", "")

    try:
        return await _run_coordination(task, context, workspace)
    except Exception as exc:
        logger.error("_handle_coordinate failed: %s", exc, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


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


async def _async_main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for the cap-orchestrator-server console script."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

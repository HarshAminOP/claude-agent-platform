#!/usr/bin/env python3
"""Harness MCP Server — exposes agent lifecycle and execution tools.

Components:
- AgentStore: persistent agent records (SQLite platform.db)
- AgentExecutor: direct Bedrock invocations
- CostMeter: per-agent, per-workflow cost attribution

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cap.harness import agent_store as _store
import cap.harness.swarm as _swarm
from cap.harness.agent_store import (
    spawn_agent,
    get_agent,
    list_agents,
    terminate_agent,
    record_execution as store_record_execution,
    cleanup_stale,
)
from cap.harness.executor import AgentExecutor
import cap.harness.cost_meter as _cost_meter
import cap.harness.hooks as _hooks
import cap.harness.agentdb as _agentdb
import cap.harness.coordination as _coordination
from cap.harness.governance import (
    load_policy as _load_policy,
    check_dangerous as _check_dangerous,
    record_audit as _record_audit,
    _get_audit_conn,
)
from cap.db import get_db
from cap.harness.validation import (
    validate_identifier as _validate_id,
    validate_text as _validate_text,
    sanitize_for_storage as _sanitize,
)

logger = logging.getLogger("cap.harness")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _default_region() -> str:
    """Return the default AWS region from harness config."""
    from cap.lib.harness_config import DEFAULT_AWS_REGION
    return DEFAULT_AWS_REGION


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_executor: AgentExecutor | None = None
_DAILY_LIMIT_USD = float(os.environ.get("CAP_DAILY_LIMIT_USD", "5.0"))

# ---------------------------------------------------------------------------
# Per-turn call counter (enforces max_tool_calls_per_turn from policy)
# ---------------------------------------------------------------------------

_turn_call_count: int = 0
_turn_last_call_ts: float = 0.0
_TURN_IDLE_RESET_S: float = 30.0  # Reset turn counter after 30s of inactivity
_turn_policy: "None | object" = None  # Cached HarnessPolicy for limit


def _get_turn_limit() -> int:
    """Load max_tool_calls_per_turn from governance policy (cached)."""
    global _turn_policy
    if _turn_policy is None:
        _turn_policy = _load_policy()
    return _turn_policy.max_tool_calls_per_turn


def _check_turn_limit() -> str | None:
    """Increment turn counter; return error string if limit exceeded, else None.

    Resets the counter if more than _TURN_IDLE_RESET_S seconds have passed
    since the last call (heuristic for turn boundary).
    """
    global _turn_call_count, _turn_last_call_ts

    now = time.time()
    if now - _turn_last_call_ts > _TURN_IDLE_RESET_S:
        _turn_call_count = 0
    _turn_last_call_ts = now
    _turn_call_count += 1

    limit = _get_turn_limit()
    if _turn_call_count > limit:
        return (
            f"max_tool_calls_per_turn exceeded ({_turn_call_count}/{limit}). "
            "Wait for the current turn to complete or increase the limit in "
            ".harness/mcp-policy.json."
        )
    return None


def _get_executor() -> AgentExecutor:
    global _executor
    if _executor is None:
        _executor = AgentExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region=os.environ.get("AWS_DEFAULT_REGION") or _default_region(),
        )
    return _executor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Complexity-based model auto-selection
# ---------------------------------------------------------------------------

_COMPLEX_KEYWORDS = frozenset({
    "architect", "design", "security", "audit", "review", "refactor",
    "migrate", "analyse", "analyze", "investigate", "diagnose",
})
_SIMPLE_KEYWORDS = frozenset({
    "format", "lint", "rename", "move", "copy", "list", "count",
})


def _auto_select_model(task_description: str) -> str:
    """Pick a model tier based on keywords in the task description."""
    lower = task_description.lower()
    if any(kw in lower for kw in _COMPLEX_KEYWORDS):
        return "claude-opus-4-6"
    if any(kw in lower for kw in _SIMPLE_KEYWORDS):
        return "claude-haiku-4-5"
    return "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = Server("cap-harness")


@mcp.list_tools()
async def list_tools():
    return [
        Tool(
            name="agent_spawn",
            description=(
                "Spawn a new agent record with lifecycle tracking. "
                "If task_description is provided, the model is auto-selected "
                "based on task complexity when model is not explicitly given."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "description": "Agent role (dev, devops, security, sre, code-review, test, docs, optimization, aws-architect)",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override (claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5). Auto-selected when omitted.",
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional config dict (max_tokens, temperature, system_prompt_key).",
                    },
                    "task_description": {
                        "type": "string",
                        "description": "Task description used for auto model selection when model is not specified.",
                    },
                },
                "required": ["agent_type"],
            },
        ),
        Tool(
            name="agent_execute",
            description=(
                "Execute a prompt on an existing agent via AWS Bedrock. "
                "Records token usage and cost. "
                "Returns degraded=true if Bedrock is unavailable. "
                "Set multi_turn=true to use the Converse API with tool use support."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent to execute on.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "User-turn prompt to send to the model.",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system prompt override.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max output tokens (default 4096).",
                        "default": 4096,
                    },
                    "multi_turn": {
                        "type": "boolean",
                        "description": "Use multi-turn Converse API with tool use (default false for backward compat).",
                        "default": False,
                    },
                    "agent_type": {
                        "type": "string",
                        "description": "Agent role hint for system prompt loading (required when multi_turn=true).",
                    },
                },
                "required": ["agent_id", "prompt"],
            },
        ),
        Tool(
            name="agent_status",
            description=(
                "Get agent status. "
                "If agent_id is given returns a single record; "
                "otherwise returns all non-terminated agents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Optional agent UUID. Omit to list all active agents.",
                    },
                },
            },
        ),
        Tool(
            name="agent_terminate",
            description="Terminate an agent by ID, recording an optional reason.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent to terminate.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Termination reason (default: manual).",
                        "default": "manual",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="agent_cost",
            description=(
                "Query cost data. "
                "Pass agent_id for per-agent cost; workflow_id for workflow breakdown; "
                "omit both for today's total + model breakdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent UUID to query cost for.",
                    },
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to query cost for.",
                    },
                },
            },
        ),
        Tool(
            name="agent_health",
            description=(
                "Health summary for the harness: executor availability, "
                "active/stale agent counts, budget remaining, and circuit-breaker state."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="agent_pool",
            description=(
                "Manage a warm pool of pre-spawned agents. "
                "action=spawn: pre-warm N agents of a type. "
                "action=drain: terminate all idle agents of a type. "
                "action=status: show pool stats by type."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["spawn", "drain", "status"],
                        "description": "Pool operation to perform.",
                    },
                    "agent_type": {
                        "type": "string",
                        "description": "Agent type to target (required for spawn/drain).",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of agents to spawn (used with action=spawn, default 1).",
                        "default": 1,
                    },
                },
                "required": ["action"],
            },
        ),
        # ---- hooks tools -------------------------------------------------------
        Tool(
            name="hooks_route",
            description=(
                "Get an intelligent model/tier recommendation for a task before spawning an agent. "
                "Searches past execution patterns for the cheapest successful model on similar prompts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "Full task description to route.",
                    },
                    "agent_type": {
                        "type": "string",
                        "description": "Optional agent type hint.",
                    },
                },
                "required": ["task_description"],
            },
        ),
        Tool(
            name="hooks_pre_task",
            description=(
                "Call BEFORE agent_execute. "
                "Returns relevant KB context, similar past patterns, and a suggested system prompt "
                "derived from learned corrections."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent identifier.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt that will be sent to the agent.",
                    },
                },
                "required": ["agent_id", "prompt"],
            },
        ),
        Tool(
            name="hooks_post_task",
            description=(
                "Call AFTER agent_execute. "
                "Records success/failure to the learning engine, stores the output pattern, "
                "and updates the agent trust score."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent identifier.",
                    },
                    "execution_id": {
                        "type": "string",
                        "description": "Execution identifier (from agent_execute).",
                    },
                    "success": {
                        "type": "boolean",
                        "description": "Whether the task succeeded.",
                    },
                    "output_summary": {
                        "type": "string",
                        "description": "Optional short summary of the output (stored for future retrieval).",
                    },
                },
                "required": ["agent_id", "execution_id", "success"],
            },
        ),
        Tool(
            name="hooks_feedback",
            description=(
                "Record user quality feedback for the learning loop. "
                "quality='good' adjusts trust +0.05; 'bad' adjusts -0.10 and records a correction "
                "pattern; 'neutral' adjusts +0.01."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent identifier.",
                    },
                    "task_hash": {
                        "type": "string",
                        "description": "Hash or identifier of the task being rated.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["good", "bad", "neutral"],
                        "description": "Quality rating.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes (required context for 'bad' ratings).",
                    },
                },
                "required": ["agent_id", "task_hash", "quality"],
            },
        ),
        Tool(
            name="hooks_intelligence",
            description=(
                "Low-level intelligence storage operations: "
                "pattern_store, pattern_search, trajectory_start, trajectory_step, stats."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pattern_store", "pattern_search", "trajectory_start", "trajectory_step", "stats"],
                        "description": "Operation to perform.",
                    },
                    "data": {
                        "type": "object",
                        "description": (
                            "Payload for the action. "
                            "pattern_store: {task_type, prompt_summary, model, agent_type, cost, duration}. "
                            "pattern_search: {query, limit}. "
                            "trajectory_start: {trajectory_id?, agent_id, action}. "
                            "trajectory_step: {trajectory_id, agent_id, action, result, cost_usd?}. "
                            "stats: {} (empty)."
                        ),
                    },
                },
                "required": ["action", "data"],
            },
        ),
        # ---- agentdb tools -----------------------------------------------------
        Tool(
            name="agentdb_pattern_store",
            description=(
                "Store a learned execution pattern. "
                "Skips (deduplicates) if the same prompt was recorded within the last hour. "
                "Returns {pattern_id, deduplicated}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "Task category (e.g. feature, bugfix, refactor)."},
                    "prompt_summary": {"type": "string", "description": "Short summary of the prompt (max 500 chars)."},
                    "model": {"type": "string", "description": "Model used (e.g. claude-sonnet-4-6)."},
                    "agent_type": {"type": "string", "description": "Agent role (e.g. dev, devops)."},
                    "cost_usd": {"type": "number", "description": "Cost in USD for this execution."},
                    "duration_ms": {"type": "integer", "description": "Wall-clock duration in milliseconds."},
                    "success": {"type": "boolean", "description": "Whether the task succeeded (default true).", "default": True},
                    "output_summary": {"type": "string", "description": "Optional short summary of the output."},
                },
                "required": ["task_type", "prompt_summary", "model", "agent_type", "cost_usd", "duration_ms"],
            },
        ),
        Tool(
            name="agentdb_pattern_search",
            description=(
                "Search learned patterns by text similarity. "
                "Uses prompt hash match first, falls back to LIKE. "
                "Optionally filter by task_type. "
                "Returns ranked list of matching patterns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (matched against prompt_summary)."},
                    "task_type": {"type": "string", "description": "Optional task type filter."},
                    "limit": {"type": "integer", "description": "Max results (default 5, max 50).", "default": 5},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="agentdb_reasoning_store",
            description=(
                "Store a reasoning chain — an ordered list of reasoning steps with a conclusion. "
                "Returns {reasoning_id}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Identifier of the agent that produced this reasoning."},
                    "reasoning_chain": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of reasoning step strings.",
                    },
                    "conclusion": {"type": "string", "description": "Final conclusion drawn from the reasoning chain."},
                    "task_hash": {"type": "string", "description": "Optional hash to correlate with a task."},
                },
                "required": ["agent_id", "reasoning_chain", "conclusion"],
            },
        ),
        Tool(
            name="agentdb_reasoning_recall",
            description=(
                "Search the reasoning bank by conclusion text. "
                "Returns matching chains with their step lists."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query matched against conclusions."},
                    "agent_type": {"type": "string", "description": "Optional filter by agent_id."},
                    "limit": {"type": "integer", "description": "Max results (default 3, max 20).", "default": 3},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="agentdb_semantic_route",
            description=(
                "Given a task description, recommend the best agent_type based on past pattern success rates. "
                "Falls back to keyword heuristics when no history exists. "
                "Returns {recommended_agent_type, confidence, based_on_patterns, alternatives}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description to route."},
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="agentdb_hierarchical_recall",
            description=(
                "Search across multiple knowledge tiers simultaneously: "
                "patterns, reasoning, knowledge (knowledge_entries), sessions (session_events). "
                "Returns combined results keyed by tier."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query applied to all tiers."},
                    "tiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["patterns", "reasoning", "knowledge", "sessions"]},
                        "description": "Tiers to search. Defaults to all four.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="agentdb_stats",
            description=(
                "Return aggregate statistics: total patterns, total reasoning chains, "
                "patterns by task type, overall success rate, and average cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ---- observability tools -----------------------------------------------
        Tool(
            name="agent_logs",
            description=(
                "Return per-agent execution history from the execution_ledger. "
                "Most recent entries first. "
                "Returns [{timestamp, model, input_tokens, output_tokens, cost_usd, duration_ms, success, error}]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent whose execution log to retrieve.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of entries to return (default 50).",
                        "default": 50,
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="audit_trail",
            description=(
                "Query the audit_log table for tool-call history. "
                "Filter by tool_name, agent_id, and/or since timestamp (ISO-8601 UTC). "
                "Returns [{timestamp, tool_name, agent_id, input_summary, success}], most recent first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Filter by exact tool name.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Filter by agent ID.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 UTC lower-bound timestamp (e.g. 2026-06-30T00:00:00Z).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of entries to return (default 100).",
                        "default": 100,
                    },
                },
            },
        ),
        # ---- swarm tools -------------------------------------------------------
        Tool(
            name="swarm_init",
            description=(
                "Initialize a swarm — a named group of agents sharing a topology. "
                "topology must be one of: hierarchical, mesh, star, pipeline. "
                "Returns {swarm_id, name, topology, status, max_agents}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "User-friendly label for the swarm.",
                    },
                    "topology": {
                        "type": "string",
                        "enum": ["hierarchical", "mesh", "star", "pipeline"],
                        "description": "Swarm topology (default: hierarchical).",
                        "default": "hierarchical",
                    },
                    "max_agents": {
                        "type": "integer",
                        "description": "Maximum number of agents in this swarm (default 8).",
                        "default": 8,
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional config dict (consensus_mechanism, auto_scaling, leader_agent_id).",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="swarm_status",
            description=(
                "Get swarm record and its agents. "
                "Returns {swarm_id, topology, status, agents, agent_count, active_count}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm.",
                    },
                },
                "required": ["swarm_id"],
            },
        ),
        Tool(
            name="swarm_health",
            description=(
                "Compute health metrics for a swarm: utilization, cost, failures, avg task duration. "
                "Returns {healthy, utilization, total_cost_usd, failed_count, avg_task_duration_ms}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm.",
                    },
                },
                "required": ["swarm_id"],
            },
        ),
        Tool(
            name="swarm_shutdown",
            description=(
                "Terminate a swarm and all its active agents. "
                "reason='completed' sets status=completed; any other value sets status=terminated. "
                "Returns {swarm_id, agents_terminated, final_cost_usd}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm to shut down.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Shutdown reason (default: completed).",
                        "default": "completed",
                    },
                },
                "required": ["swarm_id"],
            },
        ),
        Tool(
            name="swarm_list",
            description=(
                "List all swarms, optionally filtered by status "
                "(running, paused, completed, terminated). "
                "Returns list ordered by created_at DESC."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["running", "paused", "completed", "terminated"],
                        "description": "Optional status filter.",
                    },
                },
            },
        ),
        # ---- coordination tools ------------------------------------------------
        Tool(
            name="coordination_assign",
            description=(
                "Assign the best available agent in a swarm to a task. "
                "Picks an idle agent of the preferred type, or uses semantic routing to pick a type. "
                "Spawns a new agent if none are idle (up to max_agents). "
                "Returns {agent_id, agent_type, model, assigned: true} "
                "or {queued: true, reason: 'swarm full'} when at capacity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm to assign within.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Task description used for semantic routing when preferred_agent_type is not given.",
                    },
                    "preferred_agent_type": {
                        "type": "string",
                        "description": "Optional explicit agent type (dev, devops, security, etc.).",
                    },
                },
                "required": ["swarm_id", "task"],
            },
        ),
        Tool(
            name="coordination_release",
            description=(
                "Mark an agent as idle after it completes a task. "
                "Returns {agent_id, status: 'idle'}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent to release.",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="coordination_balance",
            description=(
                "Analyze load distribution across agent types in a swarm. "
                "Identifies bottleneck types (all busy) and over-provisioned types (all idle). "
                "Returns {balanced, bottlenecks, over_provisioned, by_type, recommendation}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm to analyze.",
                    },
                },
                "required": ["swarm_id"],
            },
        ),
        Tool(
            name="coordination_consensus",
            description=(
                "Simple majority consensus for swarm agents. "
                "Phase 1 — omit votes: creates a pending proposal, returns {proposal_id, status: 'pending'}. "
                "Phase 2 — supply votes ({agent_id: 'approve'|'reject'}): tallies and determines outcome. "
                "Returns {proposal_id, outcome, votes_for, votes_against, total}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swarm_id": {
                        "type": "string",
                        "description": "UUID of the swarm.",
                    },
                    "proposal": {
                        "type": "string",
                        "description": "Human-readable proposal text.",
                    },
                    "votes": {
                        "type": "object",
                        "description": "Optional dict of {agent_id: 'approve'|'reject'}. Omit to create a pending proposal.",
                        "additionalProperties": {"type": "string", "enum": ["approve", "reject"]},
                    },
                },
                "required": ["swarm_id", "proposal"],
            },
        ),
    ]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict):
    # --- Governance: enforce per-turn call limit ---
    turn_err = _check_turn_limit()
    if turn_err is not None:
        _record_audit(name, agent_id=None, input_summary=f"RATE_LIMITED: {turn_err}", success=False)
        return [TextContent(type="text", text=json.dumps({"error": turn_err}))]

    # --- Governance: validate common fields and record audit ---
    agent_id_raw = arguments.get("agent_id")
    if agent_id_raw is not None:
        try:
            arguments["agent_id"] = _validate_id(agent_id_raw, "agent_id")
        except ValueError as exc:
            _record_audit(name, agent_id=None, input_summary=f"REJECTED: {exc}", success=False)
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    for text_field in ("prompt", "task_description", "description", "notes", "output_summary"):
        if text_field in arguments and arguments[text_field] is not None:
            try:
                arguments[text_field] = _validate_text(arguments[text_field], field_name=text_field)
            except ValueError as exc:
                _record_audit(name, agent_id=agent_id_raw, input_summary=f"REJECTED: {exc}", success=False)
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    # Build audit summary from first text field found
    _audit_summary = _sanitize(
        arguments.get("prompt") or arguments.get("task_description") or arguments.get("query") or str(arguments)[:200],
        max_length=200,
    )

    success = True
    try:
        if name == "agent_spawn":
            result = await _handle_spawn(arguments)
        elif name == "agent_execute":
            result = await _handle_execute(arguments)
        elif name == "agent_status":
            result = await _handle_status(arguments)
        elif name == "agent_terminate":
            result = await _handle_terminate(arguments)
        elif name == "agent_cost":
            result = await _handle_cost(arguments)
        elif name == "agent_health":
            result = await _handle_health(arguments)
        elif name == "agent_pool":
            result = await _handle_pool(arguments)
        elif name == "hooks_route":
            result = await _handle_hooks_route(arguments)
        elif name == "hooks_pre_task":
            result = await _handle_hooks_pre_task(arguments)
        elif name == "hooks_post_task":
            result = await _handle_hooks_post_task(arguments)
        elif name == "hooks_feedback":
            result = await _handle_hooks_feedback(arguments)
        elif name == "hooks_intelligence":
            result = await _handle_hooks_intelligence(arguments)
        elif name == "agentdb_pattern_store":
            result = await _handle_agentdb_pattern_store(arguments)
        elif name == "agentdb_pattern_search":
            result = await _handle_agentdb_pattern_search(arguments)
        elif name == "agentdb_reasoning_store":
            result = await _handle_agentdb_reasoning_store(arguments)
        elif name == "agentdb_reasoning_recall":
            result = await _handle_agentdb_reasoning_recall(arguments)
        elif name == "agentdb_semantic_route":
            result = await _handle_agentdb_semantic_route(arguments)
        elif name == "agentdb_hierarchical_recall":
            result = await _handle_agentdb_hierarchical_recall(arguments)
        elif name == "agentdb_stats":
            result = await _handle_agentdb_stats(arguments)
        elif name == "agent_logs":
            result = await _handle_agent_logs(arguments)
        elif name == "audit_trail":
            result = await _handle_audit_trail(arguments)
        elif name == "swarm_init":
            result = await _handle_swarm_init(arguments)
        elif name == "swarm_status":
            result = await _handle_swarm_status(arguments)
        elif name == "swarm_health":
            result = await _handle_swarm_health(arguments)
        elif name == "swarm_shutdown":
            result = await _handle_swarm_shutdown(arguments)
        elif name == "swarm_list":
            result = await _handle_swarm_list(arguments)
        elif name == "coordination_assign":
            result = await _handle_coordination_assign(arguments)
        elif name == "coordination_release":
            result = await _handle_coordination_release(arguments)
        elif name == "coordination_balance":
            result = await _handle_coordination_balance(arguments)
        elif name == "coordination_consensus":
            result = await _handle_coordination_consensus(arguments)
        else:
            success = False
            result = [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
        return result
    except Exception as exc:
        success = False
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    finally:
        _record_audit(name, agent_id=agent_id_raw, input_summary=_audit_summary, success=success)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _handle_spawn(args: dict):
    agent_type = args["agent_type"]
    model = args.get("model")
    config = args.get("config") or {}
    task_description = args.get("task_description")

    # Auto-select model from task complexity when model not explicitly given
    if model is None and task_description:
        model = _auto_select_model(task_description)

    try:
        record = spawn_agent(agent_type, model=model, config=config)
    except ValueError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return [TextContent(type="text", text=json.dumps({
        "agent_id": record.agent_id,
        "agent_type": record.agent_type,
        "model": record.model,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
    }))]


async def _handle_execute(args: dict):
    agent_id = args["agent_id"]
    prompt = args["prompt"]
    system_prompt = args.get("system_prompt")
    max_tokens = int(args.get("max_tokens", 4096))
    multi_turn = bool(args.get("multi_turn", False))
    agent_type_hint = args.get("agent_type")

    record = get_agent(agent_id)
    if record is None:
        return [TextContent(type="text", text=json.dumps({"error": "agent not found"}))]

    # Mark agent as busy before execution
    try:
        _store.update_agent(agent_id, status="busy")
    except KeyError:
        pass

    if multi_turn:
        # Use ConverseExecutor for multi-turn with tool use
        from cap.harness.converse_executor import ConverseExecutor

        converse_executor = ConverseExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region=os.environ.get("AWS_DEFAULT_REGION") or _default_region(),
            budget_limit_usd=_DAILY_LIMIT_USD,
        )

        conv_result = converse_executor.execute(
            agent_id=agent_id,
            agent_type=agent_type_hint or record.agent_type,
            prompt=prompt,
            system_prompt=system_prompt,
            model=record.model,
            max_tokens=max_tokens,
        )

        # Convert to legacy result for recording
        result = conv_result.to_execution_result()

        if result.error and "unavailable" in (result.error or ""):
            try:
                _store.update_agent(agent_id, status="idle")
            except KeyError:
                pass
            return [TextContent(type="text", text=json.dumps({
                "error": "bedrock unavailable",
                "degraded": True,
                "detail": result.error,
            }))]

        # Record execution in cost meter
        try:
            _cost_meter.record_execution(result, agent_type=record.agent_type)
        except Exception as exc:
            logger.warning("cost_meter.record_execution failed: %s", exc)

        # Update agent store counters
        try:
            store_record_execution(
                agent_id=agent_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                result=result.response,
                error=result.error,
            )
        except KeyError:
            pass

        payload: dict = {
            "agent_id": agent_id,
            "response": result.response,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "turns": conv_result.turns,
            "tool_calls": conv_result.tool_calls[:20],
        }
        return [TextContent(type="text", text=json.dumps(payload))]

    # --- Original single-turn path (unchanged) ---
    executor = _get_executor()

    result = executor.execute(
        agent_id=agent_id,
        prompt=prompt,
        system_prompt=system_prompt,
        model=record.model,
        max_tokens=max_tokens,
    )

    if result.error and "unavailable" in result.error:
        # Restore idle status on executor failure
        try:
            _store.update_agent(agent_id, status="idle")
        except KeyError:
            pass
        return [TextContent(type="text", text=json.dumps({
            "error": "bedrock unavailable",
            "degraded": True,
            "detail": result.error,
        }))]

    # Record execution in cost meter
    try:
        _cost_meter.record_execution(result, agent_type=record.agent_type)
    except Exception as exc:
        logger.warning("cost_meter.record_execution failed: %s", exc)

    # Update agent store counters
    try:
        store_record_execution(
            agent_id=agent_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            result=result.response,
            error=result.error,
        )
    except KeyError:
        pass

    # Content guardrail: scan response for dangerous patterns (warn only)
    warnings: list[str] = []
    if result.response:
        matched = _check_dangerous(result.response)
        if matched:
            logger.warning(
                "agent_execute guardrail: dangerous patterns in response for agent %s: %s",
                agent_id,
                matched,
            )
            _record_audit(
                "agent_execute",
                agent_id=agent_id,
                input_summary=f"GUARDRAIL: dangerous patterns detected: {matched}",
                success=True,
            )
            warnings = matched

    payload_st: dict = {
        "agent_id": agent_id,
        "response": result.response,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }
    if warnings:
        payload_st["warnings"] = warnings

    return [TextContent(type="text", text=json.dumps(payload_st))]


async def _handle_status(args: dict):
    agent_id = args.get("agent_id")

    if agent_id:
        record = get_agent(agent_id)
        if record is None:
            return [TextContent(type="text", text=json.dumps({"error": "agent not found", "agent_id": agent_id}))]
        return [TextContent(type="text", text=json.dumps({
            "agent_id": record.agent_id,
            "agent_type": record.agent_type,
            "status": record.status,
            "model": record.model,
            "task_count": record.task_count,
            "total_input_tokens": record.total_input_tokens,
            "total_output_tokens": record.total_output_tokens,
            "total_cost_usd": record.total_cost_usd,
            "created_at": record.created_at.isoformat(),
            "last_active": record.last_active.isoformat(),
            "last_error": record.last_error,
        }))]

    # All non-terminated agents
    active = [r for r in list_agents() if r.status != "terminated"]
    return [TextContent(type="text", text=json.dumps({
        "agents": [
            {
                "agent_id": r.agent_id,
                "agent_type": r.agent_type,
                "status": r.status,
                "model": r.model,
                "task_count": r.task_count,
                "created_at": r.created_at.isoformat(),
                "last_active": r.last_active.isoformat(),
            }
            for r in active
        ],
        "count": len(active),
    }))]


async def _handle_terminate(args: dict):
    agent_id = args["agent_id"]
    reason = args.get("reason", "manual")

    try:
        record = terminate_agent(agent_id, reason=reason)
    except KeyError:
        return [TextContent(type="text", text=json.dumps({"error": "agent not found", "agent_id": agent_id}))]

    return [TextContent(type="text", text=json.dumps({
        "agent_id": record.agent_id,
        "status": record.status,
        "reason": reason,
        "terminated_at": _now(),
    }))]


async def _handle_cost(args: dict):
    agent_id = args.get("agent_id")
    workflow_id = args.get("workflow_id")

    if agent_id:
        summary = _cost_meter.get_agent_cost(agent_id)
        return [TextContent(type="text", text=json.dumps({
            "agent_id": summary.agent_id,
            "agent_type": summary.agent_type,
            "total_cost_usd": summary.total_cost_usd,
            "total_tokens": summary.total_tokens,
            "execution_count": summary.execution_count,
        }))]

    if workflow_id:
        summary = _cost_meter.get_workflow_cost(workflow_id)
        return [TextContent(type="text", text=json.dumps({
            "workflow_id": summary.workflow_id,
            "total_cost_usd": summary.total_cost_usd,
            "by_agent_type": summary.by_agent_type,
            "by_model": summary.by_model,
        }))]

    # Today totals + model breakdown
    remaining = _cost_meter.budget_remaining(daily_limit_usd=_DAILY_LIMIT_USD)
    spent = round(_DAILY_LIMIT_USD - remaining, 6)
    breakdown = _cost_meter.get_model_breakdown()
    return [TextContent(type="text", text=json.dumps({
        "today_spent_usd": spent,
        "budget_remaining_usd": remaining,
        "daily_limit_usd": _DAILY_LIMIT_USD,
        "model_breakdown": {
            model: {
                "total_cost_usd": entry.total_cost_usd,
                "total_tokens": entry.total_tokens,
                "execution_count": entry.execution_count,
                "pct_of_total": entry.pct_of_total,
            }
            for model, entry in breakdown.items()
        },
    }))]


async def _handle_health(args: dict):
    executor = _get_executor()
    executor_available = executor.is_available

    active_agents = [r for r in list_agents() if r.status in ("idle", "busy")]
    now = datetime.now(timezone.utc)
    stale_threshold_hours = 24
    stale_agents = [
        r for r in active_agents
        if (now - r.last_active).total_seconds() > stale_threshold_hours * 3600
    ]

    remaining = _cost_meter.budget_remaining(daily_limit_usd=_DAILY_LIMIT_USD)

    # Simple circuit-breaker state: if executor is explicitly False it is open
    if executor_available is False:
        cb_state = "open"
    elif executor_available is True:
        cb_state = "closed"
    else:
        cb_state = "unknown"

    return [TextContent(type="text", text=json.dumps({
        "executor_available": executor_available,
        "model_accessible": executor_available is True,
        "active_agent_count": len(active_agents),
        "stale_agent_count": len(stale_agents),
        "budget_remaining_usd": remaining,
        "daily_limit_usd": _DAILY_LIMIT_USD,
        "circuit_breaker": cb_state,
        "timestamp": _now(),
    }))]


async def _handle_pool(args: dict):
    action = args["action"]
    agent_type = args.get("agent_type")
    count = int(args.get("count", 1))

    if action == "status":
        active = [r for r in list_agents() if r.status != "terminated"]
        by_type: dict[str, dict] = {}
        for r in active:
            entry = by_type.setdefault(r.agent_type, {"idle": 0, "busy": 0, "total": 0})
            entry["total"] += 1
            if r.status in ("idle", "busy"):
                entry[r.status] += 1
        return [TextContent(type="text", text=json.dumps({
            "action": "status",
            "total_active": len(active),
            "by_type": by_type,
        }))]

    if action == "spawn":
        if not agent_type:
            return [TextContent(type="text", text=json.dumps({"error": "agent_type required for action=spawn"}))]
        spawned = []
        errors = []
        for _ in range(count):
            try:
                record = spawn_agent(agent_type)
                spawned.append(record.agent_id)
            except ValueError as exc:
                errors.append(str(exc))
        result: dict = {"action": "spawn", "agent_type": agent_type, "requested": count, "spawned": spawned}
        if errors:
            result["errors"] = errors
        return [TextContent(type="text", text=json.dumps(result))]

    if action == "drain":
        idle = [
            r for r in list_agents(agent_type=agent_type)
            if r.status == "idle"
        ]
        terminated_ids = []
        for r in idle:
            try:
                terminate_agent(r.agent_id, reason="pool_drain")
                terminated_ids.append(r.agent_id)
            except KeyError:
                pass
        return [TextContent(type="text", text=json.dumps({
            "action": "drain",
            "agent_type": agent_type or "all",
            "terminated": len(terminated_ids),
            "agent_ids": terminated_ids,
        }))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown action: {action}"}))]


# ---------------------------------------------------------------------------
# Hooks handlers
# ---------------------------------------------------------------------------

async def _handle_hooks_route(args: dict):
    result = _hooks.hooks_route(
        task_description=args["task_description"],
        agent_type=args.get("agent_type"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_hooks_pre_task(args: dict):
    result = _hooks.hooks_pre_task(
        agent_id=args["agent_id"],
        prompt=args["prompt"],
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_hooks_post_task(args: dict):
    result = _hooks.hooks_post_task(
        agent_id=args["agent_id"],
        execution_id=args["execution_id"],
        success=bool(args["success"]),
        output_summary=args.get("output_summary"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_hooks_feedback(args: dict):
    result = _hooks.hooks_feedback(
        agent_id=args["agent_id"],
        task_hash=args["task_hash"],
        quality=args["quality"],
        notes=args.get("notes"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_hooks_intelligence(args: dict):
    result = _hooks.hooks_intelligence(
        action=args["action"],
        data=args.get("data") or {},
    )
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# AgentDB handlers
# ---------------------------------------------------------------------------

async def _handle_agentdb_pattern_store(args: dict):
    result = _agentdb.agentdb_pattern_store(
        task_type=args["task_type"],
        prompt_summary=args["prompt_summary"],
        model=args["model"],
        agent_type=args["agent_type"],
        cost_usd=float(args["cost_usd"]),
        duration_ms=int(args["duration_ms"]),
        success=bool(args.get("success", True)),
        output_summary=args.get("output_summary"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_pattern_search(args: dict):
    result = _agentdb.agentdb_pattern_search(
        query=args["query"],
        task_type=args.get("task_type"),
        limit=int(args.get("limit", 5)),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_reasoning_store(args: dict):
    result = _agentdb.agentdb_reasoning_store(
        agent_id=args["agent_id"],
        reasoning_chain=args["reasoning_chain"],
        conclusion=args["conclusion"],
        task_hash=args.get("task_hash"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_reasoning_recall(args: dict):
    result = _agentdb.agentdb_reasoning_recall(
        query=args["query"],
        agent_type=args.get("agent_type"),
        limit=int(args.get("limit", 3)),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_semantic_route(args: dict):
    result = _agentdb.agentdb_semantic_route(task=args["task"])
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_hierarchical_recall(args: dict):
    result = _agentdb.agentdb_hierarchical_recall(
        query=args["query"],
        tiers=args.get("tiers"),
    )
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_agentdb_stats(args: dict):
    result = _agentdb.agentdb_stats()
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Observability handlers
# ---------------------------------------------------------------------------

async def _handle_agent_logs(args: dict) -> list:
    """Query execution_ledger for all entries matching agent_id, most recent first."""
    agent_id = args["agent_id"]
    limit = int(args.get("limit", 50))
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    from cap.harness.cost_meter import _ensure_schema

    db = get_db()
    try:
        _ensure_schema(db)
        rows = db.execute(
            """
            SELECT created_at, model, input_tokens, output_tokens,
                   cost_usd, duration_ms, success, error
            FROM execution_ledger
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (agent_id, limit),
        ).fetchall()
    finally:
        db.close()

    entries = [
        {
            "timestamp": row[0],
            "model": row[1],
            "input_tokens": row[2],
            "output_tokens": row[3],
            "cost_usd": row[4],
            "duration_ms": row[5],
            "success": bool(row[6]),
            "error": row[7],
        }
        for row in rows
    ]

    return [TextContent(type="text", text=json.dumps({
        "agent_id": agent_id,
        "count": len(entries),
        "entries": entries,
    }))]


async def _handle_audit_trail(args: dict) -> list:
    """Query audit_log with optional filters for tool_name, agent_id, and since."""
    tool_name_filter = args.get("tool_name")
    agent_id_filter = args.get("agent_id")
    since_raw = args.get("since")
    limit = int(args.get("limit", 100))
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000

    # Convert ISO-8601 since to Unix timestamp
    since_ts: float | None = None
    if since_raw:
        try:
            dt = datetime.fromisoformat(since_raw.rstrip("Z").replace("Z", "+00:00"))
            since_ts = dt.timestamp()
        except (ValueError, TypeError):
            return [TextContent(type="text", text=json.dumps({"error": f"Invalid since timestamp: {since_raw!r}"}))]

    conn = _get_audit_conn()
    try:
        clauses: list[str] = []
        params: list = []

        if tool_name_filter:
            clauses.append("tool_name = ?")
            params.append(tool_name_filter)
        if agent_id_filter:
            clauses.append("agent_id = ?")
            params.append(agent_id_filter)
        if since_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(since_ts)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT timestamp, tool_name, agent_id, input_summary, success
            FROM audit_log
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    entries = [
        {
            "timestamp": datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat(),
            "tool_name": row[1],
            "agent_id": row[2],
            "input_summary": row[3],
            "success": bool(row[4]),
        }
        for row in rows
    ]

    return [TextContent(type="text", text=json.dumps({
        "count": len(entries),
        "entries": entries,
    }))]


# ---------------------------------------------------------------------------
# Swarm handlers
# ---------------------------------------------------------------------------

async def _handle_swarm_init(args: dict):
    try:
        result = _swarm.swarm_init(
            name=args["name"],
            topology=args.get("topology", "hierarchical"),
            max_agents=int(args.get("max_agents", 8)),
            config=args.get("config"),
        )
    except (ValueError, KeyError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_swarm_status(args: dict):
    try:
        result = _swarm.swarm_status(swarm_id=args["swarm_id"])
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_swarm_health(args: dict):
    try:
        result = _swarm.swarm_health(swarm_id=args["swarm_id"])
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_swarm_shutdown(args: dict):
    try:
        result = _swarm.swarm_shutdown(
            swarm_id=args["swarm_id"],
            reason=args.get("reason", "completed"),
        )
    except KeyError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_swarm_list(args: dict):
    try:
        result = _swarm.swarm_list(status=args.get("status"))
    except ValueError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Coordination handlers
# ---------------------------------------------------------------------------

async def _handle_coordination_assign(args: dict):
    try:
        result = _coordination.coordination_assign(
            swarm_id=args["swarm_id"],
            task=args["task"],
            preferred_agent_type=args.get("preferred_agent_type"),
        )
    except (KeyError, ValueError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_coordination_release(args: dict):
    try:
        result = _coordination.coordination_release(agent_id=args["agent_id"])
    except (KeyError, ValueError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_coordination_balance(args: dict):
    try:
        result = _coordination.coordination_balance(swarm_id=args["swarm_id"])
    except (KeyError, ValueError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_coordination_consensus(args: dict):
    try:
        result = _coordination.coordination_consensus(
            swarm_id=args["swarm_id"],
            proposal=args["proposal"],
            votes=args.get("votes"),
        )
    except (KeyError, ValueError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

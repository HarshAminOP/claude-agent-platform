#!/usr/bin/env python3
"""Backlog & Decision MCP Server.

Provides tools for:
- Persistent task backlog (create, claim, complete, verify)
- Structured decision cards (propose, resolve)
- Inter-agent disagreement protocol (raise, resolve, override)

CRITICAL: stdout is reserved for MCP JSON-RPC. All logging goes to stderr.
"""

import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.backlog import (
    BacklogTask, TaskStatus, TaskPriority, AcceptanceCriterion,
    init_backlog_table, create_task, get_task, claim_next_task,
    complete_task, verify_criteria, list_tasks, update_task, backlog_stats,
)
from lib.decision_cards import (
    DecisionCard, DecisionStatus, Option, RiskLevel,
    init_decision_cards_table, save_card, resolve_card, list_cards,
)
from lib.disagreement import (
    Conflict, ConflictSeverity, ConflictStatus, ConflictSide, Resolution,
    init_conflicts_table, raise_conflict, resolve_conflict,
    override_conflict, list_conflicts, get_blocking_conflicts,
)
from lib.reasoning_traces import (
    ReasoningTrace, ReasoningStep,
    init_traces_table, record_trace, get_trace, list_traces, explain_decision,
)
from lib.blast_radius import assess_blast_radius
from lib.autonomy import (
    init_autonomy_table, get_autonomy_level, should_ask_approval,
    record_outcome, list_autonomy_levels, reset_autonomy,
)
from lib.db_init import create_database, _open_existing

logger = logging.getLogger("cap.backlog")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DATA_DIR = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform"))) / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "backlog.db"

if DB_PATH.exists():
    db = _open_existing(DB_PATH)
else:
    db = create_database(DB_PATH)

init_backlog_table(db)
init_decision_cards_table(db)
init_conflicts_table(db)
init_traces_table(db)
init_autonomy_table(db)

server = Server("cap-backlog")


@server.list_tools()
async def handle_list_tools():
    return [
        # ── Backlog tools ─────────────────────────────────────────────────────
        Tool(
            name="backlog_create",
            description="Create a new backlog task with optional acceptance criteria.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "default": "medium"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Task IDs this depends on"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
                    },
                    "workflow_id": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "created_by": {"type": "string"},
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="backlog_claim",
            description="Claim the next ready task (highest priority, oldest first). Skips tasks with unmet deps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent claiming the task"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Filter by labels"},
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="backlog_complete",
            description="Mark a task as done (or failed). Triggers acceptance criteria check if criteria exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "output": {"type": "string", "description": "Task output/result"},
                    "status": {"type": "string", "enum": ["done", "blocked", "cancelled"], "default": "done"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="backlog_verify",
            description="Verify a specific acceptance criterion on a task (scrum-master use).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "criterion_index": {"type": "integer"},
                    "verified": {"type": "boolean", "default": True},
                    "verified_by": {"type": "string"},
                },
                "required": ["task_id", "criterion_index", "verified_by"],
            },
        ),
        Tool(
            name="backlog_list",
            description="List backlog tasks with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["backlog", "ready", "in_progress", "in_review", "blocked", "done", "cancelled"]},
                    "workflow_id": {"type": "string"},
                    "assigned_to": {"type": "string"},
                    "limit": {"type": "integer", "default": 50, "maximum": 200},
                },
            },
        ),
        Tool(
            name="backlog_stats",
            description="Get backlog statistics: completion %, blocked count, by-status breakdown.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
            },
        ),
        Tool(
            name="backlog_update",
            description="Update task fields (status, priority, assignment, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["backlog", "ready", "in_progress", "in_review", "blocked", "done", "cancelled"]},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "assigned_to": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "error": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        # ── Decision card tools ───────────────────────────────────────────────
        Tool(
            name="decision_propose",
            description="Propose a decision card with options and tradeoffs for PO review.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "context": {"type": "string", "description": "Background context for the decision"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "tradeoffs": {"type": "object"},
                                "risk": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                                "estimated_effort": {"type": "string"},
                                "recommended": {"type": "boolean"},
                            },
                            "required": ["label", "description"],
                        },
                        "minItems": 2,
                        "maxItems": 5,
                    },
                    "recommendation_index": {"type": "integer", "description": "Index of recommended option (0-based)"},
                    "recommendation_rationale": {"type": "string"},
                    "domain": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                    "deadline": {"type": "string", "description": "ISO date deadline for decision"},
                },
                "required": ["title", "options"],
            },
        ),
        Tool(
            name="decision_resolve",
            description="PO resolves a decision card by choosing an option.",
            inputSchema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "string"},
                    "chosen_option": {"type": "integer", "description": "Index of chosen option (0-based)"},
                    "status": {"type": "string", "enum": ["approved", "rejected", "deferred"], "default": "approved"},
                    "notes": {"type": "string"},
                },
                "required": ["card_id", "chosen_option"],
            },
        ),
        Tool(
            name="decision_list",
            description="List decision cards with optional status/workflow filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "approved", "rejected", "deferred", "superseded"]},
                    "workflow_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        # ── Disagreement tools ────────────────────────────────────────────────
        Tool(
            name="conflict_raise",
            description=(
                "Raise an inter-agent disagreement. Blocking conflicts auto-escalate to PO. "
                "Use when agents disagree on approach (e.g., security blocks devops)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["advisory", "warning", "blocking"]},
                    "side_a": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "agent_type": {"type": "string"},
                            "position": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                            "risk_assessment": {"type": "string"},
                            "proposed_action": {"type": "string"},
                        },
                        "required": ["agent_id", "position"],
                    },
                    "side_b": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "agent_type": {"type": "string"},
                            "position": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                            "risk_assessment": {"type": "string"},
                            "proposed_action": {"type": "string"},
                        },
                        "required": ["agent_id", "position"],
                    },
                    "workflow_id": {"type": "string"},
                    "phase": {"type": "string"},
                },
                "required": ["title", "severity", "side_a", "side_b"],
            },
        ),
        Tool(
            name="conflict_resolve",
            description="Resolve a conflict with a decision.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conflict_id": {"type": "string"},
                    "resolution": {"type": "string", "enum": ["side_a_wins", "side_b_wins", "compromise", "deferred", "overridden_by_po"]},
                    "notes": {"type": "string"},
                    "resolved_by": {"type": "string", "default": "po"},
                },
                "required": ["conflict_id", "resolution"],
            },
        ),
        Tool(
            name="conflict_override",
            description="PO override — force through despite blocking agent objection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conflict_id": {"type": "string"},
                    "notes": {"type": "string", "description": "Why the PO is overriding"},
                },
                "required": ["conflict_id"],
            },
        ),
        Tool(
            name="conflict_list",
            description="List conflicts with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "escalated", "resolved", "overridden"]},
                    "workflow_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["advisory", "warning", "blocking"]},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        Tool(
            name="conflict_blocking",
            description="Get all unresolved blocking conflicts for a workflow (checks before proceeding).",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
        # ── Reasoning traces ──────────────────────────────────────────────────
        Tool(
            name="trace_record",
            description="Record a reasoning trace — why an agent took a specific action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                    "action": {"type": "string", "description": "What was done"},
                    "decision": {"type": "string", "description": "What was decided"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "evidence": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "number"},
                                "alternatives_considered": {"type": "array", "items": {"type": "string"}},
                                "rejected_reason": {"type": "string"},
                            },
                            "required": ["description"],
                        },
                    },
                    "context_used": {"type": "array", "items": {"type": "string"}},
                    "tools_invoked": {"type": "array", "items": {"type": "string"}},
                    "files_modified": {"type": "array", "items": {"type": "string"}},
                    "duration_ms": {"type": "integer"},
                    "tokens_used": {"type": "integer"},
                    "model": {"type": "string"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="trace_explain",
            description="Explain why something was done — search reasoning traces for a workflow/action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "action": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        ),
        # ── Blast radius ──────────────────────────────────────────────────────
        Tool(
            name="blast_radius",
            description="Assess blast radius of a proposed change before executing it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "What is being changed (file, service, module name)"},
                    "change_type": {"type": "string", "enum": ["read", "modify", "delete", "create", "refactor"], "default": "modify"},
                    "workspace": {"type": "string"},
                },
                "required": ["target"],
            },
        ),
        # ── Progressive autonomy ──────────────────────────────────────────────
        Tool(
            name="autonomy_check",
            description="Check if an action needs PO approval based on earned trust level.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string"},
                    "action_type": {"type": "string"},
                    "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"], "default": "low"},
                },
                "required": ["agent_type", "action_type"],
            },
        ),
        Tool(
            name="autonomy_record",
            description="Record an action outcome (success/failure) to update trust level.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string"},
                    "action_type": {"type": "string"},
                    "success": {"type": "boolean"},
                    "details": {"type": "string"},
                },
                "required": ["agent_type", "action_type", "success"],
            },
        ),
        Tool(
            name="autonomy_levels",
            description="List current autonomy levels for all agent/action pairs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string"},
                },
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict):
    try:
        handler = _HANDLERS.get(name)
        if not handler:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
        return await handler(arguments)
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_backlog_create(args: dict):
    criteria = [
        AcceptanceCriterion(description=c["description"])
        for c in args.get("acceptance_criteria", [])
    ]
    task = BacklogTask(
        title=args["title"],
        description=args.get("description", ""),
        priority=TaskPriority(args.get("priority", "medium")),
        status=TaskStatus.ready,
        labels=args.get("labels", []),
        depends_on=args.get("depends_on", []),
        acceptance_criteria=criteria,
        workflow_id=args.get("workflow_id", ""),
        parent_id=args.get("parent_id"),
        created_by=args.get("created_by", ""),
    )
    created = create_task(db, task)
    logger.info("Task created: %s — %s", created.id, created.title)
    return [TextContent(type="text", text=json.dumps(created.to_dict()))]


async def _handle_backlog_claim(args: dict):
    agent_id = args["agent_id"]
    labels = args.get("labels")
    task = claim_next_task(db, agent_id, labels)
    if not task:
        return [TextContent(type="text", text=json.dumps({"status": "no_tasks_available"}))]
    logger.info("Task claimed: %s by %s", task.id, agent_id)
    return [TextContent(type="text", text=json.dumps(task.to_dict()))]


async def _handle_backlog_complete(args: dict):
    task_id = args["task_id"]
    output = args.get("output", "")
    status = TaskStatus(args.get("status", "done"))
    task = complete_task(db, task_id, output=output, status=status)
    if not task:
        return [TextContent(type="text", text=json.dumps({"error": "Task not found"}))]

    result = task.to_dict()
    if task.acceptance_criteria and not task.all_criteria_met:
        result["needs_verification"] = True
        result["unverified_criteria"] = [
            {"index": i, "description": c.description}
            for i, c in enumerate(task.acceptance_criteria) if not c.verified
        ]
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_backlog_verify(args: dict):
    task = verify_criteria(
        db,
        task_id=args["task_id"],
        criterion_index=args["criterion_index"],
        verified_by=args["verified_by"],
        verified=args.get("verified", True),
    )
    if not task:
        return [TextContent(type="text", text=json.dumps({"error": "Task or criterion not found"}))]
    result = task.to_dict()
    result["all_criteria_met"] = task.all_criteria_met
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_backlog_list(args: dict):
    status = TaskStatus(args["status"]) if args.get("status") else None
    tasks = list_tasks(
        db,
        status=status,
        workflow_id=args.get("workflow_id"),
        assigned_to=args.get("assigned_to"),
        limit=args.get("limit", 50),
    )
    return [TextContent(type="text", text=json.dumps({"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}))]


async def _handle_backlog_stats(args: dict):
    stats = backlog_stats(db, workflow_id=args.get("workflow_id"))
    return [TextContent(type="text", text=json.dumps(stats))]


async def _handle_backlog_update(args: dict):
    task = get_task(db, args["task_id"])
    if not task:
        return [TextContent(type="text", text=json.dumps({"error": "Task not found"}))]
    if "status" in args:
        task.status = TaskStatus(args["status"])
    if "priority" in args:
        task.priority = TaskPriority(args["priority"])
    if "assigned_to" in args:
        task.assigned_to = args["assigned_to"]
    if "labels" in args:
        task.labels = args["labels"]
    if "error" in args:
        task.error = args["error"]
    update_task(db, task)
    return [TextContent(type="text", text=json.dumps(task.to_dict()))]


async def _handle_decision_propose(args: dict):
    options = [
        Option(
            label=o["label"],
            description=o["description"],
            tradeoffs=o.get("tradeoffs", {}),
            risk=RiskLevel(o.get("risk", "low")),
            estimated_effort=o.get("estimated_effort", ""),
            recommended=o.get("recommended", False),
        )
        for o in args["options"]
    ]
    card = DecisionCard(
        title=args["title"],
        context=args.get("context", ""),
        options=options,
        recommendation_index=args.get("recommendation_index", -1),
        recommendation_rationale=args.get("recommendation_rationale", ""),
        domain=args.get("domain", ""),
        agent_id=args.get("agent_id", ""),
        workflow_id=args.get("workflow_id", ""),
        deadline=args.get("deadline"),
    )
    save_card(db, card)
    logger.info("Decision card proposed: %s — %s", card.id, card.title)
    return [TextContent(type="text", text=json.dumps(card.to_dict()))]


async def _handle_decision_resolve(args: dict):
    status = DecisionStatus(args.get("status", "approved"))
    card = resolve_card(db, args["card_id"], args["chosen_option"], status, args.get("notes", ""))
    if not card:
        return [TextContent(type="text", text=json.dumps({"error": "Decision card not found"}))]
    return [TextContent(type="text", text=json.dumps(card.to_dict()))]


async def _handle_decision_list(args: dict):
    status = DecisionStatus(args["status"]) if args.get("status") else None
    cards = list_cards(db, status=status, workflow_id=args.get("workflow_id"), limit=args.get("limit", 20))
    return [TextContent(type="text", text=json.dumps({"cards": [c.to_dict() for c in cards], "count": len(cards)}))]


async def _handle_conflict_raise(args: dict):
    side_a_data = args["side_a"]
    side_b_data = args["side_b"]
    conflict = Conflict(
        title=args["title"],
        severity=ConflictSeverity(args["severity"]),
        workflow_id=args.get("workflow_id", ""),
        phase=args.get("phase", ""),
        side_a=ConflictSide(
            agent_id=side_a_data["agent_id"],
            agent_type=side_a_data.get("agent_type", ""),
            position=side_a_data["position"],
            evidence=side_a_data.get("evidence", []),
            risk_assessment=side_a_data.get("risk_assessment", ""),
            proposed_action=side_a_data.get("proposed_action", ""),
        ),
        side_b=ConflictSide(
            agent_id=side_b_data["agent_id"],
            agent_type=side_b_data.get("agent_type", ""),
            position=side_b_data["position"],
            evidence=side_b_data.get("evidence", []),
            risk_assessment=side_b_data.get("risk_assessment", ""),
            proposed_action=side_b_data.get("proposed_action", ""),
        ),
    )
    raised = raise_conflict(db, conflict)
    logger.info("Conflict raised: %s [%s] — %s", raised.id, raised.severity.value, raised.title)
    return [TextContent(type="text", text=json.dumps(raised.to_dict()))]


async def _handle_conflict_resolve(args: dict):
    result = resolve_conflict(
        db,
        conflict_id=args["conflict_id"],
        resolution=Resolution(args["resolution"]),
        resolved_by=args.get("resolved_by", "po"),
        notes=args.get("notes", ""),
    )
    if not result:
        return [TextContent(type="text", text=json.dumps({"error": "Conflict not found"}))]
    return [TextContent(type="text", text=json.dumps(result.to_dict()))]


async def _handle_conflict_override(args: dict):
    result = override_conflict(db, args["conflict_id"], args.get("notes", ""))
    if not result:
        return [TextContent(type="text", text=json.dumps({"error": "Conflict not found"}))]
    return [TextContent(type="text", text=json.dumps(result.to_dict()))]


async def _handle_conflict_list(args: dict):
    status = ConflictStatus(args["status"]) if args.get("status") else None
    severity = ConflictSeverity(args["severity"]) if args.get("severity") else None
    conflicts = list_conflicts(
        db, status=status, workflow_id=args.get("workflow_id"),
        severity=severity, limit=args.get("limit", 20),
    )
    return [TextContent(type="text", text=json.dumps({"conflicts": [c.to_dict() for c in conflicts], "count": len(conflicts)}))]


async def _handle_conflict_blocking(args: dict):
    blockers = get_blocking_conflicts(db, args["workflow_id"])
    return [TextContent(type="text", text=json.dumps({
        "blocking": [c.to_dict() for c in blockers],
        "count": len(blockers),
        "can_proceed": len(blockers) == 0,
    }))]


async def _handle_trace_record(args: dict):
    steps = [
        ReasoningStep(
            description=s["description"],
            evidence=s.get("evidence", []),
            confidence=s.get("confidence", 1.0),
            alternatives_considered=s.get("alternatives_considered", []),
            rejected_reason=s.get("rejected_reason", ""),
        )
        for s in args.get("steps", [])
    ]
    trace = ReasoningTrace(
        agent_id=args.get("agent_id", ""),
        workflow_id=args.get("workflow_id", ""),
        action=args["action"],
        decision=args.get("decision", ""),
        steps=steps,
        context_used=args.get("context_used", []),
        tools_invoked=args.get("tools_invoked", []),
        files_modified=args.get("files_modified", []),
        duration_ms=args.get("duration_ms", 0),
        tokens_used=args.get("tokens_used", 0),
        model=args.get("model", ""),
    )
    record_trace(db, trace)
    return [TextContent(type="text", text=json.dumps({"id": trace.id, "recorded": True}))]


async def _handle_trace_explain(args: dict):
    traces = list_traces(
        db,
        agent_id=args.get("agent_id"),
        workflow_id=args.get("workflow_id"),
        action=args.get("action"),
        limit=args.get("limit", 10),
    )
    return [TextContent(type="text", text=json.dumps({
        "traces": [t.to_dict() for t in traces],
        "count": len(traces),
    }))]


async def _handle_blast_radius(args: dict):
    from lib.db_init import init_knowledge_db
    knowledge_db_path = DATA_DIR / "knowledge.db"
    if not knowledge_db_path.exists():
        return [TextContent(type="text", text=json.dumps({"error": "Knowledge DB not initialized"}))]
    knowledge_db = _open_existing(knowledge_db_path)
    assessment = assess_blast_radius(
        knowledge_db,
        target=args["target"],
        change_type=args.get("change_type", "modify"),
        workspace=args.get("workspace"),
    )
    knowledge_db.close()
    return [TextContent(type="text", text=json.dumps(assessment.to_dict()))]


async def _handle_autonomy_check(args: dict):
    needs_approval = should_ask_approval(
        db, args["agent_type"], args["action_type"], args.get("risk_level", "low")
    )
    al = get_autonomy_level(db, args["agent_type"], args["action_type"])
    result = al.to_dict()
    result["needs_approval"] = needs_approval
    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_autonomy_record(args: dict):
    al = record_outcome(
        db, args["agent_type"], args["action_type"],
        success=args["success"], details=args.get("details", ""),
    )
    return [TextContent(type="text", text=json.dumps(al.to_dict()))]


async def _handle_autonomy_levels(args: dict):
    levels = list_autonomy_levels(db, agent_type=args.get("agent_type"))
    return [TextContent(type="text", text=json.dumps({
        "levels": [al.to_dict() for al in levels],
        "count": len(levels),
    }))]


_HANDLERS = {
    "backlog_create": _handle_backlog_create,
    "backlog_claim": _handle_backlog_claim,
    "backlog_complete": _handle_backlog_complete,
    "backlog_verify": _handle_backlog_verify,
    "backlog_list": _handle_backlog_list,
    "backlog_stats": _handle_backlog_stats,
    "backlog_update": _handle_backlog_update,
    "decision_propose": _handle_decision_propose,
    "decision_resolve": _handle_decision_resolve,
    "decision_list": _handle_decision_list,
    "conflict_raise": _handle_conflict_raise,
    "conflict_resolve": _handle_conflict_resolve,
    "conflict_override": _handle_conflict_override,
    "conflict_list": _handle_conflict_list,
    "conflict_blocking": _handle_conflict_blocking,
    "trace_record": _handle_trace_record,
    "trace_explain": _handle_trace_explain,
    "blast_radius": _handle_blast_radius,
    "autonomy_check": _handle_autonomy_check,
    "autonomy_record": _handle_autonomy_record,
    "autonomy_levels": _handle_autonomy_levels,
}


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

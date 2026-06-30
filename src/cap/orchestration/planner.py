"""
Plan generation for CAP orchestration.

Generates a TaskDAG from a task description by:
1. Decomposing the task into steps with dependency relationships
2. Assigning agent types based on domain keywords
3. Validating the DAG (no cycles, all deps exist)

This module uses heuristic decomposition. In production, the orchestrator
LLM would produce structured plan output. This provides the fallback
and validation logic.

Reference: CAP System Design Section 18 — Plan Generation.
"""

import json
import logging
import re
import sqlite3
import time
import uuid
from typing import Optional

from .dag import TaskDAG, TaskStep, StepState

logger = logging.getLogger(__name__)


# Domain keyword -> agent type mapping
AGENT_TYPE_KEYWORDS: dict[str, list[str]] = {
    "devops": [
        "terraform", "kubernetes", "k8s", "helm", "argocd", "deploy",
        "pipeline", "ci/cd", "cicd", "infrastructure", "eks", "ecs",
        "cloudformation", "cdk", "docker", "container",
    ],
    "security": [
        "security", "iam", "rbac", "auth", "permission", "credential",
        "secret", "encrypt", "compliance", "audit", "vulnerability",
        "policy", "waf", "firewall",
    ],
    "sre": [
        "monitoring", "alerting", "slo", "sli", "observability",
        "prometheus", "grafana", "cloudwatch", "datadog", "pagerduty",
        "incident", "runbook", "reliability",
    ],
    "dev": [
        "implement", "refactor", "code", "function", "class", "module",
        "api", "endpoint", "service", "library", "package", "migrate",
        "upgrade", "feature", "bug", "fix",
    ],
    "test": [
        "test", "spec", "coverage", "integration test", "unit test",
        "e2e", "smoke test", "regression",
    ],
    "code-review": [
        "review", "pr review", "code review", "quality",
    ],
    "docs": [
        "documentation", "readme", "docs", "adr", "runbook",
    ],
}

# Step templates for common task patterns
DECOMPOSITION_PATTERNS: list[dict] = [
    {
        "pattern": r"(migrate|move|transfer).*(service|app|application)",
        "steps": [
            {"suffix": "plan", "desc": "Plan migration strategy and identify dependencies", "agent": "dev"},
            {"suffix": "infra", "desc": "Provision target infrastructure", "agent": "devops"},
            {"suffix": "implement", "desc": "Implement migration changes", "agent": "dev", "depends": ["plan", "infra"]},
            {"suffix": "security", "desc": "Security review of migration", "agent": "security", "depends": ["implement"]},
            {"suffix": "test", "desc": "Run integration tests", "agent": "test", "depends": ["implement"]},
            {"suffix": "deploy", "desc": "Deploy to target environment", "agent": "devops", "depends": ["security", "test"]},
        ],
    },
    {
        "pattern": r"(deploy|release|rollout)",
        "steps": [
            {"suffix": "prepare", "desc": "Prepare deployment artifacts", "agent": "devops"},
            {"suffix": "security", "desc": "Pre-deployment security scan", "agent": "security", "depends": ["prepare"]},
            {"suffix": "deploy", "desc": "Execute deployment", "agent": "devops", "depends": ["security"]},
            {"suffix": "verify", "desc": "Post-deployment verification", "agent": "sre", "depends": ["deploy"]},
        ],
    },
    {
        "pattern": r"(refactor|redesign|rewrite|restructure)",
        "steps": [
            {"suffix": "analyze", "desc": "Analyze current code structure and dependencies", "agent": "dev"},
            {"suffix": "implement", "desc": "Implement refactoring changes", "agent": "dev", "depends": ["analyze"]},
            {"suffix": "test", "desc": "Run tests to verify no regressions", "agent": "test", "depends": ["implement"]},
            {"suffix": "review", "desc": "Code review of changes", "agent": "code-review", "depends": ["implement"]},
        ],
    },
    {
        "pattern": r"(add|create|build|implement).*(feature|endpoint|api|service)",
        "steps": [
            {"suffix": "implement", "desc": "Implement the feature", "agent": "dev"},
            {"suffix": "test", "desc": "Write and run tests", "agent": "test", "depends": ["implement"]},
            {"suffix": "review", "desc": "Code review", "agent": "code-review", "depends": ["implement"]},
            {"suffix": "docs", "desc": "Update documentation", "agent": "docs", "depends": ["implement"]},
        ],
    },
]


def _infer_agent_type(description: str) -> str:
    """Infer the best agent type for a task description based on keywords."""
    desc_lower = description.lower()
    scores: dict[str, int] = {}

    for agent_type, keywords in AGENT_TYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in desc_lower)
        if score > 0:
            scores[agent_type] = score

    if not scores:
        return "dev"  # Default to dev agent

    return max(scores, key=scores.get)


def _generate_step_id(prefix: str = "step") -> str:
    """Generate a short unique step ID."""
    short_id = uuid.uuid4().hex[:8]
    return f"{prefix}_{short_id}"


def _decompose_by_pattern(task_description: str) -> Optional[list[dict]]:
    """Try to match task against known decomposition patterns."""
    task_lower = task_description.lower()
    for pattern_def in DECOMPOSITION_PATTERNS:
        if re.search(pattern_def["pattern"], task_lower):
            return pattern_def["steps"]
    return None


def _decompose_generic(task_description: str) -> list[dict]:
    """
    Generic decomposition for tasks that don't match any pattern.

    Creates: implement -> test -> review pipeline.
    """
    agent_type = _infer_agent_type(task_description)

    steps = [
        {"suffix": "implement", "desc": task_description, "agent": agent_type},
    ]

    # Add review step if not a trivial task
    if len(task_description.split()) > 10:
        steps.append(
            {"suffix": "review", "desc": f"Review: {task_description[:100]}", "agent": "code-review", "depends": ["implement"]}
        )

    return steps


def generate_plan(
    task_description: str,
    context: Optional[dict] = None,
    db: Optional[sqlite3.Connection] = None,
) -> TaskDAG:
    """
    Generate a TaskDAG from a task description.

    Decomposes the task into steps with dependency relationships,
    assigns agent types per step, and validates the resulting DAG.

    Args:
        task_description: Human description of the task to plan.
        context: Optional context dict (workspace, prior outputs, etc.).
        db: Optional SQLite connection for recording the plan.

    Returns:
        A validated TaskDAG ready for execution.
    """
    context = context or {}

    # Try pattern-based decomposition first
    template_steps = _decompose_by_pattern(task_description)
    if template_steps is None:
        template_steps = _decompose_generic(task_description)

    # Build the DAG
    dag = TaskDAG(steps={})
    id_map: dict[str, str] = {}  # suffix -> actual step ID

    for step_def in template_steps:
        suffix = step_def["suffix"]
        step_id = _generate_step_id(suffix)
        id_map[suffix] = step_id

        # Resolve depends_on from suffix to actual IDs
        depends_on = []
        for dep_suffix in step_def.get("depends", []):
            if dep_suffix in id_map:
                depends_on.append(id_map[dep_suffix])

        # Allow context to override agent type
        agent_type = step_def.get("agent", "dev")
        if "agent_overrides" in context and suffix in context["agent_overrides"]:
            agent_type = context["agent_overrides"][suffix]

        # Build description with task context
        description = step_def["desc"]
        if suffix == "implement" and description == task_description:
            pass  # Keep original description
        else:
            description = f"{step_def['desc']} for: {task_description[:200]}"

        step = TaskStep(
            id=step_id,
            description=description,
            agent_type=agent_type,
            depends_on=depends_on,
            affected_files=context.get("affected_files", []),
        )
        dag.steps[step_id] = step

    # Validate: check for cycles
    cycle = dag.detect_cycle()
    if cycle:
        # Break cycle by removing last edge
        last_step = dag.steps[cycle[-1]]
        last_step.depends_on = [d for d in last_step.depends_on if d != cycle[0]]
        logger.warning(
            "Cycle detected in plan (%s), edge removed",
            " -> ".join(cycle),
        )

    # Validate: all deps exist
    for step in dag.steps.values():
        step.depends_on = [
            dep_id for dep_id in step.depends_on
            if dep_id in dag.steps
        ]

    # Record plan to DB if available
    if db is not None:
        _record_plan(dag, task_description, db)

    return dag


def _record_plan(dag: TaskDAG, task_description: str, db: sqlite3.Connection) -> None:
    """Record the generated plan to the task_plans table."""
    workflow_id = f"wf_{uuid.uuid4().hex[:12]}"
    cp = dag.critical_path()
    pf = dag.parallelism_factor()

    try:
        db.execute(
            """INSERT INTO task_plans (workflow_id, plan_json, critical_path, parallelism_factor, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                workflow_id,
                json.dumps(dag.to_dict()),
                json.dumps(cp),
                pf,
                time.time(),
            ),
        )
        db.commit()
    except sqlite3.OperationalError:
        # Table may not exist in test contexts
        logger.debug("Could not record plan to task_plans table")

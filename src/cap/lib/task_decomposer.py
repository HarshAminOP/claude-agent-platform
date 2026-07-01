"""Task decomposer — breaks a complex task into coordinated agent steps.

Two execution paths:
1. Heuristic (fast, no LLM): pattern-matches common task shapes against known templates.
2. LLM-based (accurate): calls haiku to decompose novel/complex tasks into a JSON plan.

The heuristic path is preferred when the task clearly matches a known pattern and complexity
is simple/moderate.  The LLM path is used for novel or complex tasks, or as a fallback when
heuristic decomposition returns None.  If the LLM call fails the heuristic result (or a
minimal single-step fallback) is returned so callers always receive a usable plan.

Plans are persisted to the ``task_plans`` and ``task_steps`` SQLite tables defined in
``cap.db`` (Section 18).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("cap.lib.task_decomposer")

# ---------------------------------------------------------------------------
# Known agent types
# ---------------------------------------------------------------------------

KNOWN_AGENT_TYPES: list[str] = [
    "dev",
    "devops",
    "security",
    "sre",
    "code-review",
    "test",
    "docs",
    "explore",
    "aws-architect",
    "optimization",
    "cicd",
]

# Token estimates per agent type (rough heuristic for cost estimation)
_TOKEN_ESTIMATES: dict[str, int] = {
    "explore": 4_000,
    "dev": 12_000,
    "test": 8_000,
    "code-review": 6_000,
    "security": 8_000,
    "docs": 5_000,
    "devops": 10_000,
    "aws-architect": 8_000,
    "optimization": 8_000,
    "cicd": 6_000,
    "sre": 6_000,
}

# Approximate USD cost per 1k tokens (haiku input) used only for planning cost estimate
_COST_PER_1K_TOKENS = 0.00025


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskStep:
    """A single step in a task plan."""

    id: str
    """Step identifier, e.g. ``"step-1"``."""

    agent_type: str
    """Target agent type, e.g. ``"dev"`` or ``"security"``."""

    task: str
    """The sub-task description passed to the agent."""

    depends_on: list[str] = field(default_factory=list)
    """Step IDs that must complete before this step starts."""

    receives_from: list[str] = field(default_factory=list)
    """Step IDs whose output is explicitly fed into this step."""

    estimated_tokens: int = 0
    """Rough token estimate for budgeting purposes."""

    priority: int = 0
    """Higher value = higher priority when scheduling."""


@dataclass
class TaskPlan:
    """A decomposed task plan with steps and dependencies."""

    workflow_id: str
    """Unique identifier for this planning session."""

    original_task: str
    """The original task string that was decomposed."""

    steps: list[TaskStep]
    """Ordered list of decomposed steps."""

    dependencies: dict[str, list[str]]
    """Adjacency map: step_id -> list of step_ids it depends on."""

    parallel_groups: list[list[str]]
    """BFS layers of step IDs that can execute concurrently within each layer."""

    estimated_cost_usd: float = 0.0
    """Estimated total execution cost in USD."""

    complexity: str = "simple"
    """Task complexity classification: ``"simple"`` | ``"moderate"`` | ``"complex"``."""

    planning_cost_usd: float = 0.0
    """Cost of the LLM planning call itself (0 for heuristic path)."""

    @property
    def step_count(self) -> int:
        """Number of steps in the plan."""
        return len(self.steps)

    def get_step(self, step_id: str) -> Optional[TaskStep]:
        """Return the step with the given ID, or None if not found."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def get_ready_steps(self, completed: set[str]) -> list[TaskStep]:
        """Return steps whose dependencies are all satisfied by *completed*.

        Args:
            completed: Set of step IDs that have already finished.

        Returns:
            List of steps that are eligible to run next.
        """
        ready: list[TaskStep] = []
        for step in self.steps:
            if step.id not in completed and all(d in completed for d in step.depends_on):
                ready.append(step)
        return ready

    def topological_order(self) -> list[str]:
        """Return step IDs in a valid execution order (Kahn's topological sort).

        Returns:
            List of step IDs in dependency-respecting execution order.

        Raises:
            ValueError: If the dependency graph contains a cycle.
        """
        step_ids = {s.id for s in self.steps}
        in_degree: dict[str, int] = {sid: 0 for sid in step_ids}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for step in self.steps:
            for dep in step.depends_on:
                if dep in step_ids:
                    adjacency[dep].append(step.id)
                    in_degree[step.id] += 1

        queue: deque[str] = deque(sid for sid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            current = queue.popleft()
            order.append(current)
            for neighbour in adjacency[current]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if len(order) != len(step_ids):
            raise ValueError(
                f"Cycle detected in task plan '{self.workflow_id}': "
                f"processed {len(order)} of {len(step_ids)} steps."
            )

        return order


# ---------------------------------------------------------------------------
# Heuristic patterns
# ---------------------------------------------------------------------------

# Each entry: (pattern_re, agent_sequence)
# agent_sequence is a list of dicts with keys: agent_type, task_template, priority
# Dependencies are defined as sequential unless a list of lists is provided (parallel).
_HEURISTIC_PATTERNS: list[tuple[re.Pattern, list[dict]]] = [
    (
        re.compile(r"\b(fix\s+bug|debug|diagnose|investigate\s+error|troubleshoot)\b", re.I),
        [
            {"agent_type": "explore", "task_template": "Explore the codebase to locate the bug: {task}", "priority": 10},
            {"agent_type": "dev",     "task_template": "Fix the bug identified by explore: {task}", "priority": 9},
            {"agent_type": "test",    "task_template": "Write regression tests to prevent recurrence: {task}", "priority": 8},
        ],
    ),
    # Deploy/infrastructure must come before the generic "implement" pattern because
    # "deploy new service" would otherwise match the `new\s+service` sub-pattern first.
    (
        re.compile(r"\b(deploy|deployment|infrastructure|provision|terraform|helm|k8s|kubernetes)\b", re.I),
        [
            {"agent_type": "aws-architect", "task_template": "Design the infrastructure architecture: {task}", "priority": 10},
            {"agent_type": "devops",        "task_template": "Implement infrastructure and deployment config: {task}", "priority": 9},
            {"agent_type": "security",      "task_template": "Review infrastructure for security compliance: {task}", "priority": 8},
        ],
    ),
    (
        re.compile(r"\b(add\s+feature|implement|new\s+(endpoint|api|service|function)|build)\b", re.I),
        [
            {"agent_type": "dev",         "task_template": "Implement the feature: {task}", "priority": 10},
            {"agent_type": "test",         "task_template": "Write unit and integration tests: {task}", "priority": 9},
            {"agent_type": "code-review",  "task_template": "Review implementation for correctness and style: {task}", "priority": 8},
        ],
    ),
    (
        re.compile(r"\b(security\s+(audit|review|harden|hardening)|pentest|vulnerability|cve)\b", re.I),
        [
            {"agent_type": "security",  "task_template": "Audit and identify security issues: {task}", "priority": 10},
            {"agent_type": "dev",       "task_template": "Implement security fixes identified in audit: {task}", "priority": 9},
            {"agent_type": "security",  "task_template": "Re-review after fixes are applied: {task}", "priority": 8},
        ],
    ),
    (
        re.compile(r"\b(refactor|clean\s+up|restructure|migrate\s+code|modernise|modernize)\b", re.I),
        [
            {"agent_type": "explore",     "task_template": "Map current code structure and identify refactor targets: {task}", "priority": 10},
            {"agent_type": "dev",         "task_template": "Perform the refactor: {task}", "priority": 9},
            {"agent_type": "test",        "task_template": "Verify behaviour is preserved with tests: {task}", "priority": 8},
            {"agent_type": "code-review", "task_template": "Review refactored code for regressions: {task}", "priority": 7},
        ],
    ),
    (
        re.compile(r"\b(document|documentation|readme|runbook|changelog|adr)\b", re.I),
        [
            {"agent_type": "explore", "task_template": "Gather context and existing docs: {task}", "priority": 10},
            {"agent_type": "docs",    "task_template": "Write documentation: {task}", "priority": 9},
        ],
    ),
    (
        re.compile(r"\b(performance|optimis[ez]|optimiz|speed\s+up|latency|throughput|profil)\b", re.I),
        [
            {"agent_type": "explore",      "task_template": "Profile and identify performance bottlenecks: {task}", "priority": 10},
            {"agent_type": "optimization", "task_template": "Design and implement performance improvements: {task}", "priority": 9},
            {"agent_type": "dev",          "task_template": "Apply optimisations to the codebase: {task}", "priority": 8},
            {"agent_type": "test",         "task_template": "Validate performance gains with benchmarks: {task}", "priority": 7},
        ],
    ),
]


# ---------------------------------------------------------------------------
# TaskDecomposer
# ---------------------------------------------------------------------------


class TaskDecomposer:
    """Decomposes a complex task into coordinated agent steps.

    Two paths:

    1. **Heuristic** (fast, no LLM): pattern-matches common task shapes against
       built-in templates and returns a pre-wired ``TaskPlan``.
    2. **LLM-based** (accurate): calls haiku to decompose novel or complex tasks
       into a JSON plan.  If the LLM call fails the heuristic result is returned
       as a fallback; if neither applies a single-step ``dev`` plan is generated.

    Usage::

        decomposer = TaskDecomposer()
        plan = await decomposer.decompose("Fix the authentication bug in user service")
        for step_id in plan.topological_order():
            step = plan.get_step(step_id)
            ...
    """

    def __init__(
        self,
        executor=None,
        db_path: Optional[str] = None,
    ) -> None:
        """Initialise the decomposer.

        Args:
            executor: A ``ConverseExecutor`` instance.  Created lazily on first
                LLM call if not provided.
            db_path: Override the SQLite database path.  Defaults to the CAP
                default (``~/.cap/cap.db``).
        """
        self._executor = executor
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def decompose(
        self,
        task: str,
        context: str = "",
        workspace: str = "",
    ) -> TaskPlan:
        """Decompose *task* into a ``TaskPlan``.

        Attempts heuristic decomposition first.  Falls back to LLM for complex
        or novel tasks.  If the LLM call fails the heuristic result (or a
        minimal fallback) is returned so callers always get a usable plan.

        Args:
            task: The natural-language task description.
            context: Optional supplementary context (prior findings, workspace info).
            workspace: The current working directory, used for logging.

        Returns:
            A fully populated ``TaskPlan`` with steps, dependencies, parallel
            groups, and a unique ``workflow_id``.
        """
        if not task or not task.strip():
            raise ValueError("task must be a non-empty string")

        complexity = self._classify_complexity(task)
        logger.info(
            "Decomposing task (complexity=%s, workspace=%s): %.120s",
            complexity, workspace or "<unset>", task,
        )

        # Always try the heuristic path first.
        heuristic_plan = self._heuristic_decompose(task, complexity)

        # Use heuristic for simple/moderate tasks that matched a pattern.
        if heuristic_plan is not None and complexity in ("simple", "moderate"):
            logger.debug("Using heuristic plan (%d steps)", heuristic_plan.step_count)
            self._persist_plan(heuristic_plan)
            return heuristic_plan

        # For complex tasks or no pattern match, call the LLM.
        try:
            llm_plan = await self._llm_decompose(task, context)
            self._persist_plan(llm_plan)
            return llm_plan
        except Exception as exc:
            logger.warning(
                "LLM decomposition failed (%s); falling back to heuristic/minimal plan.", exc
            )

        # Use the heuristic plan if we have one; otherwise create a minimal fallback.
        fallback_plan = heuristic_plan or self._minimal_fallback_plan(task)
        self._persist_plan(fallback_plan)
        return fallback_plan

    # ------------------------------------------------------------------
    # Complexity classification
    # ------------------------------------------------------------------

    def _classify_complexity(self, task: str) -> str:
        """Classify task complexity as ``"simple"`` | ``"moderate"`` | ``"complex"``.

        Heuristics used:
        - Word count of the task description.
        - Number of distinct agent keywords detected.
        - Presence of multi-step or multi-service indicators.
        """
        words = task.split()
        word_count = len(words)

        multi_step_indicators = re.compile(
            r"\b(and\s+then|after\s+that|followed\s+by|also|additionally|furthermore|"
            r"multiple|several|all\s+(services?|repos?|components?)|end.to.end|e2e|full.stack)\b",
            re.I,
        )
        has_multi_step = bool(multi_step_indicators.search(task))

        # Count how many distinct agent-type keywords appear
        agent_keywords = {
            "security", "deploy", "infrastructure", "test", "refactor",
            "document", "performance", "optimiz", "review", "migrate",
            "monitor", "alert", "pipeline", "ci", "cd",
        }
        keyword_hits = sum(1 for kw in agent_keywords if kw.lower() in task.lower())

        if word_count > 60 or has_multi_step or keyword_hits >= 3:
            return "complex"
        if word_count > 20 or keyword_hits >= 2:
            return "moderate"
        return "simple"

    # ------------------------------------------------------------------
    # Heuristic decomposition
    # ------------------------------------------------------------------

    def _heuristic_decompose(self, task: str, complexity: str) -> Optional[TaskPlan]:
        """Try to decompose *task* using built-in pattern templates.

        Args:
            task: The task description.
            complexity: Pre-computed complexity label.

        Returns:
            A ``TaskPlan`` if a pattern matched, otherwise ``None``.
        """
        for pattern, template_steps in _HEURISTIC_PATTERNS:
            if pattern.search(task):
                steps = self._build_steps_from_template(template_steps, task)
                deps = {s.id: list(s.depends_on) for s in steps}
                parallel_groups = self._compute_parallel_groups(steps)
                estimated_cost = self._estimate_cost(steps)

                return TaskPlan(
                    workflow_id=self._new_workflow_id(),
                    original_task=task,
                    steps=steps,
                    dependencies=deps,
                    parallel_groups=parallel_groups,
                    estimated_cost_usd=estimated_cost,
                    complexity=complexity,
                    planning_cost_usd=0.0,
                )

        return None

    def _build_steps_from_template(
        self, template_steps: list[dict], task: str
    ) -> list[TaskStep]:
        """Instantiate ``TaskStep`` objects from a heuristic template.

        Steps are chained sequentially: step N depends on step N-1.

        Args:
            template_steps: List of template dicts from ``_HEURISTIC_PATTERNS``.
            task: The original task string used to fill ``{task}`` placeholders.

        Returns:
            List of populated ``TaskStep`` instances.
        """
        steps: list[TaskStep] = []
        for i, tmpl in enumerate(template_steps):
            step_id = f"step-{i + 1}"
            depends_on = [f"step-{i}"] if i > 0 else []
            agent_type = tmpl["agent_type"]
            task_text = tmpl["task_template"].format(task=task)
            steps.append(
                TaskStep(
                    id=step_id,
                    agent_type=agent_type,
                    task=task_text,
                    depends_on=depends_on,
                    receives_from=depends_on,
                    estimated_tokens=_TOKEN_ESTIMATES.get(agent_type, 6_000),
                    priority=tmpl.get("priority", 0),
                )
            )
        return steps

    # ------------------------------------------------------------------
    # LLM decomposition
    # ------------------------------------------------------------------

    async def _llm_decompose(self, task: str, context: str) -> TaskPlan:
        """Decompose *task* using a haiku LLM call.

        The prompt requests a structured JSON plan.  The response is parsed and
        converted into a ``TaskPlan``.  Raises on LLM error or JSON parse failure.

        Args:
            task: The task description.
            context: Optional context string prepended to the prompt.

        Returns:
            A fully constructed ``TaskPlan``.

        Raises:
            RuntimeError: If the LLM call fails or returns unparseable JSON.
        """
        executor = self._get_executor()

        context_section = f"\n\nContext:\n{context.strip()}" if context.strip() else ""
        prompt = (
            f"Decompose the following task into a sequence of sub-tasks for specialist agents."
            f"{context_section}\n\n"
            f"Available agent types: {', '.join(KNOWN_AGENT_TYPES)}\n\n"
            f"Task: {task}\n\n"
            f"Rules:\n"
            f"- Each step must have a unique id like 'step-1', 'step-2', etc.\n"
            f"- depends_on lists the IDs of steps that must complete before this one.\n"
            f"- parallel_groups is a list of lists; each inner list contains step IDs "
            f"that can run concurrently.\n"
            f"- estimated_cost_usd is the rough total execution cost in USD.\n\n"
            f"Return ONLY a JSON object with this exact structure:\n"
            f'{{\n'
            f'  "steps": [\n'
            f'    {{"id": "step-1", "agent_type": "explore", "task": "...", "depends_on": []}},\n'
            f'    {{"id": "step-2", "agent_type": "dev", "task": "...", "depends_on": ["step-1"]}}\n'
            f'  ],\n'
            f'  "parallel_groups": [["step-1"], ["step-2"]],\n'
            f'  "estimated_cost_usd": 0.05,\n'
            f'  "complexity": "moderate"\n'
            f"}}\n\n"
            f"Return ONLY the JSON.  No markdown, no explanation."
        )

        agent_id = f"planner-{uuid.uuid4().hex[:8]}"
        result = executor.execute(
            agent_id=agent_id,
            agent_type="dev",
            prompt=prompt,
            model="haiku",
            max_tokens=4096,
            temperature=0.2,
        )

        if result.error:
            raise RuntimeError(f"LLM planning call failed: {result.error}")

        raw = (result.response or "").strip()
        raw = self._strip_markdown_fences(raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse LLM plan as JSON: {exc}. "
                f"Raw response (first 500 chars): {raw[:500]}"
            ) from exc

        steps = self._parse_steps_from_llm(data.get("steps", []))
        if not steps:
            raise RuntimeError("LLM returned an empty steps list.")

        # Rebuild dependency map from steps (LLM-provided parallel_groups may be wrong)
        deps = {s.id: list(s.depends_on) for s in steps}
        parallel_groups = self._compute_parallel_groups(steps)
        estimated_cost = float(data.get("estimated_cost_usd", self._estimate_cost(steps)))
        complexity = str(data.get("complexity", self._classify_complexity(task)))
        if complexity not in ("simple", "moderate", "complex"):
            complexity = self._classify_complexity(task)

        planning_cost = result.total_cost_usd

        logger.debug(
            "LLM plan: %d steps, complexity=%s, estimated_cost=%.4f, planning_cost=%.6f",
            len(steps), complexity, estimated_cost, planning_cost,
        )

        return TaskPlan(
            workflow_id=self._new_workflow_id(),
            original_task=task,
            steps=steps,
            dependencies=deps,
            parallel_groups=parallel_groups,
            estimated_cost_usd=estimated_cost,
            complexity=complexity,
            planning_cost_usd=planning_cost,
        )

    def _parse_steps_from_llm(self, raw_steps: list[dict]) -> list[TaskStep]:
        """Convert raw LLM step dicts into validated ``TaskStep`` instances.

        Unknown agent types are replaced with ``"dev"`` with a warning.

        Args:
            raw_steps: List of dicts from the parsed LLM JSON.

        Returns:
            List of ``TaskStep`` instances.
        """
        steps: list[TaskStep] = []
        seen_ids: set[str] = set()

        for i, raw in enumerate(raw_steps):
            step_id = str(raw.get("id", f"step-{i + 1}")).strip()
            if not step_id:
                step_id = f"step-{i + 1}"

            # Deduplicate IDs
            if step_id in seen_ids:
                step_id = f"{step_id}-dup{i}"
            seen_ids.add(step_id)

            agent_type = str(raw.get("agent_type", "dev")).strip().lower()
            if agent_type not in KNOWN_AGENT_TYPES:
                logger.warning(
                    "LLM returned unknown agent_type '%s' for step '%s'; using 'dev'.",
                    agent_type, step_id,
                )
                agent_type = "dev"

            task_text = str(raw.get("task", "")).strip()
            if not task_text:
                task_text = f"Execute step {step_id}"

            depends_on = [str(d).strip() for d in raw.get("depends_on", []) if d]

            steps.append(
                TaskStep(
                    id=step_id,
                    agent_type=agent_type,
                    task=task_text,
                    depends_on=depends_on,
                    receives_from=depends_on,
                    estimated_tokens=_TOKEN_ESTIMATES.get(agent_type, 6_000),
                    priority=int(raw.get("priority", 0)),
                )
            )

        return steps

    # ------------------------------------------------------------------
    # Parallel group computation
    # ------------------------------------------------------------------

    def _compute_parallel_groups(self, steps: list[TaskStep]) -> list[list[str]]:
        """Compute BFS topological layers — steps in the same layer can run in parallel.

        Args:
            steps: The list of ``TaskStep`` instances.

        Returns:
            A list of layers, where each layer is a list of step IDs that are
            independent of each other (all their dependencies are in earlier layers).
            Returns ``[[step_id, ...]]`` as a single group if a cycle is detected.
        """
        step_ids = {s.id for s in steps}
        in_degree: dict[str, int] = {sid: 0 for sid in step_ids}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for step in steps:
            for dep in step.depends_on:
                if dep in step_ids:
                    adjacency[dep].append(step.id)
                    in_degree[step.id] += 1

        # BFS layer extraction
        current_layer = [sid for sid, deg in in_degree.items() if deg == 0]
        groups: list[list[str]] = []
        visited: set[str] = set()

        while current_layer:
            groups.append(sorted(current_layer))
            visited.update(current_layer)
            next_layer: list[str] = []
            for sid in current_layer:
                for neighbour in adjacency[sid]:
                    in_degree[neighbour] -= 1
                    if in_degree[neighbour] == 0:
                        next_layer.append(neighbour)
            current_layer = next_layer

        if len(visited) != len(step_ids):
            # Cycle detected — return all steps as a single group as a safe fallback
            logger.warning(
                "Cycle detected in steps while computing parallel groups; "
                "returning single group as fallback."
            )
            return [[s.id for s in steps]]

        return groups

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_plan(self, plan: TaskPlan) -> None:
        """Persist *plan* to ``task_plans`` and ``task_steps`` tables.

        This is best-effort: failures are logged but do not raise so callers
        are never blocked by a persistence error.

        Args:
            plan: The ``TaskPlan`` to persist.
        """
        try:
            from cap.db import get_db
            db = get_db(self._db_path)
            self._write_plan(db, plan)
            db.close()
        except Exception as exc:
            logger.warning("Failed to persist task plan '%s': %s", plan.workflow_id, exc)

    def _write_plan(self, db: sqlite3.Connection, plan: TaskPlan) -> None:
        """Write plan rows inside a single transaction.

        Args:
            db: An open SQLite connection.
            plan: The plan to write.
        """
        now = time.time()
        plan_json = json.dumps(
            {
                "original_task": plan.original_task,
                "complexity": plan.complexity,
                "estimated_cost_usd": plan.estimated_cost_usd,
                "planning_cost_usd": plan.planning_cost_usd,
                "parallel_groups": plan.parallel_groups,
                "step_count": plan.step_count,
            }
        )
        critical_path = ",".join(plan.topological_order())
        parallelism_factor = (
            plan.step_count / max(len(plan.parallel_groups), 1)
        )

        db.execute(
            """
            INSERT INTO task_plans
                (workflow_id, plan_json, critical_path, parallelism_factor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (plan.workflow_id, plan_json, critical_path, parallelism_factor, now),
        )

        for step in plan.steps:
            db.execute(
                """
                INSERT INTO task_steps
                    (id, workflow_id, description, agent_type, depends_on, state)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    f"{plan.workflow_id}:{step.id}",
                    plan.workflow_id,
                    step.task,
                    step.agent_type,
                    json.dumps(step.depends_on),
                ),
            )

        db.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_executor(self):
        """Return the executor, creating a default one lazily if needed."""
        if self._executor is not None:
            return self._executor

        from cap.harness.converse_executor import ConverseExecutor

        self._executor = ConverseExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            budget_limit_usd=float(os.environ.get("CAP_DAILY_LIMIT_USD", "5.0")),
        )
        return self._executor

    @staticmethod
    def _new_workflow_id() -> str:
        """Generate a new unique workflow identifier."""
        return f"wf-{uuid.uuid4().hex}"

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        """Remove surrounding markdown code fences from *text*.

        Args:
            text: Raw LLM response string.

        Returns:
            The string with any leading/trailing markdown fences removed.
        """
        if "```json" in text:
            parts = text.split("```json", 1)
            if len(parts) == 2:
                inner = parts[1].split("```", 1)
                return inner[0].strip()
        if "```" in text:
            parts = text.split("```", 1)
            if len(parts) == 2:
                inner = parts[1].split("```", 1)
                return inner[0].strip()
        return text

    @staticmethod
    def _estimate_cost(steps: list[TaskStep]) -> float:
        """Estimate total execution cost from per-step token estimates.

        Args:
            steps: The steps to estimate cost for.

        Returns:
            Total estimated cost in USD.
        """
        total_tokens = sum(s.estimated_tokens for s in steps)
        return round(total_tokens / 1_000 * _COST_PER_1K_TOKENS, 6)

    def _minimal_fallback_plan(self, task: str) -> TaskPlan:
        """Create a single-step ``dev`` plan used as a last-resort fallback.

        Args:
            task: The original task description.

        Returns:
            A ``TaskPlan`` with a single ``dev`` step.
        """
        step = TaskStep(
            id="step-1",
            agent_type="dev",
            task=task,
            depends_on=[],
            receives_from=[],
            estimated_tokens=_TOKEN_ESTIMATES["dev"],
            priority=10,
        )
        return TaskPlan(
            workflow_id=self._new_workflow_id(),
            original_task=task,
            steps=[step],
            dependencies={"step-1": []},
            parallel_groups=[["step-1"]],
            estimated_cost_usd=self._estimate_cost([step]),
            complexity="simple",
            planning_cost_usd=0.0,
        )

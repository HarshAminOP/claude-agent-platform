"""
DAG-based task decomposition for CAP orchestration.

Provides:
- StepState: lifecycle states for a task step
- TaskStep: a single unit of work within a DAG
- TaskDAG: directed acyclic graph of steps with dependency tracking,
  cycle detection, critical path computation, and ready-step resolution.

Reference: CAP System Design Section 18 — DAG-Based Task Decomposition.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StepState(Enum):
    PENDING = "pending"
    READY = "ready"  # All dependencies satisfied, eligible for dispatch
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"  # Dependency failed, cannot proceed


@dataclass
class TaskStep:
    """A single step within a task DAG."""

    id: str
    description: str
    agent_type: str
    depends_on: list[str] = field(default_factory=list)
    state: StepState = StepState.PENDING
    result: Optional[dict] = None
    affected_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "agent_type": self.agent_type,
            "depends_on": self.depends_on,
            "state": self.state.value,
            "result": self.result,
            "affected_files": self.affected_files,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskStep":
        return cls(
            id=data["id"],
            description=data["description"],
            agent_type=data["agent_type"],
            depends_on=data.get("depends_on", []),
            state=StepState(data.get("state", "pending")),
            result=data.get("result"),
            affected_files=data.get("affected_files", []),
        )


@dataclass
class TaskDAG:
    """
    Directed acyclic graph of task steps.

    Steps are keyed by ID. Dependencies are expressed as lists of step IDs
    that must complete before a step can run. The DAG tracks state transitions
    and provides ready-step resolution, cycle detection, and critical path.
    """

    steps: dict[str, TaskStep] = field(default_factory=dict)

    def get_ready_steps(self) -> list[TaskStep]:
        """
        Return steps whose dependencies are all COMPLETED.

        Transitions matching steps from PENDING to READY.
        """
        ready = []
        for step in self.steps.values():
            if step.state != StepState.PENDING:
                continue
            deps_met = all(
                self.steps[dep_id].state == StepState.COMPLETED
                for dep_id in step.depends_on
                if dep_id in self.steps
            )
            if deps_met:
                step.state = StepState.READY
                ready.append(step)
        return ready

    def detect_cycle(self) -> list[str]:
        """
        Detect cycles using DFS with 3-color marking.

        Returns:
            List of step IDs forming the cycle if found, else empty list.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {sid: WHITE for sid in self.steps}
        path: list[str] = []

        def dfs(node_id: str) -> Optional[list[str]]:
            color[node_id] = GRAY
            path.append(node_id)
            for dep_id in self.steps[node_id].depends_on:
                if dep_id not in self.steps:
                    continue
                if color[dep_id] == GRAY:
                    cycle_start = path.index(dep_id)
                    return path[cycle_start:]
                if color[dep_id] == WHITE:
                    result = dfs(dep_id)
                    if result:
                        return result
            color[node_id] = BLACK
            path.pop()
            return None

        for sid in self.steps:
            if color[sid] == WHITE:
                cycle = dfs(sid)
                if cycle:
                    return cycle
        return []

    def critical_path(self) -> list[str]:
        """
        Compute the longest dependency chain (critical path).

        This represents the minimum sequential execution time
        regardless of parallelism.

        Returns:
            List of step IDs forming the longest chain.
        """
        memo: dict[str, list[str]] = {}

        def longest(sid: str) -> list[str]:
            if sid in memo:
                return memo[sid]
            step = self.steps[sid]
            if not step.depends_on:
                memo[sid] = [sid]
                return memo[sid]
            best: list[str] = []
            for dep_id in step.depends_on:
                if dep_id in self.steps:
                    chain = longest(dep_id)
                    if len(chain) > len(best):
                        best = chain
            memo[sid] = best + [sid]
            return memo[sid]

        all_chains = [longest(sid) for sid in self.steps]
        return max(all_chains, key=len) if all_chains else []

    def mark_failed_dependents(self) -> list[str]:
        """
        Skip all steps that depend on a FAILED step.

        Returns:
            List of step IDs that were skipped.
        """
        skipped = []
        changed = True
        while changed:
            changed = False
            for step in self.steps.values():
                if step.state not in (StepState.PENDING, StepState.READY):
                    continue
                has_failed_dep = any(
                    self.steps[dep_id].state in (StepState.FAILED, StepState.SKIPPED)
                    for dep_id in step.depends_on
                    if dep_id in self.steps
                )
                if has_failed_dep:
                    step.state = StepState.SKIPPED
                    skipped.append(step.id)
                    changed = True
        return skipped

    def is_complete(self) -> bool:
        """True when no steps are PENDING, READY, or RUNNING."""
        active_states = (StepState.PENDING, StepState.READY, StepState.RUNNING)
        return all(s.state not in active_states for s in self.steps.values())

    def parallelism_factor(self) -> float:
        """Ratio of total steps to critical path length (higher = more parallel)."""
        cp_len = len(self.critical_path())
        if cp_len == 0:
            return 0.0
        return len(self.steps) / cp_len

    def to_dict(self) -> dict:
        return {
            "steps": {sid: step.to_dict() for sid, step in self.steps.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskDAG":
        steps = {}
        for sid, step_data in data.get("steps", {}).items():
            steps[sid] = TaskStep.from_dict(step_data)
        return cls(steps=steps)

# ADR-013: DAG-Based Task Decomposition

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP Orchestration Layer — Week 2)

## Context

The orchestrator currently executes agent tasks as linear sequences: plan → agent-1 → agent-2 → ... → agent-N → review. This serialization wastes time when agents have no data dependencies between them. Typical multi-agent workflows (e.g., "deploy new service") contain phases where 3-5 agents can execute in parallel (security review, infra provisioning, CI/CD setup) but are artificially serialized.

**Observed problems with linear execution:**

- A 7-agent workflow that could complete in 3 wall-clock rounds takes 7 rounds instead
- Token budget is consumed on idle wait (agents queued behind unrelated predecessors)
- No way to express "agent-C depends on agent-A output but not agent-B"
- Partial failures block the entire pipeline instead of just downstream dependents

**Key constraints:**
- Concurrency slots are limited (max 8 total, Opus costs 3x)
- Budget enforcement must account for parallel token burn
- Agent outputs may feed into subsequent agent inputs (true data dependencies)
- The orchestrator must be able to visualize execution state at any point

## Decision

**Tasks decompose into directed acyclic graphs (DAGs), not linear lists. Agents execute in parallel where dependency edges allow.**

The orchestrator's planning phase produces a DAG where:
- **Nodes** = agent invocations (type, model, input spec)
- **Edges** = data dependencies (output of node A feeds into input of node B)
- **Roots** = nodes with no incoming edges (can start immediately)
- **Frontier** = set of nodes whose dependencies are all satisfied (ready to execute)

Execution proceeds in waves:
1. Identify the current frontier (all nodes with satisfied deps)
2. Schedule frontier nodes respecting concurrency slot limits
3. As each node completes, propagate its output to dependent nodes
4. Recompute frontier; repeat until all nodes complete or a critical failure occurs

**Key design choices:**

- **DAG representation:** Adjacency list in `workflow_steps` table with a `depends_on` JSON array column
- **Cycle detection:** Validated at plan time using topological sort (Kahn's algorithm); reject plans with cycles
- **Partial failure handling:** Failed node marks all transitive dependents as `blocked`; independent branches continue
- **Dynamic re-planning:** If an agent's output reveals new work, the orchestrator can append nodes to the DAG mid-execution (append-only — never modify completed nodes)
- **Visualization:** `workflow_status` returns the DAG with per-node status, enabling the TUI dashboard to render a live dependency graph

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Linear execution** | Simple, deterministic, easy to debug | 2-3x slower for parallelizable workflows, wastes budget on idle wait | Rejected (current state, insufficient) |
| **Static parallelism (fixed phases)** | Moderate improvement, simple scheduling | Cannot express arbitrary dependencies, over-serializes within phases | Rejected (too rigid) |
| **Full task graph with conditional edges** | Maximum expressiveness | Turing-complete scheduling problem, hard to reason about, potential infinite loops | Rejected (over-engineered) |
| **Actor model (message-passing)** | Naturally concurrent, composable | No global view of progress, hard to enforce budget, debugging is difficult | Rejected (wrong abstraction level) |

## Consequences

### Positive
- **2-3x wall-clock speedup** for typical multi-agent workflows (measured: 7-agent deploy drops from 7 rounds to 3)
- **Fine-grained failure isolation** — failed security review blocks deploy but not docs generation
- **Budget efficiency** — parallel execution finishes faster, reducing per-session overhead
- **Observable** — DAG structure is inspectable via `workflow_status` and TUI dashboard
- **Dynamic** — append-only re-planning allows adaptation without replaying completed work

### Negative
- **Scheduling complexity** — must respect concurrency slots and model-tier weights during frontier scheduling
- **Non-deterministic ordering** — parallel agents may complete in different orders across runs (complicates debugging)
- **State management** — must track per-node status, outputs, and dependency satisfaction atomically
- **Budget estimation harder** — parallel burn rate is less predictable than sequential

## Implementation Notes

**DAG schema extension to `workflow_steps`:**
```sql
ALTER TABLE workflow_steps ADD COLUMN depends_on TEXT DEFAULT '[]';
-- JSON array of step_name values that must complete before this step starts
```

**Frontier computation:**
```python
def compute_frontier(dag: dict[str, Step]) -> list[Step]:
    """Return all steps whose dependencies are fully satisfied."""
    return [
        step for step in dag.values()
        if step.status == "pending"
        and all(dag[dep].status == "completed" for dep in step.depends_on)
    ]
```

**Concurrency-aware scheduling:**
```python
def schedule_frontier(frontier: list[Step], available_slots: int) -> list[Step]:
    """Schedule as many frontier steps as slots allow, respecting weights."""
    scheduled = []
    remaining_slots = available_slots
    for step in sorted(frontier, key=lambda s: s.priority, reverse=True):
        cost = SLOT_WEIGHTS[step.model]  # opus=3, sonnet=1, haiku=0.5
        if cost <= remaining_slots:
            scheduled.append(step)
            remaining_slots -= cost
    return scheduled
```

## Related ADRs

- [ADR-016: Checkpoint Resume](ADR-016-checkpoint-resume.md) — Checkpoints capture DAG state for resume
- [ADR-015: Circuit Breakers](ADR-015-circuit-breakers.md) — Failed nodes trigger circuit breaker checks before scheduling dependents

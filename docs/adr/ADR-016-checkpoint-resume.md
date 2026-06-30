# ADR-016: Checkpoint-Resume Workflow Persistence

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP Orchestration Layer — Week 2)

## Context

Multi-agent workflows can take 5-30 minutes and consume significant token budget. Several failure modes currently cause total loss of progress:

- **Session disconnect:** User closes terminal, SSH drops, laptop sleeps — entire workflow state is lost
- **Budget exhaustion:** Workflow hits budget cap mid-execution — completed agent results are discarded
- **Circuit breaker open:** A dependent agent type is unavailable — workflow fails instead of pausing
- **PO decision pending:** Conflict escalation requires PO input — workflow blocks indefinitely or times out
- **Process crash:** MCP server crash loses in-memory workflow state

In all cases, the user must restart the entire workflow from scratch, re-running agents that already completed successfully. For a 7-agent workflow where agent 5 fails, this means re-spending tokens on agents 1-4 that already produced valid output.

**Key constraints:**
- Checkpoints must capture the full DAG state (which nodes completed, their outputs, current frontier)
- Resume must be idempotent — replaying from checkpoint must produce the same result as continuous execution
- Agent outputs must be serializable (they are text/JSON, so this is straightforward)
- Checkpoint storage must be crash-safe (no partial writes)

## Decision

**Checkpoint at plan-complete and after each agent completion. Resume replays from the last good checkpoint state.**

The orchestrator persists workflow state at two mandatory checkpoints:

1. **Plan checkpoint:** After the DAG is constructed but before any agents execute. Captures: DAG structure, node specs, budget allocation, input parameters.

2. **Agent checkpoint:** After each agent completes successfully. Captures: updated DAG state, completed node output, remaining budget, circuit breaker states.

Resume protocol:
1. Load the latest checkpoint for the workflow
2. Reconstruct the DAG with completed nodes marked as `completed` (their outputs are preserved)
3. Recompute the frontier (pending nodes whose deps are now satisfied)
4. Continue execution from the frontier as if the workflow never stopped

**Key design choices:**

- **Storage:** Checkpoints stored in `workflow_checkpoints` table in `platform.db` as JSON blobs (DAG state + agent outputs)
- **Atomic writes:** Checkpoint write is a single SQLite transaction — either fully written or not at all
- **Retention:** Keep only the latest 2 checkpoints per workflow (older ones pruned after successful newer write)
- **Agent output serialization:** Agent outputs are already text/JSON; stored inline in the checkpoint (typical size: 2-10KB per agent)
- **Resume trigger:** Manual (`workflow resume <id>`) or automatic (on session reconnect, detect paused workflows)
- **Idempotency:** Completed nodes are never re-executed. The orchestrator skips them and uses their stored output.
- **Budget carry-forward:** Remaining budget from the original workflow carries over to the resumed execution

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **No persistence (restart from scratch)** | Zero complexity, no storage overhead | Wastes 100% of completed work on any failure, terrible UX for long workflows | Rejected (current state, unacceptable for 20+ minute workflows) |
| **Checkpoint every N seconds** | Time-based consistency | May miss critical state changes between intervals, or checkpoint mid-agent (inconsistent state) | Rejected (inconsistent snapshots) |
| **Event sourcing (replay all events)** | Full audit trail, can replay to any point | Complex replay logic, non-deterministic agent outputs mean replay diverges, expensive to reconstruct | Rejected (over-engineered, non-deterministic agents) |
| **External workflow engine (Temporal/Step Functions)** | Battle-tested persistence, retry semantics | External dependency, network latency, cost, over-provisioned for our scale | Rejected (wrong scale — our workflows are 5-15 agents, not thousands) |
| **Checkpoint to filesystem (JSON files)** | Simple, inspectable | Race conditions, no atomicity guarantees, cleanup complexity | Rejected (SQLite is already available and crash-safe) |

## Consequences

### Positive
- **Zero wasted work** — completed agent outputs are preserved across any failure mode
- **PO-friendly** — PO can close laptop, resume next morning, and the workflow continues from where it stopped
- **Budget-efficient** — no re-spending tokens on already-completed agents
- **Crash-safe** — SQLite atomic writes guarantee no partial checkpoints
- **Composable** — works naturally with DAG execution (resume = recompute frontier from persisted DAG state)
- **Observable** — checkpoint state is queryable (`workflow_status` shows "resumable from step 4/7")

### Negative
- **Storage overhead** — ~5-20KB per checkpoint (negligible for SQLite, but grows with agent output size)
- **Stale context risk** — if resumed hours later, world state may have changed (e.g., someone manually fixed the issue). Mitigated by freshness check on resume.
- **Non-determinism on resume** — parallel frontier execution may produce different ordering on resume (acceptable — outputs are deterministic per-agent, just ordering varies)
- **Checkpoint frequency tradeoff** — per-agent checkpoint adds ~5ms of write latency per agent completion (acceptable)

## Implementation Notes

**Schema (`platform.db`):**
```sql
CREATE TABLE workflow_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    checkpoint_type TEXT NOT NULL,           -- plan|agent_complete
    step_name TEXT,                          -- which step triggered this checkpoint
    dag_state TEXT NOT NULL,                 -- JSON: full DAG with node statuses
    agent_outputs TEXT NOT NULL,             -- JSON: {step_name: output} for completed nodes
    budget_remaining_usd REAL NOT NULL,
    breaker_states TEXT,                     -- JSON: circuit breaker snapshot
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(workflow_id, checkpoint_type, step_name)
);

CREATE INDEX idx_wc_workflow ON workflow_checkpoints(workflow_id);
```

**Checkpoint write (after agent completion):**
```python
async def checkpoint_after_agent(workflow_id: str, completed_step: str, output: str):
    """Atomic checkpoint after agent completes."""
    dag_state = serialize_dag(workflow.dag)
    agent_outputs = {
        step.name: step.result
        for step in workflow.dag.values()
        if step.status == "completed"
    }
    
    db.execute("""
        INSERT OR REPLACE INTO workflow_checkpoints
        (workflow_id, checkpoint_type, step_name, dag_state, agent_outputs, budget_remaining_usd, breaker_states)
        VALUES (?, 'agent_complete', ?, ?, ?, ?, ?)
    """, (
        workflow_id, completed_step,
        json.dumps(dag_state), json.dumps(agent_outputs),
        workflow.budget_remaining, json.dumps(get_breaker_states()),
    ))
```

**Resume protocol:**
```python
async def resume_workflow(workflow_id: str) -> Workflow:
    """Resume workflow from latest checkpoint."""
    checkpoint = db.execute("""
        SELECT * FROM workflow_checkpoints
        WHERE workflow_id = ?
        ORDER BY created_at DESC LIMIT 1
    """, (workflow_id,)).fetchone()
    
    if not checkpoint:
        raise NoCheckpointError(f"No checkpoint for workflow {workflow_id}")
    
    # Reconstruct DAG
    dag = deserialize_dag(json.loads(checkpoint["dag_state"]))
    
    # Restore agent outputs
    outputs = json.loads(checkpoint["agent_outputs"])
    for step_name, output in outputs.items():
        dag[step_name].result = output
        dag[step_name].status = "completed"
    
    # Freshness check — warn if stale
    age_minutes = (now() - parse(checkpoint["created_at"])).total_seconds() / 60
    if age_minutes > 60:
        logger.warning(f"Resuming from checkpoint {age_minutes:.0f}m old — context may be stale")
    
    # Continue from frontier
    workflow = Workflow(id=workflow_id, dag=dag, budget_remaining=checkpoint["budget_remaining_usd"])
    await execute_dag(workflow)
    return workflow
```

**Resume triggers:**
```bash
# Manual
cap workflow resume <workflow-id>

# Automatic (on session start, check for paused workflows)
cap workflow list --status paused  # shown to user
```

## Related ADRs

- [ADR-013: DAG Execution](ADR-013-dag-execution.md) — Checkpoints capture DAG state; resume recomputes frontier
- [ADR-014: Consensus Protocol](ADR-014-consensus-protocol.md) — Pending PO decisions are preserved in checkpoint
- [ADR-015: Circuit Breakers](ADR-015-circuit-breakers.md) — Breaker states are snapshot in checkpoint for consistent resume

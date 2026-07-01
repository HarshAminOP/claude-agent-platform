# Agent Coordination System

CAP v2 coordinates multi-agent tasks to eliminate silos, prevent duplicate work, enforce dependency ordering, and track cost across all agents in a workflow. This document describes the coordination architecture, task decomposition, and runtime execution model.

## Why Coordination Exists

Without coordination, multi-agent tasks degrade into parallel silos:
- Steps execute out of order, causing dependency failures
- Multiple agents redo the same work
- One agent's findings never reach dependent agents
- Budget tracking becomes per-agent opaque boxes
- Failures in one branch cascade unpredictably

CAP's coordination system ensures:
- Steps execute in correct dependency order
- Independent steps run in parallel for speed
- Output from agent N flows into context for agent M
- Budget is tracked cumulatively across all agents
- Failures cascade correctly (skip dependent steps, not independent ones)

## Complexity-Based Routing

The Router (`src/cap/orchestration/router.py`) determines when coordination kicks in by scoring task complexity on a 0–1 scale. The score determines the execution mode:

| Score Range | Mode | Coordination | When | Execution Model |
|-------------|------|:----------:|:----:|:---:|
| < 0.2 | INLINE | None | 1-line fixes, quick status checks, lookups | Single agent inline, no DAG |
| 0.2 - 0.5 | LIGHTWEIGHT | Minimal | Simple feature in one service, single file refactor | Specialist + review agent sequentially |
| > 0.5 | FULL | Yes | Multi-step tasks, cross-repo changes, infra provisioning | TaskDAG with parallel CoordinationEngine |

**Scoring heuristics** (keyword-based with learned threshold adaptation):
- Multi-file changes: +0.15
- Cross-repo references: +0.20
- Keywords: refactor, security, infra, migration, deprecation: +0.10 each
- Architecture change: +0.25
- Learned modifiers: historical routing decisions adjust thresholds per task pattern

Hard bounds prevent drift: INLINE ceiling 0.30, FULL floor 0.40.

## Task Decomposition

When FULL orchestration is triggered, the task must be decomposed into steps. Two pathways:

**1. Heuristic Patterns** (fast, deterministic)

Common task shapes map to pre-baked DAG templates:
- `bugfix` → research → implement fix → regression test → review
- `feature` → design → implementation → integration test → security review → docs
- `refactor` → analysis → implementation → test → review
- `infra` → design → terraform → cost review → security review → apply
- `incident` → triage → diagnosis → mitigation → remediation → postmortem

**2. LLM Fallback** (novel tasks)

For task types not matching heuristic patterns, the orchestrator LLM decomposes into steps via prompt:

```
Decompose this task into steps for parallel execution:
- Each step should be independent or clearly depend on prior steps
- Include agent type (dev, security, test, sre, etc.)
- Specify dependencies by step ID
- Estimate model tier (haiku/sonnet/opus) based on complexity
```

**Each step produces:**
- `id`: unique step identifier (step-1, step-2, etc.)
- `agent_type`: who executes (dev, security, test, sre, docs, devops, etc.)
- `description`: what the agent should do
- `dependencies`: list of step IDs that must complete first
- `estimated_tier`: model to invoke (haiku for lightweight, sonnet for standard, opus for complex)

**Output**: a directed acyclic graph (DAG) of `TaskStep` nodes.

## TaskDAG Structure

The DAG is the blueprint for execution. Each node is a `TaskStep`:

```python
class TaskStep:
    id: str                           # "step-1"
    agent_type: str                   # "dev", "security", "test", etc.
    description: str                  # What the agent should do
    dependencies: list[str]           # Step IDs that must complete first
    state: TaskStepState              # PENDING, READY, RUNNING, COMPLETED, FAILED, SKIPPED
    result: Optional[str]             # Agent output/findings
    cost: Optional[float]             # Cost of execution in API units
    duration_seconds: Optional[float] # Wall-clock duration
    error: Optional[str]              # Failure reason if FAILED
    execution_model: str              # "opus" | "sonnet" | "haiku"
```

**State Transitions:**
```
PENDING ──depends ok───→ READY ──assigned───→ RUNNING ──done───→ COMPLETED
                           │                      │
                           └─────dep failed─→ SKIPPED
                                              (if dependent step failed)
  
RUNNING ──error/timeout───→ FAILED ──cascade───→ SKIPPED (dependents)
```

**DAG Properties:**
- Cycle detection via DFS 3-color algorithm (prevents infinite loops)
- Critical path computation (identifies bottleneck chain)
- Ready-step resolution: steps whose all dependencies have transitioned to COMPLETED
- State transitions persisted immediately to `task_steps` SQLite table

## Coordination Engine

The `CoordinationEngine` orchestrates TaskDAG execution at runtime. Entry point is `execute(dag: TaskDAG, workflow_id: str, budget_limit: int)`.

### Execution Loop

```python
# 1. Initialize
dag.validate()  # Cycle check, dependency validation
shared_state = SharedState(workflow_id=workflow_id, db=db)
budget_consumed = 0

# 2. Execute until no more READY steps
while True:
    ready_steps = dag.find_ready_steps()
    if not ready_steps:
        break
    
    # Dispatch in parallel (bounded semaphore: max 4 concurrent)
    tasks = [
        execute_step(step, shared_state, budget_limit - budget_consumed)
        for step in ready_steps
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    for step, result in zip(ready_steps, results):
        if isinstance(result, Exception):
            step.state = TaskStepState.FAILED
            step.error = str(result)
            # Cascade: mark all dependents as SKIPPED
            mark_dependents_skipped(dag, step.id)
        else:
            step.result = result.output
            step.cost = result.cost
            step.duration_seconds = result.duration
            step.state = TaskStepState.COMPLETED
            budget_consumed += result.cost
            
            # Publish findings to message bus
            if result.findings:
                await bus.publish(
                    source_step=step.id,
                    agent_type=step.agent_type,
                    findings=result.findings
                )
        
        # Check budget
        if budget_consumed >= budget_limit:
            if KILL_ON_EXCEED:
                mark_remaining_skipped(dag)
                return WorkflowResult.PARTIAL_BUDGET_EXCEEDED
            else:
                await bus.publish(
                    source="coordinator",
                    topic="status.budget",
                    message="budget_limit_approaching"
                )

# 3. Synthesize
return synthesize_final_response(dag)
```

### Step Execution

Each `execute_step()` call:

1. **Context Injection**: Gather outputs from all predecessor steps + findings from message bus
2. **Prompt Construction**: Build agent prompt with:
   - Task description
   - Predecessor step outputs
   - Shared state (findings from other agents)
   - Budget remaining (transparency)
3. **Agent Dispatch**: Spawn agent via `ConverseExecutor` with model tier from `step.execution_model`
4. **Result Recording**: Capture:
   - Agent output
   - Cost (from Bedrock API response)
   - Duration (wall-clock)
   - Findings (if agent structured output)
5. **State Update**: Persist to `task_steps` table immediately
6. **Message Bus Publish**: Broadcast intermediate findings

### Critical Path Analysis

After DAG construction, `compute_critical_path()` identifies the longest dependency chain. Used for:
- Estimating total workflow duration
- Detecting if non-critical steps should be parallelized more
- Budget awareness (critical path steps shouldn't be starved)

## Shared State Store

SQLite-backed key-value store scoped per workflow. Allows agents to publish intermediate findings without embedding them in prompts (reduces context window pressure).

```python
class SharedState:
    def set(self, key: str, value: Any) -> None:
        """Store a finding. Value is JSON-serialized."""
        # INSERT OR REPLACE INTO shared_state (workflow_id, key, value, updated_at)
        
    def get(self, key: str) -> Any:
        """Retrieve a finding."""
        # SELECT value FROM shared_state WHERE workflow_id=? AND key=?
        
    def get_all(self) -> dict[str, Any]:
        """All state for this workflow."""
        # SELECT key, value FROM shared_state WHERE workflow_id=? ORDER BY updated_at
        
    def get_by_prefix(self, prefix: str) -> dict[str, Any]:
        """All keys matching prefix (e.g., "security.*")."""
        # SELECT key, value FROM shared_state WHERE workflow_id=? AND key LIKE prefix||'%'
```

**Usage by agents:**
- Security agent finds CVE → `shared_state.set("security.cves", [...])` 
- Dev agent reads findings → `shared_state.get_all()` injected into prompt
- Test agent publishes coverage → `shared_state.set("test.coverage", {"lines": 87.3})`

**Persistence:**
- Writes happen immediately (fire-and-forget, unbuffered)
- Reads are synchronous from SQLite
- Scoped by `workflow_id` to isolate concurrent workflows

## Message Bus (AgentBus)

Pub/sub system for real-time agent-to-agent communication. Enables agents to request info without waiting for predecessor to complete, and to publish findings before serialization.

**Topics:**
- `findings.*` — Discoveries published by agents (e.g., `findings.security`, `findings.test.coverage`)
- `request.<agent_type>` — Directed info requests (e.g., `request.dev` means "dev agent is requesting something")
- `status.<agent_type>.<step>` — Progress updates (e.g., `status.dev.step-2`)
- `handoff.*` — Agent-to-agent hand-off signals (e.g., `handoff.security.to-review`)

**Features:**
- Topic matching via fnmatch wildcards (subscribe to `findings.*` gets all `findings.X` messages)
- Per-agent delivery queues (asyncio.Queue, unbounded)
- Request/response pattern with configurable timeout (default: 30s)
- Broadcast to all subscribers on a topic
- SQLite persistence for audit trail (fire-and-forget writes to `message_bus` table)
- Scoped by `session_id`

**Usage Pattern:**

```python
# Agent subscribes to findings from security step
bus = AgentBus(session_id="workflow-123", db_path="/path/to/db")
await bus.subscribe("dev-1", "findings.security")

# Security agent publishes finding
await bus.publish(
    source_step="step-1",
    agent_type="security",
    topic="findings.security",
    payload={"cves": [...], "mitigation": "..."}
)

# Dev agent receives it
messages = await bus.get_messages("dev-1", timeout=5)
for msg in messages:
    print(msg.payload)  # Contains the security findings
```

## Example: Multi-Step Workflow

**Task**: "Add rate limiting to the API gateway with cost analysis and security review"

**Router scores**: 0.68 → FULL orchestration

**Decomposition produces DAG**:
```
step-1: aws-architect (opus)
  description: "Design rate limiting approach (token bucket, sliding window)"
  dependencies: []
  
step-2: dev (sonnet)
  description: "Implement rate limiting in API gateway code"
  dependencies: [step-1]
  
step-3: test (sonnet)
  description: "Write integration tests (happy path, rate limit exceeded)"
  dependencies: [step-2]
  
step-4: security (opus)
  description: "Review for bypass vectors, header spoofing, auth bypass"
  dependencies: [step-2]
  
step-5: optimization (haiku)
  description: "Analyze cost impact (requests/sec, storage, API calls)"
  dependencies: [step-2]
  
step-6: docs (haiku)
  description: "Update API documentation with rate limit headers"
  dependencies: [step-2]
```

**Execution Timeline**:
1. **T=0**: step-1 runs alone (only READY step) → outputs design doc
2. **T=5s**: step-1 COMPLETED → steps 2,3,4,5,6 all become READY
3. **T=6s**: steps 2,3,4,5,6 dispatched in parallel
   - step-2 injected with design doc
   - step-4 waits for step-2 (dependency)
   - steps 3,5,6 run concurrent with step-2
4. **T=18s**: step-2 COMPLETED → publishes findings to message bus
5. **T=20s**: step-4 now READY (depends on step-2), runs security review
6. **T=35s**: all steps COMPLETED → synthesis combines results

**Parallelism**: steps 3,5,6 can overlap with step-2; step-4 starts after step-2. Total time ≈ 35s vs ≈50s sequential.

**Failure scenario**: If step-4 (security) fails:
- step-4 transitions to FAILED
- steps that depend on step-4 (if any) transition to SKIPPED
- independent steps (3,5,6) continue
- result marked "partial" with security review skipped

## Budget Tracking

Each agent execution reports cost to CoordinationEngine. Cumulative cost tracked per workflow.

**Budget State Machine**:
```
cost += result.cost
if cumulative_cost >= limit:
    if KILL_ON_EXCEED:
        mark_all_remaining_steps(SKIPPED)
        return WorkflowResult.PARTIAL_BUDGET_EXCEEDED
    else:
        publish(topic="status.budget", message="limit_approaching")
        pause_new_step_dispatch()
```

**Budget Error Detection** (circuit breaker):
If agent output contains budget error markers:
- "budget exceeded"
- "budget paused"
- "per-agent cap exceeded"
- "daily budget exceeded"

Then: remaining steps stop, workflow marked PARTIAL, error logged.

**Workflow Result Marking**:
- `SUCCESS` — all steps COMPLETED, within budget
- `PARTIAL_BUDGET_EXCEEDED` — some steps COMPLETED, budget ran out
- `PARTIAL_DEPENDENCIES_FAILED` — some steps SKIPPED due to predecessor failure
- `FAILURE` — critical path step failed, cascade to dependents

## When Coordination Does NOT Kick In

Coordination is **disabled** (INLINE or LIGHTWEIGHT mode) for:
- Single-file changes, config value updates, status checks (score < 0.2)
- Simple tasks where the Router maps to INLINE mode
- Tasks scoring 0.2–0.5 use LIGHTWEIGHT: specialist executes, then review agent validates
- No DAG, no parallel dispatch, no message bus

LIGHTWEIGHT Example:
1. Dev agent implements a small hotfix
2. Code review agent reviews the diff
3. Sequential, no shared state, no bus

## Failure Modes & Handling

| Failure Mode | Behavior | Recovery |
|---|---|---|
| **Step timeout** (>5min) | Step marked FAILED, dependents SKIPPED | Timeout is per-step, not global. Workflow continues if independents exist. |
| **Budget exhaustion** | Remaining steps SKIPPED, result marked PARTIAL | Operator can increase budget and re-run if needed. |
| **Circuit breaker open** | Agent type unavailable (cooldown 30s), step retried or SKIPPED | Backoff strategy: retry after cooldown, then fail. Logged for alerting. |
| **Predecessor step failed** | Dependent steps transition to SKIPPED | Independent steps continue (does NOT block parallel work). |
| **Message bus timeout** | Request step gets timeout error, can gracefully degrade | Agent handles missing message (continues with partial context). |
| **Shared state write failed** | SQLite error, logged but doesn't block step completion | Async retry, no cascade failure. |

## Performance Characteristics

| Metric | Value | Notes |
|---|---|---|
| DAG cycle detection | O(V + E) | DFS 3-color, done once at startup |
| Ready-step resolution | O(V) | Linear scan, happens before each dispatch round |
| Parallel dispatch | Up to 4 concurrent steps | Bounded semaphore prevents resource exhaustion |
| Step execution | 10s–5min typical | Depends on model tier and task complexity |
| Message bus publish | ~5ms (async) | Fire-and-forget, no blocking |
| Shared state lookup | ~10ms (SSD) | SQLite indexed by workflow_id + key |
| Total workflow time | Min(critical_path_cost) | Parallelism reduces time significantly |

## Configuration

**Environment Variables:**

```bash
CAP_COORDINATION_MAX_CONCURRENT=4          # Parallel step limit
CAP_COORDINATION_STEP_TIMEOUT_SEC=300      # 5 minutes per step
CAP_COORDINATION_KILL_ON_BUDGET_EXCEED=true # True = fail fast, False = warn & continue
CAP_COORDINATION_ENABLE_MESSAGE_BUS=true   # Enable agent-to-agent pub/sub
CAP_COORDINATION_DB_PATH=~/.cap/cap.db     # SQLite database location
```

**Per-Workflow Overrides:**

```python
result = await coordinator.execute(
    dag=decomposed_dag,
    workflow_id=workflow_id,
    budget_limit=50000,  # Bedrock API units
    kill_on_exceed=True,
    enable_message_bus=True,
    max_concurrent_steps=4,
    step_timeout_sec=300
)
```

## Related Documentation

- [Architecture](ARCHITECTURE.md) — System overview, module layer, data layer
- [Agents](agents.md) — Agent catalog, model tier distribution
- [CLI Reference](cli-reference.md) — Command-line interface
- [ADR-012: Unified Database](adr/ADR-012-unified-database.md) — SQLite schema, WAL mode, concurrency

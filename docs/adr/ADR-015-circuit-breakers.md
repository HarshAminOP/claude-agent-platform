# ADR-015: Per-Agent-Type Circuit Breakers

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP Orchestration Layer — Week 2)

## Context

Agent failures are not independent events. When a model is throttled, a downstream service is degraded, or a prompt template has a systematic bug, every agent of the same type will fail in the same way. The current system retries failed agents up to 3 times with exponential backoff, but has no mechanism to detect systemic failure patterns.

**Observed cascade scenarios:**

1. **Bedrock throttling:** Opus quota exhausted → all Opus agents fail → 3 retries each × 5 agents = 15 wasted API calls before the workflow finally gives up
2. **MCP server crash:** kubernetes MCP server crashes → all devops/sre agents that call kubectl fail → retries compound the crashed server's restart loop
3. **Prompt regression:** A bad agent template produces invalid output → code-review rejects → retry with same template → reject → retry → budget exhausted on garbage
4. **Knowledge server overload:** 4 parallel agents all query knowledge_search → SQLite WAL contention → timeouts cascade

**Key constraints:**
- Circuit breakers must be per-agent-type (not global — a security agent failure should not block dev agents)
- The system must distinguish transient failures (retry) from systemic failures (stop trying)
- Recovery must be automatic when the underlying issue resolves
- Budget must not be wasted on doomed retries

## Decision

**Implement per-agent-type circuit breakers using the standard half-open pattern to prevent cascade failures.**

Each agent type (`security`, `devops`, `dev`, `sre`, `architect`, etc.) has an independent circuit breaker with three states:

```
CLOSED (normal) → OPEN (failing) → HALF-OPEN (testing recovery)
     ↑                                        │
     └────────────────────────────────────────┘
              (probe succeeds → close)
```

**State transitions:**

| From | To | Trigger |
|------|----|---------|
| CLOSED | OPEN | `failure_threshold` consecutive failures within `window_seconds` |
| OPEN | HALF-OPEN | `recovery_timeout` seconds elapsed |
| HALF-OPEN | CLOSED | Probe agent succeeds |
| HALF-OPEN | OPEN | Probe agent fails (reset recovery timer with backoff) |

**Key design choices:**

- **Per-type granularity:** Circuit breaker state is keyed by `(agent_type, model_tier)` — an Opus security failure does not trip the Sonnet dev breaker
- **Failure classification:** Not all failures trip the breaker. Only `systemic` failures count (throttle, timeout, contract violation). `Task-specific` failures (agent produced wrong answer for this specific task) do not count.
- **Probe mechanism:** In HALF-OPEN state, exactly one agent is allowed through as a probe. If it succeeds, the breaker closes. If it fails, the breaker reopens with increased recovery timeout.
- **Fallback behavior:** When a breaker is OPEN, the orchestrator either: (a) routes to a different model tier for the same agent type, or (b) marks dependent DAG nodes as `circuit_broken` and continues independent branches
- **Budget protection:** Failed attempts in OPEN state are rejected immediately (zero tokens spent)
- **State persistence:** Breaker state stored in `circuit_breakers` table in `platform.db` — survives process restart

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Unlimited retries with backoff** | Simple, eventually succeeds if transient | Wastes budget on systemic failures, delays workflow completion, no cascade protection | Rejected (current state, insufficient) |
| **Global circuit breaker** | Simple single state machine | One agent type failure blocks all agent types, over-conservative | Rejected (too coarse) |
| **Per-invocation timeout only** | No state to manage | No memory of failure patterns, each invocation rediscovers the same failure | Rejected (no learning) |
| **Kill workflow on N failures** | Prevents budget waste | Over-aggressive — partial workflows (4 of 6 agents succeed) are discarded entirely | Rejected (too aggressive) |
| **Token bucket rate limiter** | Smooth traffic, prevent spikes | Does not distinguish healthy from failing calls, reduces throughput even when healthy | Rejected (wrong abstraction) |

## Consequences

### Positive
- **Zero-cost failure rejection** — OPEN breaker rejects immediately, no tokens wasted
- **Cascade prevention** — one agent type failing does not propagate to unrelated agent types
- **Automatic recovery** — half-open probe detects when underlying issue resolves
- **Budget protection** — prevents the "15 wasted retries" scenario entirely
- **Observable** — breaker state visible in `workflow_status` and TUI dashboard
- **Composable with DAG** — open breaker marks downstream nodes as `circuit_broken` without blocking independent branches

### Negative
- **False opens** — bursty transient failures (3 timeouts in a row due to network blip) may open the breaker unnecessarily
- **Recovery delay** — `recovery_timeout` means the system waits before probing even if the issue resolved immediately
- **Complexity** — per-type × per-model state machines add operational surface area
- **Probe cost** — half-open probe consumes tokens on what might still be a failing system

## Implementation Notes

**Circuit breaker configuration (config.toml):**
```toml
[circuit_breaker]
failure_threshold = 3             # consecutive failures to trip
window_seconds = 300              # sliding window for counting failures
recovery_timeout_seconds = 60     # wait before half-open probe
recovery_backoff_multiplier = 2.0 # double recovery timeout on repeated failures
max_recovery_timeout = 600        # cap at 10 minutes
probe_timeout_seconds = 30        # timeout for half-open probe

[circuit_breaker.fallback]
# When breaker opens, attempt model-tier fallback before giving up
enable_model_fallback = true
fallback_map = { opus = "sonnet", sonnet = "haiku" }
```

**Schema extension (`platform.db`):**
```sql
CREATE TABLE circuit_breakers (
    agent_type TEXT NOT NULL,
    model_tier TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'closed',    -- closed|open|half_open
    failure_count INTEGER DEFAULT 0,
    last_failure_at TEXT,
    opened_at TEXT,
    recovery_timeout_seconds INTEGER DEFAULT 60,
    probe_count INTEGER DEFAULT 0,
    PRIMARY KEY (agent_type, model_tier)
);
```

**Core logic:**
```python
class CircuitBreaker:
    def should_allow(self, agent_type: str, model: str) -> bool:
        """Check if request should proceed."""
        state = self.get_state(agent_type, model)
        
        if state.state == "closed":
            return True
        
        if state.state == "open":
            elapsed = now() - state.opened_at
            if elapsed >= state.recovery_timeout_seconds:
                self.transition(agent_type, model, "half_open")
                return True  # Allow probe
            return False  # Reject immediately
        
        if state.state == "half_open":
            return False  # Only one probe at a time (already in flight)
        
    def record_outcome(self, agent_type: str, model: str, success: bool):
        """Record agent execution outcome."""
        state = self.get_state(agent_type, model)
        
        if success:
            if state.state == "half_open":
                self.transition(agent_type, model, "closed")
            state.failure_count = 0
        else:
            state.failure_count += 1
            if state.failure_count >= self.failure_threshold:
                self.transition(agent_type, model, "open")
```

## Related ADRs

- [ADR-013: DAG Execution](ADR-013-dag-execution.md) — Open breakers mark downstream DAG nodes as `circuit_broken`
- [ADR-014: Consensus Protocol](ADR-014-consensus-protocol.md) — Judge invocations also protected by circuit breaker
- [ADR-016: Checkpoint Resume](ADR-016-checkpoint-resume.md) — Circuit-broken workflows can resume when breaker closes

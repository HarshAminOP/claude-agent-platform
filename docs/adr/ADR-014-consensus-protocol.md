# ADR-014: Domain-Weighted Consensus for Agent Disagreements

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP Orchestration Layer — Week 2)

## Context

Multi-agent workflows produce disagreements when agents with different domain expertise evaluate the same artifact. Common conflicts:

- **Security vs. DevOps:** Security agent flags a permissive IAM policy; DevOps agent argues it is required for service functionality
- **Architect vs. Dev:** Architect proposes a microservice split; Dev agent argues the complexity is not justified for current scale
- **SRE vs. Dev:** SRE flags missing circuit breaker; Dev argues the downstream is internal-only and always available

In the current system (v0.5.0), `conflict_raise` logs the disagreement and escalates all blocking conflicts to the PO. This creates bottlenecks:
- PO is interrupted for every disagreement, including domain-specific ones where one agent clearly has authority
- No mechanism for agents to resolve conflicts autonomously when domain expertise is clear
- No escalation gradient — everything is either "blocking (PO decides)" or "advisory (ignored)"

**Key constraints:**
- The PO must retain final authority on business-impacting decisions
- Domain expertise should be respected (security agent's opinion on IAM policy outweighs devops agent's)
- Resolution must be auditable (who decided, why, based on what authority)
- The system must not deadlock when two agents of equal authority disagree

## Decision

**Use domain-weighted authority scoring with automatic resolution for clear winners and judge escalation for ties.**

Each agent type has a domain authority matrix defining its expertise weight (0.0-1.0) for each conflict domain:

```python
AUTHORITY_MATRIX = {
    #                    security  infra  code  cost  reliability  arch
    "security":        [1.0,      0.3,   0.4,  0.2,  0.5,         0.4],
    "devops":          [0.3,      0.9,   0.3,  0.5,  0.7,         0.3],
    "dev":             [0.3,      0.2,   0.9,  0.3,  0.4,         0.5],
    "sre":             [0.5,      0.6,   0.3,  0.4,  1.0,         0.4],
    "architect":       [0.4,      0.5,   0.5,  0.4,  0.5,         1.0],
    "code-review":     [0.4,      0.2,   0.8,  0.2,  0.3,         0.4],
    "optimizer":       [0.2,      0.3,   0.5,  1.0,  0.4,         0.3],
}
```

**Resolution protocol (3 tiers):**

1. **Automatic resolution (authority gap >= 0.3):** The agent with higher domain authority wins. Decision is logged with rationale and the losing agent's argument is preserved for audit.

2. **Judge escalation (authority gap < 0.3):** An Opus-tier "judge" agent receives both arguments, the authority scores, and context. It produces a binding decision with written rationale. Cost: ~2K tokens per judgment.

3. **PO escalation (judge cannot decide OR conflict is business-impacting):** Conflict surfaces to the PO via `conflict_blocking`. The judge flags cases where technical resolution is insufficient (e.g., "accept risk for speed" is a business call).

**Key design choices:**
- Authority matrix is configurable in `config.toml` (teams can tune for their context)
- Judge agent is a dedicated Opus invocation with a specialized prompt (no tool access, just reasoning)
- All resolutions are recorded in `conflict_resolutions` table with full audit trail
- Conflict domain is auto-classified from the disagreement content using keyword matching
- Resolutions feed back into progressive autonomy — repeated correct auto-resolutions increase confidence

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Always escalate to PO** | PO has full control, no risk of bad auto-decisions | PO bottleneck, 5-10 interruptions per complex workflow, breaks async execution | Rejected (current state, does not scale) |
| **Majority vote (N agents vote)** | Democratic, no single point of failure | Expensive (spawn extra agents), domain expertise not weighted, 3-way ties possible | Rejected (wrong for specialized agents) |
| **Strict hierarchy (architect > security > devops > dev)** | Simple, deterministic | Domain-inappropriate (architect should not override security on IAM) | Rejected (too rigid) |
| **Last-writer-wins** | Zero conflict resolution overhead | Non-deterministic, domain expertise ignored, audit nightmare | Rejected (unacceptable) |
| **Consensus quorum (all must agree)** | Maximum safety | Deadlocks on genuine disagreements, blocks progress | Rejected (too conservative) |

## Consequences

### Positive
- **80% of conflicts auto-resolve** without PO interruption (based on authority gap analysis of historical conflicts)
- **Domain expertise respected** — security agent wins on security topics, SRE wins on reliability topics
- **Auditable** — every resolution has: domain, authority scores, winner, rationale, timestamp
- **Configurable** — teams can adjust authority matrix to match their organizational structure
- **Progressive** — auto-resolution confidence grows as outcomes are tracked
- **Deadlock-free** — judge tier guarantees resolution within bounded time

### Negative
- **Judge cost** — ~2K tokens per judge invocation (estimated 2-3 per complex workflow)
- **Authority matrix maintenance** — must be updated when new agent types are added
- **False authority** — domain auto-classification may miscategorize edge cases (mitigated by judge escalation)
- **Opacity risk** — auto-resolutions happen without PO awareness (mitigated by inclusion in workflow report)

## Implementation Notes

**Conflict resolution flow:**
```python
async def resolve_conflict(conflict: Conflict) -> Resolution:
    """Resolve agent disagreement using domain-weighted authority."""
    domain = classify_conflict_domain(conflict)
    
    score_a = AUTHORITY_MATRIX[conflict.agent_a_type][domain]
    score_b = AUTHORITY_MATRIX[conflict.agent_b_type][domain]
    gap = abs(score_a - score_b)
    
    if gap >= 0.3:
        # Tier 1: Automatic resolution
        winner = "a" if score_a > score_b else "b"
        return Resolution(
            method="authority",
            winner=winner,
            rationale=f"{conflict.agent_a_type if winner == 'a' else conflict.agent_b_type} "
                      f"has domain authority {max(score_a, score_b):.1f} vs {min(score_a, score_b):.1f} "
                      f"in domain '{domain}'",
        )
    
    if not conflict.is_business_impacting:
        # Tier 2: Judge escalation
        return await invoke_judge(conflict, domain, score_a, score_b)
    
    # Tier 3: PO escalation
    return Resolution(method="po_escalation", pending=True)
```

**Configuration (config.toml):**
```toml
[consensus]
auto_resolve_threshold = 0.3      # authority gap for auto-resolution
judge_model = "opus"              # model tier for judge invocations
judge_max_tokens = 2000           # budget per judgment
max_judge_per_workflow = 5        # rate limit judge invocations
authority_matrix_version = 1      # bump when matrix changes
```

## Related ADRs

- [ADR-013: DAG Execution](ADR-013-dag-execution.md) — Conflicts may block downstream DAG nodes
- [ADR-015: Circuit Breakers](ADR-015-circuit-breakers.md) — Repeated conflict failures trigger circuit breaker
- [ADR-016: Checkpoint Resume](ADR-016-checkpoint-resume.md) — Pending PO decisions are checkpointed for resume

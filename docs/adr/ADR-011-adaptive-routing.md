# ADR-011: 3-Tier Adaptive Complexity Routing

**Status:** Accepted  
**Date:** 2026-06-30  
**Context:** Version 2 (CAP System Design v1)

## Context

The previous routing model was binary: either a task was trivial (do inline) or it required full orchestration (spawn orchestrator with all specialists). This wasted resources on medium-complexity tasks that needed more than a 1-line fix but less than a full engineering team:

- Simple refactors across 2-3 files were over-allocated with full orchestrator + code-review + test agents
- Quick investigations that needed one tool call were under-allocated when done inline
- No feedback loop — the system never learned which tasks were routed correctly

**Key constraints:**
- Routing must be fast (<50ms) to avoid blocking the user
- False positives (over-routing) waste tokens/budget; false negatives (under-routing) produce low-quality results
- The system should improve over time without manual tuning
- Must handle cold-start (no historical data) gracefully

## Decision

**Implement 3-tier adaptive complexity routing: INLINE / LIGHTWEIGHT / FULL, with heuristic signals for initial classification and learned thresholds that improve after 50+ decisions.**

### Tier Definitions

| Tier | Description | Agent Allocation | Example Tasks |
|------|-------------|-----------------|---------------|
| **INLINE** | Trivial, do directly | 0 agents (Claude does it) | 1-line fix, status check, quick lookup |
| **LIGHTWEIGHT** | Medium complexity | 1-2 specialist agents | Single-file refactor, focused investigation, config change |
| **FULL** | High complexity | Orchestrator + N specialists | Multi-service deployment, architecture change, security audit |

### Heuristic Signals (Initial Classification)

The router uses these signals to estimate complexity before delegation:

| Signal | Weight | How Measured |
|--------|--------|-------------|
| Estimated files touched | 0.30 | Pattern matching on task description ("across N files", file paths mentioned) |
| Domain count | 0.25 | How many domains referenced (infra + security + app = 3) |
| Keywords | 0.20 | Trigger words: "audit", "deploy", "migrate" = complex; "fix", "update", "check" = simple |
| Historical similarity | 0.15 | FTS5 match against past routing decisions with known outcomes |
| User explicit signals | 0.10 | "just" / "quickly" = simple; "thoroughly" / "across all" = complex |

### Learning Mechanism

After each routed task completes, the outcome is recorded:

```sql
INSERT INTO routing_decisions (task_hash, tier_assigned, tier_ideal, signals, outcome, tokens_used, duration_ms)
```

- `tier_ideal` is inferred from actual resource usage (if FULL was assigned but only 1 agent did useful work, ideal was LIGHTWEIGHT)
- After 50+ decisions, thresholds are recalculated from the historical distribution
- Learning is per-workspace (different repos have different complexity profiles)

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Binary routing (trivial/full)** | Simple implementation | Over-allocates medium tasks, wastes 40-60% of budget on simple-but-not-trivial work | Rejected (current state) |
| **LLM-based routing** | Accurate classification | Adds latency (500ms+), costs tokens, circular dependency (need LLM to decide if LLM is needed) | Rejected |
| **User-specified complexity** | Always correct | Requires user to assess complexity before every task (bad UX) | Rejected |
| **5-tier granularity** | More precise allocation | Harder to learn thresholds, more classification errors, diminishing returns | Rejected |
| **Fixed heuristics (no learning)** | Deterministic, simple | Never improves, requires manual threshold tuning as usage patterns change | Rejected |

## Consequences

### Positive
- **Right-sized allocation:** Medium tasks get 1-2 agents instead of full orchestration (saves 30-50% tokens)
- **Self-improving:** After 50+ decisions, routing accuracy improves based on real outcomes
- **Fast cold-start:** Heuristic signals provide reasonable routing even with no history
- **Budget efficiency:** LIGHTWEIGHT tier costs ~20% of FULL tier on average
- **Transparent:** Every routing decision is logged with signals and rationale for debugging

### Negative
- **Initial inaccuracy:** First 50 decisions rely purely on heuristics (expected ~70% accuracy)
- **Learning lag:** Threshold updates happen at decision boundaries (50, 100, 200+), not continuously
- **Signal extraction cost:** Parsing task descriptions for signals adds ~10ms (acceptable)
- **Workspace-specificity:** A workspace with no history starts cold even if other workspaces have learned

## Implementation Notes

**Key file:** `src/cap/orchestration/router.py`

**Threshold defaults (before learning kicks in):**
- INLINE: complexity_score < 0.3
- LIGHTWEIGHT: 0.3 <= complexity_score < 0.65
- FULL: complexity_score >= 0.65

**Learning recalculation trigger:** Every 50 new routing decisions, recalculate thresholds as the median split points from historical `tier_ideal` distribution.

**Fallback behavior:** If routing confidence is below 0.5 (ambiguous), default to LIGHTWEIGHT (safe middle ground).

## Related ADRs

- [ADR-010: Memory Architecture](ADR-010-memory-architecture.md) — Router reads historical decisions from memory
- [ADR-012: Unified Database](ADR-012-unified-database.md) — Routing decisions stored in cap.db
- [ADR-009: Enforcement Hooks](ADR-009-enforcement-hooks.md) — Enforcement interacts with routing (delegated edits are tracked)

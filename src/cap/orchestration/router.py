"""
3-tier complexity router for CAP orchestration.

Routes tasks to one of three tiers based on complexity scoring:
- INLINE: trivial tasks (score < 0.2)
- LIGHTWEIGHT: single specialist + review (0.2 <= score <= 0.5)
- FULL: orchestrator + multiple specialists (score > 0.5)

Scoring uses keyword heuristics with learned threshold adaptation.
Every routing decision is recorded to routing_decisions for self-learning.
"""

import json
import time
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Tier(Enum):
    INLINE = "inline"
    LIGHTWEIGHT = "lightweight"
    FULL = "full"


@dataclass
class RoutingDecision:
    tier: Tier
    reasoning: str
    estimated_agents: list[str]
    estimated_cost: float
    complexity_score: float = 0.0
    decision_id: Optional[int] = None


# Complexity signals and their weights
COMPLEXITY_SIGNALS: dict[str, dict] = {
    "multi_file_keywords": {
        "keywords": ["across", "all files", "every", "migrate"],
        "weight": 0.3,
    },
    "infra_keywords": {
        "keywords": ["terraform", "kubernetes", "helm", "argocd", "deploy"],
        "weight": 0.25,
    },
    "refactor_keywords": {
        "keywords": ["refactor", "redesign", "rewrite", "restructure"],
        "weight": 0.2,
    },
    "review_keywords": {
        "keywords": ["review", "audit", "security", "compliance"],
        "weight": 0.15,
    },
    "simple_keywords": {
        "keywords": ["fix typo", "rename", "update comment", "add log"],
        "weight": -0.3,
    },
}

# Default thresholds
DEFAULT_INLINE_MAX = 0.2
DEFAULT_FULL_MIN = 0.5


def _compute_keyword_score(prompt: str) -> float:
    """Compute complexity score from keyword signals."""
    prompt_lower = prompt.lower()
    score = 0.0

    for signal_name, signal_config in COMPLEXITY_SIGNALS.items():
        keywords = signal_config["keywords"]
        weight = signal_config["weight"]
        if any(kw in prompt_lower for kw in keywords):
            score += weight

    # Length signal
    if len(prompt) > 500:
        score += 0.2
    elif len(prompt) > 200:
        score += 0.1

    return score


def _build_reasoning(prompt: str, score: float) -> str:
    """Build human-readable reasoning for the routing decision."""
    prompt_lower = prompt.lower()
    reasons = []

    for signal_name, signal_config in COMPLEXITY_SIGNALS.items():
        keywords = signal_config["keywords"]
        weight = signal_config["weight"]
        matched = [kw for kw in keywords if kw in prompt_lower]
        if matched:
            direction = "increases" if weight > 0 else "decreases"
            reasons.append(
                f"{signal_name} ({', '.join(matched)}) {direction} complexity by {abs(weight)}"
            )

    if len(prompt) > 500:
        reasons.append("long prompt (>500 chars) adds +0.2")
    elif len(prompt) > 200:
        reasons.append("medium prompt (>200 chars) adds +0.1")

    reasons.append(f"final score: {score:.3f}")
    return "; ".join(reasons)


def _estimate_agents(tier: Tier, prompt: str) -> list[str]:
    """Suggest agents based on tier and prompt content."""
    if tier == Tier.INLINE:
        return []

    prompt_lower = prompt.lower()
    agents = []

    if tier == Tier.LIGHTWEIGHT:
        # Single specialist
        if any(kw in prompt_lower for kw in ["terraform", "kubernetes", "helm", "argocd", "deploy"]):
            agents.append("devops")
        elif any(kw in prompt_lower for kw in ["security", "audit", "compliance", "iam"]):
            agents.append("security")
        elif any(kw in prompt_lower for kw in ["review"]):
            agents.append("code-review")
        else:
            agents.append("dev")
    else:
        # Full orchestration — multiple specialists
        agents.append("orchestrator")
        if any(kw in prompt_lower for kw in ["terraform", "kubernetes", "helm", "argocd", "deploy"]):
            agents.append("devops")
        if any(kw in prompt_lower for kw in ["security", "audit", "compliance", "iam"]):
            agents.append("security")
        if any(kw in prompt_lower for kw in ["refactor", "redesign", "rewrite", "restructure"]):
            agents.append("dev")
        if any(kw in prompt_lower for kw in ["review"]):
            agents.append("code-review")
        # Always include at least dev for full tier
        if "dev" not in agents:
            agents.append("dev")

    return agents


def _estimate_cost(tier: Tier, agents: list[str]) -> float:
    """Estimate cost in USD based on tier and agent count."""
    # Rough estimates per agent invocation
    cost_per_agent = {
        Tier.INLINE: 0.0,
        Tier.LIGHTWEIGHT: 0.02,
        Tier.FULL: 0.05,
    }
    base = cost_per_agent.get(tier, 0.0)
    return base * max(len(agents), 1)


def get_learned_thresholds(db: sqlite3.Connection) -> dict:
    """
    If 50+ routing decisions exist, compute adaptive thresholds from history.
    Returns dict with 'inline_max', 'full_min', and 'source'.
    """
    row = db.execute(
        "SELECT COUNT(*) FROM routing_decisions"
    ).fetchone()

    total_decisions = row[0] if row else 0

    if total_decisions < 50:
        return {
            "inline_max": DEFAULT_INLINE_MAX,
            "full_min": DEFAULT_FULL_MIN,
            "source": "default",
        }

    # Compute from successful decisions per tier
    stats = {}
    for tier in ("inline", "lightweight", "full"):
        tier_row = db.execute(
            """SELECT AVG(complexity_score), COUNT(*)
               FROM routing_decisions
               WHERE tier_selected = ? AND outcome = 'success'""",
            (tier,)
        ).fetchone()
        stats[tier] = {
            "avg_complexity": tier_row[0] if tier_row and tier_row[0] else 0.0,
            "count": tier_row[1] if tier_row else 0,
        }

    # Derive thresholds as midpoints between tier averages
    inline_avg = stats["inline"]["avg_complexity"]
    lightweight_avg = stats["lightweight"]["avg_complexity"]
    full_avg = stats["full"]["avg_complexity"]

    # Inline max = midpoint between inline avg and lightweight avg
    if stats["inline"]["count"] > 5 and stats["lightweight"]["count"] > 5:
        inline_max = (inline_avg + lightweight_avg) / 2.0
    else:
        inline_max = DEFAULT_INLINE_MAX

    # Full min = midpoint between lightweight avg and full avg
    if stats["lightweight"]["count"] > 5 and stats["full"]["count"] > 5:
        full_min = (lightweight_avg + full_avg) / 2.0
    else:
        full_min = DEFAULT_FULL_MIN

    return {
        "inline_max": inline_max,
        "full_min": full_min,
        "source": "learned",
    }


def _record_decision(
    db: sqlite3.Connection,
    prompt: str,
    score: float,
    tier: Tier,
    agents: list[str],
    session_id: str = "unknown",
) -> int:
    """Record routing decision to routing_decisions table. Returns decision_id."""
    cursor = db.execute(
        """INSERT INTO routing_decisions
           (timestamp, session_id, task_description, complexity_score,
            tier_selected, agents_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (time.time(), session_id, prompt[:2000], score, tier.value, json.dumps(agents)),
    )
    db.commit()
    return cursor.lastrowid


def route(
    prompt: str,
    db: sqlite3.Connection,
    session_id: str = "unknown",
) -> RoutingDecision:
    """
    Route a task prompt to the appropriate tier.

    Computes complexity score from keyword signals and prompt length,
    applies learned thresholds if sufficient history exists,
    and records the decision for future learning.

    Args:
        prompt: The task description/prompt to route.
        db: SQLite connection with routing_decisions table.
        session_id: Current session identifier.

    Returns:
        RoutingDecision with tier, reasoning, estimated agents, and cost.
    """
    score = _compute_keyword_score(prompt)

    # Clamp score to [0, 1]
    score = max(0.0, min(1.0, score))

    # Get thresholds (learned or default)
    thresholds = get_learned_thresholds(db)
    inline_max = thresholds["inline_max"]
    full_min = thresholds["full_min"]

    # Classify tier
    if score < inline_max:
        tier = Tier.INLINE
    elif score > full_min:
        tier = Tier.FULL
    else:
        tier = Tier.LIGHTWEIGHT

    # Build decision
    agents = _estimate_agents(tier, prompt)
    reasoning = _build_reasoning(prompt, score)
    cost = _estimate_cost(tier, agents)

    # Record to DB
    decision_id = _record_decision(db, prompt, score, tier, agents, session_id)

    return RoutingDecision(
        tier=tier,
        reasoning=reasoning,
        estimated_agents=agents,
        estimated_cost=cost,
        complexity_score=score,
        decision_id=decision_id,
    )

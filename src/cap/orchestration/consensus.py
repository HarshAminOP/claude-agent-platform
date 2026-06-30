"""
Consensus Protocol for CAP orchestration.

Implements structured disagreement detection and resolution between agents
with domain-specific authority. Simple 2/3 quorum is insufficient when
agents have expertise-weighted authority (security on vulnerabilities,
devops on infra). This module provides a resolution cascade:

  1. Security veto (absolute on security domain)
  2. Domain authority (weighted vote)
  3. Learned precedent (historical outcomes)
  4. Judge agent (both arguments presented to neutral arbiter)
  5. PO escalation (surface to user)

Reference: CAP System Design Section 16A — Consensus Protocol.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── Domain Authority Weights ─────────────────────────────────────────────────
# agent_type -> {domain: weight}
# Higher weight = stronger authority in that domain.

DOMAIN_WEIGHTS: dict[str, dict[str, float]] = {
    "security": {
        "security": 0.95,
        "iam": 0.9,
        "secrets": 0.95,
        "compliance": 0.85,
        "code_quality": 0.3,
    },
    "code-review": {
        "code_quality": 0.9,
        "correctness": 0.85,
        "style": 0.8,
        "security": 0.4,
    },
    "devops": {
        "infrastructure": 0.9,
        "deployment": 0.9,
        "networking": 0.85,
        "security": 0.3,
    },
    "sre": {
        "reliability": 0.9,
        "observability": 0.85,
        "performance": 0.8,
        "deployment": 0.6,
    },
    "aws-architect": {
        "aws": 0.95,
        "cost": 0.85,
        "infrastructure": 0.7,
        "security": 0.5,
    },
}

# ─── Domain Signal Keywords ───────────────────────────────────────────────────
# Used by classify_domain to infer the disagreement domain from reasoning text.

DOMAIN_SIGNALS: dict[str, list[str]] = {
    "security": ["vulnerability", "injection", "auth", "credential", "CVE", "OWASP"],
    "infrastructure": ["terraform", "kubernetes", "helm", "deployment", "scaling"],
    "code_quality": ["refactor", "pattern", "naming", "complexity", "duplication"],
    "reliability": ["timeout", "retry", "circuit", "fallback", "SLO"],
    "cost": ["expensive", "budget", "right-size", "reserved", "spot"],
}

# Severity levels for severity-gap disagreement detection
SEVERITY_LEVELS: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}


# ─── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class Disagreement:
    """A detected conflict between two agent outputs."""

    agent_a: str
    agent_b: str
    field: str  # "verdict" or "severity"
    value_a: str
    value_b: str
    domain: str  # inferred domain of the disagreement


@dataclass
class Resolution:
    """The outcome of resolving a disagreement."""

    winner: Optional[str]  # agent_type that won, or None for escalation
    method: str  # security_veto | domain_authority | learned_precedent | judge | judge_needed | po_escalation
    confidence: float  # 0.0–1.0


# ─── Detection ────────────────────────────────────────────────────────────────


def detect_disagreement(outputs: list[dict]) -> list[Disagreement]:
    """
    Compare structured agent outputs for conflicts.

    Detects two kinds of disagreements:
    - Verdict conflicts: one agent approves, another rejects.
    - Severity conflicts: agents rate severity >= 2 levels apart.

    Args:
        outputs: List of agent output dicts. Each must contain at minimum
                 "agent_type". May also contain "verdict", "severity",
                 and "reasoning" fields.

    Returns:
        List of detected Disagreement instances.
    """
    disagreements: list[Disagreement] = []

    for i, a in enumerate(outputs):
        for j, b in enumerate(outputs[i + 1:], i + 1):
            # Check verdict conflicts (approve vs reject)
            if a.get("verdict") and b.get("verdict"):
                if a["verdict"] != b["verdict"]:
                    disagreements.append(Disagreement(
                        agent_a=a["agent_type"],
                        agent_b=b["agent_type"],
                        field="verdict",
                        value_a=a["verdict"],
                        value_b=b["verdict"],
                        domain=classify_domain(a, b),
                    ))

            # Check severity conflicts (>= 2 levels apart)
            if a.get("severity") and b.get("severity"):
                level_a = SEVERITY_LEVELS.get(a["severity"], 0)
                level_b = SEVERITY_LEVELS.get(b["severity"], 0)
                if abs(level_a - level_b) >= 2:
                    disagreements.append(Disagreement(
                        agent_a=a["agent_type"],
                        agent_b=b["agent_type"],
                        field="severity",
                        value_a=a["severity"],
                        value_b=b["severity"],
                        domain=classify_domain(a, b),
                    ))

    return disagreements


# ─── Domain Classification ────────────────────────────────────────────────────


def classify_domain(a: dict, b: dict) -> str:
    """
    Infer disagreement domain from agent types and content.

    Uses keyword matching against DOMAIN_SIGNALS to score which domain
    the disagreement most likely belongs to.

    Args:
        a: First agent output dict (should have "reasoning" field).
        b: Second agent output dict (should have "reasoning" field).

    Returns:
        Domain string (e.g. "security", "infrastructure", "general").
    """
    keywords = " ".join([a.get("reasoning", ""), b.get("reasoning", "")])

    scores: dict[str, int] = {}
    for domain, signals in DOMAIN_SIGNALS.items():
        scores[domain] = sum(1 for s in signals if s.lower() in keywords.lower())

    if any(scores.values()):
        return max(scores, key=scores.get)  # type: ignore[arg-type]
    return "general"


# ─── Resolution Cascade ──────────────────────────────────────────────────────


def resolve_disagreement(d: Disagreement, db: sqlite3.Connection) -> Resolution:
    """
    Resolve a disagreement using a priority cascade.

    Resolution order:
      1. Security veto — absolute on security domain
      2. Domain authority — highest weight wins (margin >= 0.3)
      3. Learned precedent — historical outcomes (>= 3 matching resolutions)
      4. Judge needed — signals that a judge agent should be spawned
      5. (PO escalation is handled by spawn_judge if judge says ESCALATE)

    Args:
        d: The Disagreement to resolve.
        db: SQLite connection for querying historical resolutions.

    Returns:
        Resolution with winner, method, and confidence.
    """
    # Step 1: Security veto — absolute on security domain
    if d.domain == "security" and d.agent_a == "security":
        logger.info("Security veto: %s wins over %s on domain=%s", d.agent_a, d.agent_b, d.domain)
        return Resolution(winner=d.agent_a, method="security_veto", confidence=0.95)
    if d.domain == "security" and d.agent_b == "security":
        logger.info("Security veto: %s wins over %s on domain=%s", d.agent_b, d.agent_a, d.domain)
        return Resolution(winner=d.agent_b, method="security_veto", confidence=0.95)

    # Step 2: Domain authority — highest weight wins
    weight_a = DOMAIN_WEIGHTS.get(d.agent_a, {}).get(d.domain, 0.5)
    weight_b = DOMAIN_WEIGHTS.get(d.agent_b, {}).get(d.domain, 0.5)
    margin = abs(weight_a - weight_b)

    if margin >= 0.3:  # Clear authority
        winner = d.agent_a if weight_a > weight_b else d.agent_b
        logger.info(
            "Domain authority: %s wins over %s on domain=%s (margin=%.2f)",
            winner, d.agent_a if winner == d.agent_b else d.agent_b, d.domain, margin,
        )
        return Resolution(winner=winner, method="domain_authority", confidence=margin)

    # Step 3: Check historical outcomes for this pattern
    past = db.execute(
        """
        SELECT winner, COUNT(*) as cnt FROM disagreement_resolutions
        WHERE domain = ? AND method != 'po_escalation'
        GROUP BY winner ORDER BY cnt DESC LIMIT 1
        """,
        (d.domain,),
    ).fetchone()

    if past and past[1] >= 3:
        logger.info(
            "Learned precedent: %s wins on domain=%s (%d prior resolutions)",
            past[0], d.domain, past[1],
        )
        return Resolution(winner=past[0], method="learned_precedent", confidence=0.7)

    # Step 4: Judge needed — caller should invoke spawn_judge
    logger.info(
        "No clear resolution for %s vs %s on domain=%s — judge needed",
        d.agent_a, d.agent_b, d.domain,
    )
    return Resolution(winner=None, method="judge_needed", confidence=0.0)


# ─── Judge Agent ──────────────────────────────────────────────────────────────


async def spawn_judge(
    d: Disagreement,
    orchestrator: Any,
    db: sqlite3.Connection,
) -> Resolution:
    """
    Spawn a judge agent with both arguments presented neutrally.

    Constructs a prompt containing both positions and dispatches to a
    code-review agent acting as arbiter. If the judge cannot determine
    a winner, escalates to the PO.

    Args:
        d: The Disagreement to judge.
        orchestrator: Orchestrator instance with dispatch_agent method.
        db: SQLite connection (passed through to dispatch).

    Returns:
        Resolution from the judge, or po_escalation if inconclusive.
    """
    judge_prompt = (
        f"Two agents disagree on domain '{d.domain}'.\n\n"
        f"AGENT A ({d.agent_a}) says {d.field} = '{d.value_a}'\n"
        f"AGENT B ({d.agent_b}) says {d.field} = '{d.value_b}'\n\n"
        f"Analyze both positions. Which is correct and why? "
        f"If you cannot determine a winner, say 'ESCALATE'."
    )

    result = await orchestrator.dispatch_agent(
        step={"description": judge_prompt, "agent_type": "code-review"},
        workflow_id=orchestrator.current_workflow_id,
        db=db,
    )

    verdict_text = result.get("summary", "").lower()

    if "escalate" in verdict_text:
        logger.info("Judge escalated: %s vs %s on domain=%s", d.agent_a, d.agent_b, d.domain)
        return Resolution(winner=None, method="po_escalation", confidence=0.0)

    # Determine winner from judge verdict
    winner = d.agent_a if d.agent_a.lower() in verdict_text else d.agent_b
    logger.info("Judge resolved: %s wins over %s on domain=%s", winner,
                d.agent_a if winner == d.agent_b else d.agent_b, d.domain)
    return Resolution(winner=winner, method="judge", confidence=0.75)


# ─── Persistence ──────────────────────────────────────────────────────────────


def record_resolution(
    d: Disagreement,
    resolution: Resolution,
    workflow_id: str,
    db: sqlite3.Connection,
) -> int:
    """
    Persist a disagreement resolution to the database.

    Saves the full context (agents, field, values, domain) and outcome
    (winner, method, confidence) for future learning.

    Args:
        d: The Disagreement that was resolved.
        resolution: The Resolution outcome.
        workflow_id: ID of the workflow/orchestration this belongs to.
        db: SQLite connection.

    Returns:
        Row ID of the inserted record.
    """
    cursor = db.execute(
        """
        INSERT INTO disagreement_resolutions
            (workflow_id, domain, agent_a, agent_b, field, value_a, value_b,
             winner, method, confidence, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_id,
            d.domain,
            d.agent_a,
            d.agent_b,
            d.field,
            d.value_a,
            d.value_b,
            resolution.winner,
            resolution.method,
            resolution.confidence,
            time.time(),
        ),
    )
    db.commit()

    row_id = cursor.lastrowid
    logger.info(
        "Recorded resolution id=%d: %s won via %s (confidence=%.2f) for workflow=%s",
        row_id, resolution.winner, resolution.method, resolution.confidence, workflow_id,
    )
    return row_id

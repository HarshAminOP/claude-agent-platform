"""
CAP Self-Learning Engine.

Records routing decisions, outcomes, corrections, and trust levels.
Learns from history to auto-generate baseline rules and adapt thresholds.

Tables used:
- routing_decisions: every routing choice and its outcome
- correction_patterns: repeated mistakes that become baseline rules
- trust_levels: per-agent trust scores based on success/failure history
- memory_active: for auto-generated baseline entries
"""

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


CORRECTION_THRESHOLD = 3  # same mistake 3x -> auto-generate baseline
LEARNED_THRESHOLD_MIN_SAMPLES = 50  # samples per tier before learned thresholds activate


@dataclass
class RoutingDecision:
    """Full routing decision record for the learning system."""

    session_id: str
    task_description: str
    complexity_score: float
    tier_selected: str  # 'inline', 'lightweight', 'full'
    agents_used: list[str] = field(default_factory=list)
    task_hash: str = ""

    def __post_init__(self):
        if not self.task_hash:
            self.task_hash = _compute_task_hash(self.task_description)


@dataclass
class RoutingRecord:
    """A stored routing decision with its database ID and timestamp."""

    decision_id: int
    timestamp: float
    session_id: str
    task_description: str
    complexity_score: float
    tier_selected: str
    agents_used: list[str]


def _compute_task_hash(task_description: str) -> str:
    """Compute a stable hash for deduplication of similar tasks."""
    # Normalize: lowercase, strip whitespace, first 200 chars for stability
    normalized = task_description.strip().lower()[:200]
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def record_routing(
    decision: RoutingDecision,
    db: sqlite3.Connection,
) -> int:
    """
    Record a routing decision to routing_decisions table.

    Stores full RoutingDecision with task_hash for deduplication.
    If an identical task_hash exists within the last 60 seconds from the
    same session, returns the existing decision_id (dedup).

    Args:
        decision: RoutingDecision dataclass with all routing metadata.
        db: SQLite connection.

    Returns:
        The decision_id (row ID) for later outcome recording.
    """
    now = time.time()

    # Dedup: check if same task_hash was recorded in the last 60s from same session
    existing = db.execute(
        """SELECT id FROM routing_decisions
           WHERE task_hash = ? AND session_id = ? AND timestamp > ?
           LIMIT 1""",
        (decision.task_hash, decision.session_id, now - 60.0),
    ).fetchone()

    if existing:
        return existing[0] if isinstance(existing, tuple) else existing["id"]

    cursor = db.execute(
        """INSERT INTO routing_decisions
           (timestamp, session_id, task_description, complexity_score,
            tier_selected, agents_used, task_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            decision.session_id,
            decision.task_description[:2000],
            decision.complexity_score,
            decision.tier_selected,
            json.dumps(decision.agents_used),
            decision.task_hash,
        ),
    )
    db.commit()
    return cursor.lastrowid


def record_outcome(
    decision_id: int,
    outcome: str,
    db: sqlite3.Connection,
    duration_ms: Optional[int] = None,
    token_cost: Optional[int] = None,
    user_satisfaction: Optional[int] = None,
) -> float:
    """
    Record the outcome of a routing decision and compute accuracy.

    Args:
        decision_id: The ID returned by record_routing.
        outcome: One of 'success', 'failure', 'escalated', 'user_corrected'.
        db: SQLite connection.
        duration_ms: How long the task took in milliseconds.
        token_cost: Total tokens consumed.
        user_satisfaction: 1-5 rating if user provided feedback.

    Returns:
        Current rolling accuracy (ratio of successes to total completed decisions).
    """
    db.execute(
        """UPDATE routing_decisions
           SET outcome = ?, duration_ms = ?, token_cost = ?, user_satisfaction = ?
           WHERE id = ?""",
        (outcome, duration_ms, token_cost, user_satisfaction, decision_id),
    )
    db.commit()

    # Check if this outcome suggests the routing was wrong
    if outcome in ("failure", "escalated", "user_corrected"):
        _check_correction_from_outcome(decision_id, db)

    # Compute accuracy: successes / total with outcomes
    row = db.execute(
        """SELECT
               COUNT(*) FILTER (WHERE outcome = 'success') AS successes,
               COUNT(*) AS total
           FROM routing_decisions
           WHERE outcome IS NOT NULL"""
    ).fetchone()

    if row:
        total = row[1] if isinstance(row, tuple) else row["total"]
        successes = row[0] if isinstance(row, tuple) else row["successes"]
        if total > 0:
            return successes / total

    return 0.0


def get_learned_thresholds(db: sqlite3.Connection) -> dict:
    """
    Compute optimal complexity thresholds from routing history.

    Requires 50+ samples per tier to produce learned thresholds.
    Uses the average complexity score of successful routings per tier,
    with boundaries at the midpoint between adjacent tier averages.

    Args:
        db: SQLite connection.

    Returns:
        Dict with keys:
        - inline_max: upper complexity bound for inline tier
        - lightweight_max: upper complexity bound for lightweight tier
        - source: 'learned' or 'default'
        - sample_counts: dict of tier -> sample count
        - accuracy_per_tier: dict of tier -> accuracy ratio
    """
    stats = {}
    for tier in ("inline", "lightweight", "full"):
        row = db.execute(
            """SELECT AVG(complexity_score) AS avg_c, COUNT(*) AS cnt
               FROM routing_decisions
               WHERE tier_selected = ? AND outcome = 'success'""",
            (tier,),
        ).fetchone()

        if row:
            avg_c = row[0] if isinstance(row, tuple) else row["avg_c"]
            cnt = row[1] if isinstance(row, tuple) else row["cnt"]
        else:
            avg_c, cnt = None, 0

        stats[tier] = {
            "avg_complexity": avg_c if avg_c is not None else 0.5,
            "count": cnt or 0,
        }

    # Compute per-tier accuracy
    accuracy_per_tier = {}
    for tier in ("inline", "lightweight", "full"):
        row = db.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE outcome = 'success') AS s,
                   COUNT(*) AS t
               FROM routing_decisions
               WHERE tier_selected = ? AND outcome IS NOT NULL""",
            (tier,),
        ).fetchone()
        if row:
            t = row[1] if isinstance(row, tuple) else row["t"]
            s = row[0] if isinstance(row, tuple) else row["s"]
            accuracy_per_tier[tier] = s / t if t > 0 else 0.0
        else:
            accuracy_per_tier[tier] = 0.0

    sample_counts = {t: stats[t]["count"] for t in stats}

    # Only use learned thresholds if we have enough data per tier
    if all(s["count"] >= LEARNED_THRESHOLD_MIN_SAMPLES for s in stats.values()):
        inline_avg = stats["inline"]["avg_complexity"]
        lightweight_avg = stats["lightweight"]["avg_complexity"]
        full_avg = stats["full"]["avg_complexity"]

        # Boundary = midpoint between adjacent tier averages + small buffer
        inline_max = (inline_avg + lightweight_avg) / 2.0
        lightweight_max = (lightweight_avg + full_avg) / 2.0

        return {
            "inline_max": inline_max,
            "lightweight_max": lightweight_max,
            "source": "learned",
            "sample_counts": sample_counts,
            "accuracy_per_tier": accuracy_per_tier,
        }

    # Default thresholds when insufficient data
    return {
        "inline_max": 0.3,
        "lightweight_max": 0.6,
        "source": "default",
        "sample_counts": sample_counts,
        "accuracy_per_tier": accuracy_per_tier,
    }


def record_correction(
    what_wrong: str,
    what_correct: str,
    category: str,
    db: sqlite3.Connection,
) -> int:
    """
    Record a user correction. If the same pattern has been seen 3+ times,
    auto-generates a baseline rule.

    Args:
        what_wrong: Description of what went wrong.
        what_correct: What should have happened instead.
        category: Category of the correction (e.g., 'routing', 'agent_choice').
        db: SQLite connection.

    Returns:
        The correction pattern ID.
    """
    # Check if similar correction exists
    existing = db.execute(
        """SELECT id, occurrence_count FROM correction_patterns
           WHERE pattern LIKE ? LIMIT 1""",
        (f"%{what_wrong[:50]}%",),
    ).fetchone()

    if existing:
        correction_id = existing[0] if isinstance(existing, tuple) else existing["id"]
        old_count = existing[1] if isinstance(existing, tuple) else existing["occurrence_count"]
        new_count = old_count + 1
        db.execute(
            """UPDATE correction_patterns
               SET occurrence_count = ?, last_seen = ?, correction = ?
               WHERE id = ?""",
            (new_count, time.time(), what_correct, correction_id),
        )
        db.commit()

        # Check threshold for auto-generation
        if new_count >= CORRECTION_THRESHOLD:
            auto_generate_baseline(db, correction_id=correction_id)

        return correction_id
    else:
        cursor = db.execute(
            """INSERT INTO correction_patterns
               (pattern, correction, occurrence_count, first_seen, last_seen)
               VALUES (?, ?, 1, ?, ?)""",
            (what_wrong, what_correct, time.time(), time.time()),
        )
        db.commit()
        return cursor.lastrowid


def auto_generate_baseline(
    db: sqlite3.Connection,
    correction_id: Optional[int] = None,
) -> list[int]:
    """
    Auto-generate baseline rules from correction patterns that have
    occurrence_count >= 3 and no existing baseline.

    When triggered, creates a memory_active entry with:
    - importance = 0.9 (high priority for retrieval)
    - tier = 'active'
    - tagged as auto_baseline in metadata

    Args:
        db: SQLite connection.
        correction_id: Optional specific correction to process.

    Returns:
        List of correction IDs that had baselines generated.
    """
    generated = []

    if correction_id is not None:
        rows = db.execute(
            """SELECT id, pattern, correction, occurrence_count
               FROM correction_patterns
               WHERE id = ? AND occurrence_count >= ?
                 AND (baseline_rule IS NULL OR baseline_rule = '')""",
            (correction_id, CORRECTION_THRESHOLD),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT id, pattern, correction, occurrence_count
               FROM correction_patterns
               WHERE occurrence_count >= ?
                 AND (baseline_rule IS NULL OR baseline_rule = '')""",
            (CORRECTION_THRESHOLD,),
        ).fetchall()

    now = time.time()

    for row in rows:
        cid = row[0] if isinstance(row, tuple) else row["id"]
        pattern = row[1] if isinstance(row, tuple) else row["pattern"]
        correction = row[2] if isinstance(row, tuple) else row["correction"]
        count = row[3] if isinstance(row, tuple) else row["occurrence_count"]

        rule = f"LEARNED RULE: When encountering '{pattern}', always '{correction}'"

        db.execute(
            """UPDATE correction_patterns
               SET auto_generated = 1, baseline_rule = ?
               WHERE id = ?""",
            (rule, cid),
        )

        # Create memory_active entry with importance=0.9, tagged as auto_baseline
        entry_id = f"auto_baseline_{uuid.uuid4().hex[:12]}"
        metadata = json.dumps({
            "source": "auto_baseline",
            "correction_id": cid,
            "occurrence_count": count,
            "generated_at": now,
            "tier": "active",
        })

        try:
            db.execute(
                """INSERT INTO memory_active
                   (id, workspace, category, content, metadata, token_count,
                    created_at, last_accessed, access_count, importance,
                    relevance_score, frequency_score, composite_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    "__global__",         # workspace: global
                    "auto_baseline",      # category
                    rule,                 # content
                    metadata,            # metadata JSON
                    len(rule) // 4,      # token_count approximation
                    now,                 # created_at
                    now,                 # last_accessed
                    1,                   # access_count
                    0.9,                 # importance (high for learned rules)
                    0.9,                 # relevance_score (starts high)
                    0.0,                 # frequency_score
                    0.9,                 # composite_score (starts high)
                ),
            )
        except sqlite3.IntegrityError:
            # Entry already exists (shouldn't happen with UUID, but be safe)
            pass

        generated.append(cid)

    if generated:
        db.commit()

    return generated


def retrieval_feedback(
    entry_id: str,
    was_used: bool,
    db: sqlite3.Connection,
) -> None:
    """
    Adjust relevance score of a memory entry based on retrieval feedback.

    Boost +0.05 if the entry was used after retrieval.
    Decay -0.01 if the entry was shown but not used.

    Args:
        entry_id: The memory_active entry ID.
        was_used: Whether the entry was actually used by the agent.
        db: SQLite connection.
    """
    if was_used:
        db.execute(
            """UPDATE memory_active
               SET relevance_score = MIN(1.0, relevance_score + 0.05),
                   last_accessed = ?
               WHERE id = ?""",
            (time.time(), entry_id),
        )
    else:
        db.execute(
            """UPDATE memory_active
               SET relevance_score = MAX(0.0, relevance_score - 0.01)
               WHERE id = ?""",
            (entry_id,),
        )
    db.commit()


def update_trust(
    agent_type: str,
    success: bool,
    db: sqlite3.Connection,
    action_type: str = "general",
) -> float:
    """
    Adjust trust level for an agent based on outcome.

    Uses Bayesian update: trust = (successes + 1) / (successes + failures + 2)
    The +1/+2 is a Beta(1,1) uniform prior.

    Args:
        agent_type: Type of agent (e.g., 'dev', 'devops', 'security').
        success: Whether the agent succeeded.
        db: SQLite connection.
        action_type: Type of action performed.

    Returns:
        The new trust score.
    """
    row = db.execute(
        """SELECT trust_score, success_count, failure_count
           FROM trust_levels
           WHERE agent_type = ? AND action_type = ?""",
        (agent_type, action_type),
    ).fetchone()

    if row:
        successes = row[1] if isinstance(row, tuple) else row["success_count"]
        failures = row[2] if isinstance(row, tuple) else row["failure_count"]
        if success:
            successes += 1
        else:
            failures += 1

        # Bayesian update with Beta(1,1) prior
        new_score = (successes + 1) / (successes + failures + 2)

        db.execute(
            """UPDATE trust_levels
               SET trust_score = ?, success_count = ?, failure_count = ?, last_updated = ?
               WHERE agent_type = ? AND action_type = ?""",
            (new_score, successes, failures, time.time(), agent_type, action_type),
        )
    else:
        # First record for this agent+action
        initial_score = 0.6 if success else 0.4
        successes = 1 if success else 0
        failures = 0 if success else 1
        new_score = initial_score

        db.execute(
            """INSERT INTO trust_levels
               (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_type, action_type, new_score, successes, failures, time.time()),
        )

    db.commit()
    return new_score


def get_trust(
    agent_type: str,
    db: sqlite3.Connection,
    action_type: str = "general",
) -> float:
    """
    Get current trust score for an agent.

    Returns 0.5 (neutral) if no history exists.
    """
    row = db.execute(
        """SELECT trust_score FROM trust_levels
           WHERE agent_type = ? AND action_type = ?""",
        (agent_type, action_type),
    ).fetchone()

    if row:
        return row[0] if isinstance(row, tuple) else row["trust_score"]
    return 0.5


def _check_correction_from_outcome(decision_id: int, db: sqlite3.Connection) -> None:
    """
    When a routing decision fails, check if there's a pattern emerging.
    Records a learning event for analysis.
    """
    row = db.execute(
        """SELECT task_description, tier_selected, complexity_score
           FROM routing_decisions WHERE id = ?""",
        (decision_id,),
    ).fetchone()

    if not row:
        return

    task = row[0] if isinstance(row, tuple) else row["task_description"]
    tier = row[1] if isinstance(row, tuple) else row["tier_selected"]
    complexity = row[2] if isinstance(row, tuple) else row["complexity_score"]

    # Record as a learning event for pattern detection
    try:
        db.execute(
            """INSERT INTO learning_events
               (timestamp, event_type, payload, session_id)
               VALUES (?, 'routing_failure', ?, ?)""",
            (
                time.time(),
                json.dumps({
                    "decision_id": decision_id,
                    "task": task[:200],
                    "tier": tier,
                    "complexity": complexity,
                }),
                "system",
            ),
        )
        db.commit()
    except sqlite3.OperationalError:
        pass

"""
Agent Health Monitor — health inference, token estimation, failure prediction.

Tracks agent performance metrics, infers health from recent events,
estimates token usage, and predicts failure risk for upcoming tasks.

Reference: CAP System Design Section 16C.
"""

import enum
import sqlite3
import time
from typing import Optional


class HealthState(enum.Enum):
    """Agent health states."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class AgentHealthMonitor:
    """
    Monitors agent health based on recent events and baselines.

    Uses agent_health_events and agent_health_baselines tables to:
    - Infer current health state from recent success/failure ratios
    - Estimate token usage from tool input/output sizes
    - Predict failure probability for new tasks
    - Recompute baselines from recent event history
    """

    # Default timeouts per agent type (seconds)
    AGENT_TIMEOUTS = {
        "dev": 300,
        "devops": 300,
        "security": 240,
        "sre": 240,
        "code-review": 180,
        "test": 300,
        "docs": 120,
        "explore": 180,
    }

    # Health thresholds
    DEGRADED_FAILURE_RATE = 0.3   # 30% failure rate = degraded
    UNHEALTHY_FAILURE_RATE = 0.6  # 60% failure rate = unhealthy
    HEALTH_WINDOW = 3600          # 1 hour lookback for health inference

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def infer_health(self, agent_id: str) -> HealthState:
        """
        Infer current health state for an agent based on recent events.

        Looks at events in the last hour and computes failure rate.

        Args:
            agent_id: Agent identifier (e.g., "dev", "dev-abc123").

        Returns:
            HealthState enum value.
        """
        cutoff = time.time() - self.HEALTH_WINDOW

        total = self.db.execute(
            """SELECT COUNT(*) FROM agent_health_events
               WHERE agent_id = ? AND timestamp > ?
               AND event_type IN ('completed', 'failed')""",
            (agent_id, cutoff),
        ).fetchone()[0]

        if total == 0:
            return HealthState.UNKNOWN

        failures = self.db.execute(
            """SELECT COUNT(*) FROM agent_health_events
               WHERE agent_id = ? AND event_type = 'failed' AND timestamp > ?""",
            (agent_id, cutoff),
        ).fetchone()[0]

        failure_rate = failures / total

        if failure_rate >= self.UNHEALTHY_FAILURE_RATE:
            return HealthState.UNHEALTHY
        if failure_rate >= self.DEGRADED_FAILURE_RATE:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def estimate_tokens(self, tool_input: str, tool_output: str) -> int:
        """
        Estimate token count from tool input/output text.

        Uses a simple heuristic: ~4 characters per token (rough approximation
        of BPE tokenization for English text and code). This avoids requiring
        tiktoken as a dependency while providing useful estimates for
        budget tracking.

        Args:
            tool_input: The input text sent to the tool.
            tool_output: The output text received from the tool.

        Returns:
            Estimated total token count (input + output).
        """
        # ~4 chars per token is a reasonable approximation for mixed code/text
        chars_per_token = 4
        input_tokens = max(1, len(tool_input) // chars_per_token) if tool_input else 0
        output_tokens = max(1, len(tool_output) // chars_per_token) if tool_output else 0
        return input_tokens + output_tokens

    def predict_failure_risk(self, task_desc: str, agent_type: str) -> float:
        """
        Predict failure probability for a task given the agent type's history.

        Combines:
        1. Agent's baseline failure rate (from agent_health_baselines)
        2. Recent health trend (improving/degrading)
        3. Task description similarity to past failures (keyword match)

        Args:
            task_desc: Description of the task to be dispatched.
            agent_type: The agent type that would handle this task.

        Returns:
            Float 0.0-1.0 representing estimated failure probability.
        """
        # 1. Get baseline failure rate
        baseline = self.db.execute(
            "SELECT failure_rate, sample_count FROM agent_health_baselines WHERE agent_type = ?",
            (agent_type,),
        ).fetchone()

        if not baseline:
            return 0.1  # Low default risk for unknown agents

        if isinstance(baseline, (tuple, list)):
            base_rate = baseline[0] or 0.0
            sample_count = baseline[1] or 0
        else:
            base_rate = baseline["failure_rate"] or 0.0
            sample_count = baseline["sample_count"] or 0

        # Low confidence with few samples
        if sample_count < 5:
            return min(0.3, base_rate + 0.1)

        # 2. Recent health trend (last 30 min vs last hour)
        now = time.time()
        recent_failures = self.db.execute(
            """SELECT COUNT(*) FROM agent_health_events
               WHERE agent_id LIKE ? AND event_type = 'failed'
               AND timestamp > ?""",
            (f"{agent_type}%", now - 1800),
        ).fetchone()[0]

        recent_total = self.db.execute(
            """SELECT COUNT(*) FROM agent_health_events
               WHERE agent_id LIKE ?
               AND event_type IN ('completed', 'failed')
               AND timestamp > ?""",
            (f"{agent_type}%", now - 1800),
        ).fetchone()[0]

        recent_rate = (recent_failures / recent_total) if recent_total > 0 else base_rate

        # 3. Task similarity to past failures (simple keyword overlap)
        task_words = set(task_desc.lower().split()) if task_desc else set()
        failed_descs = self.db.execute(
            """SELECT task_description FROM routing_decisions
               WHERE agents_used LIKE ? AND outcome = 'failed'
               AND timestamp > ? LIMIT 10""",
            (f'%"{agent_type}"%', now - 86400),
        ).fetchall()

        similarity_boost = 0.0
        if task_words and failed_descs:
            for row in failed_descs:
                desc = row[0] if isinstance(row, (tuple, list)) else row["task_description"]
                if desc:
                    failed_words = set(desc.lower().split())
                    overlap = len(task_words & failed_words) / max(len(task_words), 1)
                    similarity_boost = max(similarity_boost, overlap * 0.2)

        # Combine signals: weighted average of baseline and recent, plus similarity
        risk = (base_rate * 0.4) + (recent_rate * 0.5) + similarity_boost

        return min(1.0, max(0.0, risk))

    def update_baselines(self) -> int:
        """
        Recompute agent_health_baselines from recent events.

        Aggregates the last 7 days of agent_health_events and
        routing_decisions to compute per-agent-type statistics.

        Returns:
            Number of agent types whose baselines were updated.
        """
        cutoff = time.time() - 7 * 86400  # 7 days
        now = time.time()

        # Get all agent types with recent events
        agent_types = self.db.execute(
            """SELECT DISTINCT agent_id FROM agent_health_events
               WHERE timestamp > ?""",
            (cutoff,),
        ).fetchall()

        updated = 0
        for row in agent_types:
            agent_id = row[0] if isinstance(row, (tuple, list)) else row["agent_id"]
            # Use the base agent type (strip instance suffix)
            agent_type = agent_id.split("-")[0] if "-" in agent_id else agent_id

            # Total events
            total = self.db.execute(
                """SELECT COUNT(*) FROM agent_health_events
                   WHERE agent_id LIKE ? AND timestamp > ?
                   AND event_type IN ('completed', 'failed')""",
                (f"{agent_type}%", cutoff),
            ).fetchone()[0]

            if total < 3:
                continue

            # Failure count
            failures = self.db.execute(
                """SELECT COUNT(*) FROM agent_health_events
                   WHERE agent_id LIKE ? AND event_type = 'failed' AND timestamp > ?""",
                (f"{agent_type}%", cutoff),
            ).fetchone()[0]

            failure_rate = failures / total if total > 0 else 0.0

            # Duration stats from routing_decisions
            durations = self.db.execute(
                """SELECT duration_ms FROM routing_decisions
                   WHERE agents_used LIKE ? AND duration_ms IS NOT NULL
                   AND timestamp > ? ORDER BY duration_ms""",
                (f'%"{agent_type}"%', cutoff),
            ).fetchall()

            avg_duration: Optional[float] = None
            p95_duration: Optional[float] = None
            if durations:
                dur_values = [d[0] if isinstance(d, (tuple, list)) else d["duration_ms"] for d in durations]
                avg_duration = sum(dur_values) / len(dur_values)
                p95_idx = int(len(dur_values) * 0.95)
                p95_duration = dur_values[min(p95_idx, len(dur_values) - 1)]

            # Token stats
            tokens = self.db.execute(
                """SELECT AVG(estimated_tokens) FROM agent_health_events
                   WHERE agent_id LIKE ? AND estimated_tokens > 0 AND timestamp > ?""",
                (f"{agent_type}%", cutoff),
            ).fetchone()[0]

            avg_tokens = int(tokens) if tokens else None

            # Tool call count (approximate from events)
            tool_calls = self.db.execute(
                """SELECT COUNT(*) FROM agent_health_events
                   WHERE agent_id LIKE ? AND event_type = 'tool_call' AND timestamp > ?""",
                (f"{agent_type}%", cutoff),
            ).fetchone()[0]

            avg_tool_calls = int(tool_calls / max(total, 1))

            # Upsert baseline
            self.db.execute(
                """INSERT OR REPLACE INTO agent_health_baselines
                   (agent_type, avg_duration, avg_tool_calls, avg_tokens,
                    p95_duration, failure_rate, sample_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_type, avg_duration, avg_tool_calls, avg_tokens,
                 p95_duration, failure_rate, total, now),
            )
            updated += 1

        if updated > 0:
            self.db.commit()

        return updated

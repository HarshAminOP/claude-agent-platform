"""
Cascade Detection — identifies systemic failures across multiple agents.

When 3+ distinct agents fail within a 10-second window, this is likely
an API rate limit or service outage, not isolated agent issues. The
cascade handler pauses the workflow and notifies the user.

Reference: CAP System Design Section 16C.3 and C.4.
"""

import json
import sqlite3
import time
from collections import Counter


CASCADE_WINDOW = 10       # seconds
CASCADE_THRESHOLD = 3     # distinct agent failures in window = cascade


def detect_cascade(db: sqlite3.Connection, window: int = CASCADE_WINDOW, threshold: int = CASCADE_THRESHOLD) -> bool:
    """
    Detect whether a failure cascade is occurring.

    A cascade is defined as 3+ distinct agent IDs failing within the
    specified time window.

    Args:
        db: SQLite connection.
        window: Time window in seconds (default 10).
        threshold: Minimum distinct agent failures to trigger (default 3).

    Returns:
        True if cascade detected.
    """
    recent_failures = db.execute(
        """SELECT COUNT(DISTINCT agent_id) FROM agent_health_events
           WHERE event_type = 'failed' AND timestamp > ?""",
        (time.time() - window,),
    ).fetchone()[0]
    return recent_failures >= threshold


def handle_cascade(db: sqlite3.Connection, workflow_id: str) -> str:
    """
    Handle a detected cascade: pause the workflow and record the event.

    Args:
        db: SQLite connection.
        workflow_id: The workflow to pause.

    Returns:
        User-facing message about the cascade.
    """
    now = time.time()

    # Pause the workflow
    db.execute(
        """UPDATE orchestration_checkpoints SET phase = 'paused_cascade'
           WHERE workflow_id = ? AND phase = 'running'""",
        (workflow_id,),
    )

    # Get affected agent types for the cascade record
    affected = db.execute(
        """SELECT DISTINCT agent_id FROM agent_health_events
           WHERE event_type = 'failed' AND timestamp > ?""",
        (now - CASCADE_WINDOW,),
    ).fetchall()
    agent_types = [row[0] if isinstance(row, (tuple, list)) else row["agent_id"] for row in affected]

    # Record cascade event
    db.execute(
        """INSERT INTO cascade_events
           (workflow_id, detected_at, failure_count, agent_types, resolution)
           VALUES (?, ?, ?, ?, 'paused')""",
        (workflow_id, now, len(agent_types), json.dumps(agent_types)),
    )
    db.commit()

    return (
        "CASCADE DETECTED: Multiple agents failing simultaneously.\n"
        "Likely cause: API rate limit or service outage.\n"
        "Workflow paused. Run `cap resume` when ready, or `cap dlq` to review failures."
    )


def get_failure_pattern(agent_type: str, db: sqlite3.Connection) -> dict:
    """
    Detect whether failures cluster by time (API issue) or task type (capability gap).

    Analyzes the last 24 hours of failures for the given agent type
    in routing_decisions.

    Args:
        agent_type: The agent type to analyze.
        db: SQLite connection.

    Returns:
        Dict with 'pattern' key:
        - {"pattern": "none"} — too few failures to classify
        - {"pattern": "temporal", "likely_cause": "api_issue", "action": "backoff_all"}
        - {"pattern": "task_type", "likely_cause": "capability_gap_<word>", "action": "reroute"}
        - {"pattern": "random", "action": "normal_retry"}
    """
    failures = db.execute(
        """SELECT timestamp, task_description FROM routing_decisions
           WHERE agents_used LIKE ? AND outcome = 'failed'
           AND timestamp > ? ORDER BY timestamp DESC LIMIT 20""",
        (f'%"{agent_type}"%', time.time() - 86400),
    ).fetchall()

    if len(failures) < 3:
        return {"pattern": "none"}

    # Extract values handling both tuple and Row types
    timestamps = []
    descriptions = []
    for f in failures:
        if isinstance(f, (tuple, list)):
            timestamps.append(f[0])
            descriptions.append(f[1])
        else:
            timestamps.append(f["timestamp"])
            descriptions.append(f["task_description"])

    # Time clustering: are failures bunched together?
    gaps = [timestamps[i] - timestamps[i + 1] for i in range(len(timestamps) - 1)]
    avg_gap = sum(gaps) / len(gaps) if gaps else float("inf")

    if avg_gap < 60:  # Failures <1 min apart = API/systemic
        return {"pattern": "temporal", "likely_cause": "api_issue", "action": "backoff_all"}

    # Task clustering: do descriptions share keywords?
    words = Counter()
    for desc in descriptions:
        if desc:
            words.update(desc.lower().split())

    # Remove common stop words for better signal
    stop_words = {"the", "a", "an", "and", "or", "to", "for", "in", "on", "of", "is", "it"}
    for sw in stop_words:
        words.pop(sw, None)

    common = words.most_common(3)
    if common and common[0][1] >= len(failures) * 0.6:
        return {
            "pattern": "task_type",
            "likely_cause": f"capability_gap_{common[0][0]}",
            "action": "reroute",
        }

    return {"pattern": "random", "action": "normal_retry"}

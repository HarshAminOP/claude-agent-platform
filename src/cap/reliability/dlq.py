"""
Dead Letter Queue — stores tasks that exhausted all retries.

Tasks in the DLQ await user action: retry, dismiss, or auto-expire after 7 days.

Reference: CAP System Design Section 16C.2.
"""

import json
import sqlite3
import time
import uuid


def enqueue_dead_letter(
    db: sqlite3.Connection,
    task: dict,
    failures: list[dict],
    workflow_id: str,
) -> str:
    """
    Enqueue a task that exhausted all retries into the dead-letter queue.

    Args:
        db: SQLite connection.
        task: Task dict with keys 'id', 'description', 'agent_type'.
        failures: List of failure dicts (each with 'error', 'timestamp', etc.).
        workflow_id: Workflow that owns this task.

    Returns:
        The task_id stored in the DLQ.
    """
    task_id = task.get("id", str(uuid.uuid4()))
    db.execute(
        """INSERT OR REPLACE INTO dead_letter_queue
           (task_id, workflow_id, task_description, failures_json, agent_type, created_at, expires_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (
            task_id,
            workflow_id,
            task.get("description", ""),
            json.dumps(failures),
            task.get("agent_type", "unknown"),
            time.time(),
            time.time() + 7 * 86400,  # 7-day expiry
        ),
    )
    db.commit()
    return task_id


def list_dlq(db: sqlite3.Connection) -> list[dict]:
    """
    List all pending tasks in the dead-letter queue (non-expired).

    Returns:
        List of dicts with task_id, task_description, agent_type, created_at,
        last_error, workflow_id, status.
    """
    rows = db.execute(
        """SELECT task_id, task_description, agent_type, created_at,
                  failures_json, workflow_id, status
           FROM dead_letter_queue
           WHERE status = 'pending' AND expires_at > ?
           ORDER BY created_at DESC""",
        (time.time(),),
    ).fetchall()

    results = []
    for row in rows:
        if isinstance(row, (tuple, list)):
            task_id, desc, agent_type, created_at, failures_json, wf_id, status = row
        else:
            task_id = row["task_id"]
            desc = row["task_description"]
            agent_type = row["agent_type"]
            created_at = row["created_at"]
            failures_json = row["failures_json"]
            wf_id = row["workflow_id"]
            status = row["status"]

        # Extract last error from failures JSON
        try:
            failures = json.loads(failures_json)
            last_error = failures[-1].get("error", "unknown") if failures else "unknown"
        except (json.JSONDecodeError, IndexError, TypeError):
            last_error = "unknown"

        results.append({
            "task_id": task_id,
            "task_description": desc,
            "agent_type": agent_type,
            "created_at": created_at,
            "last_error": last_error,
            "workflow_id": wf_id,
            "status": status,
        })

    return results


def retry_task(task_id: str, db: sqlite3.Connection) -> dict:
    """
    Mark a DLQ task for retry and return it for re-queuing.

    Args:
        task_id: The task ID to retry.
        db: SQLite connection.

    Returns:
        Dict with task info for re-queuing, or error dict if not found.
    """
    row = db.execute(
        """SELECT task_id, task_description, agent_type, workflow_id, failures_json
           FROM dead_letter_queue WHERE task_id = ? AND status = 'pending'""",
        (task_id,),
    ).fetchone()

    if not row:
        return {"error": f"Task {task_id} not found in DLQ or already processed"}

    if isinstance(row, (tuple, list)):
        tid, desc, agent_type, wf_id, failures_json = row
    else:
        tid = row["task_id"]
        desc = row["task_description"]
        agent_type = row["agent_type"]
        wf_id = row["workflow_id"]
        failures_json = row["failures_json"]

    # Mark as retried
    db.execute(
        "UPDATE dead_letter_queue SET status = 'retried' WHERE task_id = ?",
        (task_id,),
    )
    db.commit()

    return {
        "task_id": tid,
        "description": desc,
        "agent_type": agent_type,
        "workflow_id": wf_id,
        "previous_failures": json.loads(failures_json),
        "status": "retried",
    }


def dismiss_task(task_id: str, db: sqlite3.Connection) -> bool:
    """
    Dismiss a DLQ task (mark as dismissed, won't be retried).

    Args:
        task_id: The task ID to dismiss.
        db: SQLite connection.

    Returns:
        True if task was found and dismissed, False if not found.
    """
    cursor = db.execute(
        "UPDATE dead_letter_queue SET status = 'dismissed' WHERE task_id = ? AND status = 'pending'",
        (task_id,),
    )
    db.commit()
    return cursor.rowcount > 0


def cleanup_expired(db: sqlite3.Connection) -> int:
    """
    Delete DLQ entries that have expired (older than 7 days from creation).

    Returns:
        Number of entries deleted.
    """
    cursor = db.execute(
        "DELETE FROM dead_letter_queue WHERE expires_at <= ?",
        (time.time(),),
    )
    db.commit()
    return cursor.rowcount

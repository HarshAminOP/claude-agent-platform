"""
Checkpoint and resume for CAP orchestration.

Saves DAG state + ContextThread to SQLite so that crashed or interrupted
workflows can be resumed from the last completed step.

Uses the `orchestration_checkpoints` table (Section 15 schema).

Reference: CAP System Design Sections 9 and 18.
"""

import json
import sqlite3
import time
from typing import Optional

from .dag import TaskDAG, StepState
from .context import ContextThread


def save_checkpoint(
    workflow_id: str,
    dag: TaskDAG,
    context_thread: ContextThread,
    db: sqlite3.Connection,
    phase: str = "running",
) -> None:
    """
    Save orchestration checkpoint to SQLite.

    Called after each step completes so that the workflow can resume
    from the last completed step on crash/interrupt.

    Args:
        workflow_id: Unique workflow identifier.
        dag: Current DAG state (includes step states and results).
        context_thread: Current context thread with all frames.
        db: SQLite connection.
        phase: Workflow phase ('running', 'completed', 'failed').
    """
    data = json.dumps({
        "dag": dag.to_dict(),
        "context_thread": context_thread.to_dict(),
        "saved_at": time.time(),
        "phase": phase,
    })

    db.execute(
        """INSERT OR REPLACE INTO orchestration_checkpoints
           (orchestration_id, workflow_id, data, phase, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (workflow_id, workflow_id, data, phase, time.time()),
    )
    db.commit()


def save_initial_checkpoint(
    workflow_id: str,
    plan: TaskDAG,
    db: sqlite3.Connection,
) -> None:
    """
    Save the initial checkpoint at plan-complete, before any dispatch.

    This captures the full plan so that even if the first step fails,
    we can resume with the original DAG structure.

    Args:
        workflow_id: Unique workflow identifier.
        plan: The generated TaskDAG (all steps in PENDING state).
        db: SQLite connection.
    """
    # Create an empty context thread for the initial state
    context_thread = ContextThread(orchestration_id=workflow_id)

    data = json.dumps({
        "dag": plan.to_dict(),
        "context_thread": context_thread.to_dict(),
        "saved_at": time.time(),
        "phase": "planned",
    })

    db.execute(
        """INSERT OR REPLACE INTO orchestration_checkpoints
           (orchestration_id, workflow_id, data, phase, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (workflow_id, workflow_id, data, "planned", time.time()),
    )

    # Also save the plan to orchestration_plans for audit
    db.execute(
        """INSERT OR REPLACE INTO orchestration_plans
           (orchestration_id, plan_data, created_at)
           VALUES (?, ?, ?)""",
        (workflow_id, json.dumps(plan.to_dict()), time.time()),
    )

    db.commit()


def resume_from_checkpoint(
    workflow_id: str,
    db: sqlite3.Connection,
) -> tuple[TaskDAG, ContextThread]:
    """
    Resume a workflow from its last checkpoint.

    Reconstructs the DAG and ContextThread. Steps in COMPLETED state
    are skipped (their results are preserved). Steps in RUNNING state
    are reset to PENDING for re-execution. Steps in PENDING/READY
    are left as-is.

    Args:
        workflow_id: Workflow identifier to resume.
        db: SQLite connection.

    Returns:
        Tuple of (TaskDAG, ContextThread) restored from checkpoint.

    Raises:
        ValueError: If no checkpoint exists for the given workflow_id.
    """
    row = db.execute(
        "SELECT data FROM orchestration_checkpoints WHERE orchestration_id = ?",
        (workflow_id,),
    ).fetchone()

    if not row:
        raise ValueError(f"No checkpoint found for workflow_id={workflow_id}")

    checkpoint_data = json.loads(row[0] if isinstance(row, (tuple, list)) else row["data"])

    # Reconstruct DAG
    dag = TaskDAG.from_dict(checkpoint_data["dag"])

    # Reset RUNNING steps to PENDING (they may have partially completed)
    for step in dag.steps.values():
        if step.state == StepState.RUNNING:
            step.state = StepState.PENDING
            step.result = None

    # Reconstruct ContextThread
    context_thread = ContextThread.from_dict(checkpoint_data["context_thread"])

    return dag, context_thread


def list_checkpoints(
    db: sqlite3.Connection,
    phase: Optional[str] = None,
) -> list[dict]:
    """
    List all checkpoints, optionally filtered by phase.

    Args:
        db: SQLite connection.
        phase: Optional filter ('planned', 'running', 'completed', 'failed').

    Returns:
        List of checkpoint metadata dicts.
    """
    if phase:
        rows = db.execute(
            """SELECT orchestration_id, workflow_id, phase, created_at
               FROM orchestration_checkpoints WHERE phase = ?
               ORDER BY created_at DESC""",
            (phase,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT orchestration_id, workflow_id, phase, created_at
               FROM orchestration_checkpoints
               ORDER BY created_at DESC""",
        ).fetchall()

    return [
        {
            "orchestration_id": row[0] if isinstance(row, (tuple, list)) else row["orchestration_id"],
            "workflow_id": row[1] if isinstance(row, (tuple, list)) else row["workflow_id"],
            "phase": row[2] if isinstance(row, (tuple, list)) else row["phase"],
            "created_at": row[3] if isinstance(row, (tuple, list)) else row["created_at"],
        }
        for row in rows
    ]


def delete_checkpoint(workflow_id: str, db: sqlite3.Connection) -> bool:
    """
    Delete a checkpoint by workflow_id.

    Returns True if a checkpoint was deleted, False if not found.
    """
    cursor = db.execute(
        "DELETE FROM orchestration_checkpoints WHERE orchestration_id = ?",
        (workflow_id,),
    )
    db.commit()
    return cursor.rowcount > 0

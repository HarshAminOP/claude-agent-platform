"""Persistent backlog — SQLite task queue for agent orchestration.

Tasks are created by the PO or agents, stored persistently, and picked up
by agents based on priority and readiness. Supports acceptance criteria
and auto-verification by the scrum-master agent.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    backlog = "backlog"
    ready = "ready"
    in_progress = "in_progress"
    in_review = "in_review"
    blocked = "blocked"
    done = "done"
    cancelled = "cancelled"


class TaskPriority(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


@dataclass
class AcceptanceCriterion:
    description: str
    verified: bool = False
    verified_by: str = ""
    verified_at: Optional[str] = None


@dataclass
class BacklogTask:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    priority: TaskPriority = TaskPriority.medium
    status: TaskStatus = TaskStatus.backlog
    assigned_to: str = ""
    created_by: str = ""
    workflow_id: str = ""
    parent_id: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    acceptance_criteria: List[AcceptanceCriterion] = field(default_factory=list)
    output: str = ""
    error: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def all_criteria_met(self) -> bool:
        if not self.acceptance_criteria:
            return True
        return all(c.verified for c in self.acceptance_criteria)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority.value,
            "status": self.status.value,
            "assigned_to": self.assigned_to,
            "created_by": self.created_by,
            "workflow_id": self.workflow_id,
            "parent_id": self.parent_id,
            "depends_on": self.depends_on,
            "labels": self.labels,
            "acceptance_criteria": [
                {
                    "description": c.description,
                    "verified": c.verified,
                    "verified_by": c.verified_by,
                    "verified_at": c.verified_at,
                }
                for c in self.acceptance_criteria
            ],
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BacklogTask":
        criteria = [
            AcceptanceCriterion(
                description=c["description"],
                verified=c.get("verified", False),
                verified_by=c.get("verified_by", ""),
                verified_at=c.get("verified_at"),
            )
            for c in data.get("acceptance_criteria", [])
        ]
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", ""),
            description=data.get("description", ""),
            priority=TaskPriority(data.get("priority", "medium")),
            status=TaskStatus(data.get("status", "backlog")),
            assigned_to=data.get("assigned_to", ""),
            created_by=data.get("created_by", ""),
            workflow_id=data.get("workflow_id", ""),
            parent_id=data.get("parent_id"),
            depends_on=data.get("depends_on", []),
            labels=data.get("labels", []),
            acceptance_criteria=criteria,
            output=data.get("output", ""),
            error=data.get("error", ""),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backlog_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'backlog',
    assigned_to TEXT,
    created_by TEXT,
    workflow_id TEXT,
    parent_id TEXT,
    depends_on TEXT DEFAULT '[]',
    labels TEXT DEFAULT '[]',
    acceptance_criteria TEXT DEFAULT '[]',
    output TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bt_status ON backlog_tasks(status);
CREATE INDEX IF NOT EXISTS idx_bt_priority ON backlog_tasks(priority);
CREATE INDEX IF NOT EXISTS idx_bt_assigned ON backlog_tasks(assigned_to);
CREATE INDEX IF NOT EXISTS idx_bt_workflow ON backlog_tasks(workflow_id);
CREATE INDEX IF NOT EXISTS idx_bt_parent ON backlog_tasks(parent_id);
"""


def init_backlog_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ── CRUD ───────────────────────────────────────────────────────────────────────

def create_task(conn: sqlite3.Connection, task: BacklogTask) -> BacklogTask:
    conn.execute(
        """INSERT INTO backlog_tasks
           (id, title, description, priority, status, assigned_to, created_by,
            workflow_id, parent_id, depends_on, labels, acceptance_criteria,
            output, error, started_at, completed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task.id, task.title, task.description, task.priority.value,
            task.status.value, task.assigned_to, task.created_by,
            task.workflow_id, task.parent_id,
            json.dumps(task.depends_on), json.dumps(task.labels),
            json.dumps([c.__dict__ for c in task.acceptance_criteria]),
            task.output, task.error,
            task.started_at, task.completed_at,
            task.created_at, task.updated_at,
        ),
    )
    conn.commit()
    return task


def update_task(conn: sqlite3.Connection, task: BacklogTask) -> None:
    task.updated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE backlog_tasks SET
            title = ?, description = ?, priority = ?, status = ?,
            assigned_to = ?, workflow_id = ?, parent_id = ?,
            depends_on = ?, labels = ?, acceptance_criteria = ?,
            output = ?, error = ?, started_at = ?, completed_at = ?,
            updated_at = ?
           WHERE id = ?""",
        (
            task.title, task.description, task.priority.value, task.status.value,
            task.assigned_to, task.workflow_id, task.parent_id,
            json.dumps(task.depends_on), json.dumps(task.labels),
            json.dumps([c.__dict__ for c in task.acceptance_criteria]),
            task.output, task.error,
            task.started_at, task.completed_at,
            task.updated_at, task.id,
        ),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[BacklogTask]:
    row = conn.execute(
        """SELECT id, title, description, priority, status, assigned_to, created_by,
                  workflow_id, parent_id, depends_on, labels, acceptance_criteria,
                  output, error, started_at, completed_at, created_at, updated_at
           FROM backlog_tasks WHERE id = ?""",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_task(row)


def claim_next_task(
    conn: sqlite3.Connection,
    agent_id: str,
    labels: Optional[List[str]] = None,
) -> Optional[BacklogTask]:
    """Atomically claim the next ready task for an agent.

    Priority ordering: critical > high > medium > low, then oldest first.
    Skips tasks with unmet dependencies.
    """
    priority_order = "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END"

    label_filter = ""
    params: list = []
    if labels:
        placeholders = ",".join("?" * len(labels))
        label_filter = f"AND EXISTS (SELECT 1 FROM json_each(labels) WHERE value IN ({placeholders}))"
        params.extend(labels)

    rows = conn.execute(
        f"""SELECT id, title, description, priority, status, assigned_to, created_by,
                  workflow_id, parent_id, depends_on, labels, acceptance_criteria,
                  output, error, started_at, completed_at, created_at, updated_at
           FROM backlog_tasks
           WHERE status = 'ready' {label_filter}
           ORDER BY {priority_order}, created_at ASC
           LIMIT 20""",
        params,
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        task = _row_to_task(row)
        if task.depends_on:
            unmet = conn.execute(
                f"SELECT COUNT(*) FROM backlog_tasks WHERE id IN ({','.join('?' * len(task.depends_on))}) AND status != 'done'",
                task.depends_on,
            ).fetchone()[0]
            if unmet > 0:
                continue

        cursor = conn.execute(
            "UPDATE backlog_tasks SET status = 'in_progress', assigned_to = ?, started_at = ?, updated_at = ? WHERE id = ? AND status = 'ready'",
            (agent_id, now, now, task.id),
        )
        if cursor.rowcount > 0:
            conn.commit()
            task.status = TaskStatus.in_progress
            task.assigned_to = agent_id
            task.started_at = now
            return task

    return None


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    output: str = "",
    status: TaskStatus = TaskStatus.done,
) -> Optional[BacklogTask]:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE backlog_tasks SET status = ?, output = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (status.value, output, now, now, task_id),
    )
    conn.commit()
    return get_task(conn, task_id)


def verify_criteria(
    conn: sqlite3.Connection,
    task_id: str,
    criterion_index: int,
    verified_by: str,
    verified: bool = True,
) -> Optional[BacklogTask]:
    """Mark a specific acceptance criterion as verified/failed."""
    task = get_task(conn, task_id)
    if not task or criterion_index >= len(task.acceptance_criteria):
        return None

    now = datetime.now(timezone.utc).isoformat()
    task.acceptance_criteria[criterion_index].verified = verified
    task.acceptance_criteria[criterion_index].verified_by = verified_by
    task.acceptance_criteria[criterion_index].verified_at = now
    update_task(conn, task)
    return task


def list_tasks(
    conn: sqlite3.Connection,
    status: Optional[TaskStatus] = None,
    workflow_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
    limit: int = 100,
) -> List[BacklogTask]:
    conditions = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status.value)
    if workflow_id:
        conditions.append("workflow_id = ?")
        params.append(workflow_id)
    if assigned_to:
        conditions.append("assigned_to = ?")
        params.append(assigned_to)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, title, description, priority, status, assigned_to, created_by,
                  workflow_id, parent_id, depends_on, labels, acceptance_criteria,
                  output, error, started_at, completed_at, created_at, updated_at
           FROM backlog_tasks {where}
           ORDER BY
               CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END,
               created_at ASC
           LIMIT ?""",
        params,
    ).fetchall()

    return [_row_to_task(r) for r in rows]


def backlog_stats(conn: sqlite3.Connection, workflow_id: Optional[str] = None) -> Dict[str, Any]:
    where = "WHERE workflow_id = ?" if workflow_id else ""
    params = [workflow_id] if workflow_id else []

    rows = conn.execute(
        f"SELECT status, COUNT(*) FROM backlog_tasks {where} GROUP BY status",
        params,
    ).fetchall()

    stats = {r[0]: r[1] for r in rows}
    total = sum(stats.values())
    done = stats.get("done", 0)
    return {
        "total": total,
        "by_status": stats,
        "completion_pct": round(done / max(total, 1) * 100, 1),
        "blocked": stats.get("blocked", 0),
        "in_progress": stats.get("in_progress", 0),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row_to_task(row: tuple) -> BacklogTask:
    criteria_raw = json.loads(row[11]) if row[11] else []
    criteria = [
        AcceptanceCriterion(
            description=c.get("description", ""),
            verified=c.get("verified", False),
            verified_by=c.get("verified_by", ""),
            verified_at=c.get("verified_at"),
        )
        for c in criteria_raw
    ]
    return BacklogTask(
        id=row[0], title=row[1], description=row[2] or "",
        priority=TaskPriority(row[3]),
        status=TaskStatus(row[4]),
        assigned_to=row[5] or "", created_by=row[6] or "",
        workflow_id=row[7] or "", parent_id=row[8],
        depends_on=json.loads(row[9]) if row[9] else [],
        labels=json.loads(row[10]) if row[10] else [],
        acceptance_criteria=criteria,
        output=row[12] or "", error=row[13] or "",
        started_at=row[14], completed_at=row[15],
        created_at=row[16], updated_at=row[17],
    )

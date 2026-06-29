"""Reasoning traces — "why did you do this?" for every agent action.

Records the chain of reasoning that led to each decision, making agent
behavior auditable and debuggable. Stored in platform.db alongside workflow events.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ReasoningStep:
    description: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 1.0
    alternatives_considered: List[str] = field(default_factory=list)
    rejected_reason: str = ""


@dataclass
class ReasoningTrace:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    workflow_id: str = ""
    action: str = ""
    decision: str = ""
    steps: List[ReasoningStep] = field(default_factory=list)
    context_used: List[str] = field(default_factory=list)
    tools_invoked: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    duration_ms: int = 0
    tokens_used: int = 0
    model: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "workflow_id": self.workflow_id,
            "action": self.action,
            "decision": self.decision,
            "steps": [
                {
                    "description": s.description,
                    "evidence": s.evidence,
                    "confidence": s.confidence,
                    "alternatives_considered": s.alternatives_considered,
                    "rejected_reason": s.rejected_reason,
                }
                for s in self.steps
            ],
            "context_used": self.context_used,
            "tools_invoked": self.tools_invoked,
            "files_modified": self.files_modified,
            "duration_ms": self.duration_ms,
            "tokens_used": self.tokens_used,
            "model": self.model,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReasoningTrace":
        steps = [
            ReasoningStep(
                description=s["description"],
                evidence=s.get("evidence", []),
                confidence=s.get("confidence", 1.0),
                alternatives_considered=s.get("alternatives_considered", []),
                rejected_reason=s.get("rejected_reason", ""),
            )
            for s in data.get("steps", [])
        ]
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            agent_id=data.get("agent_id", ""),
            workflow_id=data.get("workflow_id", ""),
            action=data.get("action", ""),
            decision=data.get("decision", ""),
            steps=steps,
            context_used=data.get("context_used", []),
            tools_invoked=data.get("tools_invoked", []),
            files_modified=data.get("files_modified", []),
            duration_ms=data.get("duration_ms", 0),
            tokens_used=data.get("tokens_used", 0),
            model=data.get("model", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reasoning_traces (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    workflow_id TEXT,
    action TEXT NOT NULL,
    decision TEXT,
    steps TEXT DEFAULT '[]',
    context_used TEXT DEFAULT '[]',
    tools_invoked TEXT DEFAULT '[]',
    files_modified TEXT DEFAULT '[]',
    duration_ms INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rt_agent ON reasoning_traces(agent_id);
CREATE INDEX IF NOT EXISTS idx_rt_workflow ON reasoning_traces(workflow_id);
CREATE INDEX IF NOT EXISTS idx_rt_action ON reasoning_traces(action);
CREATE INDEX IF NOT EXISTS idx_rt_created ON reasoning_traces(created_at);
"""


def init_traces_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def record_trace(conn: sqlite3.Connection, trace: ReasoningTrace) -> None:
    conn.execute(
        """INSERT INTO reasoning_traces
           (id, agent_id, workflow_id, action, decision, steps, context_used,
            tools_invoked, files_modified, duration_ms, tokens_used, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trace.id, trace.agent_id, trace.workflow_id, trace.action, trace.decision,
            json.dumps([s.__dict__ for s in trace.steps]),
            json.dumps(trace.context_used),
            json.dumps(trace.tools_invoked),
            json.dumps(trace.files_modified),
            trace.duration_ms, trace.tokens_used, trace.model, trace.created_at,
        ),
    )
    conn.commit()


def get_trace(conn: sqlite3.Connection, trace_id: str) -> Optional[ReasoningTrace]:
    row = conn.execute(
        """SELECT id, agent_id, workflow_id, action, decision, steps, context_used,
                  tools_invoked, files_modified, duration_ms, tokens_used, model, created_at
           FROM reasoning_traces WHERE id = ?""",
        (trace_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_trace(row)


def list_traces(
    conn: sqlite3.Connection,
    agent_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 50,
) -> List[ReasoningTrace]:
    conditions = []
    params: list = []
    if agent_id:
        conditions.append("agent_id = ?")
        params.append(agent_id)
    if workflow_id:
        conditions.append("workflow_id = ?")
        params.append(workflow_id)
    if action:
        conditions.append("action LIKE ?")
        params.append(f"%{action}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, agent_id, workflow_id, action, decision, steps, context_used,
                  tools_invoked, files_modified, duration_ms, tokens_used, model, created_at
           FROM reasoning_traces {where} ORDER BY created_at DESC LIMIT ?""",
        params,
    ).fetchall()

    return [_row_to_trace(r) for r in rows]


def explain_decision(conn: sqlite3.Connection, workflow_id: str, action: str) -> List[ReasoningTrace]:
    """Find all traces related to a specific action in a workflow — the "why" query."""
    return list_traces(conn, workflow_id=workflow_id, action=action)


def _row_to_trace(row: tuple) -> ReasoningTrace:
    steps_raw = json.loads(row[5]) if row[5] else []
    steps = [
        ReasoningStep(
            description=s.get("description", ""),
            evidence=s.get("evidence", []),
            confidence=s.get("confidence", 1.0),
            alternatives_considered=s.get("alternatives_considered", []),
            rejected_reason=s.get("rejected_reason", ""),
        )
        for s in steps_raw
    ]
    return ReasoningTrace(
        id=row[0], agent_id=row[1] or "", workflow_id=row[2] or "",
        action=row[3], decision=row[4] or "",
        steps=steps,
        context_used=json.loads(row[6]) if row[6] else [],
        tools_invoked=json.loads(row[7]) if row[7] else [],
        files_modified=json.loads(row[8]) if row[8] else [],
        duration_ms=row[9] or 0, tokens_used=row[10] or 0,
        model=row[11] or "", created_at=row[12],
    )

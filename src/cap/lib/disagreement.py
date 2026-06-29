"""Inter-agent disagreement protocol.

When agents disagree (e.g., security agent blocks a devops change), this
module escalates the conflict to the PO with both sides' arguments,
and records the resolution for future reference.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ConflictSeverity(str, Enum):
    advisory = "advisory"
    warning = "warning"
    blocking = "blocking"


class ConflictStatus(str, Enum):
    open = "open"
    escalated = "escalated"
    resolved = "resolved"
    overridden = "overridden"


class Resolution(str, Enum):
    side_a_wins = "side_a_wins"
    side_b_wins = "side_b_wins"
    compromise = "compromise"
    deferred = "deferred"
    overridden_by_po = "overridden_by_po"


@dataclass
class ConflictSide:
    agent_id: str
    agent_type: str
    position: str
    evidence: List[str] = field(default_factory=list)
    risk_assessment: str = ""
    proposed_action: str = ""


@dataclass
class Conflict:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    workflow_id: str = ""
    phase: str = ""
    severity: ConflictSeverity = ConflictSeverity.warning
    status: ConflictStatus = ConflictStatus.open
    side_a: Optional[ConflictSide] = None
    side_b: Optional[ConflictSide] = None
    resolution: Optional[Resolution] = None
    resolution_notes: str = ""
    resolved_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: Optional[str] = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == ConflictSeverity.blocking and self.status != ConflictStatus.resolved

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "workflow_id": self.workflow_id,
            "phase": self.phase,
            "severity": self.severity.value,
            "status": self.status.value,
            "side_a": _side_to_dict(self.side_a) if self.side_a else None,
            "side_b": _side_to_dict(self.side_b) if self.side_b else None,
            "resolution": self.resolution.value if self.resolution else None,
            "resolution_notes": self.resolution_notes,
            "resolved_by": self.resolved_by,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Conflict":
        side_a = _dict_to_side(data["side_a"]) if data.get("side_a") else None
        side_b = _dict_to_side(data["side_b"]) if data.get("side_b") else None
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", ""),
            workflow_id=data.get("workflow_id", ""),
            phase=data.get("phase", ""),
            severity=ConflictSeverity(data.get("severity", "warning")),
            status=ConflictStatus(data.get("status", "open")),
            side_a=side_a,
            side_b=side_b,
            resolution=Resolution(data["resolution"]) if data.get("resolution") else None,
            resolution_notes=data.get("resolution_notes", ""),
            resolved_by=data.get("resolved_by", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            resolved_at=data.get("resolved_at"),
        )


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workflow_id TEXT,
    phase TEXT,
    severity TEXT NOT NULL DEFAULT 'warning',
    status TEXT NOT NULL DEFAULT 'open',
    side_a TEXT,
    side_b TEXT,
    resolution TEXT,
    resolution_notes TEXT,
    resolved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status);
CREATE INDEX IF NOT EXISTS idx_conflicts_workflow ON conflicts(workflow_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_severity ON conflicts(severity);
"""


def init_conflicts_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ── Operations ─────────────────────────────────────────────────────────────────

def raise_conflict(conn: sqlite3.Connection, conflict: Conflict) -> Conflict:
    """Raise a new conflict. Auto-escalates blocking conflicts."""
    if conflict.severity == ConflictSeverity.blocking:
        conflict.status = ConflictStatus.escalated

    conn.execute(
        """INSERT INTO conflicts
           (id, title, workflow_id, phase, severity, status, side_a, side_b,
            resolution, resolution_notes, resolved_by, created_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conflict.id, conflict.title, conflict.workflow_id, conflict.phase,
            conflict.severity.value, conflict.status.value,
            json.dumps(_side_to_dict(conflict.side_a)) if conflict.side_a else None,
            json.dumps(_side_to_dict(conflict.side_b)) if conflict.side_b else None,
            conflict.resolution.value if conflict.resolution else None,
            conflict.resolution_notes, conflict.resolved_by,
            conflict.created_at, conflict.resolved_at,
        ),
    )
    conn.commit()
    return conflict


def resolve_conflict(
    conn: sqlite3.Connection,
    conflict_id: str,
    resolution: Resolution,
    resolved_by: str = "po",
    notes: str = "",
) -> Optional[Conflict]:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE conflicts
           SET status = 'resolved', resolution = ?, resolution_notes = ?,
               resolved_by = ?, resolved_at = ?
           WHERE id = ?""",
        (resolution.value, notes, resolved_by, now, conflict_id),
    )
    conn.commit()
    return get_conflict(conn, conflict_id)


def override_conflict(
    conn: sqlite3.Connection,
    conflict_id: str,
    notes: str = "",
) -> Optional[Conflict]:
    """PO override — forces through despite blocking agent."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE conflicts
           SET status = 'overridden', resolution = 'overridden_by_po',
               resolution_notes = ?, resolved_by = 'po', resolved_at = ?
           WHERE id = ?""",
        (notes, now, conflict_id),
    )
    conn.commit()
    return get_conflict(conn, conflict_id)


def get_conflict(conn: sqlite3.Connection, conflict_id: str) -> Optional[Conflict]:
    row = conn.execute(
        """SELECT id, title, workflow_id, phase, severity, status,
                  side_a, side_b, resolution, resolution_notes, resolved_by,
                  created_at, resolved_at
           FROM conflicts WHERE id = ?""",
        (conflict_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_conflict(row)


def list_conflicts(
    conn: sqlite3.Connection,
    status: Optional[ConflictStatus] = None,
    workflow_id: Optional[str] = None,
    severity: Optional[ConflictSeverity] = None,
    limit: int = 50,
) -> List[Conflict]:
    conditions = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status.value)
    if workflow_id:
        conditions.append("workflow_id = ?")
        params.append(workflow_id)
    if severity:
        conditions.append("severity = ?")
        params.append(severity.value)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, title, workflow_id, phase, severity, status,
                  side_a, side_b, resolution, resolution_notes, resolved_by,
                  created_at, resolved_at
           FROM conflicts {where} ORDER BY created_at DESC LIMIT ?""",
        params,
    ).fetchall()

    return [_row_to_conflict(r) for r in rows]


def get_blocking_conflicts(conn: sqlite3.Connection, workflow_id: str) -> List[Conflict]:
    """Return all unresolved blocking conflicts for a workflow."""
    return list_conflicts(
        conn,
        status=ConflictStatus.escalated,
        workflow_id=workflow_id,
        severity=ConflictSeverity.blocking,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _side_to_dict(side: Optional[ConflictSide]) -> Optional[Dict[str, Any]]:
    if not side:
        return None
    return {
        "agent_id": side.agent_id,
        "agent_type": side.agent_type,
        "position": side.position,
        "evidence": side.evidence,
        "risk_assessment": side.risk_assessment,
        "proposed_action": side.proposed_action,
    }


def _dict_to_side(data: Optional[Dict[str, Any]]) -> Optional[ConflictSide]:
    if not data:
        return None
    return ConflictSide(
        agent_id=data.get("agent_id", ""),
        agent_type=data.get("agent_type", ""),
        position=data.get("position", ""),
        evidence=data.get("evidence", []),
        risk_assessment=data.get("risk_assessment", ""),
        proposed_action=data.get("proposed_action", ""),
    )


def _row_to_conflict(row: tuple) -> Conflict:
    side_a = json.loads(row[6]) if row[6] else None
    side_b = json.loads(row[7]) if row[7] else None
    return Conflict(
        id=row[0], title=row[1], workflow_id=row[2] or "", phase=row[3] or "",
        severity=ConflictSeverity(row[4]),
        status=ConflictStatus(row[5]),
        side_a=_dict_to_side(side_a),
        side_b=_dict_to_side(side_b),
        resolution=Resolution(row[8]) if row[8] else None,
        resolution_notes=row[9] or "",
        resolved_by=row[10] or "",
        created_at=row[11], resolved_at=row[12],
    )

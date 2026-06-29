"""Structured decision cards for PO-agent interaction.

Agents present options with tradeoffs; PO picks. Each decision is recorded
in the session memory for future recall.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class DecisionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    deferred = "deferred"
    superseded = "superseded"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


@dataclass
class Option:
    label: str
    description: str
    tradeoffs: Dict[str, str] = field(default_factory=dict)
    risk: RiskLevel = RiskLevel.low
    estimated_effort: str = ""
    recommended: bool = False


@dataclass
class DecisionCard:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    context: str = ""
    options: List[Option] = field(default_factory=list)
    recommendation_index: int = -1
    recommendation_rationale: str = ""
    deadline: Optional[str] = None
    domain: str = ""
    agent_id: str = ""
    workflow_id: str = ""
    status: DecisionStatus = DecisionStatus.pending
    chosen_option: int = -1
    po_notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "context": self.context,
            "options": [
                {
                    "label": o.label,
                    "description": o.description,
                    "tradeoffs": o.tradeoffs,
                    "risk": o.risk.value,
                    "estimated_effort": o.estimated_effort,
                    "recommended": o.recommended,
                }
                for o in self.options
            ],
            "recommendation_index": self.recommendation_index,
            "recommendation_rationale": self.recommendation_rationale,
            "deadline": self.deadline,
            "domain": self.domain,
            "agent_id": self.agent_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "chosen_option": self.chosen_option,
            "po_notes": self.po_notes,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionCard":
        options = [
            Option(
                label=o["label"],
                description=o["description"],
                tradeoffs=o.get("tradeoffs", {}),
                risk=RiskLevel(o.get("risk", "low")),
                estimated_effort=o.get("estimated_effort", ""),
                recommended=o.get("recommended", False),
            )
            for o in data.get("options", [])
        ]
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            title=data.get("title", ""),
            context=data.get("context", ""),
            options=options,
            recommendation_index=data.get("recommendation_index", -1),
            recommendation_rationale=data.get("recommendation_rationale", ""),
            deadline=data.get("deadline"),
            domain=data.get("domain", ""),
            agent_id=data.get("agent_id", ""),
            workflow_id=data.get("workflow_id", ""),
            status=DecisionStatus(data.get("status", "pending")),
            chosen_option=data.get("chosen_option", -1),
            po_notes=data.get("po_notes", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            resolved_at=data.get("resolved_at"),
        )


# ── Storage ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_cards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    context TEXT,
    options TEXT NOT NULL,
    recommendation_index INTEGER DEFAULT -1,
    recommendation_rationale TEXT,
    deadline TEXT,
    domain TEXT,
    agent_id TEXT,
    workflow_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    chosen_option INTEGER DEFAULT -1,
    po_notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_status ON decision_cards(status);
CREATE INDEX IF NOT EXISTS idx_dc_workflow ON decision_cards(workflow_id);
CREATE INDEX IF NOT EXISTS idx_dc_domain ON decision_cards(domain);
"""


def init_decision_cards_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def save_card(conn: sqlite3.Connection, card: DecisionCard) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO decision_cards
           (id, title, context, options, recommendation_index, recommendation_rationale,
            deadline, domain, agent_id, workflow_id, status, chosen_option, po_notes,
            created_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            card.id, card.title, card.context,
            json.dumps([o.__dict__ | {"risk": o.risk.value} for o in card.options]),
            card.recommendation_index, card.recommendation_rationale,
            card.deadline, card.domain, card.agent_id, card.workflow_id,
            card.status.value, card.chosen_option, card.po_notes,
            card.created_at, card.resolved_at,
        ),
    )
    conn.commit()


def resolve_card(
    conn: sqlite3.Connection,
    card_id: str,
    chosen_option: int,
    status: DecisionStatus = DecisionStatus.approved,
    po_notes: str = "",
) -> Optional[DecisionCard]:
    card = get_card(conn, card_id)
    if not card:
        return None
    if status != DecisionStatus.rejected:
        if chosen_option < 0 or chosen_option >= len(card.options):
            return None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE decision_cards
           SET status = ?, chosen_option = ?, po_notes = ?, resolved_at = ?
           WHERE id = ?""",
        (status.value, chosen_option, po_notes, now, card_id),
    )
    conn.commit()
    return get_card(conn, card_id)


def get_card(conn: sqlite3.Connection, card_id: str) -> Optional[DecisionCard]:
    row = conn.execute(
        """SELECT id, title, context, options, recommendation_index,
                  recommendation_rationale, deadline, domain, agent_id, workflow_id,
                  status, chosen_option, po_notes, created_at, resolved_at
           FROM decision_cards WHERE id = ?""",
        (card_id,),
    ).fetchone()
    if not row:
        return None
    return DecisionCard.from_dict({
        "id": row[0], "title": row[1], "context": row[2],
        "options": json.loads(row[3]),
        "recommendation_index": row[4], "recommendation_rationale": row[5],
        "deadline": row[6], "domain": row[7], "agent_id": row[8],
        "workflow_id": row[9], "status": row[10], "chosen_option": row[11],
        "po_notes": row[12], "created_at": row[13], "resolved_at": row[14],
    })


def list_cards(
    conn: sqlite3.Connection,
    status: Optional[DecisionStatus] = None,
    workflow_id: Optional[str] = None,
    limit: int = 50,
) -> List[DecisionCard]:
    conditions = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status.value)
    if workflow_id:
        conditions.append("workflow_id = ?")
        params.append(workflow_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, title, context, options, recommendation_index,
                  recommendation_rationale, deadline, domain, agent_id, workflow_id,
                  status, chosen_option, po_notes, created_at, resolved_at
           FROM decision_cards {where} ORDER BY created_at DESC LIMIT ?""",
        params,
    ).fetchall()

    return [
        DecisionCard.from_dict({
            "id": r[0], "title": r[1], "context": r[2],
            "options": json.loads(r[3]),
            "recommendation_index": r[4], "recommendation_rationale": r[5],
            "deadline": r[6], "domain": r[7], "agent_id": r[8],
            "workflow_id": r[9], "status": r[10], "chosen_option": r[11],
            "po_notes": r[12], "created_at": r[13], "resolved_at": r[14],
        })
        for r in rows
    ]

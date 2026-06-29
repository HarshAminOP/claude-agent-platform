"""Progressive autonomy — earned trust per change type.

Tracks success rates per (agent_type, action_type) pair. As an agent
proves reliable for a specific action, its autonomy level increases,
reducing the need for PO approval on routine operations.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class AutonomyLevel:
    agent_type: str
    action_type: str
    level: int  # 0=always_ask, 1=ask_first_time, 2=ask_on_risk, 3=auto
    success_count: int = 0
    failure_count: int = 0
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    promoted_at: Optional[str] = None

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total

    @property
    def total_actions(self) -> int:
        return self.success_count + self.failure_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "action_type": self.action_type,
            "level": self.level,
            "level_name": _LEVEL_NAMES.get(self.level, "unknown"),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 3),
            "total_actions": self.total_actions,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "promoted_at": self.promoted_at,
        }


_LEVEL_NAMES = {
    0: "always_ask",
    1: "ask_first_time",
    2: "ask_on_risk",
    3: "auto",
}

# Thresholds for promotion
_PROMOTION_THRESHOLDS = {
    0: {"min_successes": 3, "min_rate": 0.9},   # 0 -> 1
    1: {"min_successes": 10, "min_rate": 0.95},  # 1 -> 2
    2: {"min_successes": 25, "min_rate": 0.98},  # 2 -> 3
}

# Any failure at level 3 demotes back to 2
_DEMOTION_THRESHOLD = 2  # failures before demotion


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS autonomy_levels (
    agent_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    promoted_at TEXT,
    PRIMARY KEY (agent_type, action_type)
);

CREATE TABLE IF NOT EXISTS autonomy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details TEXT,
    level_before INTEGER,
    level_after INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_al_agent_action ON autonomy_log(agent_type, action_type);
"""


def init_autonomy_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ── Core operations ────────────────────────────────────────────────────────────

def get_autonomy_level(conn: sqlite3.Connection, agent_type: str, action_type: str) -> AutonomyLevel:
    """Get current autonomy level for an agent/action pair."""
    row = conn.execute(
        """SELECT level, success_count, failure_count, last_success_at, last_failure_at, promoted_at
           FROM autonomy_levels WHERE agent_type = ? AND action_type = ?""",
        (agent_type, action_type),
    ).fetchone()

    if not row:
        return AutonomyLevel(agent_type=agent_type, action_type=action_type, level=0)

    return AutonomyLevel(
        agent_type=agent_type,
        action_type=action_type,
        level=row[0],
        success_count=row[1],
        failure_count=row[2],
        last_success_at=row[3],
        last_failure_at=row[4],
        promoted_at=row[5],
    )


def should_ask_approval(conn: sqlite3.Connection, agent_type: str, action_type: str, risk_level: str = "low") -> bool:
    """Determine if an action requires PO approval based on earned trust."""
    al = get_autonomy_level(conn, agent_type, action_type)

    if al.level == 0:
        return True
    elif al.level == 1:
        return al.total_actions == 0
    elif al.level == 2:
        return risk_level in ("high", "critical")
    else:  # level 3
        return risk_level == "critical"


def record_outcome(
    conn: sqlite3.Connection,
    agent_type: str,
    action_type: str,
    success: bool,
    details: str = "",
) -> AutonomyLevel:
    """Record an action outcome and potentially promote/demote."""
    now = datetime.now(timezone.utc).isoformat()
    al = get_autonomy_level(conn, agent_type, action_type)
    level_before = al.level

    if success:
        al.success_count += 1
        al.last_success_at = now
    else:
        al.failure_count += 1
        al.last_failure_at = now

    # Check promotion
    if success and al.level < 3:
        threshold = _PROMOTION_THRESHOLDS.get(al.level)
        if threshold:
            if (al.success_count >= threshold["min_successes"]
                    and al.success_rate >= threshold["min_rate"]):
                al.level += 1
                al.promoted_at = now

    # Check demotion
    if not success and al.level > 0:
        recent_failures = conn.execute(
            """SELECT COUNT(*) FROM autonomy_log
               WHERE agent_type = ? AND action_type = ? AND outcome = 'failure'
               AND created_at >= datetime('now', '-24 hours')""",
            (agent_type, action_type),
        ).fetchone()[0]
        if recent_failures >= _DEMOTION_THRESHOLD:
            al.level = max(0, al.level - 1)

    # Upsert
    conn.execute(
        """INSERT INTO autonomy_levels
           (agent_type, action_type, level, success_count, failure_count,
            last_success_at, last_failure_at, promoted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_type, action_type) DO UPDATE SET
               level = excluded.level,
               success_count = excluded.success_count,
               failure_count = excluded.failure_count,
               last_success_at = excluded.last_success_at,
               last_failure_at = excluded.last_failure_at,
               promoted_at = excluded.promoted_at""",
        (
            al.agent_type, al.action_type, al.level,
            al.success_count, al.failure_count,
            al.last_success_at, al.last_failure_at, al.promoted_at,
        ),
    )

    # Log
    conn.execute(
        """INSERT INTO autonomy_log
           (agent_type, action_type, outcome, details, level_before, level_after)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_type, action_type, "success" if success else "failure",
         details, level_before, al.level),
    )
    conn.commit()
    return al


def list_autonomy_levels(conn: sqlite3.Connection, agent_type: Optional[str] = None) -> List[AutonomyLevel]:
    """List all autonomy levels, optionally filtered by agent type."""
    if agent_type:
        rows = conn.execute(
            """SELECT agent_type, action_type, level, success_count, failure_count,
                      last_success_at, last_failure_at, promoted_at
               FROM autonomy_levels WHERE agent_type = ?
               ORDER BY level DESC, success_count DESC""",
            (agent_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT agent_type, action_type, level, success_count, failure_count,
                      last_success_at, last_failure_at, promoted_at
               FROM autonomy_levels ORDER BY level DESC, success_count DESC""",
        ).fetchall()

    return [
        AutonomyLevel(
            agent_type=r[0], action_type=r[1], level=r[2],
            success_count=r[3], failure_count=r[4],
            last_success_at=r[5], last_failure_at=r[6], promoted_at=r[7],
        )
        for r in rows
    ]


def reset_autonomy(conn: sqlite3.Connection, agent_type: str, action_type: Optional[str] = None) -> None:
    """Reset autonomy back to level 0 (e.g., after a security incident)."""
    if action_type:
        conn.execute(
            "UPDATE autonomy_levels SET level = 0 WHERE agent_type = ? AND action_type = ?",
            (agent_type, action_type),
        )
    else:
        conn.execute(
            "UPDATE autonomy_levels SET level = 0 WHERE agent_type = ?",
            (agent_type,),
        )
    conn.commit()

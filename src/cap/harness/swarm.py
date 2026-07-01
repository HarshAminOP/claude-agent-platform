"""Swarm coordination layer for CAP harness.

A swarm is a group of agents working on a shared task with a defined topology.
Swarm records are persisted in platform.db alongside agent records.

Table: swarms
DB path: ~/.claude-platform/data/platform.db  (same as agent_store)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cap.harness.agent_store import (
    PLATFORM_DB_PATH,
    _open_db,
    _get_conn as _agent_get_conn,
    list_agents,
    terminate_agent,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOPOLOGIES = frozenset({"hierarchical", "mesh", "star", "pipeline"})
_VALID_STATUSES = frozenset({"running", "paused", "completed", "terminated"})

_SWARMS_DDL = """
CREATE TABLE IF NOT EXISTS swarms (
    swarm_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    topology        TEXT NOT NULL DEFAULT 'hierarchical',
    status          TEXT NOT NULL DEFAULT 'running',
    max_agents      INTEGER NOT NULL DEFAULT 8,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP,
    config_json     TEXT NOT NULL DEFAULT '{}',
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_swarms_status ON swarms(status);
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SwarmRecord:
    swarm_id: str
    name: str
    topology: str
    status: str
    max_agents: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "swarm_id": self.swarm_id,
            "name": self.name,
            "topology": self.topology,
            "status": self.status,
            "max_agents": self.max_agents,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "config": self.config,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

# Module-level connection cache (mirrors agent_store pattern).
_conn = None


def _get_conn(db_path: Optional[Path] = None):
    """Return a connection that has the swarms table ensured."""
    global _conn
    if db_path is not None:
        c = _open_db(db_path)
        c.executescript(_SWARMS_DDL)
        c.commit()
        return c
    if _conn is None:
        _conn = _open_db(PLATFORM_DB_PATH)
        _conn.executescript(_SWARMS_DDL)
        _conn.commit()
    return _conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_record(row) -> SwarmRecord:
    return SwarmRecord(
        swarm_id=row["swarm_id"],
        name=row["name"],
        topology=row["topology"],
        status=row["status"],
        max_agents=row["max_agents"],
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
        config=json.loads(row["config_json"] or "{}"),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


# ---------------------------------------------------------------------------
# Public API — become MCP tools
# ---------------------------------------------------------------------------

def swarm_init(
    name: str,
    topology: str = "hierarchical",
    max_agents: int = 8,
    config: Optional[dict] = None,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Create a new swarm record.

    Args:
        name:       User-friendly label for the swarm.
        topology:   One of: hierarchical, mesh, star, pipeline.
        max_agents: Maximum number of agents allowed (default 8).
        config:     Optional dict with consensus_mechanism, auto_scaling,
                    leader_agent_id, etc.
        _db_path:   DB path override (used by tests).

    Returns:
        {swarm_id, name, topology, status, max_agents}
    """
    if not name or not name.strip():
        raise ValueError("name must be a non-empty string")
    if topology not in _VALID_TOPOLOGIES:
        raise ValueError(
            f"Invalid topology {topology!r}. "
            f"Valid: {sorted(_VALID_TOPOLOGIES)}"
        )
    if max_agents < 1:
        raise ValueError("max_agents must be >= 1")

    swarm_id = str(uuid.uuid4())
    now = _now()
    cfg = config or {}

    conn = _get_conn(_db_path)
    conn.execute(
        """
        INSERT INTO swarms
            (swarm_id, name, topology, status, max_agents,
             created_at, config_json, metadata_json)
        VALUES (?, ?, ?, 'running', ?, ?, ?, '{}')
        """,
        (
            swarm_id,
            name.strip(),
            topology,
            max_agents,
            now.isoformat(),
            json.dumps(cfg),
        ),
    )
    conn.commit()

    return {
        "swarm_id": swarm_id,
        "name": name.strip(),
        "topology": topology,
        "status": "running",
        "max_agents": max_agents,
    }


def swarm_status(
    swarm_id: str,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Return swarm record plus agents belonging to this swarm.

    Args:
        swarm_id: UUID of the swarm.
        _db_path: DB path override (used by tests).

    Returns:
        {swarm_id, topology, status, agents, agent_count, active_count}
    Raises:
        KeyError if swarm_id is not found.
    """
    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT * FROM swarms WHERE swarm_id = ?", (swarm_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    record = _row_to_record(row)

    # Query agents in this swarm from the agents table.
    # list_agents accepts _db_path which re-opens a separate connection —
    # that is fine for correctness; tests pass the same tmp db_path.
    agents = list_agents(swarm_id=swarm_id, _db_path=_db_path)
    active_statuses = {"idle", "busy"}
    agents_out = [
        {
            "agent_id": a.agent_id,
            "agent_type": a.agent_type,
            "status": a.status,
        }
        for a in agents
    ]
    active_count = sum(1 for a in agents if a.status in active_statuses)

    return {
        "swarm_id": record.swarm_id,
        "name": record.name,
        "topology": record.topology,
        "status": record.status,
        "max_agents": record.max_agents,
        "created_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "config": record.config,
        "agents": agents_out,
        "agent_count": len(agents_out),
        "active_count": active_count,
    }


def swarm_health(
    swarm_id: str,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Compute health metrics for a swarm.

    Metrics:
        agent_utilization: busy_count / total_count  (0.0 when no agents)
        total_cost_usd:    sum of total_cost_usd across swarm agents
        failed_count:      agents with status='failed'
        avg_task_duration_ms: mean duration from execution_ledger for swarm agents

    Returns:
        {healthy, utilization, total_cost_usd, failed_count, avg_task_duration_ms}
    Raises:
        KeyError if swarm_id is not found.
    """
    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT status FROM swarms WHERE swarm_id = ?", (swarm_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    swarm_status_val = row["status"]
    agents = list_agents(swarm_id=swarm_id, _db_path=_db_path)

    total_count = len(agents)
    busy_count = sum(1 for a in agents if a.status == "busy")
    failed_count = sum(1 for a in agents if a.status == "failed")
    total_cost = round(sum(a.total_cost_usd for a in agents), 8)
    utilization = round(busy_count / total_count, 4) if total_count > 0 else 0.0

    # Compute avg task duration from execution_ledger for this swarm's agents.
    avg_duration_ms: Optional[float] = None
    if agents:
        agent_ids = [a.agent_id for a in agents]
        placeholders = ",".join("?" * len(agent_ids))
        try:
            from cap.harness.cost_meter import _ensure_schema
            from cap.db import get_db as _get_cap_db

            ledger_db_path = str(_db_path) if _db_path else None
            ledger_conn = _get_cap_db(ledger_db_path)
            try:
                _ensure_schema(ledger_conn)
                result = ledger_conn.execute(
                    f"""
                    SELECT AVG(duration_ms)
                    FROM execution_ledger
                    WHERE agent_id IN ({placeholders})
                    """,
                    agent_ids,
                ).fetchone()
                if result and result[0] is not None:
                    avg_duration_ms = round(result[0], 2)
            finally:
                ledger_conn.close()
        except Exception:
            pass

    healthy = swarm_status_val == "running" and failed_count == 0

    return {
        "healthy": healthy,
        "swarm_status": swarm_status_val,
        "agent_utilization": utilization,
        "total_cost_usd": total_cost,
        "failed_count": failed_count,
        "avg_task_duration_ms": avg_duration_ms,
        "total_agents": total_count,
        "busy_agents": busy_count,
    }


def swarm_shutdown(
    swarm_id: str,
    reason: str = "completed",
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Terminate a swarm and all active agents within it.

    Sets swarm status to 'completed' or 'terminated' (based on reason).
    Terminates all active agents (idle/busy).

    Args:
        swarm_id: UUID of the swarm to shut down.
        reason:   'completed' (normal finish) or any other value → 'terminated'.
        _db_path: DB path override (used by tests).

    Returns:
        {swarm_id, agents_terminated, final_cost_usd}
    Raises:
        KeyError if swarm_id is not found.
    """
    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT * FROM swarms WHERE swarm_id = ?", (swarm_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    new_status = "completed" if reason == "completed" else "terminated"
    now = _now()

    # Terminate all active agents in this swarm.
    agents = list_agents(swarm_id=swarm_id, _db_path=_db_path)
    active_statuses = {"idle", "busy"}
    terminated_count = 0
    final_cost = 0.0

    for agent in agents:
        final_cost += agent.total_cost_usd
        if agent.status in active_statuses:
            try:
                terminate_agent(agent.agent_id, reason=f"swarm_shutdown:{reason}", _db_path=_db_path)
                terminated_count += 1
            except KeyError:
                pass

    conn.execute(
        """
        UPDATE swarms
        SET status = ?, completed_at = ?
        WHERE swarm_id = ?
        """,
        (new_status, now.isoformat(), swarm_id),
    )
    conn.commit()

    return {
        "swarm_id": swarm_id,
        "status": new_status,
        "reason": reason,
        "agents_terminated": terminated_count,
        "final_cost_usd": round(final_cost, 8),
        "completed_at": now.isoformat(),
    }


def swarm_list(
    status: Optional[str] = None,
    *,
    _db_path: Optional[Path] = None,
) -> list[dict]:
    """List all swarms, optionally filtered by status.

    Args:
        status:   Optional filter (running, paused, completed, terminated).
        _db_path: DB path override (used by tests).

    Returns:
        List of swarm summary dicts, ordered by created_at DESC.
    """
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Valid: {sorted(_VALID_STATUSES)}"
        )

    conn = _get_conn(_db_path)
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM swarms WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM swarms ORDER BY created_at DESC"
        ).fetchall()

    return [_row_to_record(r).to_dict() for r in rows]

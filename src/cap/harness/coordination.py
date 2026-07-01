"""Task assignment and load balancing for CAP swarms.

Each function in this module becomes an MCP tool registered in harness_server.py.

Storage: proposals table lives in platform.db alongside agents and swarms.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.coordination")

try:
    from cap.harness.agent_store import (
        PLATFORM_DB_PATH,
        _open_db,
        list_agents,
        spawn_agent,
        update_agent,
        get_agent,
    )
    import cap.harness.agentdb as _agentdb
    from cap.harness.swarm import _SWARMS_DDL
except ImportError as _import_err:  # pragma: no cover
    raise ImportError(f"coordination requires cap.harness.agent_store: {_import_err}") from _import_err

# ---------------------------------------------------------------------------
# DB schema — proposals table
# ---------------------------------------------------------------------------

_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    swarm_id    TEXT NOT NULL,
    proposal    TEXT NOT NULL,
    votes_json  TEXT NOT NULL DEFAULT '{}',
    outcome     TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_proposals_swarm ON proposals(swarm_id);
CREATE INDEX IF NOT EXISTS idx_proposals_outcome ON proposals(outcome);
"""

# Module-level connection cache (mirrors swarm / agent_store pattern).
_conn = None


def _get_conn(db_path: Optional[Path] = None):
    """Open platform.db, ensure swarms and proposals tables exist, return connection."""
    global _conn
    if db_path is not None:
        c = _open_db(db_path)
        c.executescript(_SWARMS_DDL)
        c.executescript(_PROPOSALS_DDL)
        c.commit()
        return c
    if _conn is None:
        _conn = _open_db(PLATFORM_DB_PATH)
        _conn.executescript(_SWARMS_DDL)
        _conn.executescript(_PROPOSALS_DDL)
        _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API — become MCP tools
# ---------------------------------------------------------------------------

def coordination_assign(
    swarm_id: str,
    task: str,
    preferred_agent_type: Optional[str] = None,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Find the best agent in a swarm to handle a task and mark it busy.

    Assignment priority:
    a) If preferred_agent_type: find an idle agent of that type within the swarm.
    b) Otherwise: use agentdb_semantic_route to pick the best type, then find idle.
    c) If no idle agent of the right type: spawn a new one (if under max_agents).
    d) If at capacity: return {queued: True, reason: "swarm full"}.

    Args:
        swarm_id:             UUID of the swarm.
        task:                 Task description (used for semantic routing).
        preferred_agent_type: Optional explicit agent type.
        _db_path:             DB path override (tests).

    Returns:
        {agent_id, agent_type, model, assigned: True}
        or {queued: True, reason: "swarm full"}
    Raises:
        KeyError if swarm_id not found.
    """
    if not swarm_id or not swarm_id.strip():
        raise ValueError("swarm_id must be a non-empty string")
    if not task or not task.strip():
        raise ValueError("task must be a non-empty string")

    # Fetch swarm record to get max_agents.
    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT max_agents, status FROM swarms WHERE swarm_id = ?",
        (swarm_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    max_agents: int = row["max_agents"]
    swarm_status: str = row["status"]
    if swarm_status not in ("running", "paused"):
        raise ValueError(f"Cannot assign to swarm with status={swarm_status!r}")

    # Determine target agent type.
    if preferred_agent_type:
        target_type = preferred_agent_type
    else:
        route_result = _agentdb.agentdb_semantic_route(task=task, _db_path=_db_path)
        target_type = route_result.get("recommended_agent_type", "dev")

    # Find an idle agent of the target type in this swarm.
    swarm_agents = list_agents(swarm_id=swarm_id, agent_type=target_type, _db_path=_db_path)
    idle_agents = [a for a in swarm_agents if a.status == "idle"]

    if idle_agents:
        chosen = idle_agents[0]
        update_agent(chosen.agent_id, status="busy", _db_path=_db_path)
        return {
            "agent_id": chosen.agent_id,
            "agent_type": chosen.agent_type,
            "model": chosen.model,
            "assigned": True,
            "spawned": False,
        }

    # No idle agent — check capacity before spawning.
    all_swarm_agents = list_agents(swarm_id=swarm_id, _db_path=_db_path)
    active_count = sum(1 for a in all_swarm_agents if a.status in ("idle", "busy"))
    if active_count >= max_agents:
        return {
            "queued": True,
            "reason": "swarm full",
            "swarm_id": swarm_id,
            "max_agents": max_agents,
            "active_agents": active_count,
        }

    # Spawn a new agent for this swarm.
    new_agent = spawn_agent(agent_type=target_type, swarm_id=swarm_id, _db_path=_db_path)
    update_agent(new_agent.agent_id, status="busy", _db_path=_db_path)
    return {
        "agent_id": new_agent.agent_id,
        "agent_type": new_agent.agent_type,
        "model": new_agent.model,
        "assigned": True,
        "spawned": True,
    }


def coordination_release(
    agent_id: str,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Mark an agent as idle after completing a task.

    Args:
        agent_id: UUID of the agent to release.
        _db_path: DB path override (tests).

    Returns:
        {agent_id, status: "idle"}
    Raises:
        KeyError if agent_id not found.
    """
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id must be a non-empty string")

    existing = get_agent(agent_id, _db_path=_db_path)
    if existing is None:
        raise KeyError(f"Agent {agent_id!r} not found")

    update_agent(agent_id, status="idle", _db_path=_db_path)
    return {"agent_id": agent_id, "status": "idle"}


def coordination_balance(
    swarm_id: str,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Analyze load distribution across agent types in a swarm.

    Metrics per agent_type:
    - busy_count / idle_count
    - bottleneck: all agents of that type are busy
    - over_provisioned: all agents of that type are idle

    Args:
        swarm_id: UUID of the swarm.
        _db_path: DB path override (tests).

    Returns:
        {balanced, bottlenecks, over_provisioned, by_type, recommendation}
    Raises:
        KeyError if swarm_id not found.
    """
    if not swarm_id or not swarm_id.strip():
        raise ValueError("swarm_id must be a non-empty string")

    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT swarm_id FROM swarms WHERE swarm_id = ?", (swarm_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    agents = list_agents(swarm_id=swarm_id, _db_path=_db_path)
    active_agents = [a for a in agents if a.status in ("idle", "busy")]

    # Aggregate by type.
    by_type: dict[str, dict] = {}
    for agent in active_agents:
        entry = by_type.setdefault(agent.agent_type, {"idle": 0, "busy": 0, "total": 0})
        entry["total"] += 1
        if agent.status == "idle":
            entry["idle"] += 1
        elif agent.status == "busy":
            entry["busy"] += 1

    bottlenecks: list[str] = []
    over_provisioned: list[str] = []

    for atype, counts in by_type.items():
        if counts["total"] > 0:
            if counts["idle"] == 0:
                bottlenecks.append(atype)
            elif counts["busy"] == 0:
                over_provisioned.append(atype)

    balanced = len(bottlenecks) == 0 and len(over_provisioned) == 0

    # Build a human-readable recommendation.
    parts: list[str] = []
    if bottlenecks:
        parts.append(f"Spawn more agents of type(s): {', '.join(sorted(bottlenecks))}")
    if over_provisioned:
        parts.append(f"Drain idle agents of type(s): {', '.join(sorted(over_provisioned))}")
    if balanced and active_agents:
        parts.append("Swarm is balanced")
    elif not active_agents:
        parts.append("No active agents in swarm")

    recommendation = "; ".join(parts) if parts else "No agents to balance"

    return {
        "swarm_id": swarm_id,
        "balanced": balanced,
        "bottlenecks": sorted(bottlenecks),
        "over_provisioned": sorted(over_provisioned),
        "by_type": by_type,
        "recommendation": recommendation,
    }


def coordination_consensus(
    swarm_id: str,
    proposal: str,
    votes: Optional[dict] = None,
    *,
    _db_path: Optional[Path] = None,
) -> dict:
    """Simple majority consensus mechanism for swarm agents.

    Two-phase usage:
    1. Create a proposal (votes=None):
       Returns {proposal_id, status: "pending"}.
    2. Tally votes (votes={"agent_id": "approve"|"reject", ...}):
       Returns {proposal_id, outcome, votes_for, votes_against, total}.

    Outcome logic: majority approve => "approved", majority reject => "rejected",
    tie (equal votes) => "rejected".

    Args:
        swarm_id:  UUID of the swarm.
        proposal:  Human-readable proposal text.
        votes:     Dict mapping agent_id -> "approve" | "reject". If None,
                   creates a new pending proposal record.
        _db_path:  DB path override (tests).

    Returns:
        Phase 1: {proposal_id, swarm_id, proposal, status: "pending", created_at}
        Phase 2: {proposal_id, outcome, votes_for, votes_against, total}
    Raises:
        KeyError if swarm_id not found.
        ValueError on bad vote values.
    """
    if not swarm_id or not swarm_id.strip():
        raise ValueError("swarm_id must be a non-empty string")
    if not proposal or not proposal.strip():
        raise ValueError("proposal must be a non-empty string")

    conn = _get_conn(_db_path)
    swarm_row = conn.execute(
        "SELECT swarm_id FROM swarms WHERE swarm_id = ?", (swarm_id,)
    ).fetchone()
    if swarm_row is None:
        raise KeyError(f"Swarm {swarm_id!r} not found")

    if votes is None:
        # Phase 1: create proposal record.
        proposal_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            """
            INSERT INTO proposals (id, swarm_id, proposal, votes_json, outcome, created_at)
            VALUES (?, ?, ?, '{}', NULL, ?)
            """,
            (proposal_id, swarm_id, proposal.strip(), now),
        )
        conn.commit()
        return {
            "proposal_id": proposal_id,
            "swarm_id": swarm_id,
            "proposal": proposal.strip(),
            "status": "pending",
            "created_at": now,
        }

    # Phase 2: tally votes.
    _valid_votes = {"approve", "reject"}
    for agent_id_key, vote_value in votes.items():
        if vote_value not in _valid_votes:
            raise ValueError(
                f"Invalid vote {vote_value!r} for agent {agent_id_key!r}. "
                f"Must be 'approve' or 'reject'."
            )

    votes_for = sum(1 for v in votes.values() if v == "approve")
    votes_against = sum(1 for v in votes.values() if v == "reject")
    total = len(votes)

    # Majority: strictly more than half; tie => rejected.
    outcome = "approved" if votes_for > votes_against else "rejected"

    # Persist: create a proposal record with the outcome directly.
    proposal_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO proposals (id, swarm_id, proposal, votes_json, outcome, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (proposal_id, swarm_id, proposal.strip(), json.dumps(votes), outcome, now),
    )
    conn.commit()

    return {
        "proposal_id": proposal_id,
        "swarm_id": swarm_id,
        "proposal": proposal.strip(),
        "outcome": outcome,
        "votes_for": votes_for,
        "votes_against": votes_against,
        "total": total,
        "created_at": now,
    }

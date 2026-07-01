"""Agent lifecycle store for CAP.

Manages persistent agent records across sessions using SQLite (platform.db).
Each AgentRecord tracks identity, status, model assignment, token usage,
cost, and execution history for a single specialist agent instance.

Platform DB path: ~/.claude-platform/data/platform.db
Table: agents
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORM_DB_PATH = Path.home() / ".claude-platform" / "data" / "platform.db"

# Agent types with explicit model defaults (matches CAP agent roster)
_SONNET_AGENTS = frozenset({"dev", "devops", "test", "docs", "sre", "cicd", "explore"})
_OPUS_AGENTS = frozenset({"security", "code-review", "aws-architect"})
_HAIKU_AGENTS = frozenset({"optimization"})

_DEFAULT_MODEL: dict[str, str] = {
    **{t: "claude-sonnet-4-6" for t in _SONNET_AGENTS},
    **{t: "claude-opus-4-6" for t in _OPUS_AGENTS},
    **{t: "claude-haiku-4-5" for t in _HAIKU_AGENTS},
}

_VALID_STATUSES = frozenset({"idle", "busy", "completed", "failed", "terminated"})
_VALID_AGENT_TYPES = frozenset(_DEFAULT_MODEL.keys())
_VALID_MODELS = frozenset({
    "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"
})

_LAST_RESULT_MAX = 2000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AgentRecord:
    agent_id: str
    agent_type: str
    status: str
    model: str
    created_at: datetime
    last_active: datetime
    task_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    last_result: Optional[str] = None
    last_error: Optional[str] = None
    config: dict = field(default_factory=dict)
    swarm_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def to_row(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "status": self.status,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "task_count": self.task_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": self.total_cost_usd,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "config": json.dumps(self.config),
            "swarm_id": self.swarm_id,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentRecord":
        return cls(
            agent_id=row["agent_id"],
            agent_type=row["agent_type"],
            status=row["status"],
            model=row["model"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_active=datetime.fromisoformat(row["last_active"]),
            task_count=row["task_count"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_cost_usd=row["total_cost_usd"],
            last_result=row["last_result"],
            last_error=row["last_error"],
            config=json.loads(row["config"] or "{}"),
            swarm_id=row["swarm_id"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id            TEXT PRIMARY KEY,
    agent_type          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'idle',
    model               TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_active         TEXT NOT NULL,
    task_count          INTEGER NOT NULL DEFAULT 0,
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd      REAL NOT NULL DEFAULT 0.0,
    last_result         TEXT,
    last_error          TEXT,
    config              TEXT NOT NULL DEFAULT '{}',
    swarm_id            TEXT,
    metadata            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agents_status    ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_type      ON agents(agent_type);
CREATE INDEX IF NOT EXISTS idx_agents_swarm     ON agents(swarm_id);
CREATE INDEX IF NOT EXISTS idx_agents_last_active ON agents(last_active);
"""


def _open_db(path: Path = PLATFORM_DB_PATH) -> sqlite3.Connection:
    """Open (or create) platform.db and ensure the agents table exists."""
    path.parent.mkdir(parents=True, exist_ok=True)

    db_exists = path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=False)

    # Set restrictive permissions on newly created DB files (thread-safe alternative to umask)
    if not db_exists and path.exists():
        os.chmod(str(path), 0o600)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # Ensure agents table exists (idempotent — does not re-run DDL already
    # applied by db_init.py for other tables).
    conn.executescript(_AGENTS_DDL)
    conn.commit()

    return conn


# Module-level connection (lazy, per-process singleton).
# Tests can override by passing an explicit `_conn` argument to public fns.
_conn: Optional[sqlite3.Connection] = None


def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    global _conn
    if db_path is not None:
        return _open_db(db_path)
    if _conn is None:
        _conn = _open_db()
    return _conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def spawn_agent(
    agent_type: str,
    model: Optional[str] = None,
    config: Optional[dict] = None,
    swarm_id: Optional[str] = None,
    *,
    _db_path: Optional[Path] = None,
) -> AgentRecord:
    """Create a new agent record and persist it to the database.

    Args:
        agent_type: One of the valid CAP agent types (dev, security, …).
        model:      Explicit model override.  Auto-selected from agent_type
                    when omitted.
        config:     Optional dict with max_tokens, temperature,
                    system_prompt_key.
        swarm_id:   Optional swarm this agent belongs to.
        _db_path:   Override DB path (used by tests).

    Returns:
        Persisted AgentRecord with status='idle'.
    """
    if agent_type not in _VALID_AGENT_TYPES:
        raise ValueError(
            f"Unknown agent_type {agent_type!r}. "
            f"Valid types: {sorted(_VALID_AGENT_TYPES)}"
        )

    resolved_model = model or _DEFAULT_MODEL[agent_type]
    if resolved_model not in _VALID_MODELS:
        raise ValueError(
            f"Unknown model {resolved_model!r}. Valid: {sorted(_VALID_MODELS)}"
        )

    now = _now()
    record = AgentRecord(
        agent_id=str(uuid.uuid4()),
        agent_type=agent_type,
        status="idle",
        model=resolved_model,
        created_at=now,
        last_active=now,
        config=config or {},
        swarm_id=swarm_id,
    )

    conn = _get_conn(_db_path)
    row = record.to_row()
    conn.execute(
        """
        INSERT INTO agents
            (agent_id, agent_type, status, model, created_at, last_active,
             task_count, total_input_tokens, total_output_tokens, total_cost_usd,
             last_result, last_error, config, swarm_id, metadata)
        VALUES
            (:agent_id, :agent_type, :status, :model, :created_at, :last_active,
             :task_count, :total_input_tokens, :total_output_tokens, :total_cost_usd,
             :last_result, :last_error, :config, :swarm_id, :metadata)
        """,
        row,
    )
    conn.commit()
    return record


def get_agent(
    agent_id: str,
    *,
    _db_path: Optional[Path] = None,
) -> Optional[AgentRecord]:
    """Fetch a single agent by ID.  Returns None if not found."""
    conn = _get_conn(_db_path)
    row = conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return AgentRecord.from_row(row) if row else None


def list_agents(
    status: Optional[str] = None,
    agent_type: Optional[str] = None,
    swarm_id: Optional[str] = None,
    *,
    _db_path: Optional[Path] = None,
) -> list[AgentRecord]:
    """Return agents filtered by any combination of status / type / swarm."""
    conn = _get_conn(_db_path)
    clauses: list[str] = []
    params: list[object] = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if agent_type is not None:
        clauses.append("agent_type = ?")
        params.append(agent_type)
    if swarm_id is not None:
        clauses.append("swarm_id = ?")
        params.append(swarm_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM agents {where} ORDER BY last_active DESC",
        params,
    ).fetchall()
    return [AgentRecord.from_row(r) for r in rows]


def update_agent(
    target_id: str,
    *,
    _db_path: Optional[Path] = None,
    **kwargs,
) -> AgentRecord:
    """Update arbitrary fields on an agent record.

    Allowed kwargs: status, model, task_count, total_input_tokens,
    total_output_tokens, total_cost_usd, last_result, last_error,
    config, swarm_id, metadata, last_active.

    Returns the updated AgentRecord.
    Raises KeyError if target_id is not found.
    """
    # Expose a stable public alias so callers that stored `agent_id` as a
    # variable name can still call update_agent(agent_id, ...).
    agent_id = target_id

    allowed = {
        "status", "model", "task_count", "total_input_tokens",
        "total_output_tokens", "total_cost_usd", "last_result", "last_error",
        "config", "swarm_id", "metadata", "last_active",
    }
    # agent_id / agent_type / created_at are immutable primary-key fields.
    protected = {"agent_id", "agent_type", "created_at"}
    bad = (set(kwargs) - allowed) | (set(kwargs) & protected)
    if bad:
        raise ValueError(f"Non-updatable fields: {bad}")

    if "status" in kwargs and kwargs["status"] not in _VALID_STATUSES:
        raise ValueError(f"Invalid status {kwargs['status']!r}")

    conn = _get_conn(_db_path)
    # Serialize dicts before storing
    serialised = {}
    for k, v in kwargs.items():
        if k in ("config", "metadata"):
            serialised[k] = json.dumps(v)
        elif k == "last_active" and isinstance(v, datetime):
            serialised[k] = v.isoformat()
        else:
            serialised[k] = v

    set_clause = ", ".join(f"{k} = :{k}" for k in serialised)
    serialised["_agent_id"] = agent_id
    result = conn.execute(
        f"UPDATE agents SET {set_clause} WHERE agent_id = :_agent_id",
        serialised,
    )
    if result.rowcount == 0:
        raise KeyError(f"Agent {agent_id!r} not found")
    conn.commit()

    updated = get_agent(agent_id, _db_path=_db_path)
    assert updated is not None  # we just confirmed rowcount > 0
    return updated


def terminate_agent(
    agent_id: str,
    reason: str = "manual",
    *,
    _db_path: Optional[Path] = None,
) -> AgentRecord:
    """Set an agent's status to 'terminated' and record the reason.

    Args:
        agent_id: UUID of the agent to terminate.
        reason:   Human-readable termination reason stored in metadata.

    Returns:
        Updated AgentRecord.
    Raises:
        KeyError if agent_id is not found.
    """
    existing = get_agent(agent_id, _db_path=_db_path)
    if existing is None:
        raise KeyError(f"Agent {agent_id!r} not found")

    meta = {**existing.metadata, "termination_reason": reason, "terminated_at": _now().isoformat()}
    return update_agent(
        agent_id,
        status="terminated",
        metadata=meta,
        last_active=_now(),
        _db_path=_db_path,
    )


def record_execution(
    agent_id: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    result: Optional[str] = None,
    error: Optional[str] = None,
    *,
    _db_path: Optional[Path] = None,
) -> None:
    """Accumulate token/cost counters and update execution state.

    - Increments task_count by 1.
    - Adds input_tokens / output_tokens / cost_usd to running totals.
    - Stores truncated result / error.
    - Sets status back to 'idle' after execution completes.
    - Updates last_active to now.

    Raises:
        KeyError if agent_id is not found.
    """
    existing = get_agent(agent_id, _db_path=_db_path)
    if existing is None:
        raise KeyError(f"Agent {agent_id!r} not found")

    truncated_result = (result[:_LAST_RESULT_MAX] if result else None)

    update_agent(
        agent_id,
        status="idle",
        task_count=existing.task_count + 1,
        total_input_tokens=existing.total_input_tokens + input_tokens,
        total_output_tokens=existing.total_output_tokens + output_tokens,
        total_cost_usd=round(existing.total_cost_usd + cost_usd, 8),
        last_result=truncated_result,
        last_error=error,
        last_active=_now(),
        _db_path=_db_path,
    )


def cleanup_stale(
    max_age_hours: int = 24,
    *,
    _db_path: Optional[Path] = None,
) -> int:
    """Terminate agents that have been idle for longer than max_age_hours.

    Only agents with status in ('idle', 'busy') are considered stale;
    already-terminal states (completed, failed, terminated) are left alone.

    Args:
        max_age_hours: Inactivity threshold in hours.

    Returns:
        Number of agents terminated.
    """
    conn = _get_conn(_db_path)

    # Compute cutoff as ISO-8601 string so SQLite datetime comparison works.
    from datetime import timedelta
    cutoff = (_now() - timedelta(hours=max_age_hours)).isoformat()

    stale_rows = conn.execute(
        """
        SELECT agent_id FROM agents
        WHERE status IN ('idle', 'busy')
          AND last_active < ?
        """,
        (cutoff,),
    ).fetchall()

    count = 0
    for row in stale_rows:
        terminate_agent(
            row["agent_id"],
            reason=f"stale: idle for more than {max_age_hours}h",
            _db_path=_db_path,
        )
        count += 1

    return count

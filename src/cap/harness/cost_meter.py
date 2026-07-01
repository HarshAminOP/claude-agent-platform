"""
CAP Cost Meter — Per-agent, per-task, per-model cost attribution.

Provides granular cost tracking with ledger entries in ``execution_ledger``.
The existing ``cost_events`` table (tracker.py) tracks aggregate budget;
this module tracks attribution — who spent what, on which task, in which
workflow.

Public API
----------
record_execution(result, agent_type, task_hash=None, swarm_id=None, workflow_id=None) -> str
get_agent_cost(agent_id, since=None) -> AgentCostSummary
get_workflow_cost(workflow_id) -> WorkflowCostSummary
get_model_breakdown(since=None) -> dict[str, ModelCostEntry]
budget_remaining(daily_limit_usd=5.0) -> float
top_spenders(n=10, since=None) -> list[AgentCostSummary]
"""

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from cap.db import get_db
from cap.harness.executor import ExecutionResult


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AgentCostSummary:
    agent_id: str
    agent_type: str
    total_cost_usd: float
    total_tokens: int
    execution_count: int


@dataclass
class WorkflowCostSummary:
    workflow_id: str
    total_cost_usd: float
    by_agent_type: dict = field(default_factory=dict)
    by_model: dict = field(default_factory=dict)


@dataclass
class ModelCostEntry:
    model: str
    total_cost_usd: float
    total_tokens: int
    execution_count: int
    pct_of_total: float


# ---------------------------------------------------------------------------
# Schema migration helper
# ---------------------------------------------------------------------------


def _ensure_schema(db) -> None:
    """Create execution_ledger if it does not exist yet."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS execution_ledger (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            agent_type TEXT NOT NULL,
            model TEXT NOT NULL,
            task_hash TEXT,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            duration_ms INTEGER NOT NULL,
            success INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            swarm_id TEXT,
            workflow_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_agent    ON execution_ledger(agent_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_workflow ON execution_ledger(workflow_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_model    ON execution_ledger(model)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_created  ON execution_ledger(created_at)"
    )
    db.commit()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def record_execution(
    result: ExecutionResult,
    agent_type: str,
    task_hash: Optional[str] = None,
    swarm_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    db=None,
) -> str:
    """Insert one row into execution_ledger and mirror into cost_events.

    Parameters
    ----------
    result:
        The ``ExecutionResult`` returned by ``AgentExecutor.execute()``.
    agent_type:
        Logical agent role (e.g. ``"dev"``, ``"orchestrator"``).
    task_hash:
        Caller-supplied SHA-256 of the task prompt prefix.  When omitted the
        function derives a hash from ``result.agent_id`` + timestamp so the
        column is always populated.
    swarm_id:
        Optional swarm group identifier.
    workflow_id:
        Optional parent workflow identifier.
    db:
        Optional pre-opened ``sqlite3.Connection``.  Opened from the default
        path when ``None``.

    Returns
    -------
    str
        The UUID primary key of the new ledger row.
    """
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    entry_id = str(uuid.uuid4())
    success = 1 if result.error is None else 0

    if task_hash is None:
        task_hash = hashlib.sha256(
            f"{result.agent_id}:{result.timestamp.isoformat()}".encode()
        ).hexdigest()

    db.execute(
        """
        INSERT INTO execution_ledger
            (id, agent_id, agent_type, model, task_hash,
             input_tokens, output_tokens, cost_usd, duration_ms,
             success, error, swarm_id, workflow_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            result.agent_id,
            agent_type,
            result.model,
            task_hash,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
            result.duration_ms,
            success,
            result.error,
            swarm_id,
            workflow_id,
        ),
    )

    # Mirror into the aggregate cost_events table so CostTracker.budget_check()
    # remains accurate without a second write path.
    db.execute(
        """
        INSERT INTO cost_events
            (agent_type, model, input_tokens, output_tokens, cost_usd, workflow_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_type,
            result.model,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
            workflow_id,
            time.time(),
        ),
    )

    db.commit()

    if owned:
        db.close()

    return entry_id


def get_agent_cost(
    agent_id: str,
    since: Optional[datetime] = None,
    db=None,
) -> AgentCostSummary:
    """Return aggregated cost for a single agent.

    Parameters
    ----------
    agent_id:
        The ``ExecutionResult.agent_id`` value to filter on.
    since:
        Lower-bound timestamp (UTC-aware).  ``None`` means all time.
    """
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    query = """
        SELECT agent_id, agent_type,
               COALESCE(SUM(cost_usd), 0.0)               AS total_cost,
               COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
               COUNT(*)                                    AS exec_count
        FROM execution_ledger
        WHERE agent_id = ?
    """
    params: list = [agent_id]

    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())

    row = db.execute(query, params).fetchone()

    if owned:
        db.close()

    if row is None or row["exec_count"] == 0:
        return AgentCostSummary(
            agent_id=agent_id,
            agent_type="unknown",
            total_cost_usd=0.0,
            total_tokens=0,
            execution_count=0,
        )

    return AgentCostSummary(
        agent_id=row["agent_id"],
        agent_type=row["agent_type"] or "unknown",
        total_cost_usd=round(row["total_cost"], 6),
        total_tokens=row["total_tokens"],
        execution_count=row["exec_count"],
    )


def get_workflow_cost(workflow_id: str, db=None) -> WorkflowCostSummary:
    """Return total cost for a workflow, broken down by agent_type and model."""
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    total_row = db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM execution_ledger WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    total_cost = round(total_row[0], 6)

    by_agent_rows = db.execute(
        """
        SELECT agent_type, COALESCE(SUM(cost_usd), 0.0) AS cost
        FROM execution_ledger WHERE workflow_id = ?
        GROUP BY agent_type
        """,
        (workflow_id,),
    ).fetchall()

    by_model_rows = db.execute(
        """
        SELECT model, COALESCE(SUM(cost_usd), 0.0) AS cost
        FROM execution_ledger WHERE workflow_id = ?
        GROUP BY model
        """,
        (workflow_id,),
    ).fetchall()

    if owned:
        db.close()

    return WorkflowCostSummary(
        workflow_id=workflow_id,
        total_cost_usd=total_cost,
        by_agent_type={r["agent_type"]: round(r["cost"], 6) for r in by_agent_rows},
        by_model={r["model"]: round(r["cost"], 6) for r in by_model_rows},
    )


def get_model_breakdown(
    since: Optional[datetime] = None,
    db=None,
) -> dict[str, ModelCostEntry]:
    """Return per-model cost summary with percentage-of-total.

    Keys are model identifiers as stored in execution_ledger (e.g. ``"haiku"``,
    ``"sonnet"``, ``"us.anthropic.claude-sonnet-4-6-20250514"``).
    """
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    query = """
        SELECT model,
               COALESCE(SUM(cost_usd), 0.0)               AS total_cost,
               COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
               COUNT(*)                                    AS exec_count
        FROM execution_ledger
        WHERE 1=1
    """
    params: list = []
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    query += " GROUP BY model"

    rows = db.execute(query, params).fetchall()

    if owned:
        db.close()

    grand_total = sum(r["total_cost"] for r in rows) or 1.0  # avoid div-by-zero

    return {
        r["model"]: ModelCostEntry(
            model=r["model"],
            total_cost_usd=round(r["total_cost"], 6),
            total_tokens=r["total_tokens"],
            execution_count=r["exec_count"],
            pct_of_total=round(r["total_cost"] / grand_total * 100, 2),
        )
        for r in rows
    }


def budget_remaining(daily_limit_usd: float = 5.0, db=None) -> float:
    """Return remaining USD budget for today.

    Reads today's spend from ``execution_ledger`` (UTC calendar day).
    Returns a negative value when the limit has been exceeded.

    Parameters
    ----------
    daily_limit_usd:
        Daily spending cap in USD.  Defaults to ``5.0``.
    """
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    today_start = datetime.now(timezone.utc).date().isoformat()
    row = db.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0.0)
        FROM execution_ledger
        WHERE date(created_at) = date(?)
        """,
        (today_start,),
    ).fetchone()

    if owned:
        db.close()

    spent = row[0] if row else 0.0
    return round(daily_limit_usd - spent, 6)


def top_spenders(
    n: int = 10,
    since: Optional[datetime] = None,
    db=None,
) -> list[AgentCostSummary]:
    """Return the top-N agents ranked by total spend.

    Parameters
    ----------
    n:
        Number of agents to return (default 10).
    since:
        Lower-bound timestamp.  ``None`` means all time.
    """
    owned = db is None
    if owned:
        db = get_db()

    _ensure_schema(db)

    query = """
        SELECT agent_id, agent_type,
               COALESCE(SUM(cost_usd), 0.0)               AS total_cost,
               COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
               COUNT(*)                                    AS exec_count
        FROM execution_ledger
        WHERE 1=1
    """
    params: list = []
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    query += " GROUP BY agent_id ORDER BY total_cost DESC LIMIT ?"
    params.append(n)

    rows = db.execute(query, params).fetchall()

    if owned:
        db.close()

    return [
        AgentCostSummary(
            agent_id=r["agent_id"],
            agent_type=r["agent_type"] or "unknown",
            total_cost_usd=round(r["total_cost"], 6),
            total_tokens=r["total_tokens"],
            execution_count=r["exec_count"],
        )
        for r in rows
    ]

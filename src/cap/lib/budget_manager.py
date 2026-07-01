"""Budget Management — daily tracking, pause/resume, per-agent caps, per-project isolation.

Provides the core logic for budget enforcement and CLI commands.
Uses a dedicated budget_log table for daily tracking and a flag file
for pause state.

Public API
----------
init_budget_log_table(db) -> None
get_today_spend(db, workspace=None) -> dict
get_history(db, days=7, workspace=None) -> list[dict]
pause_budget(db) -> None
resume_budget(db) -> None
is_budget_paused() -> bool
reset_today(db, workspace=None) -> None
check_budget_enforcement(db, agent_type, cost_usd, workspace=None, config=None) -> dict
record_budget_spend(db, agent_type, cost_usd, workspace=None) -> None
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUDGET_PAUSED_FLAG = Path(os.environ.get(
    "CAP_HOME", str(Path.home() / ".claude-platform")
)) / "data" / "budget_paused"


def _budget_paused_path() -> Path:
    """Return the budget_paused flag file path (respects CAP_HOME env)."""
    cap_home = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))
    return cap_home / "data" / "budget_paused"


def _today_str() -> str:
    """Return today's date as ISO string in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def init_budget_log_table(db: sqlite3.Connection) -> None:
    """Create budget_log table if it does not exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS budget_log (
            date TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '__global__',
            total_spend_usd REAL DEFAULT 0.0,
            execution_count INTEGER DEFAULT 0,
            top_agent_type TEXT,
            paused INTEGER DEFAULT 0,
            PRIMARY KEY (date, workspace)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_budget_log_date
        ON budget_log(date)
    """)
    db.commit()


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------


def is_budget_paused() -> bool:
    """Check if budget is paused via flag file."""
    return _budget_paused_path().exists()


def pause_budget(db: sqlite3.Connection) -> None:
    """Pause budget — write flag file and set DB flag."""
    flag = _budget_paused_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(datetime.now(timezone.utc).isoformat())

    init_budget_log_table(db)
    today = _today_str()
    db.execute(
        """INSERT INTO budget_log (date, workspace, paused)
           VALUES (?, '__global__', 1)
           ON CONFLICT(date, workspace) DO UPDATE SET paused = 1""",
        (today,),
    )
    db.commit()


def resume_budget(db: sqlite3.Connection) -> None:
    """Resume budget — remove flag file and clear DB flag."""
    flag = _budget_paused_path()
    if flag.exists():
        flag.unlink()

    init_budget_log_table(db)
    today = _today_str()
    db.execute(
        """INSERT INTO budget_log (date, workspace, paused)
           VALUES (?, '__global__', 0)
           ON CONFLICT(date, workspace) DO UPDATE SET paused = 0""",
        (today,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Spend tracking
# ---------------------------------------------------------------------------


def get_today_spend(db: sqlite3.Connection, workspace: Optional[str] = None) -> dict:
    """Get today's spend summary.

    Returns dict with: date, total_spend_usd, execution_count, top_agent_type, paused.
    """
    init_budget_log_table(db)
    today = _today_str()

    if workspace:
        row = db.execute(
            "SELECT total_spend_usd, execution_count, top_agent_type, paused "
            "FROM budget_log WHERE date = ? AND workspace = ?",
            (today, workspace),
        ).fetchone()
    else:
        # Aggregate across all workspaces for today
        row = db.execute(
            "SELECT COALESCE(SUM(total_spend_usd), 0.0), "
            "COALESCE(SUM(execution_count), 0), "
            "top_agent_type, MAX(paused) "
            "FROM budget_log WHERE date = ?",
            (today,),
        ).fetchone()

    if row and (row[0] or row[1]):
        return {
            "date": today,
            "total_spend_usd": row[0] or 0.0,
            "execution_count": row[1] or 0,
            "top_agent_type": row[2] or "none",
            "paused": bool(row[3]),
        }

    return {
        "date": today,
        "total_spend_usd": 0.0,
        "execution_count": 0,
        "top_agent_type": "none",
        "paused": is_budget_paused(),
    }


def get_top_consumers(db: sqlite3.Connection, n: int = 5, workspace: Optional[str] = None) -> list[dict]:
    """Get top N agent types by spend today."""
    init_budget_log_table(db)
    today = _today_str()

    # We need execution_ledger for per-agent breakdown
    try:
        if workspace:
            rows = db.execute(
                """SELECT agent_type, COALESCE(SUM(cost_usd), 0.0) as total,
                          COUNT(*) as count
                   FROM execution_ledger
                   WHERE date(created_at) = ?
                   GROUP BY agent_type ORDER BY total DESC LIMIT ?""",
                (today, n),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT agent_type, COALESCE(SUM(cost_usd), 0.0) as total,
                          COUNT(*) as count
                   FROM execution_ledger
                   WHERE date(created_at) = ?
                   GROUP BY agent_type ORDER BY total DESC LIMIT ?""",
                (today, n),
            ).fetchall()
    except Exception:
        return []

    return [
        {"agent_type": r[0], "spend_usd": r[1], "executions": r[2]}
        for r in rows
    ]


def get_history(db: sqlite3.Connection, days: int = 7, workspace: Optional[str] = None) -> list[dict]:
    """Get daily spend totals for the last N days."""
    init_budget_log_table(db)

    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    if workspace:
        rows = db.execute(
            "SELECT date, total_spend_usd, execution_count "
            "FROM budget_log WHERE workspace = ? AND date >= ? ORDER BY date DESC",
            (workspace, start_date),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT date, COALESCE(SUM(total_spend_usd), 0.0), COALESCE(SUM(execution_count), 0) "
            "FROM budget_log WHERE date >= ? GROUP BY date ORDER BY date DESC",
            (start_date,),
        ).fetchall()

    return [
        {"date": r[0], "total_spend_usd": r[1], "execution_count": r[2]}
        for r in rows
    ]


def reset_today(db: sqlite3.Connection, workspace: Optional[str] = None) -> None:
    """Reset today's spend counter."""
    init_budget_log_table(db)
    today = _today_str()

    if workspace:
        db.execute(
            "DELETE FROM budget_log WHERE date = ? AND workspace = ?",
            (today, workspace),
        )
    else:
        db.execute("DELETE FROM budget_log WHERE date = ?", (today,))

    db.commit()


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def check_budget_enforcement(
    db: sqlite3.Connection,
    agent_type: str,
    cost_usd: float = 0.0,
    workspace: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict:
    """Check if execution is allowed under budget constraints.

    Returns dict:
        {"allowed": bool, "reason": str | None}

    Checks performed:
    1. Pause flag file
    2. Daily limit
    3. Per-agent-type cap
    """
    if config is None:
        config = {}

    budget_cfg = config.get("budget", {})
    daily_limit = budget_cfg.get("daily_limit_usd", 5.0)
    agent_caps = budget_cfg.get("agent_caps", {})
    per_project = budget_cfg.get("per_project", False)

    # Check 1: Is budget paused?
    if is_budget_paused():
        return {"allowed": False, "reason": "Budget is paused. Run 'cap budget resume' to resume."}

    # Check 2: Daily limit
    init_budget_log_table(db)
    today = _today_str()

    ws_filter = workspace if (per_project and workspace) else None
    spend_info = get_today_spend(db, workspace=ws_filter)
    today_spend = spend_info["total_spend_usd"]

    if today_spend + cost_usd > daily_limit:
        return {
            "allowed": False,
            "reason": f"Daily budget limit exceeded: ${today_spend:.4f} spent of ${daily_limit:.2f} limit.",
        }

    # Check 3: Per-agent-type cap
    if agent_type in agent_caps:
        cap = agent_caps[agent_type]
        # Get today's spend for this agent type
        try:
            row = db.execute(
                """SELECT COALESCE(SUM(cost_usd), 0.0)
                   FROM execution_ledger
                   WHERE agent_type = ? AND date(created_at) = ?""",
                (agent_type, today),
            ).fetchone()
            agent_spend = row[0] if row else 0.0
        except Exception:
            agent_spend = 0.0

        if agent_spend + cost_usd > cap:
            return {
                "allowed": False,
                "reason": f"Per-agent cap exceeded for '{agent_type}': ${agent_spend:.4f} spent of ${cap:.2f} cap.",
            }

    return {"allowed": True, "reason": None}


def record_budget_spend(
    db: sqlite3.Connection,
    agent_type: str,
    cost_usd: float,
    workspace: Optional[str] = None,
) -> None:
    """Record a spend event in budget_log (increment spend + count)."""
    init_budget_log_table(db)
    today = _today_str()
    ws = workspace or "__global__"

    # Upsert: increment spend and count
    db.execute(
        """INSERT INTO budget_log (date, workspace, total_spend_usd, execution_count, top_agent_type)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(date, workspace) DO UPDATE SET
               total_spend_usd = total_spend_usd + excluded.total_spend_usd,
               execution_count = execution_count + 1,
               top_agent_type = CASE
                   WHEN excluded.total_spend_usd > 0 THEN excluded.top_agent_type
                   ELSE budget_log.top_agent_type
               END""",
        (today, ws, cost_usd, agent_type),
    )
    db.commit()

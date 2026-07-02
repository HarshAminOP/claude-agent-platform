"""
CAP Routing Enforcer — Tracks Agent vs cap_orchestrate usage.

Records every routing event (native Agent() call vs cap_orchestrate MCP tool)
to cap_routing_stats table for observability and enforcement reporting.
"""

import os
import sqlite3
import time

from cap.config import get_platform_db_path

DB_PATH = str(get_platform_db_path())


def _ensure_table():
    """Create cap_routing_stats table if it does not exist."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cap_routing_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            tool_name TEXT NOT NULL,
            routed_via TEXT NOT NULL,
            args_summary TEXT
        )
    """)
    conn.commit()
    conn.close()


def check_agent_routing(tool_name: str, args: dict) -> dict:
    """
    Log a routing event and return routing metadata.

    Args:
        tool_name: The tool being invoked (Agent, agent, cap_orchestrate, etc.)
        args: The tool arguments dict.

    Returns:
        dict with routed_via ("cap" or "native") and logged (True/False).
    """
    if tool_name == "cap_orchestrate":
        routed_via = "cap"
    elif tool_name in ("Agent", "agent"):
        routed_via = "native"
    else:
        # Not a routing-relevant tool, no-op
        return {"routed_via": "unknown", "logged": False}

    # Summarize args (truncate to prevent DB bloat)
    args_summary = ""
    if isinstance(args, dict):
        summary_parts = []
        for key in ("subagent_type", "description", "prompt"):
            val = args.get(key)
            if val:
                summary_parts.append(f"{key}={str(val)[:100]}")
        args_summary = "; ".join(summary_parts)[:500]

    try:
        _ensure_table()
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO cap_routing_stats (timestamp, tool_name, routed_via, args_summary) VALUES (?, ?, ?, ?)",
            (time.time(), tool_name, routed_via, args_summary),
        )
        conn.commit()
        conn.close()
        return {"routed_via": routed_via, "logged": True}
    except (sqlite3.Error, OSError):
        return {"routed_via": routed_via, "logged": False}


def get_routing_stats() -> dict:
    """
    Query routing statistics from the database.

    Returns:
        dict with cap_routes, native_routes, total, enforcement_rate.
        enforcement_rate = cap_routes / total (0.0 if no routes recorded).
    """
    try:
        _ensure_table()
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")

        cap_routes = conn.execute(
            "SELECT COUNT(*) FROM cap_routing_stats WHERE routed_via = 'cap'"
        ).fetchone()[0]

        native_routes = conn.execute(
            "SELECT COUNT(*) FROM cap_routing_stats WHERE routed_via = 'native'"
        ).fetchone()[0]

        conn.close()

        total = cap_routes + native_routes
        enforcement_rate = cap_routes / total if total > 0 else 0.0

        return {
            "cap_routes": cap_routes,
            "native_routes": native_routes,
            "total": total,
            "enforcement_rate": round(enforcement_rate, 4),
        }
    except (sqlite3.Error, OSError):
        return {
            "cap_routes": 0,
            "native_routes": 0,
            "total": 0,
            "enforcement_rate": 0.0,
        }

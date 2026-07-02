"""
Workflow persistence store — survives session death.

Provides save/load/heartbeat/stale-detection for background workflows
so the orchestrator can recover them after restart.
"""

import json
import os
import sqlite3
import time

from cap.config import get_platform_db_path

TABLE = "cap_workflows"


def _get_conn():
    db_path = str(get_platform_db_path())
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'running',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL,
        args_json TEXT,
        result_json TEXT,
        error TEXT,
        steps_completed INTEGER DEFAULT 0,
        steps_total INTEGER DEFAULT 0,
        current_step_json TEXT
    )""")
    conn.commit()
    return conn


def save_workflow(wf_id, status, steps_completed=0, steps_total=0, current_step=None, args=None, result=None, error=None):
    """Persist workflow state (insert or update)."""
    conn = _get_conn()
    now = time.time()
    conn.execute(
        f"""INSERT OR REPLACE INTO {TABLE}
        (id, status, created_at, updated_at, heartbeat_at, args_json, result_json, error, steps_completed, steps_total, current_step_json)
        VALUES (?, ?, COALESCE((SELECT created_at FROM {TABLE} WHERE id=?), ?), ?, ?, ?, ?, ?, ?, ?, ?)""",
        (wf_id, status, wf_id, now, now, now,
         json.dumps(args) if args else None,
         json.dumps(result) if result else None,
         error, steps_completed, steps_total,
         json.dumps(current_step) if current_step else None))
    conn.commit()
    conn.close()


def update_heartbeat(wf_id):
    """Bump heartbeat timestamp to signal liveness."""
    conn = _get_conn()
    conn.execute(f"UPDATE {TABLE} SET heartbeat_at=?, updated_at=? WHERE id=?", (time.time(), time.time(), wf_id))
    conn.commit()
    conn.close()


def load_workflow(wf_id):
    """Load a single workflow by ID. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE id=?", (wf_id,)).fetchone()
    conn.close()
    if not row:
        return None
    cols = ['id', 'status', 'created_at', 'updated_at', 'heartbeat_at',
            'args_json', 'result_json', 'error', 'steps_completed', 'steps_total', 'current_step_json']
    return dict(zip(cols, row))


def list_active_workflows():
    """Return all workflows with status='running'."""
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT id, status, heartbeat_at, steps_completed, steps_total FROM {TABLE} WHERE status='running'"
    ).fetchall()
    conn.close()
    return [{'id': r[0], 'status': r[1], 'heartbeat_at': r[2], 'steps_completed': r[3], 'steps_total': r[4]} for r in rows]


def mark_failed_stale(timeout_seconds=120):
    """Mark running workflows as failed_stale if heartbeat exceeded timeout.

    Returns the number of workflows marked as stale.
    Handles both epoch float and ISO datetime string heartbeat formats.
    """
    conn = _get_conn()
    cutoff = time.time() - timeout_seconds
    # Also compute an ISO-format cutoff for rows where heartbeat_at was stored as datetime string
    import datetime as _dt
    iso_cutoff = _dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    # For string-based heartbeats, subtract timeout_seconds from now
    iso_cutoff_dt = _dt.datetime.utcnow() - _dt.timedelta(seconds=timeout_seconds)
    iso_cutoff = iso_cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')

    cursor = conn.execute(
        f"""UPDATE {TABLE} SET status='failed_stale', error='heartbeat timeout'
        WHERE status='running' AND (
            (typeof(heartbeat_at) = 'real' AND heartbeat_at < ?) OR
            (typeof(heartbeat_at) = 'integer' AND heartbeat_at < ?) OR
            (typeof(heartbeat_at) = 'text' AND heartbeat_at < ?)
        )""",
        (cutoff, cutoff, iso_cutoff))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

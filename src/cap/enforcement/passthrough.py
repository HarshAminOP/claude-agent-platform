"""
CAP Enforcement Passthrough — Temporary enforcement bypass.

- Default TTL: 300 seconds (5 minutes)
- Maximum TTL: 900 seconds (15 minutes) — hard cap
- Logged to passthrough_log for audit
- Auto-expires; cannot be renewed without explicit re-invocation
- Max 3 activations per hour (abuse prevention)
"""

import os
import time

from cap.db import get_db as _get_db, migrate as _migrate

DB_PATH = os.path.expanduser("~/.cap/cap.db")
MAX_TTL = 900  # Hard cap: 15 minutes maximum passthrough duration

_migrated = False


def _db():
    """Get the enforcement database connection, ensuring schema exists."""
    global _migrated
    conn = _get_db(DB_PATH)
    if not _migrated:
        _migrate(conn)
        _migrated = True
    return conn


def enable(workspace: str, ttl: int = 300, reason: str = "") -> dict:
    """
    Enable passthrough mode for a workspace.

    Args:
        workspace: Absolute path to the workspace.
        ttl: Time-to-live in seconds (default 300 = 5 minutes, max 900).
        reason: Optional reason for enabling passthrough.

    Returns:
        dict with status, expires_in, reason — or error on rate limit.
    """
    # Validate workspace is an absolute path
    if not workspace or not os.path.isabs(workspace):
        return {"error": "workspace must be an absolute path"}

    # Cap TTL to prevent effectively-permanent bypass
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        ttl = 300
    ttl = min(int(ttl), MAX_TTL)

    # Truncate reason to prevent DB bloat from malicious input
    if reason and len(reason) > 500:
        reason = reason[:500]

    db = _db()
    now = time.time()

    # Rate limit: max 3 per hour
    recent = db.execute(
        "SELECT COUNT(*) FROM passthrough_log WHERE workspace = ? AND timestamp > ?",
        (workspace, now - 3600)
    ).fetchone()[0]
    if recent >= 3:
        return {"error": "Rate limit: max 3 passthrough activations per hour"}

    db.execute(
        "INSERT OR REPLACE INTO passthrough (workspace, enabled_at, expires_at, reason) VALUES (?, ?, ?, ?)",
        (workspace, now, now + ttl, reason)
    )
    db.execute(
        "INSERT INTO passthrough_log (workspace, timestamp, ttl, reason) VALUES (?, ?, ?, ?)",
        (workspace, now, ttl, reason)
    )
    db.commit()
    return {"status": "enabled", "expires_in": ttl, "reason": reason}


def check(workspace: str) -> bool:
    """
    Check if passthrough mode is active for a workspace.

    Args:
        workspace: Absolute path to the workspace.

    Returns:
        True if passthrough is active and not expired.
    """
    db = _db()
    row = db.execute(
        "SELECT expires_at FROM passthrough WHERE workspace = ? AND expires_at > ?",
        (workspace, time.time())
    ).fetchone()
    return row is not None


def disable(workspace: str) -> None:
    """
    Disable (expire) passthrough mode for a workspace immediately.

    Args:
        workspace: Absolute path to the workspace.
    """
    db = _db()
    db.execute("DELETE FROM passthrough WHERE workspace = ?", (workspace,))
    db.commit()

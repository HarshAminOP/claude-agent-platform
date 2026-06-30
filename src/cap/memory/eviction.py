"""
CAP Memory Eviction Daemon.

Manages memory lifecycle:
  - Score < 0.15: move tier from 'active' to 'archive'
  - Archive entries > 365 days with < 3 accesses: DELETE
  - Confidence decay: reduce confidence by 0.01/day since last reinforcement
  - Disk budget: if DB size > config.disk_budget_mb, aggressively archive

Callable standalone and also from session_start.
"""

import os
import time
import json
import uuid
import sqlite3
from typing import Optional

# Defaults (overridden by config when available)
DEFAULT_SCORE_THRESHOLD = 0.15
DEFAULT_DELETE_DAYS = 365
DEFAULT_DELETE_MIN_ACCESSES = 3
DEFAULT_STALE_DAYS = 90
DEFAULT_DISK_BUDGET_MB = 256
CONFIDENCE_DECAY_PER_DAY = 0.01


def evict(db: sqlite3.Connection, config=None) -> dict:
    """
    Run eviction pass on the memory database.

    Args:
        db: SQLite connection with CAP schema.
        config: Optional Config object with eviction parameters.

    Returns:
        Dict with stats: {archived, deleted, decayed, disk_action}.
    """
    now = time.time()

    # Extract config values
    score_threshold = DEFAULT_SCORE_THRESHOLD
    delete_days = DEFAULT_DELETE_DAYS
    delete_min_accesses = DEFAULT_DELETE_MIN_ACCESSES
    stale_days = DEFAULT_STALE_DAYS
    disk_budget_mb = DEFAULT_DISK_BUDGET_MB

    if config is not None:
        score_threshold = getattr(config, "eviction_score_threshold", score_threshold)
        delete_days = getattr(config, "delete_days", delete_days)
        delete_min_accesses = getattr(config, "delete_min_accesses", delete_min_accesses)
        stale_days = getattr(config, "stale_days", stale_days)
        disk_budget_mb = getattr(config, "disk_budget_mb", disk_budget_mb)

    stats = {
        "archived": 0,
        "deleted": 0,
        "decayed": 0,
        "disk_action": False,
    }

    # 1. Mark stale entries (no access in stale_days, not already stale)
    stale_cutoff = now - (stale_days * 86400)
    db.execute(
        """UPDATE memory_active SET stale_since = ?
           WHERE last_accessed < ? AND stale_since IS NULL""",
        (now, stale_cutoff),
    )

    # 2. Confidence decay: reduce importance by 0.01/day since last reinforcement
    stale_entries = db.execute(
        """SELECT id, importance, stale_since FROM memory_active
           WHERE stale_since IS NOT NULL AND importance > 0.1"""
    ).fetchall()

    for row in stale_entries:
        entry_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        importance = row["importance"] if isinstance(row, sqlite3.Row) else row[1]
        stale_since = row["stale_since"] if isinstance(row, sqlite3.Row) else row[2]

        days_stale = (now - stale_since) / 86400.0
        decay = CONFIDENCE_DECAY_PER_DAY * days_stale
        new_importance = max(0.1, importance - decay)

        if new_importance < importance:
            db.execute(
                "UPDATE memory_active SET importance = ? WHERE id = ?",
                (round(new_importance, 4), entry_id),
            )
            stats["decayed"] += 1

    # 3. Archive low-score entries (score < threshold)
    low_score_entries = db.execute(
        """SELECT id, workspace, content, metadata, category
           FROM memory_active
           WHERE composite_score < ? AND consolidated_into IS NULL
           ORDER BY composite_score ASC
           LIMIT 100""",
        (score_threshold,),
    ).fetchall()

    if low_score_entries:
        stats["archived"] = _archive_entries(db, low_score_entries, now)

    # 4. Delete expired archive entries (> delete_days with < delete_min_accesses)
    delete_cutoff = now - (delete_days * 86400)
    cursor = db.execute(
        """DELETE FROM memory_archive
           WHERE last_accessed < ?
           AND access_count < ?""",
        (delete_cutoff, delete_min_accesses),
    )
    stats["deleted"] = cursor.rowcount

    # 5. Disk budget enforcement
    disk_action = _enforce_disk_budget(db, disk_budget_mb, now)
    stats["disk_action"] = disk_action

    db.commit()
    return stats


def _archive_entries(
    db: sqlite3.Connection, entries: list, now: float
) -> int:
    """
    Move low-score active entries to archive tier.

    Returns count of entries archived.
    """
    archived = 0

    for row in entries:
        if isinstance(row, sqlite3.Row):
            entry_id = row["id"]
            workspace = row["workspace"]
            content = row["content"]
        else:
            entry_id = row[0]
            workspace = row[1]
            content = row[2]

        archive_id = str(uuid.uuid4())

        try:
            db.execute(
                """INSERT INTO memory_archive
                   (id, workspace, summary, source_ids, created_at, last_accessed, access_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    archive_id,
                    workspace,
                    content[:500],  # summary is truncated content
                    json.dumps([entry_id]),
                    now,
                    now,
                    0,
                ),
            )
            db.execute("DELETE FROM memory_active WHERE id = ?", (entry_id,))
            archived += 1
        except sqlite3.IntegrityError:
            continue

    return archived


def _enforce_disk_budget(
    db: sqlite3.Connection, budget_mb: int, now: float
) -> bool:
    """
    If DB file exceeds disk budget, aggressively archive/delete.

    Returns True if action was taken.
    """
    # Find DB file path from connection
    db_path = None
    try:
        # pragma database_list returns (seq, name, file)
        row = db.execute("PRAGMA database_list").fetchone()
        if row:
            db_path = row[2] if isinstance(row, sqlite3.Row) else row[2]
    except (sqlite3.OperationalError, IndexError):
        pass

    if not db_path or not os.path.exists(db_path):
        return False

    db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
    if db_size_mb <= budget_mb:
        return False

    # Aggressive archival: delete old archive entries
    db.execute(
        """DELETE FROM memory_archive
           WHERE id IN (
               SELECT id FROM memory_archive
               ORDER BY last_accessed ASC
               LIMIT 500
           )"""
    )

    # Archive active entries with low scores more aggressively
    db.execute(
        """DELETE FROM memory_active
           WHERE composite_score < 0.3
           AND access_count < 5
           AND consolidated_into IS NULL"""
    )

    return True

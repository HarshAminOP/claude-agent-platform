"""Pattern retention policies — prevents catastrophic forgetting.

High-value patterns are retained; stale, low-value ones are pruned based on
a weighted score computed from success rate, recency, usage frequency, and cost.

No neural nets required — pure SQL + scoring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.retention")

# ---------------------------------------------------------------------------
# DB helpers — reuse agentdb's connection factory
# ---------------------------------------------------------------------------

try:
    from cap.harness.agent_store import PLATFORM_DB_PATH
except ImportError:
    PLATFORM_DB_PATH = Path.home() / ".claude-platform" / "data" / "platform.db"


def _get_conn(db_path: Optional[Path] = None):
    """Open platform.db and ensure retention columns exist on patterns table."""
    import sqlite3

    path = db_path or PLATFORM_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Migrate: add retention columns if not present
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN use_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN last_used_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN retention_score REAL DEFAULT 0.5")
    except Exception:
        pass

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. compute_retention_score
# ---------------------------------------------------------------------------

def compute_retention_score(pattern_id: str, db=None) -> float:
    """Compute and persist a retention score for a single pattern.

    Score weights:
      0.3 * success_factor  — was the pattern a success?
      0.2 * recency_factor  — how recently was it created (decay over 90 days)?
      0.3 * usage_factor    — how often has it been used (saturates at 10)?
      0.2 * cost_factor     — lower cost is better (saturates at $0.10)?

    Returns the computed score in [0.0, 1.0].
    """
    try:
        conn = _get_conn(db)
    except Exception as exc:
        logger.warning("compute_retention_score: db unavailable: %s", exc)
        return 0.5

    try:
        row = conn.execute(
            "SELECT success, created_at, use_count, cost_usd FROM patterns WHERE id = ?",
            (pattern_id,),
        ).fetchone()

        if row is None:
            return 0.0

        # success factor
        success_factor = 1.0 if row["success"] else 0.0

        # recency factor
        age_days = 0
        if row["created_at"]:
            try:
                created = datetime.fromisoformat(str(row["created_at"]))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                now = datetime.now(tz=timezone.utc)
                age_days = (now - created).days
            except Exception:
                age_days = 0
        recency_factor = max(0.0, 1.0 - age_days / 90.0)

        # usage factor
        use_count = row["use_count"] or 0
        usage_factor = min(1.0, use_count / 10.0)

        # cost factor (lower cost = higher score)
        cost = row["cost_usd"] if row["cost_usd"] is not None else 0.01
        cost_factor = max(0.0, 1.0 - min(1.0, cost / 0.10))

        score = (
            0.3 * success_factor
            + 0.2 * recency_factor
            + 0.3 * usage_factor
            + 0.2 * cost_factor
        )
        score = round(min(1.0, max(0.0, score)), 6)

        conn.execute(
            "UPDATE patterns SET retention_score = ? WHERE id = ?",
            (score, pattern_id),
        )
        conn.commit()
        return score

    except Exception as exc:
        logger.warning("compute_retention_score(%s): %s", pattern_id, exc)
        return 0.5
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. prune_stale_patterns
# ---------------------------------------------------------------------------

def prune_stale_patterns(
    min_score: float = 0.1,
    max_age_days: int = 90,
    keep_min: int = 100,
    db=None,
) -> int:
    """Delete low-value, stale patterns while always keeping at least keep_min rows.

    Steps:
      1. Refresh all retention scores.
      2. Bail out if total count <= keep_min.
      3. Delete patterns where retention_score < min_score AND age > max_age_days,
         limited so that at least keep_min patterns remain.
      4. Attempt to remove corresponding LanceDB vectors (best-effort).

    Returns:
        Number of rows deleted.
    """
    try:
        conn = _get_conn(db)
    except Exception as exc:
        logger.warning("prune_stale_patterns: db unavailable: %s", exc)
        return 0

    try:
        # Refresh all scores first
        refresh_retention_scores(db=db)

        count_row = conn.execute("SELECT COUNT(*) AS n FROM patterns").fetchone()
        count = int(count_row["n"]) if count_row else 0

        if count <= keep_min:
            return 0

        max_to_delete = count - keep_min

        # Identify IDs to delete (for LanceDB cleanup)
        candidates = conn.execute(
            """SELECT id FROM patterns
               WHERE retention_score < ?
                 AND julianday('now') - julianday(created_at) > ?
               ORDER BY retention_score ASC
               LIMIT ?""",
            (min_score, max_age_days, max_to_delete),
        ).fetchall()

        if not candidates:
            return 0

        ids_to_delete = [r["id"] for r in candidates]
        placeholders = ",".join("?" * len(ids_to_delete))
        conn.execute(
            f"DELETE FROM patterns WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        conn.commit()
        deleted = len(ids_to_delete)

        # Best-effort: remove LanceDB vectors for pruned patterns
        try:
            from cap.harness.vector_patterns import PatternEmbedder
            pe = PatternEmbedder()
            if pe.is_available:
                for pid in ids_to_delete:
                    try:
                        pe.delete(pid)
                    except Exception:
                        pass
        except Exception:
            pass  # LanceDB unavailable — not fatal

        logger.info("prune_stale_patterns: deleted %d patterns", deleted)
        return deleted

    except Exception as exc:
        logger.warning("prune_stale_patterns: unexpected error: %s", exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. refresh_retention_scores
# ---------------------------------------------------------------------------

def refresh_retention_scores(batch_size: int = 200, db=None) -> int:
    """Recompute retention_score for up to batch_size patterns.

    Returns:
        Number of patterns updated.
    """
    try:
        conn = _get_conn(db)
    except Exception as exc:
        logger.warning("refresh_retention_scores: db unavailable: %s", exc)
        return 0

    try:
        rows = conn.execute(
            "SELECT id FROM patterns LIMIT ?",
            (batch_size,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("refresh_retention_scores: query failed: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return 0

    updated = 0
    for row in rows:
        try:
            compute_retention_score(row["id"], db=db)
            updated += 1
        except Exception:
            pass

    return updated


# ---------------------------------------------------------------------------
# 4. record_pattern_use
# ---------------------------------------------------------------------------

def record_pattern_use(pattern_id: str, db=None) -> None:
    """Increment use_count and update last_used_at for a pattern."""
    try:
        conn = _get_conn(db)
        conn.execute(
            """UPDATE patterns
               SET use_count = COALESCE(use_count, 0) + 1,
                   last_used_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (pattern_id,),
        )
        conn.commit()
    except Exception as exc:
        logger.debug("record_pattern_use(%s): %s", pattern_id, exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. protect_high_value
# ---------------------------------------------------------------------------

def protect_high_value(threshold: float = 0.8, db=None) -> list[str]:
    """Pin retention_score to 1.0 for patterns scoring >= threshold.

    Returns:
        List of pattern_ids that were protected.
    """
    try:
        conn = _get_conn(db)
    except Exception as exc:
        logger.warning("protect_high_value: db unavailable: %s", exc)
        return []

    try:
        rows = conn.execute(
            "SELECT id FROM patterns WHERE retention_score >= ?",
            (threshold,),
        ).fetchall()

        protected = [r["id"] for r in rows]
        if protected:
            placeholders = ",".join("?" * len(protected))
            conn.execute(
                f"UPDATE patterns SET retention_score = 1.0 WHERE id IN ({placeholders})",
                protected,
            )
            conn.commit()

        return protected

    except Exception as exc:
        logger.warning("protect_high_value: unexpected error: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

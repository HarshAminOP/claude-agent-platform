"""Knowledge base consolidation for the CAP platform.

Runs a multi-phase cleanup pass against an open knowledge.db connection:

  Phase 1 — WAL checkpoint (TRUNCATE, non-blocking)
  Phase 2 — Delete expired knowledge_entries
  Phase 3 — Deduplicate by content_hash (keep highest id per workspace+source_path)
  Phase 4 — Requeue transient failed embeddings; leave permanent failures alone
  Phase 5 — Purge embedding_cache orphans (no matching knowledge_entry)
  Phase 6 — Correlate session workflow_complete events with routing decisions
             to update outcome fields (learning feedback loop)

All phases operate inside a single savepoint so the DB is never left in a
partial state.  The WAL checkpoint (Phase 1) runs outside the savepoint because
SQLite does not allow checkpoint inside a transaction.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cap.learning.engine import compute_thresholds_from_session_events

logger = logging.getLogger("cap.consolidator")

# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class ConsolidationResult:
    """Summary of a single consolidation run."""

    expired_deleted: int
    duplicates_removed: int
    failed_requeued: int       # transient failures reset to pending
    failed_permanent: int      # permanent failures left as-is (counted only)
    cache_orphans_purged: int
    wal_checkpointed: bool
    duration_ms: int
    thresholds_updated: bool = False  # True if Phase 6 correlated session events


# ── Failure classification ────────────────────────────────────────────────────

_TRANSIENT_PATTERNS: tuple[str, ...] = ("Throttl", "Timeout")


def _classify_failure(last_error: str | None) -> str:
    """Return "transient" or "permanent" for an embedding_queue last_error value.

    Rules
    -----
    - None          → transient  (no recorded error; safe to retry)
    - contains "Throttl" → transient  (rate-limit, back-off and retry)
    - contains "Timeout" → transient  (network hiccup, back-off and retry)
    - anything else → permanent  (e.g. ValidationException, bad payload)
    """
    if last_error is None:
        return "transient"
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in last_error:
            return "transient"
    return "permanent"


# ── Main function ─────────────────────────────────────────────────────────────


def run_consolidation(
    db: sqlite3.Connection,
    sessions_db_path: Optional[Path] = None,
) -> ConsolidationResult:
    """Alias for :func:`consolidate` — backward compatibility."""
    return consolidate(db, sessions_db_path)


def consolidate(
    db: sqlite3.Connection,
    sessions_db_path: Optional[Path] = None,
) -> ConsolidationResult:
    """Run all consolidation phases and return a summary.

    Parameters
    ----------
    db:
        Open ``sqlite3.Connection`` to knowledge.db.  The caller retains
        ownership; this function never closes the connection.
    sessions_db_path:
        Optional path to sessions.db.  When provided (or auto-derived from
        the knowledge.db path), Phase 6 correlates session workflow_complete
        events with routing decisions to update outcome fields.  If the file
        does not exist the phase is silently skipped.

    Returns
    -------
    ConsolidationResult
        Counts of rows affected by each phase plus overall duration.
    """
    start = time.monotonic()

    wal_checkpointed = False
    expired_deleted = 0
    duplicates_removed = 0
    failed_requeued = 0
    failed_permanent = 0
    cache_orphans_purged = 0
    thresholds_updated = False

    # ── Phase 1: WAL checkpoint ───────────────────────────────────────────────
    # Must run outside any transaction; TRUNCATE resets WAL position to zero
    # which keeps the WAL file small without blocking readers.
    try:
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # row = (busy, wal_pages, checkpointed_pages)
        # busy==0 means all WAL pages were checkpointed successfully.
        if row is not None and row[0] == 0:
            wal_checkpointed = True
            logger.debug(
                "WAL checkpoint(TRUNCATE): wal_pages=%s checkpointed=%s",
                row[1],
                row[2],
            )
        else:
            logger.debug(
                "WAL checkpoint(TRUNCATE) partially blocked: busy=%s wal_pages=%s checkpointed=%s",
                row[0] if row else "?",
                row[1] if row else "?",
                row[2] if row else "?",
            )
    except sqlite3.OperationalError as exc:
        logger.warning("WAL checkpoint failed (non-fatal): %s", exc)

    # ── Phases 2-5 inside a savepoint ────────────────────────────────────────
    # Using a savepoint (nested transaction) so we can roll back cleanly if
    # any phase raises, while still committing all phases atomically.
    try:
        db.execute("SAVEPOINT consolidate")

        # ── Phase 2: Delete expired entries ──────────────────────────────────
        db.execute(
            'DELETE FROM knowledge_entries '
            'WHERE expires_at IS NOT NULL AND expires_at < datetime("now")'
        )
        expired_deleted = db.execute("SELECT changes()").fetchone()[0]
        logger.debug("Phase 2 — expired deleted: %d", expired_deleted)

        # ── Phase 3: Deduplicate by content_hash ─────────────────────────────
        # For each (workspace, source_path, content_hash) group keep the row
        # with the highest id; delete all others.  Using a NOT IN subquery on
        # id is safe here because id is an INTEGER PRIMARY KEY (bounded set).
        db.execute(
            """
            DELETE FROM knowledge_entries
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM knowledge_entries
                GROUP BY workspace, source_path, content_hash
            )
            """
        )
        duplicates_removed = db.execute("SELECT changes()").fetchone()[0]
        logger.debug("Phase 3 — duplicates removed: %d", duplicates_removed)

        # ── Phase 4: Requeue transient failed embeddings ──────────────────────
        # Fetch all failed rows so we can classify each error string in Python,
        # then bulk-update only the transient ones.  This avoids encoding
        # complex pattern-matching logic in SQL and keeps classification logic
        # in one place (_classify_failure).
        failed_rows = db.execute(
            "SELECT id, last_error FROM embedding_queue WHERE status = 'failed'"
        ).fetchall()

        transient_ids: list[int] = []
        for row_id, last_error in failed_rows:
            if _classify_failure(last_error) == "transient":
                transient_ids.append(row_id)
            else:
                failed_permanent += 1

        if transient_ids:
            placeholders = ",".join("?" * len(transient_ids))
            db.execute(
                f"UPDATE embedding_queue SET status = 'pending', attempts = 0 "
                f"WHERE id IN ({placeholders})",
                transient_ids,
            )
            failed_requeued = db.execute("SELECT changes()").fetchone()[0]

        logger.debug(
            "Phase 4 — requeued: %d  permanent failures left: %d",
            failed_requeued,
            failed_permanent,
        )

        # ── Phase 5: Purge embedding_cache orphans ────────────────────────────
        # Delete cache entries whose content_hash no longer exists in
        # knowledge_entries (e.g. the entry was expired or deduplicated above).
        db.execute(
            """
            DELETE FROM embedding_cache
            WHERE content_hash NOT IN (
                SELECT DISTINCT content_hash FROM knowledge_entries
            )
            """
        )
        cache_orphans_purged = db.execute("SELECT changes()").fetchone()[0]
        logger.debug("Phase 5 — cache orphans purged: %d", cache_orphans_purged)

        db.execute("RELEASE SAVEPOINT consolidate")

    except Exception as exc:
        logger.error("Consolidation failed, rolling back: %s", exc)
        try:
            db.execute("ROLLBACK TO SAVEPOINT consolidate")
            db.execute("RELEASE SAVEPOINT consolidate")
        except sqlite3.OperationalError:
            pass  # savepoint may already be gone if connection is broken
        raise

    # ── Phase 6: Learning feedback loop ──────────────────────────────────────
    # Correlate session workflow_complete events with routing_decisions to
    # backfill outcome fields.  Runs outside the savepoint because it writes
    # to a separate DB (sessions.db / routing_decisions) and is best-effort.
    try:
        # Auto-derive sessions.db path from the knowledge.db connection if not
        # provided.  sqlite3 exposes the file path via PRAGMA database_list.
        resolved_sessions_path: Optional[Path] = sessions_db_path
        if resolved_sessions_path is None:
            row = db.execute(
                "SELECT file FROM pragma_database_list WHERE name = 'main'"
            ).fetchone()
            if row and row[0]:
                resolved_sessions_path = Path(row[0]).parent / "sessions.db"

        if resolved_sessions_path is not None and resolved_sessions_path.exists():
            sessions_conn = sqlite3.connect(str(resolved_sessions_path))
            try:
                result_phase6 = compute_thresholds_from_session_events(
                    sessions_db=sessions_conn,
                    routing_db=db,
                )
                thresholds_updated = result_phase6.get("sample_count", 0) > 0
                logger.debug(
                    "Phase 6 — learning feedback: sample_count=%d success_rate=%.3f avg_duration=%.1fs",
                    result_phase6.get("sample_count", 0),
                    result_phase6.get("success_rate", 0.0),
                    result_phase6.get("avg_duration", 0.0),
                )
            finally:
                sessions_conn.close()
        else:
            logger.debug("Phase 6 — sessions.db not found, skipping learning feedback")
    except Exception as exc:
        logger.warning("Phase 6 learning feedback failed (non-fatal): %s", exc)

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Consolidation complete in %dms: expired=%d dupes=%d requeued=%d "
        "permanent=%d orphans=%d wal_checkpointed=%s thresholds_updated=%s",
        duration_ms,
        expired_deleted,
        duplicates_removed,
        failed_requeued,
        failed_permanent,
        cache_orphans_purged,
        wal_checkpointed,
        thresholds_updated,
    )

    return ConsolidationResult(
        expired_deleted=expired_deleted,
        duplicates_removed=duplicates_removed,
        failed_requeued=failed_requeued,
        failed_permanent=failed_permanent,
        cache_orphans_purged=cache_orphans_purged,
        wal_checkpointed=wal_checkpointed,
        duration_ms=duration_ms,
        thresholds_updated=thresholds_updated,
    )

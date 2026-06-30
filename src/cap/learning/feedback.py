"""
CAP Retrieval Feedback Loop.

Tracks which memory search results were actually used by agents,
then computes relevance adjustments to improve future retrieval quality.

Flow:
1. record_retrieval(query, results, db) — stores what was returned for a query
2. record_usage(query_id, used_result_ids, db) — marks which results were used
3. compute_relevance_adjustments(db) — computes (entry_id, adjustment) pairs
4. apply_adjustments(db) — boosts used entries, decays unused

Boost: +0.05 per use
Decay: -0.01 per non-use
Scores clamped to [0.0, 1.0].
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional


# Score adjustment constants
BOOST_AMOUNT = 0.05  # boost for used results
DECAY_AMOUNT = 0.01  # decay for shown-but-unused results


@dataclass
class RetrievalRecord:
    """A recorded retrieval event."""

    query_id: str
    query: str
    result_ids: list[str]
    timestamp: float


def record_retrieval(
    query: str,
    results: list[str],
    db: sqlite3.Connection,
) -> str:
    """
    Store what was returned for a retrieval query.

    Creates a retrieval_queries record and individual result entries
    in the learning_events table for tracking.

    Args:
        query: The search query that produced these results.
        results: List of memory_active entry IDs that were returned.
        db: SQLite connection.

    Returns:
        The query_id for later usage recording.
    """
    query_id = f"ret_{uuid.uuid4().hex[:12]}"
    now = time.time()

    # Record the retrieval event with all results
    db.execute(
        """INSERT INTO learning_events
           (timestamp, event_type, payload, session_id)
           VALUES (?, 'retrieval_query', ?, ?)""",
        (
            now,
            json.dumps({
                "query_id": query_id,
                "query": query[:500],
                "result_ids": results,
                "result_count": len(results),
            }),
            "system",
        ),
    )
    db.commit()
    return query_id


def record_usage(
    query_id: str,
    used_result_ids: list[str],
    db: sqlite3.Connection,
) -> None:
    """
    Mark which results from a prior retrieval were actually used.

    Records a usage event linked to the original query_id so that
    compute_relevance_adjustments can calculate boost/decay.

    Args:
        query_id: The ID returned by record_retrieval.
        used_result_ids: List of entry IDs that the agent actually used.
        db: SQLite connection.
    """
    now = time.time()

    db.execute(
        """INSERT INTO learning_events
           (timestamp, event_type, payload, session_id)
           VALUES (?, 'retrieval_usage', ?, ?)""",
        (
            now,
            json.dumps({
                "query_id": query_id,
                "used_result_ids": used_result_ids,
            }),
            "system",
        ),
    )
    db.commit()


def compute_relevance_adjustments(
    db: sqlite3.Connection,
    since: Optional[float] = None,
) -> list[tuple[str, float]]:
    """
    Compute relevance score adjustments from retrieval feedback.

    Analyzes paired retrieval_query and retrieval_usage events to determine:
    - Which entries were shown and used -> boost (+0.05)
    - Which entries were shown but NOT used -> decay (-0.01)

    Args:
        db: SQLite connection.
        since: Only consider events after this timestamp.
               Defaults to last 24 hours.

    Returns:
        List of (entry_id, adjustment) pairs where adjustment is positive
        (boost) or negative (decay).
    """
    if since is None:
        since = time.time() - 86400  # last 24 hours

    # Get all retrieval queries since the cutoff
    query_rows = db.execute(
        """SELECT payload FROM learning_events
           WHERE event_type = 'retrieval_query' AND timestamp > ?""",
        (since,),
    ).fetchall()

    # Get all usage records since the cutoff
    usage_rows = db.execute(
        """SELECT payload FROM learning_events
           WHERE event_type = 'retrieval_usage' AND timestamp > ?""",
        (since,),
    ).fetchall()

    # Parse queries: query_id -> list of result_ids
    queries: dict[str, list[str]] = {}
    for row in query_rows:
        payload_str = row[0] if isinstance(row, tuple) else row["payload"]
        try:
            payload = json.loads(payload_str)
            qid = payload.get("query_id", "")
            results = payload.get("result_ids", [])
            if qid and results:
                queries[qid] = results
        except (json.JSONDecodeError, TypeError):
            continue

    # Parse usage: query_id -> set of used_result_ids
    usage: dict[str, set[str]] = {}
    for row in usage_rows:
        payload_str = row[0] if isinstance(row, tuple) else row["payload"]
        try:
            payload = json.loads(payload_str)
            qid = payload.get("query_id", "")
            used = payload.get("used_result_ids", [])
            if qid:
                usage[qid] = set(used)
        except (json.JSONDecodeError, TypeError):
            continue

    # Compute adjustments: aggregate per entry_id
    adjustments: dict[str, float] = {}

    for query_id, result_ids in queries.items():
        used_set = usage.get(query_id, set())

        # Only process queries that have corresponding usage records
        if query_id not in usage:
            continue

        for entry_id in result_ids:
            if entry_id in used_set:
                # Boost: entry was shown and used
                adjustments[entry_id] = adjustments.get(entry_id, 0.0) + BOOST_AMOUNT
            else:
                # Decay: entry was shown but not used
                adjustments[entry_id] = adjustments.get(entry_id, 0.0) - DECAY_AMOUNT

    return [(eid, adj) for eid, adj in adjustments.items()]


def apply_adjustments(
    db: sqlite3.Connection,
    since: Optional[float] = None,
) -> int:
    """
    Compute and apply relevance adjustments to memory_active entries.

    Combines compute_relevance_adjustments with actual DB updates.
    Clamps scores to [0.0, 1.0].

    Args:
        db: SQLite connection.
        since: Only consider events after this timestamp.

    Returns:
        Number of entries adjusted.
    """
    adjustments = compute_relevance_adjustments(db, since=since)

    if not adjustments:
        return 0

    count = 0
    now = time.time()

    for entry_id, adjustment in adjustments:
        if adjustment > 0:
            db.execute(
                """UPDATE memory_active
                   SET relevance_score = MIN(1.0, relevance_score + ?),
                       last_accessed = ?
                   WHERE id = ?""",
                (adjustment, now, entry_id),
            )
        elif adjustment < 0:
            db.execute(
                """UPDATE memory_active
                   SET relevance_score = MAX(0.0, relevance_score + ?)
                   WHERE id = ?""",
                (adjustment, entry_id),
            )
        count += 1

    if count > 0:
        db.commit()

    return count

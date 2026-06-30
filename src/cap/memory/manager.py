"""
CAP Working Memory Assembly.

Assembles working memory context from three sources within a token budget:
  - Pinned (5000 tokens): corrections with importance >= 0.8, active baseline rules
  - Session (5000 tokens): last 10 events from current session
  - Retrieved (5000 tokens): top-K by score from memory_active, truncated to budget

Token counting uses len(text) // 4 approximation.
"""

import time
import sqlite3
from typing import Optional

from cap.memory.scorer import score as compute_score

# Budget allocation
DEFAULT_MAX_TOKENS = 15000
PINNED_BUDGET = 5000
SESSION_BUDGET = 5000
RETRIEVED_BUDGET = 5000


def _count_tokens(text: str) -> int:
    """Approximate token count: len(text) // 4."""
    return len(text) // 4


def _truncate_to_budget(text: str, budget: int) -> str:
    """Truncate text to fit within token budget (chars = budget * 4)."""
    max_chars = budget * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def assemble_working_memory(
    query: str,
    session_id: str,
    db: sqlite3.Connection,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """
    Assemble working memory context for a query.

    Splits budget across three tiers:
      - Pinned (1/3): high-importance corrections and baseline rules
      - Session (1/3): recent session events
      - Retrieved (1/3): top-K scored entries matching query

    Args:
        query: The current query/context to score relevance against.
        session_id: Current session identifier.
        db: SQLite connection with CAP schema.
        max_tokens: Total token budget (default 15000).

    Returns:
        Assembled working memory as a formatted string.
    """
    pinned_budget = max_tokens // 3
    session_budget = max_tokens // 3
    retrieved_budget = max_tokens - pinned_budget - session_budget

    sections = []

    # 1. Pinned: corrections with importance >= 0.8 + active baseline rules
    pinned_text = _assemble_pinned(db, pinned_budget)
    if pinned_text:
        sections.append("## Pinned\n" + pinned_text)

    # 2. Session: last 10 events from current session
    session_text = _assemble_session(db, session_id, session_budget)
    if session_text:
        sections.append("## Session Context\n" + session_text)

    # 3. Retrieved: top-K by score
    retrieved_text = _assemble_retrieved(db, query, retrieved_budget)
    if retrieved_text:
        sections.append("## Retrieved\n" + retrieved_text)

    return "\n\n".join(sections)


def _assemble_pinned(db: sqlite3.Connection, budget: int) -> str:
    """
    Fetch pinned entries: corrections with importance >= 0.8 and active baseline rules.
    """
    entries = []

    # High-importance corrections
    try:
        rows = db.execute(
            """SELECT content, importance FROM memory_active
               WHERE category IN ('correction', 'baseline')
               AND importance >= 0.8
               AND consolidated_into IS NULL
               ORDER BY importance DESC, last_accessed DESC
               LIMIT 50"""
        ).fetchall()
        for row in rows:
            entries.append(row["content"] if isinstance(row, sqlite3.Row) else row[0])
    except (sqlite3.OperationalError, KeyError):
        pass

    # Active baseline rules from correction_patterns
    try:
        rules = db.execute(
            """SELECT correction FROM correction_patterns
               WHERE baseline_rule IS NOT NULL
               ORDER BY occurrence_count DESC
               LIMIT 20"""
        ).fetchall()
        for row in rules:
            entries.append(row["correction"] if isinstance(row, sqlite3.Row) else row[0])
    except (sqlite3.OperationalError, KeyError):
        pass

    # Truncate to budget
    result = []
    tokens_used = 0
    for entry in entries:
        entry_tokens = _count_tokens(entry)
        if tokens_used + entry_tokens > budget:
            remaining = budget - tokens_used
            if remaining > 10:
                result.append(_truncate_to_budget(entry, remaining))
            break
        result.append(entry)
        tokens_used += entry_tokens

    return "\n---\n".join(result)


def _assemble_session(db: sqlite3.Connection, session_id: str, budget: int) -> str:
    """
    Fetch last 10 events from the current session via memory_working or learning_events.
    """
    entries = []

    # Try memory_working first (session-specific loaded entries)
    try:
        rows = db.execute(
            """SELECT content FROM memory_working
               WHERE session_id = ?
               ORDER BY loaded_at DESC
               LIMIT 10""",
            (session_id,),
        ).fetchall()
        for row in rows:
            entries.append(row["content"] if isinstance(row, sqlite3.Row) else row[0])
    except (sqlite3.OperationalError, KeyError):
        pass

    # Also pull from learning_events for this session
    if len(entries) < 10:
        remaining = 10 - len(entries)
        try:
            rows = db.execute(
                """SELECT payload FROM learning_events
                   WHERE session_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (session_id, remaining),
            ).fetchall()
            for row in rows:
                entries.append(row["payload"] if isinstance(row, sqlite3.Row) else row[0])
        except (sqlite3.OperationalError, KeyError):
            pass

    # Truncate to budget
    result = []
    tokens_used = 0
    for entry in entries:
        entry_tokens = _count_tokens(entry)
        if tokens_used + entry_tokens > budget:
            remaining = budget - tokens_used
            if remaining > 10:
                result.append(_truncate_to_budget(entry, remaining))
            break
        result.append(entry)
        tokens_used += entry_tokens

    return "\n---\n".join(result)


def _assemble_retrieved(db: sqlite3.Connection, query: str, budget: int) -> str:
    """
    Retrieve top-K entries by composite score, optionally boosted by FTS5 relevance.
    """
    entries = []
    now = time.time()

    # Get max_access for frequency normalization
    try:
        max_access_row = db.execute(
            "SELECT MAX(access_count) as max_ac FROM memory_active"
        ).fetchone()
        max_access = (max_access_row["max_ac"] if isinstance(max_access_row, sqlite3.Row)
                      else max_access_row[0]) or 100
    except (sqlite3.OperationalError, TypeError):
        max_access = 100

    # If query provided, use FTS5 for relevance-boosted retrieval
    if query.strip():
        try:
            # Sanitize query for FTS5 (remove special chars)
            safe_query = " ".join(
                w for w in query.split() if w.isalnum() or w.replace("_", "").isalnum()
            )
            if safe_query:
                rows = db.execute(
                    """SELECT a.id, a.content, a.last_accessed, a.importance,
                              a.access_count, a.relevance_score, rank
                       FROM memory_active a
                       JOIN memory_fts ON memory_fts.rowid = a.rowid
                       WHERE memory_fts MATCH ?
                       AND a.consolidated_into IS NULL
                       ORDER BY rank
                       LIMIT 50""",
                    (safe_query,),
                ).fetchall()

                # Compute max BM25 rank for normalization
                max_rank = 1.0
                if rows:
                    max_rank = max(abs(r["rank"] if isinstance(r, sqlite3.Row) else r[6]) for r in rows) or 1.0

                scored = []
                for row in rows:
                    if isinstance(row, sqlite3.Row):
                        entry_dict = {
                            "last_accessed": row["last_accessed"],
                            "importance": row["importance"],
                            "access_count": row["access_count"],
                            "bm25_rank": row["rank"],
                            "max_bm25_rank": max_rank,
                            "max_access": max_access,
                        }
                        content = row["content"]
                    else:
                        entry_dict = {
                            "last_accessed": row[2],
                            "importance": row[3],
                            "access_count": row[4],
                            "bm25_rank": row[6],
                            "max_bm25_rank": max_rank,
                            "max_access": max_access,
                        }
                        content = row[1]

                    s = compute_score(entry_dict, query=query, now=now)
                    scored.append((s, content))

                scored.sort(key=lambda x: x[0], reverse=True)
                entries = [content for _, content in scored]
        except (sqlite3.OperationalError, KeyError):
            pass

    # Fallback: use pre-computed composite_score if FTS didn't yield results
    if not entries:
        try:
            rows = db.execute(
                """SELECT content FROM memory_active
                   WHERE consolidated_into IS NULL
                   ORDER BY composite_score DESC
                   LIMIT 30"""
            ).fetchall()
            for row in rows:
                entries.append(row["content"] if isinstance(row, sqlite3.Row) else row[0])
        except (sqlite3.OperationalError, KeyError):
            pass

    # Truncate to budget
    result = []
    tokens_used = 0
    for entry in entries:
        entry_tokens = _count_tokens(entry)
        if tokens_used + entry_tokens > budget:
            remaining = budget - tokens_used
            if remaining > 10:
                result.append(_truncate_to_budget(entry, remaining))
            break
        result.append(entry)
        tokens_used += entry_tokens

    return "\n---\n".join(result)

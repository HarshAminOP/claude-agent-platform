"""
CAP Memory Relevance Scoring.

Computes composite relevance scores for memory entries using a weighted formula:
  0.25 * recency + 0.25 * importance + 0.35 * relevance + 0.15 * frequency

Recency uses exponential decay (half-life 7 days).
Importance is a static 0-1 value from entry metadata.
Relevance is FTS5 BM25 rank normalized to 0-1.
Frequency is log-scaled access count relative to max access count.
"""

import math
import time
from typing import Optional

# Scoring weights
WEIGHT_RECENCY = 0.25
WEIGHT_IMPORTANCE = 0.25
WEIGHT_RELEVANCE = 0.35
WEIGHT_FREQUENCY = 0.15

# Recency half-life in days
DECAY_HALF_LIFE_DAYS = 7

# ln(2) precomputed for decay calculation
_LN2 = 0.6931471805599453


def score(entry: dict, query: str = "", now: Optional[float] = None) -> float:
    """
    Compute composite relevance score for a memory entry.

    Args:
        entry: Dict with keys: last_accessed, importance, access_count,
               relevance_score (or bm25_rank), max_access (optional).
        query: Optional query string (unused in formula directly;
               caller should pre-compute bm25_rank or relevance_score).
        now: Current timestamp. Defaults to time.time().

    Returns:
        Float between 0.0 and 1.0 representing composite relevance.
    """
    if now is None:
        now = time.time()

    recency = _compute_recency(entry, now)
    importance = _compute_importance(entry)
    relevance = _compute_relevance(entry)
    frequency = _compute_frequency(entry)

    composite = (
        WEIGHT_RECENCY * recency
        + WEIGHT_IMPORTANCE * importance
        + WEIGHT_RELEVANCE * relevance
        + WEIGHT_FREQUENCY * frequency
    )

    return round(max(0.0, min(1.0, composite)), 4)


def _compute_recency(entry: dict, now: float) -> float:
    """
    Exponential decay from last_accessed timestamp.
    Half-life of 7 days: score = exp(-ln(2) * days / half_life)
    """
    last_accessed = entry.get("last_accessed", now)
    days_since = (now - last_accessed) / 86400.0
    if days_since <= 0:
        return 1.0
    return math.exp(-_LN2 * days_since / DECAY_HALF_LIFE_DAYS)


def _compute_importance(entry: dict) -> float:
    """
    Static importance value from entry metadata, clamped to [0, 1].
    """
    imp = entry.get("importance", 0.5)
    return max(0.0, min(1.0, float(imp)))


def _compute_relevance(entry: dict) -> float:
    """
    FTS5 BM25 rank normalized to [0, 1].

    Expects either:
      - 'bm25_rank': raw BM25 score (negative, lower = more relevant in SQLite FTS5)
      - 'relevance_score': pre-normalized 0-1 value

    For BM25 normalization: score = min(1.0, abs(bm25_rank) / max_rank)
    where max_rank is the maximum absolute BM25 score in the result set.
    """
    # Pre-normalized relevance score takes priority
    if "relevance_score" in entry and entry["relevance_score"] is not None:
        return max(0.0, min(1.0, float(entry["relevance_score"])))

    # Raw BM25 rank (SQLite FTS5 returns negative values; more negative = more relevant)
    bm25_rank = entry.get("bm25_rank")
    if bm25_rank is not None:
        max_rank = entry.get("max_bm25_rank", 10.0)  # normalization ceiling
        if max_rank == 0:
            return 0.0
        normalized = min(1.0, abs(float(bm25_rank)) / abs(float(max_rank)))
        return normalized

    return 0.0


def _compute_frequency(entry: dict) -> float:
    """
    Log-scaled access frequency: log(access_count + 1) / log(max_access + 1).
    """
    access_count = entry.get("access_count", 1)
    max_access = entry.get("max_access", 100)

    if max_access <= 0:
        return 0.0

    numerator = math.log(access_count + 1)
    denominator = math.log(max_access + 1)

    if denominator == 0:
        return 0.0

    return min(1.0, numerator / denominator)

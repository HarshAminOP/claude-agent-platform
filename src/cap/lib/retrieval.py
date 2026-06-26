"""Hybrid retrieval engine for the CAP knowledge base.

Combines three search channels:
  1. FTS5 keyword search  (BM25 via SQLite ``knowledge_fts`` virtual table)
  2. LanceDB vector search (cosine similarity via ``vectors_table``)
  3. Graph traversal       (BFS on ``knowledge_graph_nodes`` / ``knowledge_graph_edges``)

Results from all active channels are merged with Reciprocal Rank Fusion (RRF).

Graceful degradation rules
--------------------------
- No query_vector (Bedrock unavailable)  -> skip semantic; weights: keyword=0.6, graph=0.4
- No graph entities found                -> skip graph;    weights: keyword=0.4, semantic=0.6
- Both absent                            -> all keyword;   weights: keyword=1.0
- All channels empty                     -> return []
"""

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .graph import bfs_traverse, find_entities, get_related_entries_with_depth

logger = logging.getLogger("platform.retrieval")


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    entry_id: int
    uuid: str
    title: str
    content_preview: str          # first 200 chars of content
    source_path: str | None
    content_type: str
    workspace: str
    score: float
    channels: list[str] = field(default_factory=list)  # which channels contributed


# ---------------------------------------------------------------------------
# Individual channel implementations
# ---------------------------------------------------------------------------

def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    workspace: str,
    top_k: int = 20,
    scope: str | None = None,
) -> list[tuple[int, float]]:
    """FTS5 BM25 keyword search on the ``knowledge_fts`` virtual table.

    Args:
        conn:      Active SQLite connection.
        query:     Full-text search query string.
        workspace: Workspace to scope results.
        top_k:     Maximum number of results to return.
        scope:     Optional content_type filter.

    Returns:
        List of (entry_id, bm25_score) sorted by score descending.
        BM25 scores from SQLite FTS5 are negative (more negative = better match);
        we return them as-is so callers can rank by ascending order or negate.
    """
    try:
        if scope:
            rows = conn.execute(
                """
                SELECT ke.id,
                       bm25(knowledge_fts) AS score
                FROM   knowledge_fts
                JOIN   knowledge_entries ke ON ke.id = knowledge_fts.rowid
                WHERE  knowledge_fts MATCH ?
                  AND  ke.workspace = ?
                  AND  ke.content_type = ?
                ORDER  BY score
                LIMIT  ?
                """,
                (query, workspace, scope, top_k),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ke.id,
                       bm25(knowledge_fts) AS score
                FROM   knowledge_fts
                JOIN   knowledge_entries ke ON ke.id = knowledge_fts.rowid
                WHERE  knowledge_fts MATCH ?
                  AND  ke.workspace = ?
                ORDER  BY score
                LIMIT  ?
                """,
                (query, workspace, top_k),
            ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    except sqlite3.OperationalError as exc:
        logger.warning("keyword_search failed (FTS5 unavailable?): %s", exc)
        return []


def semantic_search(
    vectors_table: Any,
    query_vector: list[float],
    workspace: str,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """LanceDB cosine similarity search.

    Args:
        vectors_table:  LanceDB table object (already opened by the caller).
        query_vector:   Embedding vector for the query.
        workspace:      Workspace to scope results.
        top_k:          Maximum number of results to return.

    Returns:
        List of (uuid, cosine_similarity) sorted by similarity descending.
    """
    try:
        from cap.lib.security import validate_workspace

        validate_workspace(workspace)
        results = (
            vectors_table.search(query_vector)
            .metric("cosine")
            .where(f"workspace = '{workspace}'")
            .limit(top_k)
            .to_list()
        )
        # LanceDB returns _distance (lower = closer for cosine distance).
        # Convert to similarity: similarity = 1 - distance.
        return [(str(r["uuid"]), 1.0 - float(r.get("_distance", 0.0))) for r in results]
    except Exception as exc:  # noqa: BLE001
        logger.warning("semantic_search failed: %s", exc)
        return []


def graph_search(
    conn: sqlite3.Connection,
    query_entities: list[str],
    workspace: str,
    depth: int = 2,
    top_k: int = 20,
) -> list[tuple[int, float]]:
    """BFS-based graph search starting from *query_entities*.

    Args:
        conn:            Active SQLite connection.
        query_entities:  Entity name fragments to seed the traversal.
        workspace:       Workspace to scope nodes and edges.
        depth:           Maximum BFS hop depth.
        top_k:           Maximum number of (entry_id, score) pairs to return.

    Returns:
        List of (entry_id, score) where score = 1/(hop_distance+1), capped at *top_k*.
    """
    # Collect seed node IDs from entity name fragments
    seed_ids: list[str] = []
    for fragment in query_entities:
        seed_ids.extend(find_entities(conn, fragment, workspace))

    if not seed_ids:
        return []

    nodes_with_depth = bfs_traverse(conn, seed_ids, max_depth=depth, workspace=workspace)
    if not nodes_with_depth:
        return []

    entry_scores = get_related_entries_with_depth(conn, nodes_with_depth, workspace)

    # Sort by score descending and cap at top_k
    entry_scores.sort(key=lambda x: x[1], reverse=True)
    return entry_scores[:top_k]


# ---------------------------------------------------------------------------
# RRF merger
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS = {"keyword": 0.3, "semantic": 0.5, "graph": 0.2}


def rrf_merge(
    keyword_results: list[tuple[int, float]],
    semantic_results: list[tuple[str, float]],
    graph_results: list[tuple[int, float]],
    weights: dict[str, float] | None = None,
    k: int = 60,
    top_k: int = 10,
) -> list[tuple[int | str, float]]:
    """Reciprocal Rank Fusion across three ranked lists.

    RRF formula: score(d) = sum_over_channels( weight_c * 1/(k + rank_c(d)) )

    IDs from the keyword and graph channels are integers (entry_id).
    IDs from the semantic channel are strings (uuid).  The merged list may
    therefore contain a mix of types; ``hybrid_search`` resolves UUIDs to
    entry_ids before constructing SearchResult objects.

    Args:
        keyword_results:   (entry_id, score) from keyword_search.  Score is
                           BM25 (negative); rank is determined by ascending
                           order (most negative = rank 1).
        semantic_results:  (uuid, cosine_similarity) from semantic_search.
                           Higher similarity = better rank.
        graph_results:     (entry_id, score) from graph_search.  Higher = better.
        weights:           Per-channel weights.  Defaults to keyword=0.3,
                           semantic=0.5, graph=0.2.
        k:                 RRF smoothing constant (default 60).
        top_k:             Number of results to return.

    Returns:
        List of (id, rrf_score) sorted by rrf_score descending, length <= top_k.
    """
    if weights is None:
        weights = dict(_DEFAULT_WEIGHTS)

    rrf_scores: dict[int | str, float] = defaultdict(float)

    # Keyword: sort ascending (most-negative BM25 first = rank 1)
    kw_sorted = sorted(keyword_results, key=lambda x: x[1])
    for rank, (entry_id, _) in enumerate(kw_sorted, start=1):
        rrf_scores[entry_id] += weights.get("keyword", 0.0) * (1.0 / (k + rank))

    # Semantic: sort descending (highest cosine first = rank 1)
    sem_sorted = sorted(semantic_results, key=lambda x: x[1], reverse=True)
    for rank, (doc_uuid, _) in enumerate(sem_sorted, start=1):
        rrf_scores[doc_uuid] += weights.get("semantic", 0.0) * (1.0 / (k + rank))

    # Graph: sort descending (highest score first = rank 1)
    gr_sorted = sorted(graph_results, key=lambda x: x[1], reverse=True)
    for rank, (entry_id, _) in enumerate(gr_sorted, start=1):
        rrf_scores[entry_id] += weights.get("graph", 0.0) * (1.0 / (k + rank))

    merged = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return merged[:top_k]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def hybrid_search(
    conn: sqlite3.Connection,
    vectors_table: Any,
    query: str,
    query_vector: list[float] | None,
    workspace: str,
    strategy: str = "hybrid",
    top_k: int = 10,
    scope: str | None = None,
) -> list[SearchResult]:
    """Full hybrid retrieval pipeline with graceful degradation.

    Args:
        conn:          Active SQLite connection.
        vectors_table: LanceDB table object, or None if vector search disabled.
        query:         Raw search query string.
        query_vector:  Pre-computed embedding vector, or None (Bedrock unavailable).
        workspace:     Workspace to scope all searches.
        strategy:      Reserved for future routing (currently all paths run hybrid).
        top_k:         Number of SearchResult objects to return.
        scope:         Optional content_type filter (e.g., "code", "config", "doc").

    Returns:
        List of SearchResult objects, sorted by RRF score descending.
    """
    # ------------------------------------------------------------------
    # 1. Run all channels
    # ------------------------------------------------------------------
    kw_results: list[tuple[int, float]] = keyword_search(conn, query, workspace, top_k=20, scope=scope)
    logger.debug("keyword_search: %d results", len(kw_results))

    sem_results: list[tuple[str, float]] = []
    if query_vector is not None and vectors_table is not None:
        sem_results = semantic_search(vectors_table, query_vector, workspace, top_k=20)
    logger.debug("semantic_search: %d results", len(sem_results))

    # Extract entity tokens from the query for graph traversal
    graph_tokens = [t.strip() for t in query.split() if len(t.strip()) >= 3]
    gr_results: list[tuple[int, float]] = graph_search(
        conn, graph_tokens, workspace, depth=2, top_k=20
    )
    logger.debug("graph_search: %d results", len(gr_results))

    # ------------------------------------------------------------------
    # 2. Determine active channels and rebalance weights
    # ------------------------------------------------------------------
    has_keyword = bool(kw_results)
    has_semantic = bool(sem_results)
    has_graph = bool(gr_results)

    if not has_keyword and not has_semantic and not has_graph:
        logger.info("hybrid_search: all channels empty for query=%r workspace=%s", query, workspace)
        return []

    weights = _compute_weights(
        has_keyword=has_keyword,
        has_semantic=has_semantic,
        has_graph=has_graph,
        query_vector=query_vector,
    )
    logger.debug("rrf weights: %s", weights)

    # ------------------------------------------------------------------
    # 3. RRF merge
    # ------------------------------------------------------------------
    merged = rrf_merge(
        kw_results if has_keyword else [],
        sem_results if has_semantic else [],
        gr_results if has_graph else [],
        weights=weights,
        top_k=top_k,
    )

    if not merged:
        return []

    # ------------------------------------------------------------------
    # 4. Resolve UUIDs and hydrate SearchResult objects
    # ------------------------------------------------------------------
    # Build a uuid -> entry_id map for semantic hits
    uuid_to_entry = _resolve_uuids(conn, [doc_id for doc_id, _ in merged if isinstance(doc_id, str)], workspace)

    # Collect all entry_ids with their rrf scores
    entry_scores: dict[int, float] = {}
    entry_channels: dict[int, list[str]] = defaultdict(list)

    kw_ids = {eid for eid, _ in kw_results}
    sem_uuids = {u for u, _ in sem_results}
    gr_ids = {eid for eid, _ in gr_results}

    for doc_id, rrf_score in merged:
        if isinstance(doc_id, str):
            # Semantic UUID — resolve to entry_id
            entry_id = uuid_to_entry.get(doc_id)
            if entry_id is None:
                continue
            if entry_id not in entry_scores or entry_scores[entry_id] < rrf_score:
                entry_scores[entry_id] = rrf_score
            if doc_id in sem_uuids:
                _append_unique(entry_channels[entry_id], "semantic")
        else:
            entry_id = doc_id
            if entry_id not in entry_scores or entry_scores[entry_id] < rrf_score:
                entry_scores[entry_id] = rrf_score
            if entry_id in kw_ids:
                _append_unique(entry_channels[entry_id], "keyword")
            if entry_id in gr_ids:
                _append_unique(entry_channels[entry_id], "graph")

    if not entry_scores:
        return []

    # ------------------------------------------------------------------
    # 5. Fetch entry metadata from knowledge_entries
    # ------------------------------------------------------------------
    placeholders = ",".join("?" * len(entry_scores))
    rows = conn.execute(
        f"""
        SELECT id, uuid, title, content, source_path, content_type, workspace
        FROM   knowledge_entries
        WHERE  id IN ({placeholders})
          AND  workspace = ?
        """,
        list(entry_scores.keys()) + [workspace],
    ).fetchall()

    results: list[SearchResult] = []
    for row in rows:
        eid, doc_uuid, title, content, source_path, content_type, ws = row
        preview = (content or "")[:200]
        results.append(
            SearchResult(
                entry_id=eid,
                uuid=doc_uuid or "",
                title=title or "",
                content_preview=preview,
                source_path=source_path,
                content_type=content_type or "unknown",
                workspace=ws,
                score=entry_scores.get(eid, 0.0),
                channels=entry_channels.get(eid, []),
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_weights(
    *,
    has_keyword: bool,
    has_semantic: bool,
    has_graph: bool,
    query_vector: list[float] | None,
) -> dict[str, float]:
    """Return normalised per-channel weights based on which channels are active.

    Degradation rules (in priority order):
      1. All three present         -> default: keyword=0.3, semantic=0.5, graph=0.2
      2. No semantic               -> keyword=0.6, graph=0.4
      3. No graph                  -> keyword=0.4, semantic=0.6
      4. Only keyword              -> keyword=1.0
      5. Only semantic             -> semantic=1.0
      6. Only graph                -> graph=1.0
      7. Keyword + semantic only   -> keyword=0.4, semantic=0.6
      8. Keyword + graph only      -> keyword=0.6, graph=0.4
      9. Semantic + graph only     -> semantic=0.6, graph=0.4
    """
    active = sum([has_keyword, has_semantic, has_graph])

    if active == 3:
        return {"keyword": 0.3, "semantic": 0.5, "graph": 0.2}

    if active == 0:
        return {"keyword": 1.0, "semantic": 0.0, "graph": 0.0}

    if has_keyword and has_semantic and not has_graph:
        return {"keyword": 0.4, "semantic": 0.6, "graph": 0.0}

    if has_keyword and not has_semantic and has_graph:
        return {"keyword": 0.6, "semantic": 0.0, "graph": 0.4}

    if not has_keyword and has_semantic and has_graph:
        return {"keyword": 0.0, "semantic": 0.6, "graph": 0.4}

    if has_keyword and not has_semantic and not has_graph:
        return {"keyword": 1.0, "semantic": 0.0, "graph": 0.0}

    if not has_keyword and has_semantic and not has_graph:
        return {"keyword": 0.0, "semantic": 1.0, "graph": 0.0}

    if not has_keyword and not has_semantic and has_graph:
        return {"keyword": 0.0, "semantic": 0.0, "graph": 1.0}

    # Fallback — should not be reached
    return {"keyword": 0.3, "semantic": 0.5, "graph": 0.2}


def _resolve_uuids(
    conn: sqlite3.Connection,
    uuids: list[str],
    workspace: str,
) -> dict[str, int]:
    """Map uuid -> entry_id for all provided UUIDs."""
    if not uuids:
        return {}
    ph = ",".join("?" * len(uuids))
    rows = conn.execute(
        f"SELECT uuid, id FROM knowledge_entries WHERE uuid IN ({ph}) AND workspace = ?",
        uuids + [workspace],
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _append_unique(lst: list[str], value: str) -> None:
    """Append *value* to *lst* only if not already present."""
    if value not in lst:
        lst.append(value)

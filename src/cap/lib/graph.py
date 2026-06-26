"""Graph traversal for the CAP knowledge graph.

Operates on the SQLite adjacency tables created by db_init:
  - knowledge_graph_nodes (id TEXT PK, entity_name, entity_type, workspace, metadata, created_at)
  - knowledge_graph_edges (id INTEGER PK, source_id, target_id, predicate, weight, metadata,
                           workspace, UNIQUE(source_id, target_id, predicate))

All traversal uses iterative BFS with a visited set to handle cycles (review finding H13).
No recursion anywhere in this module.
"""

import json
import logging
import sqlite3
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("platform.graph")


# ---------------------------------------------------------------------------
# Node / edge helpers
# ---------------------------------------------------------------------------

def _node_id(entity_name: str, workspace: str) -> str:
    """Deterministic node ID: stable across upserts."""
    slug = f"{workspace}::{entity_name}".lower().replace(" ", "_")
    return uuid.uuid5(uuid.NAMESPACE_URL, slug).hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_entities(conn: sqlite3.Connection, query: str, workspace: str) -> list[str]:
    """Return node IDs whose entity_name matches *query* (case-insensitive LIKE).

    Args:
        conn:       Active SQLite connection.
        query:      Text fragment to search for.  Wildcards are added automatically.
        workspace:  Workspace to scope the search.

    Returns:
        List of node ID strings (may be empty).
    """
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id
        FROM   knowledge_graph_nodes
        WHERE  entity_name LIKE ?
          AND  workspace   = ?
        """,
        (pattern, workspace),
    ).fetchall()
    return [r[0] for r in rows]


def bfs_traverse(
    conn: sqlite3.Connection,
    start_ids: list[str],
    max_depth: int = 2,
    workspace: str | None = None,
) -> list[tuple[str, int]]:
    """BFS from *start_ids*, up to *max_depth* hops.

    Iterative BFS with a visited set — safe against cycles in the graph.

    Args:
        conn:       Active SQLite connection.
        start_ids:  Seed node IDs for the traversal.
        max_depth:  Maximum number of hops from any seed node.
        workspace:  If given, only cross edges that belong to this workspace.

    Returns:
        List of (node_id, hop_distance) pairs, sorted by hop_distance ascending.
        Seed nodes themselves are included at distance 0.
    """
    if not start_ids:
        return []

    visited: dict[str, int] = {}          # node_id -> first-seen hop distance
    queue: deque[tuple[str, int]] = deque()

    for sid in start_ids:
        if sid not in visited:
            visited[sid] = 0
            queue.append((sid, 0))

    while queue:
        current_id, depth = queue.popleft()

        if depth >= max_depth:
            continue

        next_depth = depth + 1

        rows = _neighbours(conn, current_id, workspace)

        for (neighbour_id,) in rows:
            if neighbour_id not in visited:
                visited[neighbour_id] = next_depth
                queue.append((neighbour_id, next_depth))

    return sorted(visited.items(), key=lambda x: x[1])


def _neighbours(
    conn: sqlite3.Connection,
    node_id: str,
    workspace: str | None,
) -> list[tuple[str]]:
    """Return all direct neighbours of *node_id* (undirected view of the graph)."""
    if workspace:
        return conn.execute(
            """
            SELECT target_id AS neighbour FROM knowledge_graph_edges
            WHERE  source_id = ? AND workspace = ?
            UNION
            SELECT source_id AS neighbour FROM knowledge_graph_edges
            WHERE  target_id = ? AND workspace = ?
            """,
            (node_id, workspace, node_id, workspace),
        ).fetchall()
    else:
        return conn.execute(
            """
            SELECT target_id AS neighbour FROM knowledge_graph_edges
            WHERE  source_id = ?
            UNION
            SELECT source_id AS neighbour FROM knowledge_graph_edges
            WHERE  target_id = ?
            """,
            (node_id, node_id),
        ).fetchall()


def get_related_entries(
    conn: sqlite3.Connection,
    node_ids: list[str],
    workspace: str,
) -> list[tuple[int, float]]:
    """Find knowledge_entries linked to *node_ids* via graph edges.

    The graph edges store either source_id or target_id as entry references
    encoded in the node metadata (``entry_id`` key).  This function looks up
    nodes for all provided IDs and collects the associated entry IDs, then
    scores them by 1/(hop_distance+1).

    In practice the caller (retrieval.graph_search) has already run BFS and
    provides (node_id, hop_distance) pairs implicitly via the node_ids list.
    To preserve hop-distance scoring this function accepts node_ids as plain
    strings and assigns a flat score of 1.0; the caller should pass
    (node_id, hop_distance) pairs using the companion helper below.

    Args:
        conn:      Active SQLite connection.
        node_ids:  Node IDs to look up.
        workspace: Workspace filter for entries.

    Returns:
        List of (entry_id, score) pairs, deduplicated (highest score wins).
    """
    if not node_ids:
        return []

    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"""
        SELECT n.metadata
        FROM   knowledge_graph_nodes n
        WHERE  n.id IN ({placeholders})
          AND  n.workspace = ?
        """,
        node_ids + [workspace],
    ).fetchall()

    best: dict[int, float] = {}
    for (raw_meta,) in rows:
        if not raw_meta:
            continue
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            continue
        entry_id = meta.get("entry_id")
        if entry_id is None:
            continue
        score = meta.get("score", 1.0)
        if entry_id not in best or best[entry_id] < score:
            best[entry_id] = float(score)

    return list(best.items())


def get_related_entries_with_depth(
    conn: sqlite3.Connection,
    nodes_with_depth: list[tuple[str, int]],
    workspace: str,
) -> list[tuple[int, float]]:
    """Like ``get_related_entries`` but scores by 1/(hop_distance+1).

    Args:
        conn:              Active SQLite connection.
        nodes_with_depth:  (node_id, hop_distance) pairs from BFS.
        workspace:         Workspace filter.

    Returns:
        List of (entry_id, score) pairs, deduplicated (highest score wins).
    """
    if not nodes_with_depth:
        return []

    depth_map = {nid: depth for nid, depth in nodes_with_depth}
    node_ids = list(depth_map.keys())
    placeholders = ",".join("?" * len(node_ids))

    rows = conn.execute(
        f"""
        SELECT n.id, n.metadata
        FROM   knowledge_graph_nodes n
        WHERE  n.id IN ({placeholders})
          AND  n.workspace = ?
        """,
        node_ids + [workspace],
    ).fetchall()

    best: dict[int, float] = {}
    for node_id, raw_meta in rows:
        if not raw_meta:
            continue
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            continue
        entry_id = meta.get("entry_id")
        if entry_id is None:
            continue
        depth = depth_map.get(node_id, 0)
        score = 1.0 / (depth + 1)
        if entry_id not in best or best[entry_id] < score:
            best[entry_id] = score

    return list(best.items())


def add_edge(
    conn: sqlite3.Connection,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    predicate: str,
    workspace: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Upsert an edge between two named entities, creating nodes if needed.

    The UNIQUE constraint on (source_id, target_id, predicate) means a
    duplicate insert updates the weight and metadata instead of erroring.

    Args:
        conn:         Active SQLite connection.
        source_name:  Human-readable name of the source entity.
        source_type:  Entity type tag (e.g. "service", "repo", "team").
        target_name:  Human-readable name of the target entity.
        target_type:  Entity type tag for the target.
        predicate:    Relationship label (e.g. "depends_on", "owns").
        workspace:    Workspace these entities belong to.
        metadata:     Optional dict serialised to JSON on the edge.
    """
    now = _now()
    source_id = _node_id(source_name, workspace)
    target_id = _node_id(target_name, workspace)

    # Upsert source node
    conn.execute(
        """
        INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entity_name = excluded.entity_name,
            entity_type = excluded.entity_type
        """,
        (source_id, source_name, source_type, workspace, None, now),
    )

    # Upsert target node
    conn.execute(
        """
        INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entity_name = excluded.entity_name,
            entity_type = excluded.entity_type
        """,
        (target_id, target_name, target_type, workspace, None, now),
    )

    meta_json = json.dumps(metadata) if metadata else None

    # Upsert edge (update weight/metadata on conflict)
    conn.execute(
        """
        INSERT INTO knowledge_graph_edges
            (source_id, target_id, predicate, weight, metadata, workspace)
        VALUES (?, ?, ?, 1.0, ?, ?)
        ON CONFLICT(source_id, target_id, predicate) DO UPDATE SET
            weight   = weight + 1,
            metadata = excluded.metadata
        """,
        (source_id, target_id, predicate, meta_json, workspace),
    )

    conn.commit()
    logger.debug(
        "add_edge: %s -[%s]-> %s (workspace=%s)", source_name, predicate, target_name, workspace
    )


def get_node_context(
    conn: sqlite3.Connection,
    entity_name: str,
    workspace: str,
) -> dict:
    """Return a node's full context: its own attributes, all edges in/out, and adjacent nodes.

    Args:
        conn:         Active SQLite connection.
        entity_name:  Name of the entity to look up.
        workspace:    Workspace scope.

    Returns:
        Dict with keys ``node``, ``edges_out``, ``edges_in``, ``related_nodes``.
        Returns an empty dict if the node does not exist.
    """
    node_id = _node_id(entity_name, workspace)

    node_row = conn.execute(
        """
        SELECT id, entity_name, entity_type, workspace, metadata, created_at
        FROM   knowledge_graph_nodes
        WHERE  id = ? AND workspace = ?
        """,
        (node_id, workspace),
    ).fetchone()

    if not node_row:
        return {}

    node = {
        "id": node_row[0],
        "entity_name": node_row[1],
        "entity_type": node_row[2],
        "workspace": node_row[3],
        "metadata": _safe_json(node_row[4]),
        "created_at": node_row[5],
    }

    # Outbound edges
    out_rows = conn.execute(
        """
        SELECT e.id, e.target_id, n.entity_name, e.predicate, e.weight, e.metadata
        FROM   knowledge_graph_edges e
        JOIN   knowledge_graph_nodes n ON n.id = e.target_id
        WHERE  e.source_id = ? AND e.workspace = ?
        """,
        (node_id, workspace),
    ).fetchall()

    edges_out = [
        {
            "edge_id": r[0],
            "target_id": r[1],
            "target_name": r[2],
            "predicate": r[3],
            "weight": r[4],
            "metadata": _safe_json(r[5]),
        }
        for r in out_rows
    ]

    # Inbound edges
    in_rows = conn.execute(
        """
        SELECT e.id, e.source_id, n.entity_name, e.predicate, e.weight, e.metadata
        FROM   knowledge_graph_edges e
        JOIN   knowledge_graph_nodes n ON n.id = e.source_id
        WHERE  e.target_id = ? AND e.workspace = ?
        """,
        (node_id, workspace),
    ).fetchall()

    edges_in = [
        {
            "edge_id": r[0],
            "source_id": r[1],
            "source_name": r[2],
            "predicate": r[3],
            "weight": r[4],
            "metadata": _safe_json(r[5]),
        }
        for r in in_rows
    ]

    # Adjacent node names (union of all neighbours)
    all_neighbour_ids = {r[1] for r in out_rows} | {r[1] for r in in_rows}
    related_nodes: list[dict] = []
    if all_neighbour_ids:
        ph = ",".join("?" * len(all_neighbour_ids))
        neighbour_rows = conn.execute(
            f"""
            SELECT id, entity_name, entity_type
            FROM   knowledge_graph_nodes
            WHERE  id IN ({ph})
            """,
            list(all_neighbour_ids),
        ).fetchall()
        related_nodes = [
            {"id": r[0], "entity_name": r[1], "entity_type": r[2]}
            for r in neighbour_rows
        ]

    return {
        "node": node,
        "edges_out": edges_out,
        "edges_in": edges_in,
        "related_nodes": related_nodes,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_json(raw: str | None) -> Any:
    """Parse JSON silently; return None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

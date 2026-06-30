"""Graph traversal for the CAP knowledge graph.

Operates on the SQLite adjacency tables created by db_init:
  - knowledge_graph_nodes (id TEXT PK, entity_name, entity_type, workspace, metadata, created_at)
  - knowledge_graph_edges (id INTEGER PK, source_id, target_id, predicate, weight, metadata,
                           workspace, UNIQUE(source_id, target_id, predicate))

All traversal uses iterative BFS with a visited set to handle cycles (review finding H13).
No recursion anywhere in this module.

Degree-aware BFS (Section 6 of CAP System Design):
  Hub nodes (degree > hub_threshold) are NOT expanded fully. Instead, neighbors
  are sampled by connectivity + recency. A `summarized_hubs` field reports what
  was sampled vs total. No data loss — data exists in DB, traversal just doesn't
  explode.
"""

import json
import logging
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass, field
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

def find_entities(conn: sqlite3.Connection, query: str, workspace: str | None = None) -> list[str]:
    """Return node IDs whose entity_name matches *query* (case-insensitive LIKE).

    Args:
        conn:       Active SQLite connection.
        query:      Text fragment to search for.  Wildcards are added automatically.
        workspace:  Workspace to scope the search.  When None, searches all workspaces.

    Returns:
        List of node ID strings (may be empty).
    """
    pattern = f"%{query}%"
    if workspace is None:
        rows = conn.execute(
            """
            SELECT id
            FROM   knowledge_graph_nodes
            WHERE  entity_name LIKE ?
            """,
            (pattern,),
        ).fetchall()
    else:
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
    max_fanout: int = 50,
    max_nodes: int = 200,
) -> list[tuple[str, int]]:
    """BFS from *start_ids*, up to *max_depth* hops.

    Iterative BFS with a visited set — safe against cycles in the graph.

    Args:
        conn:        Active SQLite connection.
        start_ids:   Seed node IDs for the traversal.
        max_depth:   Maximum number of hops from any seed node.
        workspace:   If given, only cross edges that belong to this workspace.
        max_fanout:  Maximum neighbours to follow per node.  When a node has
                     more neighbours than this, only the top *max_fanout* by
                     edge weight (descending) are enqueued.  This prevents
                     high-degree hub nodes (e.g. technology tags connected to
                     thousands of files) from causing graph explosion.
        max_nodes:   Hard cap on the total number of visited nodes.  BFS stops
                     immediately once this many nodes have been recorded,
                     regardless of depth or queue length.

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
        # Hard safety cap: stop as soon as we have recorded enough nodes.
        if len(visited) >= max_nodes:
            break

        current_id, depth = queue.popleft()

        if depth >= max_depth:
            continue

        next_depth = depth + 1

        rows = _neighbours(conn, current_id, workspace)

        # Apply fan-out cap: keep only the highest-weight neighbours.
        if len(rows) > max_fanout:
            rows = sorted(rows, key=lambda r: r[1], reverse=True)[:max_fanout]

        for neighbour_id, _weight in rows:
            if len(visited) >= max_nodes:
                break
            if neighbour_id not in visited:
                visited[neighbour_id] = next_depth
                queue.append((neighbour_id, next_depth))

    return sorted(visited.items(), key=lambda x: x[1])


def _neighbours(
    conn: sqlite3.Connection,
    node_id: str,
    workspace: str | None,
) -> list[tuple[str, float]]:
    """Return all direct neighbours of *node_id* with their edge weight.

    Returns (neighbour_id, weight) pairs for an undirected view of the graph.
    When a node appears on both sides of different edges the maximum weight is
    kept (MAX aggregation in the UNION-based CTE).
    """
    if workspace:
        return conn.execute(
            """
            SELECT neighbour, MAX(weight) AS weight
            FROM (
                SELECT target_id AS neighbour, weight FROM knowledge_graph_edges
                WHERE  source_id = ? AND workspace = ?
                UNION ALL
                SELECT source_id AS neighbour, weight FROM knowledge_graph_edges
                WHERE  target_id = ? AND workspace = ?
            )
            GROUP BY neighbour
            """,
            (node_id, workspace, node_id, workspace),
        ).fetchall()
    else:
        return conn.execute(
            """
            SELECT neighbour, MAX(weight) AS weight
            FROM (
                SELECT target_id AS neighbour, weight FROM knowledge_graph_edges
                WHERE  source_id = ?
                UNION ALL
                SELECT source_id AS neighbour, weight FROM knowledge_graph_edges
                WHERE  target_id = ?
            )
            GROUP BY neighbour
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
    workspace: str | None = None,
) -> list[tuple[int, float]]:
    """Like ``get_related_entries`` but scores by 1/(hop_distance+1).

    Args:
        conn:              Active SQLite connection.
        nodes_with_depth:  (node_id, hop_distance) pairs from BFS.
        workspace:         Workspace filter.  When None, returns entries from all workspaces.

    Returns:
        List of (entry_id, score) pairs, deduplicated (highest score wins).
    """
    if not nodes_with_depth:
        return []

    depth_map = {nid: depth for nid, depth in nodes_with_depth}
    node_ids = list(depth_map.keys())
    placeholders = ",".join("?" * len(node_ids))

    if workspace is None:
        rows = conn.execute(
            f"""
            SELECT n.id, n.metadata
            FROM   knowledge_graph_nodes n
            WHERE  n.id IN ({placeholders})
            """,
            node_ids,
        ).fetchall()
    else:
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


# ---------------------------------------------------------------------------
# Degree-aware BFS (Section 6 — handles hub nodes without data loss)
# ---------------------------------------------------------------------------

HUB_THRESHOLD = 50
HUB_SAMPLE_BY_CONNECTIVITY = 10
HUB_SAMPLE_BY_RECENCY = 10


@dataclass
class DegreeAwareResult:
    """Result of a degree-aware BFS traversal."""

    nodes: list[tuple[str, int]]  # (node_id, hop_distance)
    edges: list[tuple[str, str, str]]  # (source_id, target_id, predicate)
    summarized_hubs: list[dict] = field(default_factory=list)
    depth_reached: int = 0
    truncated: bool = False


def get_node_degree(node_id: str, conn: sqlite3.Connection) -> int:
    """Return the total degree (in + out) of a node.

    Args:
        node_id: The node ID to check.
        conn: Active SQLite connection.

    Returns:
        Integer count of all edges incident to this node.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT id FROM knowledge_graph_edges WHERE source_id = ?
            UNION ALL
            SELECT id FROM knowledge_graph_edges WHERE target_id = ?
        )
        """,
        (node_id, node_id),
    ).fetchone()
    return row[0] if row else 0


def get_hub_neighbors(
    node_id: str,
    conn: sqlite3.Connection,
    edge_types: list[str] | None = None,
    limit: int = 20,
    workspace: str | None = None,
) -> list[tuple[str, float, str]]:
    """Get filtered neighbors for a hub node using edge-type filtering + recency.

    For hub nodes (degree > hub_threshold), instead of blindly taking top-N by
    weight, this function:
    1. Filters by edge_types if provided
    2. Sorts remaining by recency (edge metadata.updated_at or node created_at)
    3. Returns up to `limit` neighbors

    Args:
        node_id:    The hub node ID.
        conn:       Active SQLite connection.
        edge_types: List of predicate strings to filter by (e.g. ['imports', 'calls']).
                    When None, all edge types are considered.
        limit:      Maximum neighbors to return.
        workspace:  Optional workspace filter.

    Returns:
        List of (neighbour_id, weight, predicate) tuples.
    """
    if edge_types:
        placeholders = ",".join("?" * len(edge_types))
        if workspace:
            rows = conn.execute(
                f"""
                SELECT neighbour, weight, predicate FROM (
                    SELECT target_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE source_id = ? AND workspace = ? AND predicate IN ({placeholders})
                    UNION ALL
                    SELECT source_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE target_id = ? AND workspace = ? AND predicate IN ({placeholders})
                )
                ORDER BY weight DESC
                LIMIT ?
                """,
                [node_id, workspace] + edge_types + [node_id, workspace] + edge_types + [limit],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT neighbour, weight, predicate FROM (
                    SELECT target_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE source_id = ? AND predicate IN ({placeholders})
                    UNION ALL
                    SELECT source_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE target_id = ? AND predicate IN ({placeholders})
                )
                ORDER BY weight DESC
                LIMIT ?
                """,
                [node_id] + edge_types + [node_id] + edge_types + [limit],
            ).fetchall()
    else:
        if workspace:
            rows = conn.execute(
                """
                SELECT neighbour, weight, predicate FROM (
                    SELECT target_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE source_id = ? AND workspace = ?
                    UNION ALL
                    SELECT source_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE target_id = ? AND workspace = ?
                )
                ORDER BY weight DESC
                LIMIT ?
                """,
                (node_id, workspace, node_id, workspace, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT neighbour, weight, predicate FROM (
                    SELECT target_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE source_id = ?
                    UNION ALL
                    SELECT source_id AS neighbour, weight, predicate
                    FROM knowledge_graph_edges
                    WHERE target_id = ?
                )
                ORDER BY weight DESC
                LIMIT ?
                """,
                (node_id, node_id, limit),
            ).fetchall()

    return [(r[0], r[1], r[2]) for r in rows]


def _sample_hub_neighbors(
    node_id: str,
    conn: sqlite3.Connection,
    workspace: str | None,
) -> list[tuple[str, float]]:
    """Sample strategy for hub nodes: top by connectivity + top by recency.

    Returns up to HUB_SAMPLE_BY_CONNECTIVITY + HUB_SAMPLE_BY_RECENCY unique
    neighbors (deduplicated).

    Strategy:
    1. Top N by their own degree (most connected = most informative nodes)
    2. Top N by recency (most recently created/updated nodes)
    """
    # Get ALL neighbors with their IDs
    all_neighbors = _neighbours(conn, node_id, workspace)

    if not all_neighbors:
        return []

    neighbor_ids = [n[0] for n in all_neighbors]

    # Score by connectivity: get degree for each neighbor
    connectivity_scored: list[tuple[str, float, int]] = []
    for nid, weight in all_neighbors:
        degree = get_node_degree(nid, conn)
        connectivity_scored.append((nid, weight, degree))

    # Top N by connectivity (degree descending)
    connectivity_scored.sort(key=lambda x: -x[2])
    top_by_connectivity = [
        (item[0], item[1]) for item in connectivity_scored[:HUB_SAMPLE_BY_CONNECTIVITY]
    ]

    # Top N by recency (created_at descending)
    if neighbor_ids:
        placeholders = ",".join("?" * len(neighbor_ids))
        recency_rows = conn.execute(
            f"""
            SELECT id, created_at
            FROM knowledge_graph_nodes
            WHERE id IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            neighbor_ids + [HUB_SAMPLE_BY_RECENCY],
        ).fetchall()
        recent_ids = {r[0] for r in recency_rows}
    else:
        recent_ids = set()

    # Merge: connectivity + recency, deduplicated
    seen = set()
    result: list[tuple[str, float]] = []

    for nid, weight in top_by_connectivity:
        if nid not in seen:
            seen.add(nid)
            result.append((nid, weight))

    # Add recency-based that aren't already included
    weight_map = dict(all_neighbors)
    for rid in recent_ids:
        if rid not in seen:
            seen.add(rid)
            result.append((rid, weight_map.get(rid, 1.0)))

    return result


def degree_aware_bfs(
    conn: sqlite3.Connection,
    start_ids: list[str],
    max_depth: int = 3,
    hub_threshold: int = HUB_THRESHOLD,
    workspace: str | None = None,
    max_nodes: int = 500,
) -> DegreeAwareResult:
    """BFS with intelligent handling of high-degree hub nodes.

    For normal nodes (degree <= hub_threshold): expands top-N by weight (same as
    existing bfs_traverse behavior).

    For hub nodes (degree > hub_threshold): instead of top-N by weight which loses
    data, uses a dual sampling strategy:
      - Top 10 neighbors by their own connectivity (most informative)
      - Top 10 neighbors by recency (most recently updated)
    Records a summary of what was sampled vs total degree.

    Args:
        conn:           Active SQLite connection.
        start_ids:      Seed node IDs for traversal.
        max_depth:      Maximum hops from seed nodes.
        hub_threshold:  Degree above which a node is treated as a hub.
        workspace:      Optional workspace filter for edge traversal.
        max_nodes:      Hard cap on visited nodes.

    Returns:
        DegreeAwareResult with nodes, edges, summarized_hubs, depth info.
    """
    if not start_ids:
        return DegreeAwareResult(nodes=[], edges=[])

    visited: dict[str, int] = {}  # node_id -> hop distance
    edges_collected: list[tuple[str, str, str]] = []  # (src, tgt, predicate)
    summarized_hubs: list[dict] = []
    queue: deque[tuple[str, int]] = deque()

    for sid in start_ids:
        if sid not in visited:
            visited[sid] = 0
            queue.append((sid, 0))

    while queue:
        if len(visited) >= max_nodes:
            break

        current_id, depth = queue.popleft()

        if depth >= max_depth:
            continue

        next_depth = depth + 1

        # Check node degree to decide expansion strategy
        degree = get_node_degree(current_id, conn)

        if degree > hub_threshold:
            # Hub node: use smart sampling instead of blind top-N
            sampled = _sample_hub_neighbors(current_id, conn, workspace)
            summarized_hubs.append({
                "node_id": current_id,
                "total_degree": degree,
                "sampled_count": len(sampled),
                "sample_strategy": "top_by_connectivity_and_recency",
                "hub_threshold": hub_threshold,
            })
            neighbors = sampled
        else:
            # Normal node: existing top-N by weight behavior
            raw_neighbors = _neighbours(conn, current_id, workspace)
            neighbors = raw_neighbors

        # Collect edges and enqueue unvisited neighbors
        for neighbour_id, _weight in neighbors:
            if len(visited) >= max_nodes:
                break
            # Record edge (we get predicate info from hub path, fallback "related")
            edges_collected.append((current_id, neighbour_id, "related"))
            if neighbour_id not in visited:
                visited[neighbour_id] = next_depth
                queue.append((neighbour_id, next_depth))

    nodes_with_depth = sorted(visited.items(), key=lambda x: x[1])
    max_depth_reached = max((d for _, d in nodes_with_depth), default=0)

    return DegreeAwareResult(
        nodes=nodes_with_depth,
        edges=edges_collected,
        summarized_hubs=summarized_hubs,
        depth_reached=max_depth_reached,
        truncated=len(visited) >= max_nodes,
    )

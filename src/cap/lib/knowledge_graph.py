"""Knowledge graph storage and query module for the Claude Agent Platform (CAP).

Provides a typed, metadata-rich graph API backed by the shared SQLite knowledge.db.
Nodes represent repos, services, modules, APIs, and infrastructure resources.
Edges represent relationships such as depends-on, deploys-to, provisions,
communicates-with, and owned-by.

All rich fields (summary, domain_tags, complexity, health, etc.) are stored inside
the ``metadata`` JSON column of ``knowledge_graph_nodes``, via the ``NodeMetadata``
dataclass.

Design decisions:
  - Public methods accept plain names and NodeType/EdgeType constants rather than
    full dataclass objects, keeping call sites concise.
  - ``upsert_node`` uses (entity_name, workspace) uniqueness scoped to the
    KnowledgeGraph's default workspace so callers can build graphs without
    pre-computing IDs.
  - Metadata upserts merge via ``NodeMetadata.merge_from`` so existing data is
    never silently overwritten.
  - ``_directed_bfs``, ``_outbound_targets``, ``_inbound_sources`` are module-level
    helpers exported for direct use in tests and callers that hold a raw connection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("cap.knowledge_graph")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".claude-platform" / "data" / "knowledge.db"


# ---------------------------------------------------------------------------
# NodeType and EdgeType enumerations
# ---------------------------------------------------------------------------

class NodeType:
    """Valid entity_type values for knowledge graph nodes."""

    REPO = "repo"
    SERVICE = "service"
    TERRAFORM_MODULE = "terraform_module"
    HELM_CHART = "helm_chart"
    API = "api"
    AWS_RESOURCE = "aws_resource"
    DOMAIN = "domain"
    TEAM = "team"
    CLUSTER = "cluster"
    NAMESPACE = "namespace"
    CI_PIPELINE = "ci_pipeline"
    ARGOCD_APP = "argocd_app"
    TECHNOLOGY = "technology"

    _ALL: frozenset[str] = frozenset()  # populated below


NodeType._ALL = frozenset(
    v for k, v in vars(NodeType).items() if not k.startswith("_") and isinstance(v, str)
)


class EdgeType:
    """Valid predicate values for knowledge graph edges."""

    DEPENDS_ON = "depends_on"
    DEPLOYS_TO = "deploys_to"
    PROVISIONS = "provisions"
    COMMUNICATES_WITH = "communicates_with"
    OWNED_BY = "owned_by"
    BELONGS_TO_DOMAIN = "belongs_to_domain"
    USES_TECHNOLOGY = "uses_technology"
    PROVIDES_API = "provides_api"
    CONSUMES_API = "consumes_api"
    DEPLOYED_BY = "deployed_by"
    CONTAINS = "contains"
    PROVIDES_CHART = "provides_chart"
    USES_MODULE = "uses_module"

    _ALL: frozenset[str] = frozenset()  # populated below


EdgeType._ALL = frozenset(
    v for k, v in vars(EdgeType).items() if not k.startswith("_") and isinstance(v, str)
)


# ---------------------------------------------------------------------------
# NodeMetadata dataclass
# ---------------------------------------------------------------------------

@dataclass
class NodeMetadata:
    """Rich metadata for a knowledge graph node.

    Stored as JSON in the ``metadata`` column of ``knowledge_graph_nodes``.
    All fields have safe defaults so partial construction is always valid.
    """

    summary: str = ""
    domain_tags: list[str] = field(default_factory=list)
    architectural_pattern: str = ""
    complexity: int = 0
    health: str = "unknown"
    last_analyzed_at: str = ""
    analysis_model: str = ""
    entry_id: int | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "NodeMetadata":
        """Deserialise from a JSON string; returns defaults on any error."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (json.JSONDecodeError, TypeError):
            return cls()

    def merge_from(self, other: "NodeMetadata") -> "NodeMetadata":
        """Return a new NodeMetadata preferring *other*'s non-empty values.

        Rules:
          - Scalar strings: prefer other if non-empty.
          - ``complexity``: prefer other if non-zero.
          - ``health``: prefer other unless other is "unknown".
          - ``domain_tags``: prefer other if non-empty.
          - ``extra``: union; other's values overwrite base on conflict.
          - ``entry_id``: prefer other if not None.
        """
        return NodeMetadata(
            summary=other.summary or self.summary,
            domain_tags=other.domain_tags if other.domain_tags else self.domain_tags,
            architectural_pattern=other.architectural_pattern or self.architectural_pattern,
            complexity=other.complexity if other.complexity else self.complexity,
            health=other.health if other.health != "unknown" else self.health,
            last_analyzed_at=other.last_analyzed_at or self.last_analyzed_at,
            analysis_model=other.analysis_model or self.analysis_model,
            entry_id=other.entry_id if other.entry_id is not None else self.entry_id,
            extra={**self.extra, **other.extra},
        )


# ---------------------------------------------------------------------------
# Backward-compat dataclasses (kept for callers that import GraphNode/GraphEdge)
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """A node in the CAP knowledge graph (legacy dataclass, kept for compatibility)."""

    id: str
    entity_name: str
    entity_type: str
    workspace: str
    summary: str = ""
    domain: str = ""
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def _to_metadata_json(self) -> str:
        payload: dict[str, Any] = {
            "summary": self.summary,
            "domain": self.domain,
            "tags": self.tags,
            "dependencies": self.dependencies,
            **self.metadata,
        }
        return json.dumps(payload)


@dataclass
class GraphEdge:
    """A directed edge between two nodes (legacy dataclass, kept for compatibility)."""

    id: str
    source_id: str
    target_id: str
    predicate: str
    workspace: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Module-level helpers (exported for tests and external callers)
# ---------------------------------------------------------------------------

def _node_id(entity_name: str, workspace: str) -> str:
    """Deterministic UUID5 hex ID scoped to entity name + workspace."""
    slug = f"{workspace}::{entity_name}".lower().replace(" ", "_")
    return uuid.uuid5(uuid.NAMESPACE_URL, slug).hex


def _directed_bfs(
    conn: sqlite3.Connection,
    start_id: str,
    predicate: str,
    workspace: str,
    max_depth: int,
) -> set[str]:
    """Iterative BFS following outbound edges with a given predicate.

    The start node IS included in the returned visited set.

    Args:
        conn:      SQLite connection.
        start_id:  Node ID to start from.
        predicate: Edge predicate to follow.
        workspace: Workspace scope for edge lookup.
        max_depth: Maximum number of hops to follow.

    Returns:
        Set of node IDs reachable from *start_id* within *max_depth* hops,
        including *start_id* itself.
    """
    visited: set[str] = {start_id}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])

    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        rows = conn.execute(
            "SELECT target_id FROM knowledge_graph_edges "
            "WHERE source_id = ? AND predicate = ? AND workspace = ?",
            (current_id, predicate, workspace),
        ).fetchall()
        for (target_id,) in rows:
            if target_id not in visited:
                visited.add(target_id)
                queue.append((target_id, depth + 1))

    return visited


def _outbound_targets(
    conn: sqlite3.Connection,
    node_id: str,
    predicate: str,
    workspace: str,
) -> set[str]:
    """Return target node IDs for outbound edges matching *predicate*."""
    rows = conn.execute(
        "SELECT target_id FROM knowledge_graph_edges "
        "WHERE source_id = ? AND predicate = ? AND workspace = ?",
        (node_id, predicate, workspace),
    ).fetchall()
    return {r[0] for r in rows}


def _inbound_sources(
    conn: sqlite3.Connection,
    node_id: str,
    predicate: str,
    workspace: str,
) -> set[str]:
    """Return source node IDs for inbound edges matching *predicate*."""
    rows = conn.execute(
        "SELECT source_id FROM knowledge_graph_edges "
        "WHERE target_id = ? AND predicate = ? AND workspace = ?",
        (node_id, predicate, workspace),
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Internal timestamp helper
# ---------------------------------------------------------------------------

def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# KnowledgeGraph
# ---------------------------------------------------------------------------

class KnowledgeGraph:
    """High-level graph API for the CAP knowledge graph.

    Manages its own SQLite connection or accepts an external one (for testing).

    Args:
        db_path_or_conn: Path to the SQLite database file, or an existing
                         ``sqlite3.Connection`` (used in tests).  Defaults to
                         ``~/.claude-platform/data/knowledge.db``.
        workspace:       Default workspace scope applied to all write and query
                         operations when no explicit workspace is passed.
    """

    def __init__(self, db_path_or_conn=None, workspace: str | None = None) -> None:
        if isinstance(db_path_or_conn, sqlite3.Connection):
            self._conn = db_path_or_conn
            self._db_path = None
        else:
            db_path: Path = Path(db_path_or_conn) if db_path_or_conn else _DEFAULT_DB_PATH
            self._db_path = db_path
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=True,
            )
        self._default_workspace: str | None = workspace
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_tables()

    # =========================================================================
    # Schema management
    # =========================================================================

    def _ensure_tables(self) -> None:
        """Create graph tables if they do not already exist (idempotent)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
                id          TEXT PRIMARY KEY,
                entity_name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                workspace   TEXT NOT NULL,
                metadata    TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kgn_entity
                ON knowledge_graph_nodes(entity_name, entity_type, workspace);

            CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
                target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
                predicate TEXT NOT NULL,
                weight    REAL NOT NULL DEFAULT 1.0,
                metadata  TEXT,
                workspace TEXT NOT NULL,
                UNIQUE(source_id, target_id, predicate)
            );
            CREATE INDEX IF NOT EXISTS idx_kge_source
                ON knowledge_graph_edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_kge_target
                ON knowledge_graph_edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_kge_predicate
                ON knowledge_graph_edges(predicate);
            CREATE INDEX IF NOT EXISTS idx_kge_workspace
                ON knowledge_graph_edges(workspace);
        """)
        self._conn.commit()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _ws(self) -> str:
        """Return the effective workspace, raising if none is configured."""
        if self._default_workspace is None:
            raise RuntimeError("No workspace configured on this KnowledgeGraph instance")
        return self._default_workspace

    def _make_node_id(self, entity_name: str) -> str:
        """Return the deterministic node ID for *entity_name* in the default workspace."""
        return _node_id(entity_name, self._ws())

    def _raw_upsert_node(
        self,
        entity_name: str,
        entity_type: str,
        metadata: NodeMetadata | None,
    ) -> str:
        """Insert or merge-update a single node; returns its stable ID.

        On conflict the existing metadata is loaded, merged with *metadata* via
        ``NodeMetadata.merge_from``, and written back.
        """
        if entity_type not in NodeType._ALL:
            raise ValueError(f"Unknown node_type: {entity_type!r}")

        ws = self._ws()
        node_id = _node_id(entity_name, ws)
        now = _now()

        # Load existing metadata for merge
        existing_row = self._conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()

        if existing_row is not None:
            existing_meta = NodeMetadata.from_json(existing_row[0])
            merged = existing_meta.merge_from(metadata) if metadata else existing_meta
        else:
            merged = metadata or NodeMetadata()

        self._conn.execute(
            """
            INSERT INTO knowledge_graph_nodes
                (id, entity_name, entity_type, workspace, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                entity_type = excluded.entity_type,
                metadata    = excluded.metadata
            """,
            (node_id, entity_name, entity_type, ws, merged.to_json(), now),
        )
        self._conn.commit()
        return node_id

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a ``knowledge_graph_nodes`` row to a plain dict."""
        return {
            "id": row["id"],
            "entity_name": row["entity_name"],
            "entity_type": row["entity_type"],
            "workspace": row["workspace"],
            "metadata": row["metadata"],
            "created_at": row["created_at"],
        }

    # =========================================================================
    # Write API — nodes
    # =========================================================================

    def upsert_node(
        self,
        entity_name: str,
        node_type: str,
        metadata: NodeMetadata | None = None,
    ) -> str:
        """Insert or update a node, returning its stable node ID.

        If the node already exists its metadata is *merged* with *metadata* via
        ``NodeMetadata.merge_from`` so previously stored values are not lost.

        Args:
            entity_name: Human-readable name (e.g. ``"payment-service"``).
            node_type:   One of the ``NodeType`` constants.
            metadata:    Optional rich metadata.  When omitted on an existing
                         node the existing metadata is preserved unchanged.

        Returns:
            The stable node ID (UUID5 hex string).

        Raises:
            ValueError: If *node_type* is not a recognised ``NodeType`` value.
        """
        return self._raw_upsert_node(entity_name, node_type, metadata)

    def upsert_edge(
        self,
        source_name: str,
        source_type: str,
        target_name: str,
        target_type: str,
        edge_type: str,
        metadata: dict | None = None,
    ) -> str:
        """Insert or update a directed edge, auto-creating endpoint nodes.

        Both endpoint nodes are upserted (without metadata) before the edge is
        written so foreign-key constraints are satisfied.

        Args:
            source_name: entity_name of the source node.
            source_type: NodeType of the source node.
            target_name: entity_name of the target node.
            target_type: NodeType of the target node.
            edge_type:   One of the ``EdgeType`` constants.
            metadata:    Optional dict to store on the edge.

        Returns:
            String representation of the edge's integer primary key.

        Raises:
            ValueError: If any type argument is not a recognised constant.
        """
        if source_type not in NodeType._ALL:
            raise ValueError(f"Invalid source_type: {source_type!r}")
        if target_type not in NodeType._ALL:
            raise ValueError(f"Invalid target_type: {target_type!r}")
        if edge_type not in EdgeType._ALL:
            raise ValueError(f"Invalid edge_type: {edge_type!r}")

        source_id = self._raw_upsert_node(source_name, source_type, None)
        target_id = self._raw_upsert_node(target_name, target_type, None)

        ws = self._ws()
        meta_json = json.dumps(metadata) if metadata else None
        cur = self._conn.execute(
            """
            INSERT INTO knowledge_graph_edges
                (source_id, target_id, predicate, metadata, workspace)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, target_id, predicate) DO UPDATE SET
                metadata = excluded.metadata
            """,
            (source_id, target_id, edge_type, meta_json, ws),
        )
        self._conn.commit()
        return str(cur.lastrowid)

    def update_node_metadata(self, entity_name: str, metadata: NodeMetadata) -> None:
        """Merge *metadata* into an existing node's stored metadata.

        Silently no-ops if the node does not exist.

        Args:
            entity_name: Name of the node to update.
            metadata:    Metadata to merge in via ``NodeMetadata.merge_from``.
        """
        node_id = self._make_node_id(entity_name)
        row = self._conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return
        existing = NodeMetadata.from_json(row[0])
        merged = existing.merge_from(metadata)
        self._conn.execute(
            "UPDATE knowledge_graph_nodes SET metadata = ? WHERE id = ?",
            (merged.to_json(), node_id),
        )
        self._conn.commit()

    def remove_stale_edges(
        self,
        source_name: str,
        edge_type: str,
        current_targets: list[str],
    ) -> int:
        """Delete outbound edges from *source_name* not in *current_targets*.

        Only edges with the specified *edge_type* predicate are considered.
        Edges to targets present in *current_targets* are kept; all others are
        removed.

        Args:
            source_name:     entity_name of the source node.
            edge_type:       Predicate to filter edges by.
            current_targets: Names of target nodes that should be kept.

        Returns:
            Number of edges deleted.
        """
        ws = self._ws()
        source_id = _node_id(source_name, ws)
        keep_ids = {_node_id(t, ws) for t in current_targets}

        existing_rows = self._conn.execute(
            "SELECT id, target_id FROM knowledge_graph_edges "
            "WHERE source_id = ? AND predicate = ? AND workspace = ?",
            (source_id, edge_type, ws),
        ).fetchall()

        stale_ids = [r["id"] for r in existing_rows if r["target_id"] not in keep_ids]
        if not stale_ids:
            return 0

        ph = ",".join("?" * len(stale_ids))
        self._conn.execute(
            f"DELETE FROM knowledge_graph_edges WHERE id IN ({ph})",
            stale_ids,
        )
        self._conn.commit()
        return len(stale_ids)

    # =========================================================================
    # Write API — bulk
    # =========================================================================

    def bulk_upsert_nodes(
        self,
        nodes: list[tuple[str, str, NodeMetadata | None]],
    ) -> int:
        """Insert or update multiple nodes in a single transaction.

        Args:
            nodes: List of ``(entity_name, node_type, metadata_or_None)`` tuples.

        Returns:
            Number of nodes processed.

        Raises:
            ValueError: If any ``node_type`` tuple element is invalid.
        """
        if not nodes:
            return 0
        # Validate all types upfront before mutating anything
        for name, node_type, _meta in nodes:
            if node_type not in NodeType._ALL:
                raise ValueError(f"Invalid node_type: {node_type!r}")

        for name, node_type, meta in nodes:
            self._raw_upsert_node(name, node_type, meta)

        return len(nodes)

    def bulk_upsert_edges(
        self,
        edges: list[tuple[str, str, str, str, str, dict | None]],
    ) -> int:
        """Insert or update multiple edges in a single transaction.

        Args:
            edges: List of
                ``(source_name, source_type, target_name, target_type,
                   edge_type, metadata_or_None)`` tuples.

        Returns:
            Number of edges processed.

        Raises:
            ValueError: If any type argument is invalid.
        """
        if not edges:
            return 0
        # Validate all types upfront
        for src, src_t, tgt, tgt_t, et, _meta in edges:
            if src_t not in NodeType._ALL:
                raise ValueError(f"Invalid source_type: {src_t!r}")
            if tgt_t not in NodeType._ALL:
                raise ValueError(f"Invalid target_type: {tgt_t!r}")
            if et not in EdgeType._ALL:
                raise ValueError(f"Invalid edge_type: {et!r}")

        for src, src_t, tgt, tgt_t, et, meta in edges:
            self.upsert_edge(src, src_t, tgt, tgt_t, et, meta)

        return len(edges)

    # =========================================================================
    # Query API
    # =========================================================================

    def find_by_type(self, node_type: str) -> list[dict]:
        """Return all nodes of *node_type* in the default workspace.

        Args:
            node_type: One of the ``NodeType`` constants.

        Returns:
            List of node dicts ordered by entity_name.
        """
        ws = self._ws()
        rows = self._conn.execute(
            "SELECT * FROM knowledge_graph_nodes "
            "WHERE entity_type = ? AND workspace = ? "
            "ORDER BY entity_name",
            (node_type, ws),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_domain(self, domain: str) -> list[dict]:
        """Return all nodes tagged with *domain* in the default workspace.

        Matches are found via two strategies combined (deduped):
          1. Nodes whose ``NodeMetadata.domain_tags`` contains *domain*.
          2. Nodes that have a ``BELONGS_TO_DOMAIN`` edge pointing to a node
             whose entity_name equals *domain*.

        Args:
            domain: Domain name or tag to search for.

        Returns:
            List of unique node dicts (no duplicates from the two strategies).
        """
        ws = self._ws()
        seen_ids: set[str] = set()
        results: list[dict] = []

        # Strategy 1: domain_tags LIKE scan + Python-side validation
        pattern = f'%"{domain}"%'
        rows = self._conn.execute(
            "SELECT * FROM knowledge_graph_nodes "
            "WHERE workspace = ? AND metadata LIKE ? "
            "ORDER BY entity_name",
            (ws, pattern),
        ).fetchall()
        for row in rows:
            meta = NodeMetadata.from_json(row["metadata"])
            if domain in meta.domain_tags:
                d = self._row_to_dict(row)
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    results.append(d)

        # Strategy 2: BELONGS_TO_DOMAIN edge pointing to a node named *domain*
        domain_node_id = _node_id(domain, ws)
        edge_rows = self._conn.execute(
            "SELECT source_id FROM knowledge_graph_edges "
            "WHERE target_id = ? AND predicate = ? AND workspace = ?",
            (domain_node_id, EdgeType.BELONGS_TO_DOMAIN, ws),
        ).fetchall()
        source_ids = [r[0] for r in edge_rows if r[0] not in seen_ids]
        if source_ids:
            ph = ",".join("?" * len(source_ids))
            node_rows = self._conn.execute(
                f"SELECT * FROM knowledge_graph_nodes WHERE id IN ({ph})",
                source_ids,
            ).fetchall()
            for row in node_rows:
                d = self._row_to_dict(row)
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    results.append(d)

        return results

    def get_node(self, entity_name: str, node_type: str) -> dict | None:
        """Look up a single node by name and type in the default workspace.

        Args:
            entity_name: Exact entity name to match.
            node_type:   NodeType to match.

        Returns:
            Node dict or ``None`` if not found.
        """
        ws = self._ws()
        row = self._conn.execute(
            "SELECT * FROM knowledge_graph_nodes "
            "WHERE entity_name = ? AND entity_type = ? AND workspace = ?",
            (entity_name, node_type, ws),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_dependencies(self, entity_name: str, depth: int = 1) -> list[dict]:
        """Return nodes that *entity_name* depends on (DEPENDS_ON outbound BFS).

        The source node itself is excluded from the result.

        Args:
            entity_name: Name of the node whose dependencies to resolve.
            depth:       Maximum BFS depth (default 1 for direct dependencies).

        Returns:
            List of dependency node dicts (source excluded).
        """
        ws = self._ws()
        source_id = _node_id(entity_name, ws)
        visited = _directed_bfs(self._conn, source_id, EdgeType.DEPENDS_ON, ws, max_depth=depth)
        visited.discard(source_id)
        if not visited:
            return []
        ph = ",".join("?" * len(visited))
        rows = self._conn.execute(
            f"SELECT * FROM knowledge_graph_nodes WHERE id IN ({ph})",
            list(visited),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_dependents(self, entity_name: str) -> list[dict]:
        """Return nodes that directly depend on *entity_name* (DEPENDS_ON inbound).

        Args:
            entity_name: Name of the node whose dependents to find.

        Returns:
            List of dependent node dicts.
        """
        ws = self._ws()
        target_id = _node_id(entity_name, ws)
        sources = _inbound_sources(self._conn, target_id, EdgeType.DEPENDS_ON, ws)
        if not sources:
            return []
        ph = ",".join("?" * len(sources))
        rows = self._conn.execute(
            f"SELECT * FROM knowledge_graph_nodes WHERE id IN ({ph})",
            list(sources),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_deployment_chain(self, entity_name: str) -> dict:
        """Trace the deployment chain from a source node.

        Follows ``DEPLOYED_BY`` edges from *entity_name* to ArgoCD apps, then
        ``DEPLOYS_TO`` edges from those apps to clusters.

        Args:
            entity_name: Name of the source node (typically a repo).

        Returns:
            Dict with keys:
              - ``source``:      Source node dict or ``None`` if not found.
              - ``argocd_apps``: List of ArgoCD app node dicts.
              - ``clusters``:    List of cluster node dicts.
        """
        ws = self._ws()
        source_id = _node_id(entity_name, ws)
        source_row = self._conn.execute(
            "SELECT * FROM knowledge_graph_nodes WHERE id = ?",
            (source_id,),
        ).fetchone()

        if source_row is None:
            return {"source": None, "argocd_apps": [], "clusters": []}

        # repo --DEPLOYED_BY--> argocd_app
        app_ids = _outbound_targets(self._conn, source_id, EdgeType.DEPLOYED_BY, ws)
        app_rows: list[dict] = []
        cluster_ids: set[str] = set()

        if app_ids:
            ph = ",".join("?" * len(app_ids))
            rows = self._conn.execute(
                f"SELECT * FROM knowledge_graph_nodes WHERE id IN ({ph})",
                list(app_ids),
            ).fetchall()
            app_rows = [self._row_to_dict(r) for r in rows]

            for app_id in app_ids:
                cluster_ids |= _outbound_targets(self._conn, app_id, EdgeType.DEPLOYS_TO, ws)

        cluster_rows: list[dict] = []
        if cluster_ids:
            ph = ",".join("?" * len(cluster_ids))
            rows = self._conn.execute(
                f"SELECT * FROM knowledge_graph_nodes WHERE id IN ({ph})",
                list(cluster_ids),
            ).fetchall()
            cluster_rows = [self._row_to_dict(r) for r in rows]

        return {
            "source": self._row_to_dict(source_row),
            "argocd_apps": app_rows,
            "clusters": cluster_rows,
        }

    def get_service_map(self) -> list[dict]:
        """Return all COMMUNICATES_WITH edges in the default workspace.

        Returns:
            List of dicts with keys ``source_name``, ``target_name``, and
            ``predicate`` for each communication edge.
        """
        ws = self._ws()
        rows = self._conn.execute(
            """
            SELECT src.entity_name AS source_name,
                   tgt.entity_name AS target_name,
                   e.predicate
            FROM   knowledge_graph_edges e
            JOIN   knowledge_graph_nodes src ON src.id = e.source_id
            JOIN   knowledge_graph_nodes tgt ON tgt.id = e.target_id
            WHERE  e.predicate = ? AND e.workspace = ?
            ORDER  BY src.entity_name, tgt.entity_name
            """,
            (EdgeType.COMMUNICATES_WITH, ws),
        ).fetchall()
        return [{"source_name": r[0], "target_name": r[1], "predicate": r[2]} for r in rows]

    def get_domain_overview(self) -> dict[str, list[dict]]:
        """Group all nodes in the default workspace by their domain tags.

        Nodes with no domain tags appear under ``"__unclassified__"``.
        Nodes with multiple tags appear in each corresponding group.

        Returns:
            Dict mapping domain tag → list of node dicts.
        """
        ws = self._ws()
        rows = self._conn.execute(
            "SELECT * FROM knowledge_graph_nodes WHERE workspace = ?",
            (ws,),
        ).fetchall()
        overview: dict[str, list[dict]] = {}
        for row in rows:
            meta = NodeMetadata.from_json(row["metadata"])
            d = self._row_to_dict(row)
            if meta.domain_tags:
                for tag in meta.domain_tags:
                    overview.setdefault(tag, []).append(d)
            else:
                overview.setdefault("__unclassified__", []).append(d)
        return overview

    def search(
        self,
        query: str,
        node_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search nodes by entity_name or metadata summary (LIKE-based).

        Args:
            query:     Substring to search for (case-insensitive via LIKE).
            node_type: Optional NodeType filter.
            limit:     Maximum results to return (default 50).

        Returns:
            List of matching node dicts.
        """
        if not query or not query.strip():
            return []
        ws = self._ws()
        pattern = f"%{query}%"
        if node_type is not None:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_graph_nodes "
                "WHERE workspace = ? AND entity_type = ? "
                "AND (entity_name LIKE ? OR metadata LIKE ?) "
                "ORDER BY entity_name LIMIT ?",
                (ws, node_type, pattern, pattern, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_graph_nodes "
                "WHERE workspace = ? "
                "AND (entity_name LIKE ? OR metadata LIKE ?) "
                "ORDER BY entity_name LIMIT ?",
                (ws, pattern, pattern, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return aggregate statistics about the default workspace.

        Returns:
            Dict with keys:
              - ``total_nodes``:       Total node count.
              - ``nodes_by_type``:     Dict mapping NodeType → count.
              - ``total_edges``:       Total edge count.
              - ``edges_by_predicate``: Dict mapping EdgeType → count.
              - ``total_domains``:     Count of distinct domain tags in use.
        """
        ws = self._ws()
        total_nodes: int = self._conn.execute(
            "SELECT COUNT(*) FROM knowledge_graph_nodes WHERE workspace = ?",
            (ws,),
        ).fetchone()[0]

        type_rows = self._conn.execute(
            "SELECT entity_type, COUNT(*) FROM knowledge_graph_nodes "
            "WHERE workspace = ? GROUP BY entity_type",
            (ws,),
        ).fetchall()
        nodes_by_type = {r[0]: r[1] for r in type_rows}

        total_edges: int = self._conn.execute(
            "SELECT COUNT(*) FROM knowledge_graph_edges WHERE workspace = ?",
            (ws,),
        ).fetchone()[0]

        pred_rows = self._conn.execute(
            "SELECT predicate, COUNT(*) FROM knowledge_graph_edges "
            "WHERE workspace = ? GROUP BY predicate",
            (ws,),
        ).fetchall()
        edges_by_predicate = {r[0]: r[1] for r in pred_rows}

        # Count distinct domain tags across all node metadata
        node_rows = self._conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE workspace = ?",
            (ws,),
        ).fetchall()
        all_tags: set[str] = set()
        for row in node_rows:
            meta = NodeMetadata.from_json(row[0])
            all_tags.update(meta.domain_tags)
        total_domains = len(all_tags)

        return {
            "total_nodes": total_nodes,
            "nodes_by_type": nodes_by_type,
            "total_edges": total_edges,
            "edges_by_predicate": edges_by_predicate,
            "total_domains": total_domains,
        }

    # =========================================================================
    # Change tracking
    # =========================================================================

    def mark_analyzed(self, entity_name: str, model: str) -> None:
        """Record that *entity_name* was analysed by *model* at the current time.

        Updates ``NodeMetadata.analysis_model`` and ``last_analyzed_at``
        on the node.  Silently no-ops if the node does not exist.

        Args:
            entity_name: Name of the node to mark.
            model:       Model identifier string (e.g. ``"claude-3-5-sonnet"``).
        """
        self.update_node_metadata(
            entity_name,
            NodeMetadata(analysis_model=model, last_analyzed_at=_now()),
        )

    def get_stale_nodes(self, older_than_hours: int = 24) -> list[str]:
        """Return entity names of nodes not analysed within *older_than_hours*.

        A node is considered stale if its ``last_analyzed_at`` metadata field is
        empty, cannot be parsed, or falls before the staleness threshold.

        Args:
            older_than_hours: Threshold in hours (default 24).

        Returns:
            List of entity names (strings) for stale nodes.
        """
        ws = self._ws()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        rows = self._conn.execute(
            "SELECT entity_name, metadata FROM knowledge_graph_nodes WHERE workspace = ?",
            (ws,),
        ).fetchall()

        stale: list[str] = []
        for row in rows:
            meta = NodeMetadata.from_json(row[1])
            if not meta.last_analyzed_at:
                stale.append(row[0])
                continue
            try:
                ts = datetime.fromisoformat(meta.last_analyzed_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    stale.append(row[0])
            except ValueError:
                stale.append(row[0])

        return stale

    def get_change_propagation_targets(self, entity_name: str) -> list[str]:
        """Return names of nodes that may be affected by a change to *entity_name*.

        Includes:
          - Direct dependents (nodes with a DEPENDS_ON edge pointing to this node).
          - Domain siblings (nodes sharing at least one domain tag with this node,
            excluding *entity_name* itself).

        Args:
            entity_name: Name of the changed node.

        Returns:
            Sorted, deduplicated list of entity names (self excluded).
        """
        ws = self._ws()
        node_id = _node_id(entity_name, ws)
        targets: set[str] = set()

        # Direct dependents
        source_ids = _inbound_sources(self._conn, node_id, EdgeType.DEPENDS_ON, ws)
        if source_ids:
            ph = ",".join("?" * len(source_ids))
            rows = self._conn.execute(
                f"SELECT entity_name FROM knowledge_graph_nodes WHERE id IN ({ph})",
                list(source_ids),
            ).fetchall()
            targets.update(r[0] for r in rows)

        # Domain siblings
        meta_row = self._conn.execute(
            "SELECT metadata FROM knowledge_graph_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if meta_row:
            meta = NodeMetadata.from_json(meta_row[0])
            for tag in meta.domain_tags:
                pattern = f'%"{tag}"%'
                sibling_rows = self._conn.execute(
                    "SELECT entity_name, metadata FROM knowledge_graph_nodes "
                    "WHERE workspace = ? AND metadata LIKE ? AND entity_name != ?",
                    (ws, pattern, entity_name),
                ).fetchall()
                for sibling_row in sibling_rows:
                    sibling_meta = NodeMetadata.from_json(sibling_row[1])
                    if tag in sibling_meta.domain_tags:
                        targets.add(sibling_row[0])

        targets.discard(entity_name)
        return sorted(targets)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def close(self) -> None:
        """Close the underlying SQLite connection gracefully."""
        try:
            self._conn.close()
        except sqlite3.Error:
            logger.warning("close: error closing SQLite connection", exc_info=True)

    def __enter__(self) -> "KnowledgeGraph":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

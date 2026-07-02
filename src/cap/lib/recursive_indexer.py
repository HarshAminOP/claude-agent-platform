"""Recursive directory indexer for CAP knowledge base.

Walks an entire directory tree, respects exclusions, filters by extension
and max file size, batches inserts into knowledge.db, and builds the
knowledge graph (directory->contains->file, repo->contains->directory).

This is ADDITIONAL to the existing quick-index and knowledge_search
functionality -- it does not replace them.
"""

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

logger = logging.getLogger("cap.recursive_indexer")

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_EXTENSIONS = frozenset([
    ".py", ".ts", ".js", ".tf", ".yaml", ".yml", ".json", ".md",
    ".toml", ".sh", ".go", ".java", ".rs", ".hcl",
])

# Filenames without extensions that should also be indexed
DEFAULT_EXACT_FILENAMES = frozenset([
    "Dockerfile", "Makefile",
])

DEFAULT_EXCLUDE_DIRS = frozenset([
    "node_modules", ".git", "__pycache__", "dist", "build",
    ".terraform", ".venv", "venv", "vendor", "target",
])

DEFAULT_MAX_FILE_SIZE_KB = 500
DEFAULT_BATCH_SIZE = 100


# ── Stats ─────────────────────────────────────────────────────────────────────

class IndexStats:
    """Collects statistics about an indexing run."""

    def __init__(self):
        self.files_scanned = 0
        self.files_indexed = 0
        self.files_skipped_size = 0
        self.files_skipped_permission = 0
        self.files_skipped_read_error = 0
        self.files_already_indexed = 0
        self.dirs_scanned = 0
        self.repos_detected = 0
        self.graph_nodes_created = 0
        self.graph_edges_created = 0
        self.duration_s = 0.0

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped_size": self.files_skipped_size,
            "files_skipped_permission": self.files_skipped_permission,
            "files_skipped_read_error": self.files_skipped_read_error,
            "files_already_indexed": self.files_already_indexed,
            "dirs_scanned": self.dirs_scanned,
            "repos_detected": self.repos_detected,
            "graph_nodes_created": self.graph_nodes_created,
            "graph_edges_created": self.graph_edges_created,
            "duration_s": round(self.duration_s, 2),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_index_file(path: Path, extensions: frozenset, exact_filenames: frozenset) -> bool:
    """Check if a file should be indexed based on extension or exact filename."""
    if path.name in exact_filenames:
        return True
    return path.suffix.lower() in extensions


def _content_type_for_file(path: Path) -> str:
    """Determine content_type from file path."""
    suffix = path.suffix.lower()
    name = path.name

    if suffix in (".md", ".rst"):
        return "documentation"
    elif suffix in (".yaml", ".yml", ".json", ".toml", ".hcl"):
        return "config"
    elif suffix == ".tf":
        return "infrastructure"
    elif suffix in (".py", ".ts", ".js", ".go", ".java", ".rs"):
        return "source_code"
    elif suffix == ".sh":
        return "script"
    elif name in ("Dockerfile", "Makefile"):
        return "build_config"
    else:
        return "file"


def _get_git_repo_metadata(repo_path: Path) -> dict | None:
    """Extract git repo metadata (name, remotes) from a .git directory."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=5,
        )
        remotes = {}
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    remotes[parts[0]] = parts[1]

        return {
            "name": repo_path.name,
            "path": str(repo_path),
            "remotes": remotes,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _ensure_graph_node(
    conn: sqlite3.Connection,
    entity_name: str,
    entity_type: str,
    workspace: str,
    metadata: dict | None = None,
) -> str:
    """Create a graph node if it doesn't exist. Returns the node id."""
    existing = conn.execute(
        "SELECT id FROM knowledge_graph_nodes WHERE entity_name = ? AND entity_type = ? AND workspace = ?",
        (entity_name, entity_type, workspace),
    ).fetchone()
    if existing:
        return existing[0]

    node_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace, metadata)
           VALUES (?, ?, ?, ?, ?)""",
        (node_id, entity_name, entity_type, workspace, json.dumps(metadata or {})),
    )
    return node_id


def _ensure_graph_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    predicate: str,
    workspace: str,
) -> bool:
    """Create a graph edge if it doesn't exist. Returns True if created."""
    existing = conn.execute(
        "SELECT id FROM knowledge_graph_edges WHERE source_id = ? AND target_id = ? AND predicate = ?",
        (source_id, target_id, predicate),
    ).fetchone()
    if existing:
        return False

    conn.execute(
        """INSERT INTO knowledge_graph_edges (source_id, target_id, predicate, workspace)
           VALUES (?, ?, ?, ?)""",
        (source_id, target_id, predicate, workspace),
    )
    return True


# ── Main indexing function ────────────────────────────────────────────────────

def index_directory_tree(
    root: str | Path,
    config: dict | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Recursively walk a directory tree and index all matching files into knowledge.db.

    Args:
        root: The root directory to scan.
        config: Optional config dict with keys:
            - data_dir: Path to the data directory containing knowledge.db
            - extensions: Set/list of file extensions to index (with leading dot)
            - exact_filenames: Set/list of exact filenames to index
            - exclude_dirs: Set/list of directory names to skip
            - max_file_size_kb: Maximum file size in KB to index
            - batch_size: Number of entries to batch before committing
            - workspace: Workspace identifier string
        progress_callback: Optional callback(message, files_done, files_total)
            for progress reporting.

    Returns:
        Stats dict with counts of files scanned, indexed, skipped, etc.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"Root path is not a directory: {root}")

    config = config or {}

    # Extract config values with defaults
    from cap.config import get_data_dir
    _default_data_dir = get_data_dir()
    data_dir = Path(config.get("data_dir", _default_data_dir))
    extensions = frozenset(config.get("extensions", DEFAULT_EXTENSIONS))
    exact_filenames = frozenset(config.get("exact_filenames", DEFAULT_EXACT_FILENAMES))
    exclude_dirs = frozenset(config.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS))
    max_file_size_kb = config.get("max_file_size_kb", DEFAULT_MAX_FILE_SIZE_KB)
    batch_size = config.get("batch_size", DEFAULT_BATCH_SIZE)
    workspace = config.get("workspace", str(root))

    max_file_size_bytes = max_file_size_kb * 1024

    stats = IndexStats()
    t_start = time.time()

    # Open database
    knowledge_db = data_dir / "knowledge.db"
    if not knowledge_db.exists():
        raise FileNotFoundError(f"knowledge.db not found at {knowledge_db}. Run 'cap init' first.")

    conn = sqlite3.connect(str(knowledge_db), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Collect files to index first (for progress reporting)
    files_to_index: list[Path] = []
    repos_found: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        stats.dirs_scanned += 1

        # Skip excluded directories (modifying dirnames in-place prunes traversal)
        dirnames[:] = [
            d for d in dirnames
            if d not in exclude_dirs and not d.startswith(".")
            # Allow .git detection but don't recurse into it
        ]

        # Detect git repos
        if (current_dir / ".git").is_dir():
            repos_found.append(current_dir)
            stats.repos_detected += 1

        # Re-add .git exclusion for traversal (we detected it but don't walk into it)
        dirnames[:] = [d for d in dirnames if d != ".git"]

        for filename in filenames:
            filepath = current_dir / filename
            stats.files_scanned += 1

            if not _should_index_file(filepath, extensions, exact_filenames):
                continue

            # Check file size
            try:
                file_size = filepath.stat().st_size
            except (OSError, PermissionError):
                stats.files_skipped_permission += 1
                continue

            if file_size > max_file_size_bytes:
                stats.files_skipped_size += 1
                continue

            if file_size == 0:
                continue

            files_to_index.append(filepath)

    total_files = len(files_to_index)

    if progress_callback:
        progress_callback(f"Found {total_files} files to index in {stats.dirs_scanned} directories", 0, total_files)

    # Index files in batches
    batch_entries: list[tuple] = []

    for i, filepath in enumerate(files_to_index):
        try:
            content = filepath.read_text(errors="replace")
        except PermissionError:
            stats.files_skipped_permission += 1
            continue
        except (OSError, UnicodeDecodeError):
            stats.files_skipped_read_error += 1
            continue

        # Truncate very large content
        if len(content) > 100_000:
            content = content[:100_000]

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        source_path = str(filepath)

        # Check if already indexed (by source_path and workspace)
        existing = conn.execute(
            "SELECT id FROM knowledge_entries WHERE source_path = ? AND workspace = ?",
            (source_path, workspace),
        ).fetchone()
        if existing:
            stats.files_already_indexed += 1
            continue

        entry_uuid = str(uuid.uuid4())
        content_type = _content_type_for_file(filepath)
        # Title: relative path from root
        try:
            rel_path = filepath.relative_to(root)
            title = str(rel_path)
        except ValueError:
            title = filepath.name

        metadata = json.dumps({
            "indexed_by": "recursive_indexer",
            "indexed_at": time.time(),
            "file_size": filepath.stat().st_size,
            "root": str(root),
        })

        batch_entries.append((
            entry_uuid, workspace, source_path, "recursive_index",
            content_type, title, content, content_hash, metadata,
        ))

        # Commit batch
        if len(batch_entries) >= batch_size:
            conn.executemany(
                """INSERT INTO knowledge_entries
                   (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                batch_entries,
            )
            conn.commit()
            stats.files_indexed += len(batch_entries)
            batch_entries = []

            if progress_callback:
                progress_callback(
                    f"Indexed {stats.files_indexed}/{total_files} files",
                    stats.files_indexed,
                    total_files,
                )

    # Commit remaining batch
    if batch_entries:
        conn.executemany(
            """INSERT INTO knowledge_entries
               (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch_entries,
        )
        conn.commit()
        stats.files_indexed += len(batch_entries)

    if progress_callback:
        progress_callback(f"Indexed {stats.files_indexed} files. Building knowledge graph...", stats.files_indexed, total_files)

    # ── Build knowledge graph ─────────────────────────────────────────────────

    # Create root node
    root_node_id = _ensure_graph_node(conn, str(root), "directory", workspace, {"is_root": True})
    stats.graph_nodes_created += 1

    # Create repo nodes and edges
    for repo_path in repos_found:
        repo_meta = _get_git_repo_metadata(repo_path)
        repo_node_id = _ensure_graph_node(
            conn, str(repo_path), "repository", workspace, repo_meta
        )
        stats.graph_nodes_created += 1

        # repo is contained in root (or a subdirectory of root)
        if _ensure_graph_edge(conn, root_node_id, repo_node_id, "contains", workspace):
            stats.graph_edges_created += 1

        # Create directory nodes for immediate children of repo
        try:
            for child in repo_path.iterdir():
                if child.is_dir() and child.name not in exclude_dirs and not child.name.startswith("."):
                    dir_node_id = _ensure_graph_node(conn, str(child), "directory", workspace)
                    stats.graph_nodes_created += 1
                    if _ensure_graph_edge(conn, repo_node_id, dir_node_id, "contains", workspace):
                        stats.graph_edges_created += 1
        except PermissionError:
            pass

    conn.commit()
    conn.close()

    stats.duration_s = time.time() - t_start

    if progress_callback:
        progress_callback(
            f"Complete: {stats.files_indexed} files indexed, {stats.repos_detected} repos detected",
            total_files,
            total_files,
        )

    return stats.to_dict()

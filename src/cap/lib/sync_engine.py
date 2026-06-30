"""Filesystem sync engine for the CAP knowledge base.

Walks a workspace directory, extracts content from relevant files,
indexes into knowledge_entries (which auto-populates FTS5 via triggers),
builds knowledge graph edges, and queues embeddings.

Incremental mode: only processes files changed since last sync (by mtime).
Full mode: re-indexes everything regardless of change status.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cap.lib.graph import add_edge

logger = logging.getLogger("cap.sync")

# ── File classification ──────────────────────────────────────────────────────

INDEXABLE_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst",
    ".py", ".ts", ".js", ".tsx", ".jsx",
    ".tf", ".hcl", ".tfvars",
    ".yaml", ".yml",
    ".json",
    ".toml", ".ini", ".cfg",
    ".sh", ".bash", ".zsh",
    ".go", ".rs", ".java", ".rb",
    ".sql",
    ".dockerfile",
    ".html", ".css",
    ".env.example",
})

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", ".vendor",
    ".terraform", ".terragrunt-cache",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".venv", "venv", "env", ".env",
    "dist", "build", "_build", "target",
    ".next", ".nuxt", ".output",
    ".worktrees", ".worktree",
    "coverage", ".coverage",
    ".idea", ".vscode",
})

SKIP_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock",
    "terraform.lock.hcl", ".terraform.lock.hcl",
    "go.sum",
    ".DS_Store", "Thumbs.db",
})

MAX_FILE_SIZE = 512 * 1024  # 512 KB — skip huge files

CONTENT_TYPE_MAP = {
    ".md": "markdown", ".txt": "text", ".rst": "restructuredtext",
    ".py": "python", ".ts": "typescript", ".js": "javascript",
    ".tsx": "typescript", ".jsx": "javascript",
    ".tf": "terraform", ".hcl": "terraform", ".tfvars": "terraform",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml", ".ini": "config", ".cfg": "config",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".sql": "sql", ".dockerfile": "dockerfile",
    ".html": "html", ".css": "css",
}


@dataclass
class SyncStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_unchanged: int = 0
    files_updated: int = 0
    graph_edges_created: int = 0
    embeddings_queued: int = 0
    structures_indexed: int = 0
    repo_summaries_generated: int = 0
    errors: list = field(default_factory=list)


# ── Core sync engine ─────────────────────────────────────────────────────────

def sync_workspace(
    db: sqlite3.Connection,
    workspace: str,
    full: bool = False,
) -> SyncStats:
    """Walk workspace and index all relevant files into knowledge_entries.

    Args:
        db:        SQLite connection to knowledge.db
        workspace: Absolute path to the workspace root
        full:      If True, re-index all files regardless of change status

    Returns:
        SyncStats with counts of what was done.
    """
    stats = SyncStats()
    workspace_path = Path(workspace)

    if not workspace_path.is_dir():
        stats.errors.append(f"Workspace not found: {workspace}")
        return stats

    last_sync_at = _get_last_sync_time(db, workspace) if not full else None

    existing_hashes = _get_existing_hashes(db, workspace)

    for file_path in _walk_workspace(workspace_path):
        stats.files_scanned += 1

        if not full and last_sync_at:
            mtime = datetime.fromtimestamp(
                file_path.stat().st_mtime, tz=timezone.utc
            )
            if mtime < last_sync_at:
                stats.files_unchanged += 1
                continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            stats.errors.append(f"{file_path}: {exc}")
            stats.files_skipped += 1
            continue

        if not content.strip():
            stats.files_skipped += 1
            continue

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]

        if content_hash in existing_hashes:
            stats.files_unchanged += 1
            continue

        rel_path = str(file_path.relative_to(workspace_path))
        ext = file_path.suffix.lower()
        content_type = CONTENT_TYPE_MAP.get(ext, "text")
        title = _derive_title(file_path, content, content_type)

        entry_id = _upsert_entry(
            db, workspace, rel_path, content_type, title, content, content_hash
        )

        if entry_id:
            stats.files_indexed += 1
            existing_hashes.add(content_hash)

            db.execute(
                "INSERT INTO embedding_queue (entry_id) VALUES (?)",
                (entry_id,)
            )
            stats.embeddings_queued += 1

            edges = _extract_graph_edges(file_path, content, content_type, workspace)
            for edge in edges:
                try:
                    add_edge(db, **edge)
                    stats.graph_edges_created += 1
                except Exception:
                    pass
        else:
            stats.files_updated += 1

    _update_sync_state(db, workspace, stats)

    # Index directory structures and generate repo summaries
    _index_directory_structures(db, workspace_path, workspace, stats)

    db.commit()

    logger.info(
        "Sync complete: scanned=%d indexed=%d unchanged=%d edges=%d structures=%d summaries=%d",
        stats.files_scanned, stats.files_indexed,
        stats.files_unchanged, stats.graph_edges_created,
        stats.structures_indexed, stats.repo_summaries_generated,
    )
    return stats


# ── Filesystem walker ────────────────────────────────────────────────────────

def _walk_workspace(root: Path):
    """Yield indexable file paths, skipping noise directories and files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]

        for fname in filenames:
            if fname in SKIP_FILES:
                continue
            if fname.startswith(".") and fname != ".env.example":
                continue

            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()

            if ext not in INDEXABLE_EXTENSIONS:
                if fname.lower() in ("dockerfile", "makefile", "justfile", "rakefile"):
                    pass  # index these even without extension
                else:
                    continue

            try:
                size = fpath.stat().st_size
            except OSError:
                continue

            if size == 0 or size > MAX_FILE_SIZE:
                continue

            yield fpath


# ── Database operations ──────────────────────────────────────────────────────

def _get_last_sync_time(db: sqlite3.Connection, workspace: str) -> Optional[datetime]:
    row = db.execute(
        "SELECT last_sync_at FROM sync_state WHERE workspace = ? AND source_type = 'filesystem'",
        (workspace,)
    ).fetchone()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None
    return None


def _get_existing_hashes(db: sqlite3.Connection, workspace: str) -> set:
    rows = db.execute(
        "SELECT content_hash FROM knowledge_entries WHERE workspace = ?",
        (workspace,)
    ).fetchall()
    return {r[0] for r in rows}


def _upsert_entry(
    db: sqlite3.Connection,
    workspace: str,
    source_path: str,
    content_type: str,
    title: str,
    content: str,
    content_hash: str,
) -> Optional[int]:
    """Insert or update a knowledge entry. Returns entry_id for new entries, None for updates."""
    existing = db.execute(
        "SELECT id FROM knowledge_entries WHERE workspace = ? AND source_path = ?",
        (workspace, source_path)
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        db.execute(
            """UPDATE knowledge_entries
               SET content = ?, content_hash = ?, title = ?, content_type = ?, updated_at = ?
               WHERE id = ?""",
            (content, content_hash, title, content_type, now, existing[0])
        )
        return None
    else:
        entry_uuid = str(_uuid.uuid4())
        db.execute(
            """INSERT INTO knowledge_entries
               (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
               VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?)""",
            (entry_uuid, workspace, source_path, content_type, title, content, content_hash,
             json.dumps({"synced_at": now}))
        )
        row = db.execute(
            "SELECT id FROM knowledge_entries WHERE uuid = ?", (entry_uuid,)
        ).fetchone()
        return row[0] if row else None


def _update_sync_state(db: sqlite3.Connection, workspace: str, stats: SyncStats):
    now = datetime.now(timezone.utc).isoformat()
    status = "complete" if not stats.errors else "complete_with_errors"

    existing = db.execute(
        "SELECT id FROM sync_state WHERE workspace = ? AND source_type = 'filesystem'",
        (workspace,)
    ).fetchone()

    if existing:
        db.execute(
            """UPDATE sync_state
               SET last_sync_at = ?, file_count = ?, status = ?, error = ?
               WHERE id = ?""",
            (now, stats.files_indexed + stats.files_unchanged + stats.files_updated,
             status, json.dumps(stats.errors[:10]) if stats.errors else None,
             existing[0])
        )
    else:
        db.execute(
            """INSERT INTO sync_state (id, workspace, source_type, last_sync_at, file_count, status, error)
               VALUES (?, ?, 'filesystem', ?, ?, ?, ?)""",
            (str(_uuid.uuid4()), workspace, now,
             stats.files_indexed + stats.files_unchanged + stats.files_updated,
             status, json.dumps(stats.errors[:10]) if stats.errors else None)
        )


# ── Directory structure indexing ─────────────────────────────────────────────

# Structural directory names to always inspect
_STRUCTURAL_DIR_NAMES = frozenset({
    "modules", "services", "apps", "charts", "packages", "cmd", "internal",
})

# Extension → human-readable category for labeling
_EXT_LABELS = {
    ".tf": "Terraform modules",
    ".go": "Go packages",
    ".py": "Python packages",
    ".ts": "TypeScript packages",
    ".js": "JavaScript packages",
    ".tsx": "TypeScript packages",
    ".jsx": "JavaScript packages",
    ".yaml": "configuration",
    ".yml": "configuration",
}


def _dominant_ext_label(subdir_path: str) -> str:
    """Return a human-readable label for the dominant file type found in subdir_path."""
    counts: dict[str, int] = {}
    try:
        for fname in os.listdir(subdir_path):
            ext = os.path.splitext(fname)[1].lower()
            if ext in INDEXABLE_EXTENSIONS:
                counts[ext] = counts.get(ext, 0) + 1
    except OSError:
        pass
    if not counts:
        return "files"
    dominant = max(counts, key=lambda e: counts[e])
    return _EXT_LABELS.get(dominant, "files")


def _file_list_for_subdir(subdir_path: str) -> tuple[int, list[str]]:
    """Return (total_count, list_of_names) for indexable files directly in subdir_path."""
    names: list[str] = []
    try:
        for fname in sorted(os.listdir(subdir_path)):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in INDEXABLE_EXTENSIONS or fname.lower() in ("dockerfile", "makefile", "justfile"):
                names.append(fname)
    except OSError:
        pass
    return len(names), names


def _is_structural_dir(dir_path: str, dir_name: str) -> bool:
    """Return True if dir_path qualifies as a structural directory worth indexing."""
    if dir_name in _STRUCTURAL_DIR_NAMES:
        return True

    # Detect any directory at depth 1-2 that contains 3+ subdirs with code files
    try:
        subdirs = [
            e for e in os.listdir(dir_path)
            if os.path.isdir(os.path.join(dir_path, e))
            and e not in SKIP_DIRS
            and not e.startswith(".")
        ]
        if len(subdirs) < 3:
            return False
        code_subdir_count = 0
        for sd in subdirs:
            sd_path = os.path.join(dir_path, sd)
            for fname in os.listdir(sd_path):
                ext = os.path.splitext(fname)[1].lower()
                if ext in INDEXABLE_EXTENSIONS:
                    code_subdir_count += 1
                    break
        return code_subdir_count >= 3
    except OSError:
        return False


def _generate_repo_summary(
    db: sqlite3.Connection,
    repo_path: str,
    repo_name: str,
    workspace: str,
    structural_dirs_info: list[dict],
) -> None:
    """Upsert a repo summary entry that lists top-level dirs and structural contents."""
    lines: list[str] = [f"Repository: {repo_name}", ""]

    # Top-level directory listing
    try:
        top_entries = sorted(os.listdir(repo_path))
        top_dirs = [e for e in top_entries if os.path.isdir(os.path.join(repo_path, e))
                    and e not in SKIP_DIRS and not e.startswith(".")]
        top_files = [e for e in top_entries if os.path.isfile(os.path.join(repo_path, e))
                     and not e.startswith(".")]
        if top_dirs:
            lines.append("Top-level directories: " + ", ".join(top_dirs))
        if top_files:
            lines.append("Root files: " + ", ".join(top_files[:20]))
    except OSError:
        pass

    # Structural dir summaries
    if structural_dirs_info:
        lines.append("")
        lines.append("Provides:")
        for info in structural_dirs_info:
            label = info["label"]
            names = info["subdir_names"]
            lines.append(f"  {label}: {', '.join(names)}")

    content = "\n".join(lines)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
    source_path = f"{repo_name}:summary"
    title = f"{repo_name} (repo summary)"
    _upsert_entry(db, workspace, source_path, "text", title, content, content_hash)


def _find_all_repos(workspace_path: str) -> list[tuple[str, str]]:
    """Find all git repos under workspace (up to depth 3). Returns (repo_name, repo_abs_path)."""
    repos = []
    ws = str(workspace_path)
    for depth1 in os.listdir(ws):
        d1 = os.path.join(ws, depth1)
        if not os.path.isdir(d1) or depth1.startswith(".") or depth1 in SKIP_DIRS:
            continue
        if os.path.isdir(os.path.join(d1, ".git")):
            repos.append((depth1, d1))
            continue
        # Check depth 2 (repos/{team}/{repo})
        try:
            for depth2 in os.listdir(d1):
                d2 = os.path.join(d1, depth2)
                if not os.path.isdir(d2) or depth2.startswith(".") or depth2 in SKIP_DIRS:
                    continue
                if os.path.isdir(os.path.join(d2, ".git")):
                    repos.append((depth2, d2))
                    continue
                # Check depth 3
                try:
                    for depth3 in os.listdir(d2):
                        d3 = os.path.join(d2, depth3)
                        if not os.path.isdir(d3) or depth3.startswith(".") or depth3 in SKIP_DIRS:
                            continue
                        if os.path.isdir(os.path.join(d3, ".git")):
                            repos.append((depth3, d3))
                except OSError:
                    pass
        except OSError:
            pass
    return repos


def _index_directory_structures(
    db: sqlite3.Connection,
    workspace_path: Path,
    workspace: str,
    stats: SyncStats,
) -> None:
    """Detect repos and structural dirs; upsert directory-listing entries."""
    repos = _find_all_repos(str(workspace_path))
    if not repos:
        return

    for repo_name, repo_path in repos:
        structural_dirs_info: list[dict] = []

        # Collect candidate dirs at depth 1 under repo
        try:
            repo_entries = os.listdir(repo_path)
        except OSError:
            continue

        depth1_dirs = [
            e for e in repo_entries
            if os.path.isdir(os.path.join(repo_path, e))
            and e not in SKIP_DIRS
            and not e.startswith(".")
        ]

        # Also include src/* subdirs (depth 2 via src/)
        src_path = os.path.join(repo_path, "src")
        if os.path.isdir(src_path):
            try:
                for sd in os.listdir(src_path):
                    sd_full = os.path.join(src_path, sd)
                    if os.path.isdir(sd_full) and sd not in SKIP_DIRS and not sd.startswith("."):
                        depth1_dirs.append(f"src/{sd}")
            except OSError:
                pass

        for dir_rel in depth1_dirs:
            dir_abs = os.path.join(repo_path, dir_rel)
            dir_name = os.path.basename(dir_rel)

            if not _is_structural_dir(dir_abs, dir_name):
                continue

            # Enumerate immediate subdirectories
            try:
                subdirs = sorted([
                    e for e in os.listdir(dir_abs)
                    if os.path.isdir(os.path.join(dir_abs, e))
                    and e not in SKIP_DIRS
                    and not e.startswith(".")
                ])
            except OSError:
                continue

            if not subdirs:
                continue

            # Build content lines per subdir
            subdir_lines: list[str] = []
            dominant_label = "files"
            for sd_name in subdirs:
                sd_abs = os.path.join(dir_abs, sd_name)
                count, names = _file_list_for_subdir(sd_abs)
                if count == 0:
                    continue
                if count <= 5:
                    subdir_lines.append(f"- {sd_name}/ ({count} files: {', '.join(names)})")
                else:
                    subdir_lines.append(f"- {sd_name}/ ({count} files)")
                dominant_label = _dominant_ext_label(sd_abs)

            if not subdir_lines:
                continue

            label = f"{dominant_label} in {dir_rel}/"
            n = len(subdir_lines)
            header = f"{dir_rel}/ in {repo_name} contains {n} {dominant_label}:"
            content = header + "\n" + "\n".join(subdir_lines)
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
            source_path = f"{repo_name}/{dir_rel}:structure"
            title = f"{repo_name}/{dir_rel} (structure)"

            _upsert_entry(db, workspace, source_path, "text", title, content, content_hash)
            stats.structures_indexed += 1

            structural_dirs_info.append({
                "dir_rel": dir_rel,
                "label": label,
                "subdir_names": subdirs,
            })

        _generate_repo_summary(db, repo_path, repo_name, workspace, structural_dirs_info)
        stats.repo_summaries_generated += 1


# ── Title extraction ─────────────────────────────────────────────────────────

def _derive_title(file_path: Path, content: str, content_type: str) -> str:
    """Extract a meaningful title from file content or fall back to filename."""
    if content_type == "markdown":
        for line in content.split("\n")[:10]:
            line = line.strip()
            if line.startswith("# ") and not line.startswith("##"):
                return line[2:].strip()

    if content_type == "terraform":
        match = re.search(r'^(?:module|resource|data)\s+"([^"]+)"\s+"([^"]+)"', content, re.MULTILINE)
        if match:
            return f"{match.group(1)}/{match.group(2)}"

    if content_type == "python":
        match = re.search(r'^"""(.+?)"""', content, re.DOTALL)
        if match:
            first_line = match.group(1).strip().split("\n")[0]
            if len(first_line) < 120:
                return first_line

    return file_path.name


# ── Graph edge extraction ────────────────────────────────────────────────────

def _extract_graph_edges(
    file_path: Path,
    content: str,
    content_type: str,
    workspace: str,
) -> list[dict]:
    """Extract entity relationships from file content for the knowledge graph."""
    edges = []
    rel_path = file_path.name
    parent_dir = file_path.parent.name

    if content_type == "terraform":
        _extract_terraform_edges(content, rel_path, workspace, edges)
    elif content_type in ("yaml", "json") and "argocd" in str(file_path).lower():
        _extract_argocd_edges(content, rel_path, workspace, edges)
    elif content_type == "yaml" and ("deployment" in content.lower() or "kind:" in content):
        _extract_k8s_edges(content, rel_path, workspace, edges)
    elif content_type == "markdown" and file_path.name.upper().startswith("ADR"):
        _extract_adr_edges(content, rel_path, workspace, edges)

    # Directory-level grouping
    if parent_dir and parent_dir not in (".", ""):
        edges.append({
            "source_name": rel_path,
            "source_type": "file",
            "target_name": parent_dir,
            "target_type": "directory",
            "predicate": "belongs_to",
            "workspace": workspace,
        })

    return edges


def _extract_terraform_edges(content: str, source: str, workspace: str, edges: list):
    modules = re.findall(r'module\s+"([^"]+)"', content)
    for mod in modules:
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": mod,
            "target_type": "terraform_module",
            "predicate": "uses_module",
            "workspace": workspace,
        })

    resources = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', content)
    for rtype, rname in resources:
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": f"{rtype}.{rname}",
            "target_type": "terraform_resource",
            "predicate": "defines",
            "workspace": workspace,
        })

    data_sources = re.findall(r'data\s+"([^"]+)"\s+"([^"]+)"', content)
    for dtype, dname in data_sources:
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": f"{dtype}.{dname}",
            "target_type": "terraform_data",
            "predicate": "reads",
            "workspace": workspace,
        })


def _extract_argocd_edges(content: str, source: str, workspace: str, edges: list):
    apps = re.findall(r'name:\s*([a-zA-Z0-9_-]+)', content)
    repos = re.findall(r'repoURL:\s*["\']?([^\s"\']+)', content)

    for app in apps[:5]:
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": app,
            "target_type": "argocd_app",
            "predicate": "defines",
            "workspace": workspace,
        })

    for repo in repos[:5]:
        repo_name = repo.rstrip("/").split("/")[-1].replace(".git", "")
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": repo_name,
            "target_type": "git_repo",
            "predicate": "deploys_from",
            "workspace": workspace,
        })


def _extract_k8s_edges(content: str, source: str, workspace: str, edges: list):
    kinds = re.findall(r'kind:\s*(\w+)', content)
    names = re.findall(r'name:\s*([a-zA-Z0-9_-]+)', content)

    for kind in set(kinds[:5]):
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": kind,
            "target_type": "k8s_kind",
            "predicate": "defines",
            "workspace": workspace,
        })

    images = re.findall(r'image:\s*["\']?([^\s"\']+)', content)
    for img in images[:5]:
        img_name = img.split("/")[-1].split(":")[0]
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": img_name,
            "target_type": "container_image",
            "predicate": "uses_image",
            "workspace": workspace,
        })


def _extract_adr_edges(content: str, source: str, workspace: str, edges: list):
    title_match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    if title_match:
        edges.append({
            "source_name": source,
            "source_type": "file",
            "target_name": title_match.group(1).strip(),
            "target_type": "decision",
            "predicate": "documents",
            "workspace": workspace,
        })

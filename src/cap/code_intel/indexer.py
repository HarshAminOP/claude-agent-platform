"""
CAP Code Intelligence — Batch and Incremental Indexer.

Walks a workspace directory, extracts symbols from supported files,
and stores them in the CAP database (code_files, code_symbols, code_relationships).

Supports:
- Full workspace indexing with directory exclusions
- Incremental indexing based on file mtime vs last_indexed
- Single-file re-indexing
"""

import logging
import os
import time
from pathlib import Path
from sqlite3 import Connection

from cap.code_intel.extractor import (
    EXTENSION_MAP,
    SUPPORTED_LANGUAGES,
    FileIndex,
    content_hash,
    detect_language,
    extract_file,
)

logger = logging.getLogger("cap.code_intel.indexer")

# Directories to always skip during workspace traversal
SKIP_DIRS = {
    "node_modules",
    ".git",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    "target",  # Rust/Java build output
    ".terraform",
    ".cache",
}


def index_workspace(workspace_path: str, db: Connection) -> dict:
    """
    Index all supported files in a workspace directory.

    Walks the directory tree, skipping excluded directories (node_modules, .git,
    vendor, etc.), and extracts symbols from each supported file. Uses incremental
    logic: skips files whose content hash has not changed since last index.

    Args:
        workspace_path: Absolute path to the workspace root.
        db: SQLite connection (from cap.db.get_db).

    Returns:
        Stats dict with keys: files_indexed, files_skipped, symbols_extracted,
        relationships_extracted, errors, duration_ms.
    """
    start_time = time.time()
    stats = {
        "files_indexed": 0,
        "files_skipped": 0,
        "symbols_extracted": 0,
        "relationships_extracted": 0,
        "errors": 0,
        "duration_ms": 0,
    }

    workspace_path = os.path.abspath(workspace_path)
    if not os.path.isdir(workspace_path):
        logger.error("Workspace path does not exist: %s", workspace_path)
        return stats

    supported_extensions = set(EXTENSION_MAP.keys())

    for root, dirs, files in os.walk(workspace_path):
        # Prune excluded directories in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

        for filename in files:
            ext = Path(filename).suffix.lower()
            if ext not in supported_extensions:
                continue

            file_path = os.path.join(root, filename)

            try:
                # Incremental check: compare content hash
                current_hash = content_hash(file_path)
                existing = db.execute(
                    "SELECT hash FROM code_files WHERE path = ?",
                    (file_path,),
                ).fetchone()

                if existing and existing[0] == current_hash:
                    stats["files_skipped"] += 1
                    continue

                # Extract and store
                file_index = extract_file(file_path)
                if file_index is None:
                    stats["files_skipped"] += 1
                    continue

                _store_file_index(db, file_index, workspace_path)
                stats["files_indexed"] += 1
                stats["symbols_extracted"] += len(file_index.symbols)
                stats["relationships_extracted"] += len(file_index.relationships)

            except Exception as e:
                logger.warning("Error indexing %s: %s", file_path, e)
                stats["errors"] += 1

    db.commit()
    stats["duration_ms"] = int((time.time() - start_time) * 1000)
    logger.info(
        "Workspace indexed: %d files, %d symbols, %d relationships in %dms",
        stats["files_indexed"],
        stats["symbols_extracted"],
        stats["relationships_extracted"],
        stats["duration_ms"],
    )
    return stats


def index_file(file_path: str, db: Connection) -> bool:
    """
    Re-index a single file.

    Removes existing entries for this file and re-extracts. Useful for
    post-edit re-indexing triggered by hooks.

    Args:
        file_path: Absolute path to the file.
        db: SQLite connection.

    Returns:
        True if file was successfully indexed, False otherwise.
    """
    file_path = os.path.abspath(file_path)

    if not os.path.isfile(file_path):
        # File was deleted — remove from index
        _remove_file_from_index(db, file_path)
        db.commit()
        return True

    language = detect_language(file_path)
    if language is None or language not in SUPPORTED_LANGUAGES:
        return False

    try:
        file_index = extract_file(file_path, language)
        if file_index is None:
            return False

        # Determine workspace from file path (use parent of first git root or cwd)
        workspace = _detect_workspace(file_path)
        _store_file_index(db, file_index, workspace)
        db.commit()
        return True

    except Exception as e:
        logger.warning("Error re-indexing %s: %s", file_path, e)
        return False


def _store_file_index(db: Connection, file_index: FileIndex, workspace: str) -> None:
    """Store a FileIndex into the database, replacing existing entries."""
    file_path = file_index.path
    now = time.time()

    # Remove old data for this file
    _remove_file_from_index(db, file_path)

    # Insert file record
    db.execute(
        """INSERT OR REPLACE INTO code_files (path, workspace, language, hash, extracted_at)
           VALUES (?, ?, ?, ?, ?)""",
        (file_path, workspace, file_index.language, file_index.hash, now),
    )

    # Insert symbols
    for sym in file_index.symbols:
        qualified_name = f"{Path(file_path).stem}.{sym.name}"
        if sym.parent:
            qualified_name = f"{Path(file_path).stem}.{sym.parent}.{sym.name}"

        db.execute(
            """INSERT INTO code_symbols
               (qualified_name, name, kind, file_path, line_start, line_end,
                signature, docstring, parent, visibility)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                qualified_name,
                sym.name,
                sym.kind,
                file_path,
                sym.start_line,
                sym.end_line,
                sym.signature,
                sym.docstring,
                sym.parent,
                sym.visibility,
            ),
        )

    # Insert relationships
    for rel in file_index.relationships:
        db.execute(
            """INSERT INTO code_relationships
               (source, target, kind, file_path, line)
               VALUES (?, ?, ?, ?, ?)""",
            (rel.source, rel.target, rel.kind, file_path, rel.line),
        )


def _remove_file_from_index(db: Connection, file_path: str) -> None:
    """Remove all index data for a file."""
    db.execute("DELETE FROM code_relationships WHERE file_path = ?", (file_path,))
    db.execute("DELETE FROM code_symbols WHERE file_path = ?", (file_path,))
    db.execute("DELETE FROM code_files WHERE path = ?", (file_path,))


def _detect_workspace(file_path: str) -> str:
    """Detect workspace root by walking up to find .git directory."""
    current = Path(file_path).parent
    while current != current.parent:
        if (current / ".git").exists():
            return str(current)
        current = current.parent
    # Fallback: use the file's directory
    return str(Path(file_path).parent)

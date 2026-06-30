"""
CAP Code Intelligence — Query Interface.

Provides high-level query functions over the indexed code graph:
- code_structure: all symbols in a file with hierarchy
- code_dependents: all symbols that reference a given symbol
- code_trace: call chain between two symbols
- blast_radius: all files/symbols affected by changes to a file
"""

import logging
from collections import deque
from pathlib import Path
from sqlite3 import Connection
from typing import Optional

logger = logging.getLogger("cap.code_intel.queries")


def code_structure(file_path: str, db: Connection) -> dict:
    """
    Get all symbols in a file with their hierarchy.

    Returns a dict with:
    - file_path: the queried path
    - language: detected language
    - symbols: list of symbol dicts (name, kind, start_line, end_line, signature, visibility, parent)
    - relationships: list of relationship dicts from this file
    """
    # Get file metadata
    file_row = db.execute(
        "SELECT path, language FROM code_files WHERE path = ?",
        (file_path,),
    ).fetchone()

    if not file_row:
        return {"file_path": file_path, "language": None, "symbols": [], "relationships": []}

    # Get all symbols in this file
    symbol_rows = db.execute(
        """SELECT qualified_name, name, kind, line_start, line_end,
                  signature, visibility, parent, docstring
           FROM code_symbols WHERE file_path = ?
           ORDER BY line_start""",
        (file_path,),
    ).fetchall()

    symbols = []
    for row in symbol_rows:
        symbols.append({
            "qualified_name": row[0],
            "name": row[1],
            "kind": row[2],
            "start_line": row[3],
            "end_line": row[4],
            "signature": row[5],
            "visibility": row[6],
            "parent": row[7],
            "docstring": row[8],
        })

    # Get relationships from this file
    rel_rows = db.execute(
        """SELECT source, target, kind, line
           FROM code_relationships WHERE file_path = ?
           ORDER BY line""",
        (file_path,),
    ).fetchall()

    relationships = []
    for row in rel_rows:
        relationships.append({
            "source": row[0],
            "target": row[1],
            "kind": row[2],
            "line": row[3],
        })

    return {
        "file_path": file_path,
        "language": file_row[1],
        "symbols": symbols,
        "relationships": relationships,
    }


def code_dependents(symbol_name: str, db: Connection) -> list[dict]:
    """
    Find all symbols that reference a given symbol.

    Searches by both exact name and qualified_name matches in relationships.
    Returns list of dicts with: source, kind, file_path, line.
    """
    # Find all relationships where this symbol is the target
    rows = db.execute(
        """SELECT r.source, r.kind, r.file_path, r.line
           FROM code_relationships r
           WHERE r.target = ? OR r.target LIKE ?
           ORDER BY r.file_path, r.line""",
        (symbol_name, f"%.{symbol_name}"),
    ).fetchall()

    # Also check by qualified name
    qual_rows = db.execute(
        """SELECT r.source, r.kind, r.file_path, r.line
           FROM code_relationships r
           WHERE r.target IN (
               SELECT qualified_name FROM code_symbols WHERE name = ?
           )
           ORDER BY r.file_path, r.line""",
        (symbol_name,),
    ).fetchall()

    # Deduplicate
    seen = set()
    results = []
    for row in list(rows) + list(qual_rows):
        key = (row[0], row[1], row[2], row[3])
        if key not in seen:
            seen.add(key)
            results.append({
                "source": row[0],
                "kind": row[1],
                "file_path": row[2],
                "line": row[3],
            })

    return results


def code_trace(from_symbol: str, to_symbol: str, db: Connection, max_depth: int = 5) -> Optional[list[dict]]:
    """
    Find a call chain between two symbols using BFS over the relationship graph.

    Traverses 'calls' and 'imports' relationships to find a path from
    from_symbol to to_symbol.

    Args:
        from_symbol: Starting symbol name.
        to_symbol: Target symbol name.
        db: SQLite connection.
        max_depth: Maximum traversal depth.

    Returns:
        List of edge dicts forming the path, or None if no path found.
        Each edge: {source, target, kind, file_path, line}
    """
    # Resolve symbol names to all known qualified names
    from_names = _resolve_symbol_names(from_symbol, db)
    to_names = _resolve_symbol_names(to_symbol, db)

    if not from_names or not to_names:
        return None

    # BFS from from_symbol following outgoing 'calls' edges
    queue = deque()
    visited = set()
    # parent map: node -> (parent_node, edge_dict)
    parent_map = {}

    for name in from_names:
        queue.append((name, 0))
        visited.add(name)

    to_names_set = set(to_names)

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue

        # Check if we reached the target
        if current in to_names_set:
            # Reconstruct path
            return _reconstruct_path(current, parent_map, from_names)

        # Get outgoing edges (calls, imports)
        edges = db.execute(
            """SELECT target, kind, file_path, line
               FROM code_relationships
               WHERE source = ? AND kind IN ('calls', 'imports', 'uses_type')""",
            (current,),
        ).fetchall()

        for edge in edges:
            target = edge[0]
            if target not in visited:
                visited.add(target)
                parent_map[target] = (current, {
                    "source": current,
                    "target": target,
                    "kind": edge[1],
                    "file_path": edge[2],
                    "line": edge[3],
                })
                queue.append((target, depth + 1))

                # Check target against to_names
                if target in to_names_set:
                    return _reconstruct_path(target, parent_map, from_names)

        # Also try matching against unqualified names
        # (e.g., "foo" might call "bar" which is stored as "module.bar")
        edges_by_name = db.execute(
            """SELECT r.target, r.kind, r.file_path, r.line
               FROM code_relationships r
               JOIN code_symbols s ON r.source = s.qualified_name
               WHERE s.name = ? AND r.kind IN ('calls', 'imports', 'uses_type')""",
            (current,),
        ).fetchall()

        for edge in edges_by_name:
            target = edge[0]
            if target not in visited:
                visited.add(target)
                parent_map[target] = (current, {
                    "source": current,
                    "target": target,
                    "kind": edge[1],
                    "file_path": edge[2],
                    "line": edge[3],
                })
                queue.append((target, depth + 1))

    return None  # No path found


def blast_radius(file_path: str, db: Connection) -> dict:
    """
    Compute the blast radius of changes to a given file.

    Determines all files and symbols that would be affected by modifications
    to the specified file. Uses the relationship graph to find:
    - Direct dependents: files that import from this file
    - Transitive dependents: files that depend on direct dependents
    - Affected symbols: specific symbols that reference symbols in this file

    Args:
        file_path: Absolute path to the file being changed.
        db: SQLite connection.

    Returns:
        Dict with: file_path, symbols_in_file, direct_dependents,
        transitive_dependents, affected_symbols, total_impact_files.
    """
    # Get symbols defined in this file
    symbols_in_file = db.execute(
        """SELECT qualified_name, name, kind
           FROM code_symbols WHERE file_path = ?""",
        (file_path,),
    ).fetchall()

    symbol_names = set()
    symbols_list = []
    for row in symbols_in_file:
        symbol_names.add(row[0])  # qualified_name
        symbol_names.add(row[1])  # name
        symbols_list.append({
            "qualified_name": row[0],
            "name": row[1],
            "kind": row[2],
        })

    # Find direct dependents: relationships that target symbols in this file
    direct_dep_rows = db.execute(
        """SELECT DISTINCT r.file_path, r.source, r.target, r.kind
           FROM code_relationships r
           WHERE r.file_path != ?
             AND (r.target IN (SELECT qualified_name FROM code_symbols WHERE file_path = ?)
                  OR r.target IN (SELECT name FROM code_symbols WHERE file_path = ?))""",
        (file_path, file_path, file_path),
    ).fetchall()

    direct_files = set()
    affected_symbols = []
    for row in direct_dep_rows:
        direct_files.add(row[0])
        affected_symbols.append({
            "file_path": row[0],
            "source": row[1],
            "target": row[2],
            "kind": row[3],
        })

    # Also check: files that import the module name of this file
    module_name = Path(file_path).stem
    import_deps = db.execute(
        """SELECT DISTINCT file_path
           FROM code_relationships
           WHERE kind = 'imports'
             AND (target = ? OR target LIKE ?)
             AND file_path != ?""",
        (module_name, f"%{module_name}%", file_path),
    ).fetchall()
    for row in import_deps:
        direct_files.add(row[0])

    # Transitive dependents (one level deeper)
    transitive_files = set()
    for dep_file in direct_files:
        dep_module = Path(dep_file).stem
        trans_rows = db.execute(
            """SELECT DISTINCT file_path
               FROM code_relationships
               WHERE kind = 'imports'
                 AND (target = ? OR target LIKE ?)
                 AND file_path != ?
                 AND file_path != ?""",
            (dep_module, f"%{dep_module}%", dep_file, file_path),
        ).fetchall()
        for row in trans_rows:
            transitive_files.add(row[0])

    # Remove direct from transitive to avoid overlap
    transitive_files -= direct_files

    all_affected = direct_files | transitive_files
    all_affected.discard(file_path)

    return {
        "file_path": file_path,
        "symbols_in_file": symbols_list,
        "direct_dependents": sorted(direct_files),
        "transitive_dependents": sorted(transitive_files),
        "affected_symbols": affected_symbols,
        "total_impact_files": len(all_affected),
    }


# ─── Internal Helpers ─────────────────────────────────────────────────────────


def _resolve_symbol_names(symbol_name: str, db: Connection) -> list[str]:
    """Resolve a symbol name to all matching qualified names in the DB."""
    # Try exact match first
    rows = db.execute(
        "SELECT qualified_name FROM code_symbols WHERE qualified_name = ?",
        (symbol_name,),
    ).fetchall()
    if rows:
        return [row[0] for row in rows]

    # Try by unqualified name
    rows = db.execute(
        "SELECT qualified_name FROM code_symbols WHERE name = ?",
        (symbol_name,),
    ).fetchall()
    if rows:
        return [row[0] for row in rows]

    # Try as-is (might be a module/relationship source)
    return [symbol_name]


def _reconstruct_path(
    target: str, parent_map: dict, start_names: list[str]
) -> list[dict]:
    """Reconstruct the path from start to target using the parent map."""
    path = []
    current = target
    while current in parent_map:
        parent, edge = parent_map[current]
        path.append(edge)
        current = parent
        if current in start_names:
            break

    path.reverse()
    return path

"""
CAP Code Intelligence — Blast Radius Analysis.

Computes the impact of changing a file by traversing the code relationship graph
using degree-aware BFS. Unlike the basic blast_radius in queries.py (which uses
simple SQL joins), this module leverages the knowledge graph's degree-aware
traversal to handle hub nodes (files imported by 100+ other files) without
graph explosion.

Provides:
- blast_radius: full impact analysis with risk scoring
- Uses code_relationships table + degree-aware graph traversal from cap.lib.graph
"""

import logging
from pathlib import Path
from sqlite3 import Connection

from cap.lib.graph import (
    DegreeAwareResult,
    degree_aware_bfs,
    get_node_degree,
)

logger = logging.getLogger("cap.code_intel.blast_radius")


def blast_radius(file_path: str, db: Connection) -> dict:
    """Compute the blast radius of changes to a given file.

    Determines all files and symbols that would be affected by modifications
    to the specified file. Uses the code_relationships table for direct lookups
    and the knowledge graph's degree-aware BFS for transitive analysis.

    Args:
        file_path: Absolute path to the file being changed.
        db: SQLite connection (must have both code_* tables and knowledge_graph_* tables).

    Returns:
        Dict with:
        - file_path: the input file
        - direct_dependents: list of file paths that import/call symbols in this file
        - transitive_dependents: list of file paths at 2-hop distance
        - affected_tests: list of test file paths that cover this file
        - risk_score: float 0-1 based on dependent count and test coverage
        - summarized_hubs: any hub nodes encountered during traversal
    """
    # Step 1: Get all symbols defined in this file
    symbols_in_file = db.execute(
        """SELECT qualified_name, name, kind
           FROM code_symbols WHERE file_path = ?""",
        (file_path,),
    ).fetchall()

    if not symbols_in_file:
        return {
            "file_path": file_path,
            "direct_dependents": [],
            "transitive_dependents": [],
            "affected_tests": [],
            "risk_score": 0.0,
            "summarized_hubs": [],
        }

    qualified_names = [row[0] for row in symbols_in_file]
    symbol_short_names = [row[1] for row in symbols_in_file]

    # Step 2: Find direct dependents via code_relationships table
    # These are files that import/call/use symbols defined in our target file
    direct_dep_files = _find_direct_dependents(file_path, qualified_names, symbol_short_names, db)

    # Step 3: Find transitive dependents (2-hop) via code_relationships
    transitive_dep_files = _find_transitive_dependents(file_path, direct_dep_files, db)

    # Step 4: Also attempt degree-aware BFS on the knowledge graph for richer traversal
    # This catches relationships not encoded in code_relationships (e.g., config deps)
    graph_result = _graph_traverse_dependents(file_path, db)

    # Merge graph-discovered files into transitive set
    if graph_result and graph_result.nodes:
        for node_id, depth in graph_result.nodes:
            # Resolve node_id back to file path if possible
            resolved = _resolve_node_to_file(node_id, db)
            if resolved and resolved != file_path:
                if depth == 1 and resolved not in direct_dep_files:
                    direct_dep_files.add(resolved)
                elif depth >= 2 and resolved not in direct_dep_files:
                    transitive_dep_files.add(resolved)

    # Remove overlap: transitive should not include direct
    transitive_dep_files -= direct_dep_files
    transitive_dep_files.discard(file_path)

    # Step 5: Find affected test files
    all_affected = direct_dep_files | transitive_dep_files
    affected_tests = _find_affected_tests(file_path, all_affected, db)

    # Step 6: Compute risk score
    risk_score = _compute_risk_score(
        direct_count=len(direct_dep_files),
        transitive_count=len(transitive_dep_files),
        test_count=len(affected_tests),
        total_affected=len(all_affected),
    )

    return {
        "file_path": file_path,
        "direct_dependents": sorted(direct_dep_files),
        "transitive_dependents": sorted(transitive_dep_files),
        "affected_tests": sorted(affected_tests),
        "risk_score": risk_score,
        "summarized_hubs": graph_result.summarized_hubs if graph_result else [],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_direct_dependents(
    file_path: str,
    qualified_names: list[str],
    short_names: list[str],
    db: Connection,
) -> set[str]:
    """Find files that directly import/call/use symbols from the target file."""
    direct_files: set[str] = set()

    # Find via qualified name targets
    if qualified_names:
        placeholders = ",".join("?" * len(qualified_names))
        rows = db.execute(
            f"""SELECT DISTINCT file_path
                FROM code_relationships
                WHERE target IN ({placeholders})
                  AND file_path != ?""",
            qualified_names + [file_path],
        ).fetchall()
        for row in rows:
            direct_files.add(row[0])

    # Find via short name targets (catches unqualified imports)
    if short_names:
        placeholders = ",".join("?" * len(short_names))
        rows = db.execute(
            f"""SELECT DISTINCT file_path
                FROM code_relationships
                WHERE target IN ({placeholders})
                  AND file_path != ?""",
            short_names + [file_path],
        ).fetchall()
        for row in rows:
            direct_files.add(row[0])

    # Find via module name imports (e.g., "import mymodule")
    module_name = Path(file_path).stem
    rows = db.execute(
        """SELECT DISTINCT file_path
           FROM code_relationships
           WHERE kind = 'imports'
             AND (target = ? OR target LIKE ?)
             AND file_path != ?""",
        (module_name, f"%.{module_name}", file_path),
    ).fetchall()
    for row in rows:
        direct_files.add(row[0])

    return direct_files


def _find_transitive_dependents(
    file_path: str,
    direct_files: set[str],
    db: Connection,
) -> set[str]:
    """Find 2-hop dependents: files that import from direct dependents."""
    transitive_files: set[str] = set()

    for dep_file in direct_files:
        # Get symbols exported by the direct dependent
        dep_symbols = db.execute(
            "SELECT qualified_name, name FROM code_symbols WHERE file_path = ?",
            (dep_file,),
        ).fetchall()

        dep_qualified = [r[0] for r in dep_symbols]
        dep_module = Path(dep_file).stem

        # Find files that import from this dependent
        if dep_qualified:
            placeholders = ",".join("?" * len(dep_qualified))
            rows = db.execute(
                f"""SELECT DISTINCT file_path
                    FROM code_relationships
                    WHERE target IN ({placeholders})
                      AND file_path != ?
                      AND file_path != ?""",
                dep_qualified + [dep_file, file_path],
            ).fetchall()
            for row in rows:
                transitive_files.add(row[0])

        # Also check module-level imports
        rows = db.execute(
            """SELECT DISTINCT file_path
               FROM code_relationships
               WHERE kind = 'imports'
                 AND (target = ? OR target LIKE ?)
                 AND file_path != ?
                 AND file_path != ?""",
            (dep_module, f"%.{dep_module}", dep_file, file_path),
        ).fetchall()
        for row in rows:
            transitive_files.add(row[0])

    return transitive_files


def _graph_traverse_dependents(
    file_path: str,
    db: Connection,
) -> DegreeAwareResult | None:
    """Use degree-aware BFS on the knowledge graph for the file node.

    Attempts to find the file as a node in knowledge_graph_nodes and traverse
    incoming edges. Returns None if the file is not in the knowledge graph.
    """
    # Try to find this file in the knowledge graph
    # Nodes might be stored with file path as entity_name
    rows = db.execute(
        """SELECT id FROM knowledge_graph_nodes
           WHERE entity_name = ? OR entity_name LIKE ?
           LIMIT 1""",
        (file_path, f"%{Path(file_path).name}"),
    ).fetchall()

    if not rows:
        return None

    start_id = rows[0][0]
    degree = get_node_degree(start_id, db)

    if degree == 0:
        return None

    return degree_aware_bfs(
        conn=db,
        start_ids=[start_id],
        max_depth=3,
        workspace=None,
        max_nodes=200,
    )


def _resolve_node_to_file(node_id: str, db: Connection) -> str | None:
    """Resolve a knowledge graph node ID back to a file path if possible."""
    row = db.execute(
        """SELECT entity_name, entity_type, metadata
           FROM knowledge_graph_nodes WHERE id = ?""",
        (node_id,),
    ).fetchone()

    if not row:
        return None

    entity_name, entity_type, metadata_raw = row

    # If entity_type is 'file', entity_name is likely the path
    if entity_type == "file":
        return entity_name

    # Check metadata for file_path
    if metadata_raw:
        try:
            import json
            meta = json.loads(metadata_raw)
            if "file_path" in meta:
                return meta["file_path"]
        except (ValueError, TypeError):
            pass

    # If entity_name looks like a file path, return it
    if "/" in entity_name and "." in entity_name.split("/")[-1]:
        return entity_name

    return None


def _find_affected_tests(
    file_path: str,
    all_affected_files: set[str],
    db: Connection,
) -> set[str]:
    """Find test files that cover the target file or its dependents.

    A file is considered a test file if:
    - Its path contains 'test' or 'tests' directory segment
    - Its filename starts with 'test_' or ends with '_test'
    - It imports symbols from the target file or affected files
    """
    test_files: set[str] = set()

    # Check if any affected files are test files
    for candidate in all_affected_files:
        if _is_test_file(candidate):
            test_files.add(candidate)

    # Also search for test files that import from the target file directly
    module_name = Path(file_path).stem
    test_rows = db.execute(
        """SELECT DISTINCT r.file_path
           FROM code_relationships r
           WHERE r.kind = 'imports'
             AND (r.target = ? OR r.target LIKE ?)""",
        (module_name, f"%.{module_name}"),
    ).fetchall()

    for row in test_rows:
        if _is_test_file(row[0]):
            test_files.add(row[0])

    # Search for test files that test symbols from this file
    symbols = db.execute(
        "SELECT name FROM code_symbols WHERE file_path = ?",
        (file_path,),
    ).fetchall()

    for (sym_name,) in symbols:
        # Look for test functions that reference this symbol
        test_sym_rows = db.execute(
            """SELECT DISTINCT file_path
               FROM code_relationships
               WHERE source LIKE ? AND kind IN ('calls', 'imports')""",
            (f"%test%{sym_name}%",),
        ).fetchall()
        for row in test_sym_rows:
            if _is_test_file(row[0]):
                test_files.add(row[0])

    # Remove the file itself from test results
    test_files.discard(file_path)

    return test_files


def _is_test_file(file_path: str) -> bool:
    """Determine if a file path represents a test file."""
    path = Path(file_path)
    name = path.stem

    # Filename patterns
    if name.startswith("test_") or name.endswith("_test"):
        return True
    if name.startswith("test") and name[4:5].isupper():
        return True

    # Directory patterns
    parts = path.parts
    if "test" in parts or "tests" in parts or "__tests__" in parts:
        return True
    if "spec" in parts or "specs" in parts:
        return True

    return False


def _compute_risk_score(
    direct_count: int,
    transitive_count: int,
    test_count: int,
    total_affected: int,
) -> float:
    """Compute a 0-1 risk score based on dependent counts and test coverage.

    Factors:
    - More dependents = higher risk (capped contribution: 0.6)
      Direct dependents are weighted more heavily than transitive ones.
    - Low test coverage relative to dependents = higher risk (contribution: 0.4)

    Score breakdown:
    - 0.0-0.3: Low risk (few dependents, good test coverage)
    - 0.3-0.6: Medium risk
    - 0.6-1.0: High risk (many dependents or poor test coverage)
    """
    # Dependent-based risk (0 to 0.6)
    # Weight direct dependents more heavily: each direct counts as 1.5x
    weighted_affected = direct_count * 1.5 + transitive_count
    effective_total = max(total_affected, int(weighted_affected))

    if effective_total == 0:
        dependent_risk = 0.0
    elif effective_total <= 3:
        dependent_risk = 0.1
    elif effective_total <= 10:
        dependent_risk = 0.3
    elif effective_total <= 25:
        dependent_risk = 0.45
    else:
        dependent_risk = 0.6

    # Test coverage risk (0 to 0.4)
    # Higher risk when there are many dependents but few tests
    if total_affected == 0:
        coverage_risk = 0.0
    elif test_count == 0:
        # No tests at all: maximum coverage risk
        coverage_risk = 0.4
    else:
        # Ratio of tests to affected files (ideal: >= 1 test per 2 affected files)
        coverage_ratio = test_count / max(total_affected, 1)
        if coverage_ratio >= 0.5:
            coverage_risk = 0.0
        elif coverage_ratio >= 0.25:
            coverage_risk = 0.15
        else:
            coverage_risk = 0.3

    score = dependent_risk + coverage_risk
    return min(1.0, max(0.0, round(score, 2)))

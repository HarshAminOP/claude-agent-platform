"""
CAP Code Intelligence — AST-based symbol extraction, indexing, and query.

Provides:
- extractor: Tree-sitter/ast-grep based extraction of symbols and relationships
- indexer: Batch and incremental workspace indexing
- queries: Query interface for code structure, dependents, traces, blast radius
"""

from cap.code_intel.extractor import (
    Symbol,
    Relationship,
    FileIndex,
    extract_file,
    SUPPORTED_LANGUAGES,
)
from cap.code_intel.indexer import index_workspace, index_file
from cap.code_intel.queries import (
    code_structure,
    code_dependents,
    code_trace,
    blast_radius,
)

__all__ = [
    "Symbol",
    "Relationship",
    "FileIndex",
    "extract_file",
    "SUPPORTED_LANGUAGES",
    "index_workspace",
    "index_file",
    "code_structure",
    "code_dependents",
    "code_trace",
    "blast_radius",
]

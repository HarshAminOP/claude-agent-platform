"""Content deduplication utilities for the CAP knowledge base.

Provides fast, SQLite-backed deduplication for knowledge ingestion pipelines.
All lookups operate directly on the ``knowledge_entries`` table — no LanceDB
scans are performed, so the helpers remain efficient at 50 K+ entries.
"""

import hashlib
import sqlite3
from typing import Optional


def content_hash(text: str) -> str:
    """Return a 32-character SHA-256 hex digest of *text*.

    The truncated digest is a deliberate trade-off: 128 bits of entropy are
    sufficient for deduplication purposes while halving storage compared to
    the full 256-bit digest.

    Args:
        text: The raw string content to hash.

    Returns:
        A lowercase hexadecimal string of exactly 32 characters.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def fetch_existing_hashes(
    db: sqlite3.Connection,
    workspace: Optional[str] = None,
) -> set[str]:
    """Load all non-null content hashes from the knowledge store into a set.

    Materialising the hashes upfront enables O(1) membership tests during
    batch ingestion instead of issuing one query per candidate entry.

    Args:
        db: An open SQLite connection to the CAP knowledge database.
        workspace: If provided, restrict results to entries whose
            ``workspace`` column matches this value exactly.

    Returns:
        A :class:`set` of hex-digest strings (32 chars each).
    """
    if workspace is not None:
        cursor = db.execute(
            "SELECT content_hash FROM knowledge_entries"
            " WHERE content_hash IS NOT NULL AND workspace = ?",
            (workspace,),
        )
    else:
        cursor = db.execute(
            "SELECT content_hash FROM knowledge_entries"
            " WHERE content_hash IS NOT NULL",
        )

    return {row[0] for row in cursor.fetchall()}


def deduplicated_batch(
    entries: list[dict],
    existing_hashes: set[str],
) -> list[dict]:
    """Filter *entries* to those whose ``content_hash`` is not yet stored.

    Each entry dict is expected to carry a ``content_hash`` key (as produced
    by :func:`content_hash`).  Entries that lack the key entirely are treated
    as new (i.e. they are kept) because no hash is available to compare.

    Args:
        entries: Candidate entries destined for insertion.  Each element must
            be a :class:`dict`; the ``content_hash`` key is optional.
        existing_hashes: The set returned by :func:`fetch_existing_hashes`.

    Returns:
        A new list containing only entries whose hash is absent from
        *existing_hashes*.
    """
    new_entries: list[dict] = []
    for entry in entries:
        h = entry.get("content_hash")
        if h is None or h not in existing_hashes:
            new_entries.append(entry)
    return new_entries


def is_vector_exists(db: sqlite3.Connection, entry_uuid: str) -> bool:
    """Check whether *entry_uuid* has already been embedded.

    Queries the ``knowledge_entries`` table directly via SQLite rather than
    scanning LanceDB (which requires loading the entire table into a pandas
    DataFrame and is prohibitively slow at 50 K+ rows).

    Args:
        db: An open SQLite connection to the CAP knowledge database.
        entry_uuid: The UUID of the knowledge entry to check.

    Returns:
        ``True`` if a row for *entry_uuid* exists **and** its
        ``embedding_status`` column equals ``"embedded"``; ``False`` otherwise.
    """
    cursor = db.execute(
        "SELECT 1 FROM knowledge_entries"
        " WHERE uuid = ? AND embedding_status = 'embedded'"
        " LIMIT 1",
        (entry_uuid,),
    )
    return cursor.fetchone() is not None

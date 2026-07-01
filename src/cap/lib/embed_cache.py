"""Persistent SQLite-backed embedding cache for Titan V2 vectors.

Vectors are serialised as packed binary blobs using IEEE 754 single-precision
floats (struct format ``f``), matching the 1024-dimension Titan V2 output.
LRU eviction is based on ``accessed_at`` timestamp so the most recently used
entries survive a capacity-triggered sweep.
"""

import sqlite3
import struct
from datetime import datetime, timezone, timedelta
from typing import Optional

# Titan Text Embeddings V2 output dimension (fixed at 1024 for default config).
VECTOR_DIM: int = 1024

# struct format string — 1024 single-precision floats, ~4 KB per row.
_PACK_FMT: str = f"{VECTOR_DIM}f"

_DDL = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    vector       BLOB NOT NULL,
    created_at   TEXT NOT NULL,
    accessed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embedding_cache_accessed
    ON embedding_cache (accessed_at);
"""


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string (no microseconds)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PersistentEmbedCache:
    """LRU + TTL embedding cache persisted in a SQLite table.

    Args:
        db: An open :class:`sqlite3.Connection`.  The connection must have
            ``isolation_level`` set (or be in autocommit mode) appropriate for
            the caller's transaction discipline.  This class does **not** open
            or close the connection.
        max_size: Maximum number of rows to keep in the cache.  When a
            ``put`` would exceed this limit, the row with the oldest
            ``accessed_at`` value is evicted first.
        ttl_days: Rows not accessed within this many days are considered
            expired and removed by :meth:`evict_expired`.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        max_size: int = 1000,
        ttl_days: int = 7,
    ) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        if ttl_days < 1:
            raise ValueError(f"ttl_days must be >= 1, got {ttl_days}")

        self._db = db
        self._max_size = max_size
        self._ttl_days = ttl_days

        self._db.executescript(_DDL)
        self._db.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, content_hash: str) -> Optional[list[float]]:
        """Return the cached vector for *content_hash*, or ``None`` on a miss.

        On a cache hit the ``accessed_at`` timestamp is updated so that the
        entry is not the first candidate for LRU eviction.

        Args:
            content_hash: SHA-256 (or similar) hex digest identifying the
                source content.

        Returns:
            A list of :data:`VECTOR_DIM` floats, or ``None`` if the entry is
            not present.
        """
        row = self._db.execute(
            "SELECT vector FROM embedding_cache WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

        if row is None:
            return None

        blob: bytes = row[0]
        self._db.execute(
            "UPDATE embedding_cache SET accessed_at = ? WHERE content_hash = ?",
            (_utcnow(), content_hash),
        )
        self._db.commit()

        return list(struct.unpack(_PACK_FMT, blob))

    def put(self, content_hash: str, vector: list[float]) -> None:
        """Insert or replace *vector* for *content_hash*.

        If the cache is already at :attr:`max_size`, the entry with the oldest
        ``accessed_at`` value is removed before the insert.

        Args:
            content_hash: SHA-256 (or similar) hex digest of the source text.
            vector: Embedding vector.  Must have exactly :data:`VECTOR_DIM`
                elements; callers are responsible for ensuring this.

        Raises:
            struct.error: If *vector* contains non-float-compatible values.
        """
        if len(vector) != VECTOR_DIM:
            raise ValueError(
                f"vector must have {VECTOR_DIM} dimensions, got {len(vector)}"
            )

        blob: bytes = struct.pack(_PACK_FMT, *vector)

        # Check whether we need to evict before inserting a *new* entry.
        # An INSERT OR REPLACE on an existing key does not grow the table, so
        # we only evict when the key is not already present and the table is full.
        existing = self._db.execute(
            "SELECT 1 FROM embedding_cache WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

        if existing is None:
            (current_size,) = self._db.execute(
                "SELECT COUNT(*) FROM embedding_cache"
            ).fetchone()

            if current_size >= self._max_size:
                self._evict_lru()

        now = _utcnow()
        self._db.execute(
            """
            INSERT OR REPLACE INTO embedding_cache
                (content_hash, vector, created_at, accessed_at)
            VALUES (?, ?, ?, ?)
            """,
            (content_hash, blob, now, now),
        )
        self._db.commit()

    def evict_expired(self) -> int:
        """Delete all entries not accessed within the configured TTL.

        Returns:
            The number of rows deleted.
        """
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=self._ttl_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        cursor = self._db.execute(
            "DELETE FROM embedding_cache WHERE accessed_at < ?",
            (cutoff,),
        )
        self._db.commit()
        return cursor.rowcount

    def stats(self) -> dict:
        """Return a snapshot of cache metrics.

        Returns:
            A dict with the following keys:

            * ``size`` — current number of cached entries.
            * ``max_size`` — configured capacity limit.
            * ``oldest_accessed`` — ISO-8601 timestamp of the least-recently
              accessed entry, or ``None`` if the cache is empty.
            * ``ttl_days`` — configured TTL in days.
        """
        (size,) = self._db.execute(
            "SELECT COUNT(*) FROM embedding_cache"
        ).fetchone()

        row = self._db.execute(
            "SELECT MIN(accessed_at) FROM embedding_cache"
        ).fetchone()
        oldest_accessed: Optional[str] = row[0] if row else None

        return {
            "size": size,
            "max_size": self._max_size,
            "oldest_accessed": oldest_accessed,
            "ttl_days": self._ttl_days,
        }

    def purge_for_entry(self, content_hash: str) -> None:
        """Delete the cache entry for *content_hash* if it exists.

        This is the hook called by the knowledge store when the source
        ``knowledge_entry`` is deleted, ensuring stale vectors are not served
        for re-created content with the same hash.

        Args:
            content_hash: Identifier of the entry to remove.
        """
        self._db.execute(
            "DELETE FROM embedding_cache WHERE content_hash = ?",
            (content_hash,),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_lru(self) -> None:
        """Remove the single entry with the oldest ``accessed_at`` timestamp."""
        self._db.execute(
            """
            DELETE FROM embedding_cache
            WHERE content_hash = (
                SELECT content_hash
                FROM   embedding_cache
                ORDER  BY accessed_at ASC
                LIMIT  1
            )
            """
        )
        # Commit is deferred to the caller so LRU eviction + insert are atomic.

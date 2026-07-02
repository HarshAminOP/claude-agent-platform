"""Vector-based semantic pattern search for the CAP harness.

Stores pattern embeddings in LanceDB (via Bedrock Titan V2) and exposes
cosine-similarity search so that ``agentdb_pattern_search`` can find
semantically related patterns even without keyword overlap.

All public methods degrade gracefully: if Bedrock or LanceDB is unavailable
they return False / [] instead of raising.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("cap.harness.vector_patterns")

from cap.config import get_data_dir
VECTORS_DIR = get_data_dir() / "vectors"
TABLE_NAME = "harness_patterns"


def _run_async(coro):
    """Run an async coroutine synchronously.

    Uses the running event loop's ``run_until_complete`` if one exists and is
    not already running, otherwise falls back to ``asyncio.run``.
    """
    try:
        loop = asyncio.get_running_loop()
        # Running inside an existing event loop (e.g. Jupyter, some test
        # frameworks).  Use a fresh thread to avoid blocking the loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(coro)


class PatternEmbedder:
    """Manages LanceDB-backed vector storage for harness patterns.

    Lazy-initialised: the Bedrock client and LanceDB table are created on
    the first call that needs them.  If either dependency is unavailable,
    ``is_available`` returns False and all methods return empty/False values.
    """

    def __init__(self) -> None:
        self._embedder = None
        self._db = None
        self._table = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        try:
            from cap.lib.embeddings import EmbeddingClient
            self._embedder = EmbeddingClient()
            # Probe availability: None means never tested — treat as available
            # until we actually know otherwise.  We do NOT call embed_single
            # here to avoid an unnecessary Bedrock round-trip on every import.
            if self._embedder.is_available is False:
                logger.debug("PatternEmbedder: Bedrock unavailable, disabling")
                self._embedder = None
                return

            VECTORS_DIR.mkdir(parents=True, exist_ok=True)
            import lancedb
            self._db = lancedb.connect(str(VECTORS_DIR))
            try:
                self._table = self._db.open_table(TABLE_NAME)
                logger.debug("PatternEmbedder: opened existing table %s", TABLE_NAME)
            except Exception:
                self._table = None  # will be created on first embed
                logger.debug("PatternEmbedder: table %s not yet created", TABLE_NAME)
        except Exception as exc:
            logger.debug("PatternEmbedder: init failed: %s", exc)
            self._embedder = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Return True if both Bedrock and LanceDB are reachable."""
        self._init()
        return self._embedder is not None

    def embed_pattern(self, pattern_id: str, prompt_summary: str) -> bool:
        """Embed *prompt_summary* and store the vector under *pattern_id*.

        Args:
            pattern_id:     UUID hex that corresponds to patterns.id in SQLite.
            prompt_summary: Text to embed (will be truncated to 500 chars).

        Returns:
            True on success, False on any failure.
        """
        self._init()
        if not self._embedder:
            return False
        if not prompt_summary or not prompt_summary.strip():
            return False
        try:
            text = prompt_summary[:500]
            vector = _run_async(self._embedder.embed_single(text))
            if vector is None:
                return False

            data = [{"id": pattern_id, "vector": vector, "text": text}]

            if self._table is None:
                self._table = self._db.create_table(TABLE_NAME, data)
                logger.debug("PatternEmbedder: created table %s", TABLE_NAME)
            else:
                self._table.add(data)
            return True
        except Exception as exc:
            logger.debug("PatternEmbedder.embed_pattern failed: %s", exc)
            return False

    def search_similar(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.5,
    ) -> list[dict]:
        """Search for patterns semantically similar to *query*.

        Args:
            query:     Natural-language search string.
            limit:     Maximum number of results to return.
            min_score: Minimum cosine similarity score (0-1) to include.

        Returns:
            List of ``{pattern_id, score, text}`` dicts sorted by descending
            score.  Empty list on any failure or when the table is missing.
        """
        self._init()
        if not self._embedder or not self._table:
            return []
        if not query or not query.strip():
            return []
        try:
            vector = _run_async(self._embedder.embed_single(query))
            if vector is None:
                return []

            results = (
                self._table.search(vector)
                .metric("cosine")
                .limit(limit)
                .to_list()
            )

            hits = []
            for r in results:
                # LanceDB cosine distance: 0 = identical, 2 = opposite.
                # Convert to similarity in [0, 1].
                score = 1.0 - r.get("_distance", 0.5)
                if score >= min_score:
                    hits.append(
                        {
                            "pattern_id": r["id"],
                            "score": round(score, 4),
                            "text": r.get("text", ""),
                        }
                    )
            hits.sort(key=lambda h: h["score"], reverse=True)
            return hits
        except Exception as exc:
            logger.debug("PatternEmbedder.search_similar failed: %s", exc)
            return []

    def bulk_embed_missing(self, batch_size: int = 50) -> int:
        """Backfill embeddings for patterns that have no embedding_id yet.

        Reads up to *batch_size* rows from the patterns table where
        ``embedding_id IS NULL``, embeds them, and writes back the id.

        Args:
            batch_size: Maximum number of patterns to embed in one call.

        Returns:
            Number of patterns successfully embedded.
        """
        self._init()
        if not self._embedder:
            return 0
        try:
            from cap.harness.agentdb import _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT id, prompt_summary FROM patterns WHERE embedding_id IS NULL LIMIT ?",
                (batch_size,),
            ).fetchall()

            count = 0
            for row in rows:
                pid = row[0] if not hasattr(row, "__getitem__") or isinstance(row, tuple) else row[0]
                summary = row[1] if not hasattr(row, "__getitem__") or isinstance(row, tuple) else row[1]
                # Support both sqlite3.Row and plain tuple
                try:
                    pid = row["id"]
                    summary = row["prompt_summary"]
                except (TypeError, IndexError):
                    pid, summary = row[0], row[1]

                if self.embed_pattern(pid, summary or ""):
                    conn.execute(
                        "UPDATE patterns SET embedding_id = ? WHERE id = ?",
                        (pid, pid),
                    )
                    count += 1

            if count:
                conn.commit()
            conn.close()
            logger.debug("PatternEmbedder.bulk_embed_missing: embedded %d patterns", count)
            return count
        except Exception as exc:
            logger.debug("PatternEmbedder.bulk_embed_missing failed: %s", exc)
            return 0

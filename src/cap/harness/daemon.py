"""CAP background maintenance daemon.

Runs periodic housekeeping tasks:
  1. Knowledge consolidation  (expire, dedup, requeue failed embeddings)
  2. Stale agent cleanup       (terminate agents idle > max_age_hours)
  3. Pattern embedding         (bulk-embed un-embedded patterns if available)
  4. Learning threshold update (correlate session events → routing outcomes)
  5. Pattern retention         (prune stale patterns if retention module exists)
  6. Manifest refresh          (rewrite .cap-manifest in cwd)
"""

from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.daemon")


class CapDaemon:
    """Periodic maintenance worker for the CAP platform."""

    def __init__(self, interval_seconds: int = 21600) -> None:
        self.interval = interval_seconds
        self.running = False
        self._last_run: Optional[dict] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _data_dir() -> Path:
        """Return the configured platform data directory."""
        from cap.lib.config import load_config
        return load_config().data_dir

    # ------------------------------------------------------------------
    # Individual task runners (each returns a result dict)
    # ------------------------------------------------------------------

    def _run_consolidation(self) -> dict:
        try:
            from cap.lib.consolidator import consolidate
            from cap.lib.db_init import init_knowledge_db
            db = init_knowledge_db(self._data_dir())
            r = consolidate(db)
            return {"expired": r.expired_deleted, "deduped": r.duplicates_removed}
        except Exception as exc:
            logger.warning("consolidation failed: %s", exc)
            return {"error": str(exc)}

    def _run_stale_cleanup(self) -> dict:
        try:
            from cap.harness.agent_store import cleanup_stale
            count = cleanup_stale(max_age_hours=24)
            return {"terminated": count}
        except Exception as exc:
            logger.warning("stale agent cleanup failed: %s", exc)
            return {"error": str(exc)}

    def _run_pattern_embedding(self) -> dict:
        try:
            from cap.harness.vector_patterns import PatternEmbedder
            pe = PatternEmbedder()
            if pe.is_available:
                count = pe.bulk_embed_missing(batch_size=50)
                return {"embedded": count}
            return {"skipped": "embedder_unavailable"}
        except Exception as exc:
            logger.warning("pattern embedding failed: %s", exc)
            return {"error": str(exc)}

    def _run_learning(self) -> dict:
        try:
            from cap.learning.engine import compute_thresholds_from_session_events
            from cap.lib.db_init import init_sessions_db, init_knowledge_db
            data_dir = self._data_dir()
            sdb = init_sessions_db(data_dir)
            # routing_db lives in knowledge.db (routing_decisions table)
            pdb = init_knowledge_db(data_dir)
            r = compute_thresholds_from_session_events(sdb, pdb)
            return r
        except Exception as exc:
            logger.warning("learning threshold update failed: %s", exc)
            return {"error": str(exc)}

    def _run_retention(self) -> dict:
        try:
            from cap.harness.retention import prune_stale_patterns  # type: ignore[import]
            pruned = prune_stale_patterns()
            return {"pruned": pruned}
        except ImportError:
            return {"skipped": "retention_module_unavailable"}
        except Exception as exc:
            logger.warning("pattern retention failed: %s", exc)
            return {"error": str(exc)}

    def _run_manifest(self) -> dict:
        try:
            from cap.harness.governance import write_manifest
            write_manifest(Path.cwd())
            return {"refreshed": True}
        except Exception as exc:
            logger.warning("manifest refresh failed: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        """Execute all maintenance tasks once and return a summary dict."""
        results: dict = {}

        results["consolidation"] = self._run_consolidation()
        results["stale_agents"] = self._run_stale_cleanup()
        results["pattern_embedding"] = self._run_pattern_embedding()
        results["learning"] = self._run_learning()
        results["retention"] = self._run_retention()
        results["manifest"] = self._run_manifest()

        self._last_run = results
        logger.info("Daemon run complete: %s", json.dumps(results, default=str))
        return results

    def start(self) -> None:
        """Run maintenance in a loop until SIGTERM/SIGINT."""
        self.running = True
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        logger.info("Daemon starting (interval=%ds)", self.interval)

        while self.running:
            self.run_once()
            # Sleep in short chunks so we can respond to the stop flag quickly.
            elapsed = 0
            while elapsed < self.interval and self.running:
                time.sleep(min(10, self.interval - elapsed))
                elapsed += 10

    @property
    def last_run(self) -> Optional[dict]:
        """Result dict from the most recent run_once() call, or None."""
        return self._last_run

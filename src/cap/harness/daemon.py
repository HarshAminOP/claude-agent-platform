"""CAP Platform Daemon — long-running asyncio background operator.

Runs periodic housekeeping tasks at independent intervals:
  1. health_check()       every 60s  — check MCP server PIDs, restart crashed ones
  2. budget_check()       every 5min — check spend vs limit, pause if exceeded
  3. reembed_patterns()   every 30min — embed patterns missing embeddings
  4. cleanup_stale()      every 1hr  — terminate idle agents (>24h)
  5. compact_vectors()    every 6hr  — optimize LanceDB, prune low-retention patterns
  6. workspace_detect()   every 60s  — watch pending_workspaces for new paths

Entry point: python -m cap.harness.daemon
PID file: ~/.claude-platform/run/daemon.pid
Log file: ~/.claude-platform/logs/daemon.log (rotating, 10MB, 3 backups)

CRITICAL: The daemon is OPTIONAL. CAP works without it. It is for optimization only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.harness.daemon")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _cap_home() -> Path:
    from cap.config import get_cap_home
    return get_cap_home()


def _pid_path() -> Path:
    return _cap_home() / "run" / "daemon.pid"


def _log_path() -> Path:
    return _cap_home() / "logs" / "daemon.log"


def _pending_workspaces_path() -> Path:
    return _cap_home() / "run" / "pending_workspaces"


# ---------------------------------------------------------------------------
# Task intervals (seconds)
# ---------------------------------------------------------------------------

INTERVAL_HEALTH_CHECK = 60
INTERVAL_BUDGET_CHECK = 300
INTERVAL_REEMBED = 1800
INTERVAL_CLEANUP_STALE = 3600
INTERVAL_COMPACT_VECTORS = 21600
INTERVAL_WORKSPACE_DETECT = 60
INTERVAL_WORKSPACE_SYNC = 300        # 5 min — respects per-workspace sync_frequency
INTERVAL_DB_COMPACTION = 21600       # 6 hr  — VACUUM + dedup + prune stale entries


def _parse_frequency_seconds(freq: str) -> int:
    """Convert a human-readable frequency string to seconds.

    Supported suffixes: s (seconds), m (minutes), h (hours), d (days).
    Defaults to 300 seconds on any parse error.

    Args:
        freq: Frequency string such as ``"5m"``, ``"1h"``, ``"30s"``.

    Returns:
        Integer number of seconds.
    """
    freq = freq.strip().lower()
    try:
        if freq.endswith("d"):
            return int(freq[:-1]) * 86400
        if freq.endswith("h"):
            return int(freq[:-1]) * 3600
        if freq.endswith("m"):
            return int(freq[:-1]) * 60
        if freq.endswith("s"):
            return int(freq[:-1])
        return int(freq)
    except (ValueError, IndexError):
        return 300


# ---------------------------------------------------------------------------
# CapDaemon — asyncio-based long-running process
# ---------------------------------------------------------------------------

class CapDaemon:
    """Async background operator for the CAP platform.

    Schedules independent periodic tasks using asyncio. Each task runs
    at its own interval and failures in one task do not affect others.
    """

    def __init__(self, interval_seconds: int = 21600) -> None:
        # Legacy compat: interval_seconds maps to compact_vectors interval
        self.interval = interval_seconds
        self.running = False
        self._last_run: Optional[dict] = None
        self._start_time: Optional[float] = None
        self._last_health_check: Optional[str] = None
        self._server_count: int = 0
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _data_dir() -> Path:
        """Return the configured platform data directory."""
        try:
            from cap.lib.config import load_config
            return load_config().data_dir
        except Exception:
            return _cap_home() / "data"

    @staticmethod
    def _cap_home() -> Path:
        return _cap_home()

    # ------------------------------------------------------------------
    # Task 1: Health Check (every 60s)
    # ------------------------------------------------------------------

    def health_check(self) -> dict:
        """Check MCP server PIDs, restart crashed ones."""
        results: dict = {"checked": 0, "alive": 0, "restarted": 0, "errors": []}
        try:
            from cap.lib.db_init import init_fleet_db
            db = init_fleet_db(self._data_dir())
            rows = db.execute(
                "SELECT name, pid, status, command, restart_count, max_restarts "
                "FROM fleet_servers WHERE status IN ('running', 'registered')"
            ).fetchall()

            now = datetime.now(timezone.utc).isoformat()

            for row in rows:
                name, pid, status, command, restart_count, max_restarts = row
                results["checked"] += 1

                if not pid:
                    continue

                try:
                    os.kill(pid, 0)
                    results["alive"] += 1
                except (ProcessLookupError, PermissionError):
                    # Process is dead
                    if status == "running":
                        max_r = max_restarts or 5
                        rc = restart_count or 0
                        if rc < max_r and command:
                            # Attempt restart
                            try:
                                import subprocess
                                parts = command.split()
                                proc = subprocess.Popen(
                                    parts,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True,
                                )
                                db.execute(
                                    "UPDATE fleet_servers SET pid = ?, restart_count = ?, "
                                    "last_health_check = ? WHERE name = ?",
                                    (proc.pid, rc + 1, now, name),
                                )
                                db.execute(
                                    "INSERT INTO fleet_events (server_name, event_type, message) "
                                    "VALUES (?, 'restarted', ?)",
                                    (name, f"Daemon auto-restart (attempt {rc + 1})"),
                                )
                                results["restarted"] += 1
                                logger.info("Restarted MCP server %s (PID %d)", name, proc.pid)
                            except Exception as e:
                                results["errors"].append(f"restart_{name}: {e}")
                                db.execute(
                                    "UPDATE fleet_servers SET status = 'stopped', "
                                    "last_health_check = ? WHERE name = ?",
                                    (now, name),
                                )
                        else:
                            db.execute(
                                "UPDATE fleet_servers SET status = 'stopped', "
                                "last_health_check = ? WHERE name = ?",
                                (now, name),
                            )
                    else:
                        db.execute(
                            "UPDATE fleet_servers SET last_health_check = ? WHERE name = ?",
                            (now, name),
                        )

            db.commit()
            self._server_count = results["checked"]
            self._last_health_check = now
        except Exception as exc:
            results["errors"].append(str(exc))
            logger.warning("health_check failed: %s", exc)

        return results

    # ------------------------------------------------------------------
    # Task 2: Budget Check (every 5 min)
    # ------------------------------------------------------------------

    def budget_check(self) -> dict:
        """Check spend vs daily limit. Pause if exceeded."""
        try:
            from cap.lib.harness_config import load_harness_config
            from cap.lib.budget_manager import (
                init_budget_log_table, get_today_spend, is_budget_paused, pause_budget,
            )
            import sqlite3

            harness_cfg = load_harness_config()
            budget_cfg = harness_cfg.get("budget", {})
            daily_limit = budget_cfg.get("daily_limit_usd", 5.0)

            data_dir = self._data_dir()
            db = sqlite3.connect(str(data_dir / "platform.db"))
            db.execute("PRAGMA busy_timeout=2000")
            init_budget_log_table(db)

            spend_info = get_today_spend(db)
            today_spend = spend_info["total_spend_usd"]
            paused = is_budget_paused()

            result = {
                "daily_limit_usd": daily_limit,
                "today_spend_usd": today_spend,
                "percentage": round(today_spend / max(daily_limit, 0.01) * 100, 1),
                "paused": paused,
                "action": "none",
            }

            if today_spend >= daily_limit and not paused:
                pause_budget(db)
                result["action"] = "paused"
                logger.warning(
                    "Budget exceeded: $%.4f / $%.2f. Operations paused.",
                    today_spend, daily_limit,
                )

            db.close()
            return result
        except Exception as exc:
            logger.warning("budget_check failed: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Task 3: Re-embed Patterns + Intelligent Re-index (every 30 min)
    # ------------------------------------------------------------------

    def reembed_patterns(self) -> dict:
        """Incrementally reindex repos that changed + embed missing patterns.

        Runs IntelligentIndexer in incremental mode (skips repos whose HEAD
        SHA is unchanged since last index).  Respects ``indexing.reindex_interval_minutes``
        from the harness config (default 360 min).  Stops early when the daily
        budget would be exceeded.

        Also embeds any patterns that are still missing embeddings after the
        indexing run completes.
        """
        results: dict = {"embedded": 0, "repos_indexed": 0, "files_indexed": 0}

        # ── Budget guard ──────────────────────────────────────────────────
        try:
            from cap.lib.budget_manager import is_budget_paused
            if is_budget_paused():
                logger.info("reembed_patterns: daily budget paused — skipping intelligent reindex")
                results["skipped"] = "budget_paused"
                return results
        except Exception as exc:
            logger.debug("Budget check failed: %s — proceeding without guard", exc)

        # ── Intelligent re-index (incremental, no LLM calls) ─────────────
        try:
            import asyncio
            from cap.lib.intelligent_indexer import IntelligentIndexer, IndexerConfig
            from cap.lib.harness_config import get_indexing_config

            indexing_cfg = get_indexing_config()
            local_paths: list[str] = indexing_cfg.get("local_paths", [])
            reindex_interval_minutes: int = int(
                indexing_cfg.get("reindex_interval_minutes", 360)
            )

            if not local_paths:
                logger.debug("reembed_patterns: no indexing.local_paths configured — skipping")
                results["skipped"] = "no_paths_configured"
            else:
                indexer_config = IndexerConfig(
                    workspace_roots=local_paths,
                    full_reindex=False,
                    incremental=True,            # skip repos whose SHA is unchanged
                    skip_llm_analysis=False,     # allow LLM analysis on changed repos
                    skip_embedding=False,        # generate embeddings for new entries
                    daemon_interval_minutes=reindex_interval_minutes,
                )
                indexer = IntelligentIndexer(indexer_config)
                try:
                    stats = asyncio.run(indexer.run_incremental())
                    results["repos_indexed"] = stats.repos_analyzed
                    results["files_indexed"] = stats.files_indexed
                    results["embeddings_generated"] = stats.embeddings_generated
                    results["llm_cost_usd"] = round(stats.llm_cost_usd, 5)
                    if stats.errors:
                        results["errors"] = stats.errors[:10]
                    logger.info(
                        "Intelligent reindex: repos_analyzed=%d files=%d embeddings=%d cost=$%.4f",
                        stats.repos_analyzed,
                        stats.files_indexed,
                        stats.embeddings_generated,
                        stats.llm_cost_usd,
                    )
                finally:
                    indexer.close()
        except Exception as exc:
            logger.warning("reembed_patterns: intelligent reindex failed: %s", exc)
            results["indexer_error"] = str(exc)

        # ── Embed patterns still missing vectors ──────────────────────────
        try:
            from cap.harness.vector_patterns import PatternEmbedder
            pe = PatternEmbedder()
            if pe.is_available:
                count = pe.bulk_embed_missing(batch_size=50)
                results["embedded"] = count
            else:
                results["embedder"] = "unavailable"
        except Exception as exc:
            logger.warning("reembed_patterns: PatternEmbedder failed: %s", exc)
            results["embedder_error"] = str(exc)

        return results

    # ------------------------------------------------------------------
    # Task 4: Cleanup Stale Agents (every 1 hr)
    # ------------------------------------------------------------------

    def cleanup_stale(self) -> dict:
        """Terminate agents idle for more than 24 hours."""
        try:
            from cap.harness.agent_store import cleanup_stale
            count = cleanup_stale(max_age_hours=24)
            return {"terminated": count}
        except Exception as exc:
            logger.warning("cleanup_stale failed: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Task 5: Compact Vectors (every 6 hr)
    # ------------------------------------------------------------------

    def compact_vectors(self) -> dict:
        """Optimize LanceDB and prune low-retention patterns."""
        results: dict = {"compacted": False, "pruned": 0}
        try:
            # Attempt LanceDB compaction
            vectors_dir = self._data_dir() / "vectors"
            if vectors_dir.exists():
                try:
                    import lancedb
                    lance_db = lancedb.connect(str(vectors_dir))
                    table_names = lance_db.table_names()
                    for tname in table_names:
                        try:
                            t = lance_db.open_table(tname)
                            t.compact_files()
                            results["compacted"] = True
                        except Exception:
                            pass
                except ImportError:
                    results["compacted"] = False

            # Prune low-retention patterns
            try:
                from cap.harness.retention import prune_stale_patterns
                pruned = prune_stale_patterns()
                results["pruned"] = pruned
            except (ImportError, Exception):
                pass

            return results
        except Exception as exc:
            logger.warning("compact_vectors failed: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Task 6: Workspace Detection (every 60s)
    # ------------------------------------------------------------------

    def workspace_detect(self) -> dict:
        """Watch pending_workspaces file and trigger background index."""
        results: dict = {"new_workspaces": 0, "indexed": 0}
        try:
            pending_path = _pending_workspaces_path()
            if not pending_path.exists():
                return results

            content = pending_path.read_text().strip()
            if not content:
                return results

            paths = [p.strip() for p in content.splitlines() if p.strip()]
            results["new_workspaces"] = len(paths)

            # Clear the file immediately
            pending_path.write_text("")

            # Check which paths need indexing
            from cap.lib.config import load_config
            from cap.lib.db_init import init_knowledge_db

            config = load_config()
            data_dir = config.data_dir
            db = init_knowledge_db(data_dir)

            for ws_path in paths:
                ws_path = os.path.abspath(os.path.expanduser(ws_path))
                if not os.path.isdir(ws_path):
                    continue

                # Check if already indexed
                existing = db.execute(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE workspace = ?",
                    (ws_path,),
                ).fetchone()[0]

                if existing == 0:
                    # Trigger background index
                    try:
                        from cap.lib.sync_engine import sync_workspace
                        sync_workspace(db, ws_path, full=False)
                        results["indexed"] += 1
                        logger.info("Indexed new workspace: %s", ws_path)
                    except Exception as e:
                        logger.warning("Failed to index workspace %s: %s", ws_path, e)

            return results
        except Exception as exc:
            logger.warning("workspace_detect failed: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Task 7: Workspace Sync (every 5 min, per-workspace frequency honoured)
    # ------------------------------------------------------------------

    def workspace_sync(self) -> dict:
        """Sync all registered workspaces whose sync_frequency has elapsed.

        Reads the workspace list from workspace_registry, compares each
        entry's ``last_synced`` timestamp against its ``sync_frequency``,
        and syncs those that are due.  Uses the existing sync_workspace
        logic so the result is identical to running ``cap sync``.

        Returns:
            dict with keys ``synced`` (count), ``skipped`` (count), and
            ``errors`` (list of error strings).
        """
        results: dict = {"synced": 0, "skipped": 0, "errors": []}

        try:
            from cap.lib.workspace_registry import list_workspaces, mark_workspace_synced
        except Exception as exc:
            logger.warning("workspace_sync: workspace_registry unavailable: %s", exc)
            results["errors"].append(str(exc))
            return results

        workspaces = list_workspaces()
        if not workspaces:
            logger.debug("workspace_sync: no workspaces registered")
            return results

        try:
            from cap.lib.config import load_config
            from cap.lib.db_init import init_knowledge_db
            from cap.lib.sync_engine import sync_workspace

            config = load_config()
            data_dir = config.data_dir
            db = init_knowledge_db(data_dir)
        except Exception as exc:
            logger.warning("workspace_sync: failed to open knowledge DB: %s", exc)
            results["errors"].append(str(exc))
            return results

        now = time.time()

        for ws in workspaces:
            ws_path = ws.get("path", "")
            if not ws_path or not os.path.isdir(ws_path):
                results["skipped"] += 1
                continue

            # Determine whether this workspace is due for a sync.
            freq_str = ws.get("sync_frequency", "5m")
            freq_secs = _parse_frequency_seconds(freq_str)

            last_synced_str = ws.get("last_synced")
            if last_synced_str:
                try:
                    dt = datetime.fromisoformat(last_synced_str.replace("Z", "+00:00"))
                    last_synced_ts = dt.timestamp()
                except (ValueError, TypeError):
                    last_synced_ts = 0.0
            else:
                last_synced_ts = 0.0

            elapsed = now - last_synced_ts
            if elapsed < freq_secs:
                results["skipped"] += 1
                logger.debug(
                    "workspace_sync: skipping %s (%.0fs < %.0fs freq)",
                    ws_path, elapsed, freq_secs,
                )
                continue

            try:
                sync_workspace(db, ws_path, full=False)
                mark_workspace_synced(ws_path)
                results["synced"] += 1
                logger.info("workspace_sync: synced %s", ws_path)
            except Exception as exc:
                results["errors"].append(f"{ws_path}: {exc}")
                logger.warning("workspace_sync: failed for %s: %s", ws_path, exc)

        return results

    # ------------------------------------------------------------------
    # Task 8: DB Compaction (every 6 hr)
    # ------------------------------------------------------------------

    def db_compaction(self) -> dict:
        """Run VACUUM on knowledge.db, deduplicate entries, prune stale files.

        Performs three operations:
        1. SQLite VACUUM to reclaim freed space and defragment the file.
        2. Deduplication: removes knowledge_entries whose source_path no
           longer exists on disk (stale file entries).
        3. Deduplication of content_hash duplicates keeping the newest row.

        Returns:
            dict with keys ``vacuumed``, ``stale_pruned``, ``deduped``,
            and ``errors``.
        """
        results: dict = {"vacuumed": False, "stale_pruned": 0, "deduped": 0, "errors": []}

        try:
            from cap.lib.config import load_config
            from cap.lib.db_init import init_knowledge_db

            config = load_config()
            data_dir = config.data_dir
            db = init_knowledge_db(data_dir)
        except Exception as exc:
            logger.warning("db_compaction: failed to open knowledge DB: %s", exc)
            results["errors"].append(str(exc))
            return results

        # Step 1: Prune stale entries (source_path no longer exists)
        try:
            rows = db.execute(
                "SELECT id, source_path FROM knowledge_entries "
                "WHERE source_path IS NOT NULL AND source_type = 'file'"
            ).fetchall()

            stale_ids: list[int] = []
            for row_id, source_path in rows:
                if source_path and not os.path.exists(source_path):
                    stale_ids.append(row_id)

            if stale_ids:
                placeholders = ",".join("?" * len(stale_ids))
                db.execute(
                    f"DELETE FROM knowledge_entries WHERE id IN ({placeholders})",
                    stale_ids,
                )
                # Also clean up the embedding queue for deleted entries.
                db.execute(
                    f"DELETE FROM embedding_queue WHERE entry_id IN ({placeholders})",
                    stale_ids,
                )
                db.commit()
                results["stale_pruned"] = len(stale_ids)
                logger.info("db_compaction: pruned %d stale file entries", len(stale_ids))
        except Exception as exc:
            logger.warning("db_compaction: stale prune failed: %s", exc)
            results["errors"].append(f"stale_prune: {exc}")

        # Step 2: Deduplicate content_hash — keep newest, remove older duplicates
        try:
            dupes = db.execute(
                """SELECT content_hash, COUNT(*) AS cnt
                   FROM knowledge_entries
                   WHERE content_hash IS NOT NULL
                   GROUP BY content_hash
                   HAVING cnt > 1"""
            ).fetchall()

            deduped = 0
            for content_hash, _ in dupes:
                # Keep the row with the highest id (most recently inserted)
                keep_id = db.execute(
                    "SELECT MAX(id) FROM knowledge_entries WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()[0]

                deleted = db.execute(
                    "DELETE FROM knowledge_entries WHERE content_hash = ? AND id != ?",
                    (content_hash, keep_id),
                ).rowcount
                deduped += deleted

            if deduped:
                db.commit()
            results["deduped"] = deduped
            logger.info("db_compaction: deduplicated %d entries", deduped)
        except Exception as exc:
            logger.warning("db_compaction: dedup failed: %s", exc)
            results["errors"].append(f"dedup: {exc}")

        # Step 3: VACUUM — must run outside a transaction
        try:
            db_path = str(data_dir / "knowledge.db")
            import sqlite3 as _sqlite3
            vacuum_conn = _sqlite3.connect(db_path, isolation_level=None)
            vacuum_conn.execute("VACUUM")
            vacuum_conn.close()
            results["vacuumed"] = True
            logger.info("db_compaction: VACUUM complete on %s", db_path)
        except Exception as exc:
            logger.warning("db_compaction: VACUUM failed: %s", exc)
            results["errors"].append(f"vacuum: {exc}")

        return results

    # ------------------------------------------------------------------
    # Legacy: run_once (backward compat)
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        """Execute all maintenance tasks once and return a summary dict.

        Retained for backward compatibility with existing CLI and tests.
        """
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

    # ------------------------------------------------------------------
    # Legacy internal task runners (backward compat for tests)
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
        return self.cleanup_stale()

    def _run_pattern_embedding(self) -> dict:
        """Legacy alias for reembed_patterns() — retained for backward compat."""
        return self.reembed_patterns()

    def _run_learning(self) -> dict:
        try:
            from cap.learning.engine import compute_thresholds_from_session_events
            from cap.lib.db_init import init_sessions_db, init_knowledge_db
            data_dir = self._data_dir()
            sdb = init_sessions_db(data_dir)
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
    # Async scheduling
    # ------------------------------------------------------------------

    async def _periodic_task(self, name: str, func, interval: int) -> None:
        """Run func every interval seconds, logging errors."""
        while self.running:
            try:
                result = await asyncio.get_event_loop().run_in_executor(None, func)
                logger.debug("Task %s completed: %s", name, result)
            except Exception as exc:
                logger.error("Task %s error: %s", name, exc)
            await asyncio.sleep(interval)

    async def _run_async(self) -> None:
        """Main async loop — schedules all periodic tasks."""
        self.running = True
        self._start_time = time.time()

        # Immediate health check on startup
        logger.info("Running initial health check...")
        self.health_check()

        # Schedule periodic tasks
        self._tasks = [
            asyncio.create_task(
                self._periodic_task("health_check", self.health_check, INTERVAL_HEALTH_CHECK)
            ),
            asyncio.create_task(
                self._periodic_task("budget_check", self.budget_check, INTERVAL_BUDGET_CHECK)
            ),
            asyncio.create_task(
                self._periodic_task("reembed_patterns", self.reembed_patterns, INTERVAL_REEMBED)
            ),
            asyncio.create_task(
                self._periodic_task("cleanup_stale", self.cleanup_stale, INTERVAL_CLEANUP_STALE)
            ),
            asyncio.create_task(
                self._periodic_task("compact_vectors", self.compact_vectors, INTERVAL_COMPACT_VECTORS)
            ),
            asyncio.create_task(
                self._periodic_task("workspace_detect", self.workspace_detect, INTERVAL_WORKSPACE_DETECT)
            ),
            asyncio.create_task(
                self._periodic_task("workspace_sync", self.workspace_sync, INTERVAL_WORKSPACE_SYNC)
            ),
            asyncio.create_task(
                self._periodic_task("db_compaction", self.db_compaction, INTERVAL_DB_COMPACTION)
            ),
        ]

        # Wait until stopped
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Run the daemon in the current process (blocking)."""
        self.running = True

        def _stop_handler(*_):
            self.running = False

        signal.signal(signal.SIGTERM, _stop_handler)
        signal.signal(signal.SIGINT, _stop_handler)

        logger.info("CAP Daemon starting (PID %d)", os.getpid())
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            pass
        logger.info("CAP Daemon stopped")

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self.running = False

    @property
    def last_run(self) -> Optional[dict]:
        """Result dict from the most recent run_once() call, or None."""
        return self._last_run

    @property
    def uptime_seconds(self) -> float:
        """Seconds since daemon started, or 0 if not started."""
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    def status_info(self) -> dict:
        """Return current daemon status for the CLI."""
        pid_path = _pid_path()
        pid = None
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
            except (ValueError, OSError):
                pass

        return {
            "pid": pid,
            "running": self.running,
            "uptime_seconds": self.uptime_seconds,
            "server_count": self._server_count,
            "last_health_check": self._last_health_check,
        }


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def write_pid() -> None:
    """Write current process PID to the daemon PID file."""
    pid_path = _pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def remove_pid() -> None:
    """Remove the daemon PID file."""
    pid_path = _pid_path()
    if pid_path.exists():
        pid_path.unlink(missing_ok=True)


def read_pid() -> Optional[int]:
    """Read the daemon PID from the PID file. Returns None if not found."""
    pid_path = _pid_path()
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def is_daemon_running() -> bool:
    """Check if the daemon process is alive."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Configure rotating file logging for the daemon."""
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root_logger = logging.getLogger("cap")
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    # Also log to stderr for foreground runs
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(stderr_handler)


# ---------------------------------------------------------------------------
# Entry point: python -m cap.harness.daemon
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the daemon process."""
    setup_logging()
    write_pid()
    logger.info("Daemon PID %d written to %s", os.getpid(), _pid_path())

    try:
        daemon = CapDaemon()
        daemon.start()
    finally:
        remove_pid()
        logger.info("Daemon PID file removed")


if __name__ == "__main__":
    main()

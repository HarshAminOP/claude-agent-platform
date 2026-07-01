"""Intelligent indexing pipeline orchestrator for the CAP knowledge base.

Ties together discovery, dependency resolution, LLM semantic analysis, and
knowledge graph construction into a single, phase-aware pipeline.  Each phase
is wrapped in an independent try/except so one failure never aborts the whole
run.  A progress callback is invoked at each phase boundary so callers can
surface live status to users.

Incremental mode (the default) compares each repo's current HEAD SHA against
the last-indexed SHA stored in sync_state and skips unchanged repos entirely.
Full reindex (full_reindex=True) bypasses that check.

Optional modules:
  - cap.lib.dependency_resolver   (DependencyResolver) — not yet released;
    silently skipped if absent.
  - cap.lib.knowledge_graph       (KnowledgeGraph)     — not yet released;
    silently skipped if absent.

Daemon mode: when daemon_mode=True, run_daemon() loops indefinitely, sleeping
for daemon_interval_minutes between incremental runs.  Pass an asyncio.Event
to stop it cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cap.lib.config import PlatformConfig, load_config
from cap.lib.db_init import init_knowledge_db
from cap.lib.code_understanding import CodeUnderstanding, UnderstandingConfig, BudgetExceeded
from cap.lib.embeddings import EmbeddingClient, EmbeddingConfig
from cap.lib.harness_config import get_embeddings_config, get_indexing_config, get_remotes
from cap.lib.repo_extractor import extract_and_index_repos
from cap.lib.sync_engine import sync_workspace

# Optional future modules — imported defensively so the indexer is usable
# before they land.
try:
    from cap.lib.dependency_resolver import DependencyResolver
    _DEPENDENCY_RESOLVER_AVAILABLE = True
except ImportError:
    _DEPENDENCY_RESOLVER_AVAILABLE = False

try:
    from cap.lib.knowledge_graph import KnowledgeGraph
    _KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    _KNOWLEDGE_GRAPH_AVAILABLE = False

logger = logging.getLogger("cap.intelligent_indexer")

# Source type written to sync_state for all records owned by this module.
_SYNC_SOURCE_TYPE = "intelligent_indexer"

# Representative "main" filenames to read for LLM context, tried in order.
_ENTRY_FILE_CANDIDATES = [
    "main.go",
    "main.py",
    "index.ts",
    "index.js",
    "app.py",
    "server.go",
    "cmd/main.go",
]

# Small manifest files to read in full for LLM context.
_MANIFEST_FILE_CANDIDATES = [
    "Chart.yaml",
    "go.mod",
    "pyproject.toml",
    "package.json",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class IndexerConfig:
    """Configuration for the IntelligentIndexer pipeline.

    Attributes:
        workspace_roots: Absolute paths to scan for repos.
        full_reindex: When True, ignore change tracking and reindex everything.
        skip_llm_analysis: Skip LLM semantic analysis — runs deps + graph only.
        skip_embedding: Skip embedding generation for new/updated entries.
        max_repos: Safety limit on how many repos are processed per run.
        incremental: Only process repos whose HEAD SHA changed since last run.
        parallel_analysis: Maximum concurrent LLM calls during analysis phase.
        budget_limit_usd: Per-run LLM budget; raises BudgetExceeded when hit.
        include_file_level: Also run file-level sync after repo-level indexing.
        daemon_mode: When True, run_daemon() loops on a periodic schedule.
        daemon_interval_minutes: Sleep interval between daemon runs.
    """

    workspace_roots: list[str] = field(default_factory=list)
    full_reindex: bool = False
    skip_llm_analysis: bool = False
    skip_embedding: bool = False
    max_repos: int = 100
    incremental: bool = True
    parallel_analysis: int = 3
    budget_limit_usd: float = 2.0
    include_file_level: bool = True
    daemon_mode: bool = False
    daemon_interval_minutes: int = 60


@dataclass
class IndexerStats:
    """Accumulated statistics for a single indexing run.

    Attributes:
        started_at: ISO-8601 UTC timestamp when the run started.
        completed_at: ISO-8601 UTC timestamp when the run finished.
        duration_seconds: Total elapsed wall-clock time.
        repos_discovered: Repos found across all workspace_roots.
        repos_changed: Repos that had changes and were queued for reindex.
        repos_analyzed: Repos successfully processed by LLM analysis.
        dependencies_resolved: Cross-repo dependency edges resolved.
        dependencies_unresolved: Dependencies that could not be resolved.
        graph_nodes_created: New nodes added to the knowledge graph.
        graph_edges_created: New edges added to the knowledge graph.
        files_indexed: Files processed by file-level sync.
        embeddings_generated: Embeddings generated and stored.
        llm_cost_usd: Estimated USD cost of LLM calls in this run.
        errors: Non-fatal errors encountered per phase.
        phase_durations: Elapsed seconds keyed by phase name.
    """

    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    repos_discovered: int = 0
    repos_changed: int = 0
    repos_analyzed: int = 0
    dependencies_resolved: int = 0
    dependencies_unresolved: int = 0
    graph_nodes_created: int = 0
    graph_edges_created: int = 0
    files_indexed: int = 0
    embeddings_generated: int = 0
    llm_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    phase_durations: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


class IntelligentIndexer:
    """Full intelligent indexing pipeline orchestrator.

    Initialise once and call run() (or run_sync()) to execute the pipeline.
    The instance manages its own SQLite connection which is opened in __init__
    and closed via close() or __del__.

    Usage::

        config = IndexerConfig(workspace_roots=["/path/to/workspace"])
        indexer = IntelligentIndexer(config)
        stats = indexer.run_sync(
            progress_callback=lambda msg, cur, tot: print(f"[{cur}/{tot}] {msg}")
        )
        indexer.close()
    """

    def __init__(
        self,
        config: Optional[IndexerConfig] = None,
        platform_config: Optional[PlatformConfig] = None,
    ) -> None:
        """Initialise the indexer and open the knowledge DB.

        Args:
            config: Pipeline configuration; defaults to IndexerConfig().
            platform_config: Platform config; loaded from disk when None.
        """
        self._config = config or IndexerConfig()
        self._platform_config = platform_config or load_config()

        # Merge workspace_roots from indexing config if none were passed explicitly.
        if not self._config.workspace_roots:
            try:
                indexing_cfg = get_indexing_config()
                configured_paths = indexing_cfg.get("local_paths", [])
                if configured_paths:
                    self._config.workspace_roots = [
                        str(Path(p).expanduser().resolve()) for p in configured_paths
                    ]
                    logger.info(
                        "Loaded %d workspace_roots from indexing config",
                        len(self._config.workspace_roots),
                    )
            except Exception as exc:
                logger.debug("Could not load indexing config: %s", exc)

        # Load remote configurations for auto-clone support.
        self._remotes: list[dict] = []
        try:
            self._remotes = get_remotes()
        except Exception as exc:
            logger.debug("Could not load remotes config: %s", exc)

        # Resolve clone_base_path for remote repos.
        try:
            indexing_cfg = get_indexing_config()
            self._clone_base_path = Path(
                indexing_cfg.get("clone_base_path", "~/.claude-platform/repos")
            ).expanduser()
        except Exception:
            self._clone_base_path = Path.home() / ".claude-platform" / "repos"

        # Open the shared knowledge.db connection for this run.
        self._db: sqlite3.Connection = init_knowledge_db(
            self._platform_config.data_dir
        )

        # LLM analysis client (lazily skipped when skip_llm_analysis=True).
        self._code_understanding: Optional[CodeUnderstanding] = None
        if not self._config.skip_llm_analysis:
            understanding_cfg = UnderstandingConfig(
                budget_limit_usd=self._config.budget_limit_usd,
                max_concurrent=self._config.parallel_analysis,
            )
            self._code_understanding = CodeUnderstanding(understanding_cfg)

        # Embedding client (lazily skipped when skip_embedding=True).
        self._embedding_client: Optional[EmbeddingClient] = None
        if not self._config.skip_embedding:
            try:
                emb_cfg = get_embeddings_config()
                embed_config = EmbeddingConfig(
                    model_id=emb_cfg.get("model_id", "amazon.titan-embed-text-v2:0"),
                    dimensions=emb_cfg.get("dimensions", 1024),
                    region=emb_cfg.get("region", "us-east-1"),
                    profile=emb_cfg.get("profile"),
                )
                self._embedding_client = EmbeddingClient(config=embed_config)
            except Exception as exc:
                logger.warning("EmbeddingClient init from config failed, using defaults: %s", exc)
                self._embedding_client = EmbeddingClient()

        # Optional: dependency resolver (module may not exist yet).
        self._dependency_resolver = None
        if _DEPENDENCY_RESOLVER_AVAILABLE:
            try:
                self._dependency_resolver = DependencyResolver(self._db)
            except Exception as exc:
                logger.warning("DependencyResolver init failed: %s", exc)

        # Optional: knowledge graph wrapper (module may not exist yet).
        self._knowledge_graph = None
        if _KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                self._knowledge_graph = KnowledgeGraph(self._db)
            except Exception as exc:
                logger.warning("KnowledgeGraph init failed: %s", exc)

        logger.info(
            "IntelligentIndexer ready: workspaces=%d remotes=%d llm=%s embed=%s",
            len(self._config.workspace_roots),
            len(self._remotes),
            not self._config.skip_llm_analysis,
            not self._config.skip_embedding,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> IndexerStats:
        """Execute the full indexing pipeline.

        The pipeline runs eight sequential phases.  Each phase records its
        elapsed time and any non-fatal errors in the returned IndexerStats.

        Args:
            progress_callback: Called as callback(message, current, total) at
                each phase boundary.  current and total reflect phase progress.

        Returns:
            IndexerStats populated with counts and diagnostics for this run.
        """
        stats = IndexerStats()
        stats.started_at = _utcnow()
        run_start = _monotonic()

        _progress(progress_callback, "Starting intelligent indexing pipeline", 0, 8)

        # ── Phase 1: Discovery ───────────────────────────────────────────────
        phase_start = _monotonic()
        sources = []
        try:
            sources = await self._phase_discovery(stats)
        except Exception as exc:
            stats.errors.append(f"discovery: {exc}")
            logger.error("Discovery phase failed: %s", exc, exc_info=True)
        stats.phase_durations["discovery"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Discovered {stats.repos_discovered} repos across "
            f"{len(self._config.workspace_roots)} workspaces",
            1, 8,
        )

        # ── Phase 2: Change detection ────────────────────────────────────────
        phase_start = _monotonic()
        changed_sources = sources
        try:
            changed_sources = self._phase_change_detection(sources, stats)
        except Exception as exc:
            stats.errors.append(f"change_detection: {exc}")
            logger.error("Change detection phase failed: %s", exc, exc_info=True)
        stats.phase_durations["change_detection"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Change detection: {stats.repos_changed} repos require reindexing",
            2, 8,
        )

        # ── Phase 3: Dependency resolution ──────────────────────────────────
        phase_start = _monotonic()
        try:
            await self._phase_dependency_resolution(changed_sources, stats)
        except Exception as exc:
            stats.errors.append(f"dependency_resolution: {exc}")
            logger.error("Dependency resolution phase failed: %s", exc, exc_info=True)
        stats.phase_durations["dependency_resolution"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Resolved {stats.dependencies_resolved} dependencies "
            f"({stats.dependencies_unresolved} unresolved)",
            3, 8,
        )

        # ── Phase 4: Repo extraction ─────────────────────────────────────────
        phase_start = _monotonic()
        try:
            self._phase_repo_extraction(stats)
        except Exception as exc:
            stats.errors.append(f"repo_extraction: {exc}")
            logger.error("Repo extraction phase failed: %s", exc, exc_info=True)
        stats.phase_durations["repo_extraction"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Extracted {stats.graph_nodes_created} repo summaries",
            4, 8,
        )

        # ── Phase 5: LLM analysis ────────────────────────────────────────────
        phase_start = _monotonic()
        if not self._config.skip_llm_analysis and self._code_understanding is not None:
            try:
                await self._phase_llm_analysis(changed_sources, stats)
            except BudgetExceeded as exc:
                stats.errors.append(f"llm_analysis: budget exceeded — {exc}")
                logger.warning("LLM analysis stopped: %s", exc)
            except Exception as exc:
                stats.errors.append(f"llm_analysis: {exc}")
                logger.error("LLM analysis phase failed: %s", exc, exc_info=True)
        stats.phase_durations["llm_analysis"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Analyzed {stats.repos_analyzed} repos via LLM "
            f"(${stats.llm_cost_usd:.2f} cost)",
            5, 8,
        )

        # ── Phase 6: File-level sync ─────────────────────────────────────────
        phase_start = _monotonic()
        if self._config.include_file_level:
            try:
                self._phase_file_sync(stats)
            except Exception as exc:
                stats.errors.append(f"file_sync: {exc}")
                logger.error("File sync phase failed: %s", exc, exc_info=True)
        stats.phase_durations["file_sync"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Indexed {stats.files_indexed} files",
            6, 8,
        )

        # ── Phase 7: Embedding generation ───────────────────────────────────
        phase_start = _monotonic()
        if not self._config.skip_embedding and self._embedding_client is not None:
            try:
                await self._phase_embedding(stats)
            except Exception as exc:
                stats.errors.append(f"embedding: {exc}")
                logger.error("Embedding phase failed: %s", exc, exc_info=True)
        stats.phase_durations["embedding"] = _monotonic() - phase_start

        _progress(
            progress_callback,
            f"Generated {stats.embeddings_generated} embeddings",
            7, 8,
        )

        # ── Phase 8: Update tracking ─────────────────────────────────────────
        phase_start = _monotonic()
        try:
            self._phase_update_tracking(changed_sources)
        except Exception as exc:
            stats.errors.append(f"update_tracking: {exc}")
            logger.error("Update tracking phase failed: %s", exc, exc_info=True)
        stats.phase_durations["update_tracking"] = _monotonic() - phase_start

        stats.completed_at = _utcnow()
        stats.duration_seconds = round(_monotonic() - run_start, 3)

        _progress(
            progress_callback,
            f"Indexing complete in {stats.duration_seconds:.1f}s "
            f"({len(stats.errors)} errors)",
            8, 8,
        )

        logger.info(
            "IntelligentIndexer run complete: repos_discovered=%d repos_changed=%d "
            "repos_analyzed=%d files_indexed=%d embeddings=%d cost=$%.3f duration=%.1fs errors=%d",
            stats.repos_discovered,
            stats.repos_changed,
            stats.repos_analyzed,
            stats.files_indexed,
            stats.embeddings_generated,
            stats.llm_cost_usd,
            stats.duration_seconds,
            len(stats.errors),
        )
        return stats

    def run_sync(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> IndexerStats:
        """Synchronous wrapper around run().

        Args:
            progress_callback: Forwarded to run().

        Returns:
            IndexerStats from the completed run.
        """
        return asyncio.run(self.run(progress_callback))

    async def run_incremental(
        self,
        changed_paths: Optional[list[str]] = None,
    ) -> IndexerStats:
        """Lightweight incremental reindex of specific changed files or repos.

        When changed_paths is provided, the pipeline resolves the affected
        repo roots from those file paths and limits the run to only those
        repos.  When changed_paths is None, falls back to git-diff-based
        change detection over all workspace_roots (same as run() with
        incremental=True).

        Args:
            changed_paths: Absolute file paths that changed.  If None, full
                git-diff-based change detection is used instead.

        Returns:
            IndexerStats for this incremental run.
        """
        if changed_paths:
            # Resolve affected repo roots from the changed file paths.
            affected_roots = _repos_from_paths(
                changed_paths, self._config.workspace_roots
            )
            original_roots = self._config.workspace_roots
            self._config.workspace_roots = list(affected_roots)
            try:
                return await self.run()
            finally:
                self._config.workspace_roots = original_roots
        else:
            # Standard incremental: git-diff-based, full_reindex stays False.
            saved_full = self._config.full_reindex
            self._config.full_reindex = False
            try:
                return await self.run()
            finally:
                self._config.full_reindex = saved_full

    def get_status(self) -> dict:
        """Return current indexer state suitable for display or monitoring.

        Reads from sync_state and graph node/edge counts.

        Returns:
            Dict with keys: last_run_at, repos_indexed, total_cost_usd,
            stale_nodes_count, graph_nodes, graph_edges, errors.
        """
        status: dict = {
            "last_run_at": None,
            "repos_indexed": 0,
            "graph_nodes": 0,
            "graph_edges": 0,
            "stale_nodes_count": 0,
            "errors": [],
        }
        try:
            row = self._db.execute(
                """
                SELECT last_sync_at, file_count, error
                FROM   sync_state
                WHERE  source_type = ?
                ORDER  BY last_sync_at DESC
                LIMIT  1
                """,
                (_SYNC_SOURCE_TYPE,),
            ).fetchone()
            if row:
                status["last_run_at"] = row[0]
                status["repos_indexed"] = row[1] or 0
                if row[2]:
                    status["errors"].append(row[2])
        except Exception as exc:
            status["errors"].append(f"sync_state query: {exc}")

        try:
            (status["graph_nodes"],) = self._db.execute(
                "SELECT COUNT(*) FROM knowledge_graph_nodes"
            ).fetchone()
            (status["graph_edges"],) = self._db.execute(
                "SELECT COUNT(*) FROM knowledge_graph_edges"
            ).fetchone()
        except Exception as exc:
            status["errors"].append(f"graph stats query: {exc}")

        try:
            # Nodes not updated in the last 7 days are considered stale.
            stale_row = self._db.execute(
                """
                SELECT COUNT(*)
                FROM   knowledge_graph_nodes
                WHERE  created_at < datetime('now', '-7 days')
                """
            ).fetchone()
            if stale_row:
                status["stale_nodes_count"] = stale_row[0]
        except Exception as exc:
            status["errors"].append(f"stale node query: {exc}")

        return status

    async def run_daemon(
        self,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Run the indexer as a periodic background task.

        Sleeps for daemon_interval_minutes after each incremental run.  When
        stop_event is set, the current sleep is interrupted and the loop exits
        cleanly after the current run completes.

        Args:
            stop_event: asyncio.Event signalling graceful shutdown.  If None,
                the daemon runs until the process exits.
        """
        if stop_event is None:
            stop_event = asyncio.Event()

        interval = self._config.daemon_interval_minutes * 60
        logger.info(
            "Daemon started: interval=%dm workspace_roots=%s",
            self._config.daemon_interval_minutes,
            self._config.workspace_roots,
        )

        while not stop_event.is_set():
            try:
                stats = await self.run_incremental()
                if stats.errors:
                    logger.warning(
                        "Daemon run completed with %d errors: %s",
                        len(stats.errors),
                        "; ".join(stats.errors[:5]),
                    )
                else:
                    logger.info(
                        "Daemon run OK: repos_analyzed=%d files_indexed=%d cost=$%.3f",
                        stats.repos_analyzed,
                        stats.files_indexed,
                        stats.llm_cost_usd,
                    )
            except Exception as exc:
                logger.error("Daemon run failed: %s", exc, exc_info=True)

            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=interval,
                )
                # stop_event fired during sleep — exit the loop.
                break
            except asyncio.TimeoutError:
                # Normal case: interval elapsed, run again.
                pass

        logger.info("Daemon stopped.")

    def close(self) -> None:
        """Close the shared database connection."""
        try:
            self._db.close()
        except Exception:
            pass

    def __del__(self) -> None:
        """Best-effort cleanup on garbage collection."""
        self.close()

    # ------------------------------------------------------------------
    # Change detection helpers
    # ------------------------------------------------------------------

    def _get_repo_head_sha(self, repo_path: str) -> Optional[str]:
        """Return the HEAD commit SHA of the git repo at repo_path.

        Args:
            repo_path: Absolute filesystem path to a git repo.

        Returns:
            40-character SHA string, or None if the path is not a git repo or
            the command fails.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as exc:
            logger.debug("git rev-parse failed for %s: %s", repo_path, exc)
        return None

    def _get_last_indexed_sha(self, workspace: str, repo_name: str) -> Optional[str]:
        """Query sync_state for the last commit SHA indexed for a repo.

        Args:
            workspace: Workspace root path used as the workspace key.
            repo_name: Unique repo name within that workspace.

        Returns:
            SHA string if found, else None.
        """
        key = _sync_state_key(workspace, repo_name)
        row = self._db.execute(
            "SELECT last_commit_sha FROM sync_state WHERE id = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    def _update_indexed_sha(
        self, workspace: str, repo_name: str, sha: str
    ) -> None:
        """Upsert the current HEAD SHA into sync_state for a repo.

        Args:
            workspace: Workspace root path.
            repo_name: Unique repo name within that workspace.
            sha: Current HEAD commit SHA.
        """
        key = _sync_state_key(workspace, repo_name)
        now = _utcnow()
        self._db.execute(
            """
            INSERT INTO sync_state (id, workspace, source_type, last_sync_at,
                                    last_commit_sha, status)
            VALUES (?, ?, ?, ?, ?, 'ok')
            ON CONFLICT(workspace, source_type) DO UPDATE SET
                last_sync_at    = excluded.last_sync_at,
                last_commit_sha = excluded.last_commit_sha,
                status          = 'ok',
                error           = NULL
            """,
            (key, workspace, _SYNC_SOURCE_TYPE, now, sha),
        )
        self._db.commit()

    def _detect_changed_repos(self, sources: list) -> list:
        """Filter sources to only those whose HEAD SHA has changed.

        A source dict is expected to have at minimum:
          - 'path'      (str): absolute path to the repo
          - 'workspace' (str): workspace root path
          - 'name'      (str): repo name for sync_state lookup

        Args:
            sources: List of source dicts from discovery phase.

        Returns:
            Subset of sources that have changed (or have never been indexed).
        """
        changed = []
        for source in sources:
            path = source.get("path", "")
            workspace = source.get("workspace", "")
            name = source.get("name", "")
            if not path or not workspace or not name:
                changed.append(source)
                continue
            current_sha = self._get_repo_head_sha(path)
            last_sha = self._get_last_indexed_sha(workspace, name)
            if current_sha is None or current_sha != last_sha:
                source = dict(source)  # don't mutate caller's dict
                source["_current_sha"] = current_sha
                changed.append(source)
        return changed

    # ------------------------------------------------------------------
    # Remote auto-clone helpers
    # ------------------------------------------------------------------

    def _auto_clone_remotes(self) -> list[str]:
        """Clone repos from configured remote git endpoints.

        Iterates over self._remotes, lists repos from each org/group
        via SSH, and clones any that are not already present locally.
        All clone operations use SSH exclusively.

        Returns:
            List of newly cloned repo paths.
        """
        if not self._remotes:
            return []

        self._clone_base_path.mkdir(parents=True, exist_ok=True)
        cloned_paths: list[str] = []

        for remote in self._remotes:
            remote_type = remote.get("type", "")
            ssh_endpoint = remote.get("ssh_endpoint", "")
            org = remote.get("org", "") or remote.get("group", "")
            auto_clone = remote.get("auto_clone", True)

            if not auto_clone or not ssh_endpoint or not org:
                continue

            # Determine the clone URL prefix based on remote type.
            if remote_type == "github":
                clone_prefix = f"{ssh_endpoint}:{org}"
            elif remote_type == "bitbucket":
                clone_prefix = f"{ssh_endpoint}:{org}"
            elif remote_type == "gitlab":
                clone_prefix = f"{ssh_endpoint}:{org}"
            else:
                logger.warning("Unknown remote type: %s", remote_type)
                continue

            # List repos via gh/git API is not reliable without auth tokens.
            # Instead, if there's a local directory for this org, scan it.
            # For new setups, the user can manually clone first or use `cap sync`.
            org_dir = self._clone_base_path / org
            if not org_dir.exists():
                org_dir.mkdir(parents=True, exist_ok=True)
                logger.info(
                    "Created org directory for %s/%s at %s — "
                    "clone repos here or use 'cap sync' to auto-discover",
                    remote_type, org, org_dir,
                )
                continue

            # Check for any repos that exist in the org dir but aren't git repos yet.
            # This handles the case where someone has a partial clone.
            for item in org_dir.iterdir():
                if item.is_dir() and not (item / ".git").exists():
                    # Attempt to clone into this directory.
                    repo_name = item.name
                    clone_url = f"{clone_prefix}/{repo_name}.git"
                    try:
                        result = subprocess.run(
                            ["git", "clone", clone_url, str(item)],
                            capture_output=True, text=True, timeout=60,
                        )
                        if result.returncode == 0:
                            cloned_paths.append(str(item))
                            logger.info("Cloned %s into %s", clone_url, item)
                    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                        logger.debug("Clone failed for %s: %s", repo_name, exc)

            # Also scan the org dir for existing git repos to include them.
            for item in org_dir.iterdir():
                if item.is_dir() and (item / ".git").exists():
                    item_path = str(item)
                    if item_path not in cloned_paths:
                        cloned_paths.append(item_path)

        return cloned_paths

    # ------------------------------------------------------------------
    # Pipeline phase implementations
    # ------------------------------------------------------------------

    async def _phase_discovery(self, stats: IndexerStats) -> list:
        """Discover all repo sources across workspace_roots.

        Returns:
            List of source dicts, each with at minimum 'path', 'workspace',
            and 'name' keys.
        """
        sources: list[dict] = []

        if self._dependency_resolver is not None:
            try:
                raw = await asyncio.to_thread(
                    self._dependency_resolver.discover_all_sources,
                    self._config.workspace_roots,
                )
                sources.extend(raw if raw else [])
            except Exception as exc:
                logger.warning("DependencyResolver.discover_all_sources failed: %s", exc)

        # Fallback / supplement: scan workspace_roots directly when the
        # dependency resolver is unavailable or returned nothing.
        if not sources:
            sources = _discover_repos_from_roots(self._config.workspace_roots)

        # Auto-clone from configured remotes if any are set.
        if self._remotes:
            try:
                cloned = self._auto_clone_remotes()
                if cloned:
                    # Discover repos in newly cloned directories.
                    clone_sources = _discover_repos_from_roots([str(self._clone_base_path)])
                    for cs in clone_sources:
                        # Avoid duplicates by path.
                        if not any(s.get("path") == cs.get("path") for s in sources):
                            sources.append(cs)
                    logger.info("Auto-clone: added %d repos from remotes", len(clone_sources))
            except Exception as exc:
                logger.warning("Auto-clone from remotes failed: %s", exc)
                stats.errors.append(f"auto_clone: {exc}")

        # Enforce safety limit.
        if len(sources) > self._config.max_repos:
            logger.warning(
                "Repo count %d exceeds max_repos=%d — truncating",
                len(sources),
                self._config.max_repos,
            )
            sources = sources[: self._config.max_repos]

        stats.repos_discovered = len(sources)
        logger.info(
            "Discovery: found %d repos across %d workspace_roots",
            len(sources),
            len(self._config.workspace_roots),
        )
        return sources

    def _phase_change_detection(
        self, sources: list, stats: IndexerStats
    ) -> list:
        """Filter sources to changed repos (or return all if full_reindex).

        Args:
            sources: Full list from discovery phase.
            stats:   IndexerStats to update repos_changed count.

        Returns:
            Filtered list of sources to process.
        """
        if self._config.full_reindex or not self._config.incremental:
            stats.repos_changed = len(sources)
            return sources

        changed = self._detect_changed_repos(sources)
        stats.repos_changed = len(changed)
        return changed

    async def _phase_dependency_resolution(
        self, sources: list, stats: IndexerStats
    ) -> None:
        """Resolve cross-repo dependencies and write edges to the graph.

        Args:
            sources: Changed repo sources.
            stats:   IndexerStats to update dependency counts.
        """
        if not self._dependency_resolver:
            logger.debug("DependencyResolver unavailable — skipping dep resolution")
            return

        try:
            result = await asyncio.to_thread(
                self._dependency_resolver.resolve_dependencies,
                sources,
            )
            if result:
                stats.dependencies_resolved = result.get("resolved", 0)
                stats.dependencies_unresolved = result.get("unresolved", 0)
                edges = result.get("edges", [])
                if self._knowledge_graph and edges:
                    for edge in edges:
                        try:
                            self._knowledge_graph.add_edge(**edge)
                            stats.graph_edges_created += 1
                        except Exception as exc:
                            logger.debug("Graph edge write failed: %s", exc)
        except Exception as exc:
            logger.warning("resolve_dependencies failed: %s", exc)
            stats.errors.append(f"resolve_dependencies: {exc}")

    def _phase_repo_extraction(self, stats: IndexerStats) -> None:
        """Run repo_extractor for all workspace_roots.

        Args:
            stats: IndexerStats to update graph_nodes_created and
                graph_edges_created counts.
        """
        for workspace in self._config.workspace_roots:
            if not Path(workspace).is_dir():
                logger.warning("Workspace not found, skipping: %s", workspace)
                stats.errors.append(f"repo_extraction: workspace not found: {workspace}")
                continue
            try:
                extractor_stats = extract_and_index_repos(self._db, workspace)
                stats.graph_nodes_created += extractor_stats.graph_nodes_created
                stats.graph_edges_created += extractor_stats.graph_edges_created
                if extractor_stats.errors:
                    stats.errors.extend(
                        f"repo_extraction[{workspace}]: {e}"
                        for e in extractor_stats.errors
                    )
                logger.info(
                    "Repo extraction[%s]: nodes=%d edges=%d repos=%d",
                    workspace,
                    extractor_stats.graph_nodes_created,
                    extractor_stats.graph_edges_created,
                    extractor_stats.repos_indexed,
                )
            except Exception as exc:
                stats.errors.append(f"repo_extraction[{workspace}]: {exc}")
                logger.error(
                    "Repo extraction failed for %s: %s", workspace, exc, exc_info=True
                )

    async def _phase_llm_analysis(
        self, sources: list, stats: IndexerStats
    ) -> None:
        """Run LLM semantic analysis on changed repos.

        Uses asyncio.Semaphore to cap concurrent Bedrock calls at
        parallel_analysis.  BudgetExceeded is re-raised so the caller can
        record it and stop cleanly.

        Args:
            sources: Changed repo sources to analyze.
            stats:   IndexerStats to update repos_analyzed and llm_cost_usd.
        """
        if not sources or self._code_understanding is None:
            return

        semaphore = asyncio.Semaphore(self._config.parallel_analysis)

        async def _analyze_one(source: dict) -> None:
            repo_summary = _build_repo_summary(source)
            async with semaphore:
                understanding = await self._code_understanding.analyze_repo(
                    repo_summary
                )
            stats.repos_analyzed += 1
            stats.llm_cost_usd += understanding.cost_usd
            # Update graph node metadata if the knowledge graph is available.
            if self._knowledge_graph is not None:
                try:
                    self._knowledge_graph.update_node_metadata(
                        entity_name=source.get("name", ""),
                        workspace=source.get("workspace", ""),
                        metadata={
                            "summary": understanding.summary,
                            "architectural_pattern": understanding.architectural_pattern,
                            "domain": understanding.domain,
                            "complexity": understanding.complexity,
                            "tags": understanding.tags,
                            "last_analyzed_at": _utcnow(),
                        },
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to update graph node for %s: %s",
                        source.get("name"),
                        exc,
                    )

        tasks = [_analyze_one(s) for s in sources]
        # gather() collects all results; BudgetExceeded propagates via
        # return_exceptions=False (the default), stopping the gather.
        await asyncio.gather(*tasks, return_exceptions=False)

        # Run cross-service interaction analysis for sources that have
        # known interaction data (heuristic: >1 source with exposes/consumes).
        services_with_interactions = [
            {
                "name": s.get("name", ""),
                "purpose": s.get("purpose", ""),
                "tech_stack": s.get("tech_stack", []),
            }
            for s in sources
            if s.get("exposes") or s.get("consumes")
        ]
        if len(services_with_interactions) > 1:
            try:
                interactions = await self._code_understanding.analyze_service_interactions(
                    services_with_interactions
                )
                logger.info(
                    "Service interaction analysis: %d interactions found",
                    len(interactions),
                )
                stats.llm_cost_usd += sum(
                    getattr(i, "cost_usd", 0.0) for i in interactions
                )
            except BudgetExceeded:
                raise
            except Exception as exc:
                logger.warning("analyze_service_interactions failed: %s", exc)
                stats.errors.append(f"service_interactions: {exc}")

        logger.info(
            "LLM analysis complete: repos_analyzed=%d cost=$%.3f",
            stats.repos_analyzed,
            stats.llm_cost_usd,
        )

    def _phase_file_sync(self, stats: IndexerStats) -> None:
        """Run file-level sync for all workspace_roots.

        Args:
            stats: IndexerStats to update files_indexed count.
        """
        for workspace in self._config.workspace_roots:
            if not Path(workspace).is_dir():
                stats.errors.append(f"file_sync: workspace not found: {workspace}")
                continue
            try:
                sync_stats = sync_workspace(
                    self._db,
                    workspace,
                    full=self._config.full_reindex,
                )
                stats.files_indexed += sync_stats.files_indexed
                if sync_stats.errors:
                    stats.errors.extend(
                        f"file_sync[{workspace}]: {e}" for e in sync_stats.errors
                    )
                logger.info(
                    "File sync[%s]: indexed=%d skipped=%d",
                    workspace,
                    sync_stats.files_indexed,
                    sync_stats.files_skipped,
                )
            except Exception as exc:
                stats.errors.append(f"file_sync[{workspace}]: {exc}")
                logger.error(
                    "File sync failed for %s: %s", workspace, exc, exc_info=True
                )

    async def _phase_embedding(self, stats: IndexerStats) -> None:
        """Process the embedding queue for new/updated knowledge entries.

        Fetches pending entries from embedding_queue, generates vectors, and
        stores them back — updating embedding_status on success.

        Args:
            stats: IndexerStats to update embeddings_generated count.
        """
        if self._embedding_client is None:
            return

        pending = self._db.execute(
            """
            SELECT eq.id, ke.id AS entry_id, ke.content
            FROM   embedding_queue eq
            JOIN   knowledge_entries ke ON ke.id = eq.entry_id
            WHERE  eq.status = 'pending'
              AND  eq.attempts < eq.max_attempts
            ORDER  BY eq.created_at ASC
            LIMIT  500
            """
        ).fetchall()

        if not pending:
            logger.debug("Embedding queue: nothing pending")
            return

        texts = [row[2] for row in pending]
        vectors = await self._embedding_client.embed_batch(texts)

        generated = 0
        for (queue_id, entry_id, _content), vector in zip(pending, vectors):
            if vector is None:
                self._db.execute(
                    """
                    UPDATE embedding_queue
                    SET    attempts = attempts + 1,
                           last_error = 'embedding returned None'
                    WHERE  id = ?
                    """,
                    (queue_id,),
                )
                continue
            import json as _json
            try:
                vector_blob = _json.dumps(vector).encode()
                self._db.execute(
                    "UPDATE knowledge_entries SET embedding_status = 'done' WHERE id = ?",
                    (entry_id,),
                )
                self._db.execute(
                    """
                    INSERT INTO embedding_cache (content_hash, vector, accessed_at)
                    SELECT content_hash, ?, datetime('now')
                    FROM   knowledge_entries
                    WHERE  id = ?
                    ON CONFLICT(content_hash) DO UPDATE SET
                        vector      = excluded.vector,
                        accessed_at = excluded.accessed_at
                    """,
                    (vector_blob, entry_id),
                )
                self._db.execute(
                    "UPDATE embedding_queue SET status = 'done', processed_at = ? WHERE id = ?",
                    (_utcnow(), queue_id),
                )
                generated += 1
            except Exception as exc:
                logger.warning("Failed to store embedding for entry %s: %s", entry_id, exc)
                self._db.execute(
                    "UPDATE embedding_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (str(exc), queue_id),
                )

        self._db.commit()
        stats.embeddings_generated += generated
        logger.info(
            "Embedding phase: generated=%d / queued=%d",
            generated,
            len(pending),
        )

    def _phase_update_tracking(self, sources: list) -> None:
        """Write current HEAD SHAs to sync_state for all processed sources.

        Args:
            sources: Sources that were processed in this run.  Each source
                dict may contain a '_current_sha' key set during change
                detection.
        """
        for source in sources:
            workspace = source.get("workspace", "")
            name = source.get("name", "")
            sha = source.get("_current_sha") or self._get_repo_head_sha(
                source.get("path", "")
            )
            if workspace and name and sha:
                try:
                    self._update_indexed_sha(workspace, name, sha)
                except Exception as exc:
                    logger.warning(
                        "Failed to update SHA for %s/%s: %s", workspace, name, exc
                    )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _monotonic() -> float:
    """Return the current value of the monotonic clock (seconds)."""
    import time
    return time.monotonic()


def _progress(
    callback: Optional[Callable[[str, int, int], None]],
    message: str,
    current: int,
    total: int,
) -> None:
    """Invoke the progress callback if one is provided.

    Args:
        callback: The callable to invoke, or None.
        message:  Human-readable phase status message.
        current:  Current phase index (0-based).
        total:    Total number of phases.
    """
    if callback is not None:
        try:
            callback(message, current, total)
        except Exception as exc:
            logger.debug("progress_callback raised: %s", exc)


def _sync_state_key(workspace: str, repo_name: str) -> str:
    """Build a deterministic primary key for sync_state rows.

    The key is based on workspace + source_type + repo_name to avoid
    collisions across workspaces.

    Args:
        workspace: Workspace root path.
        repo_name: Repo name within that workspace.

    Returns:
        String suitable for use as the sync_state.id value.
    """
    import hashlib
    raw = f"{_SYNC_SOURCE_TYPE}::{workspace}::{repo_name}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _discover_repos_from_roots(workspace_roots: list[str]) -> list[dict]:
    """Fallback repo discovery by walking workspace_roots directly.

    A directory is treated as a repo root if it contains a .git directory
    or any of the markers from repo_extractor.REPO_MARKERS.

    Args:
        workspace_roots: List of absolute paths to scan.

    Returns:
        List of source dicts with 'path', 'workspace', and 'name' keys.
    """
    from cap.lib.repo_extractor import REPO_MARKERS, SKIP_DIRS

    sources: list[dict] = []

    for workspace in workspace_roots:
        workspace_path = Path(workspace)
        if not workspace_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(workspace_path):
            # Prune dirs in-place to avoid descending into them.
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            current = Path(dirpath)
            dir_entries = set(os.listdir(current))
            if dir_entries & REPO_MARKERS:
                sources.append({
                    "path": str(current),
                    "workspace": workspace,
                    "name": current.name,
                })
                # Don't recurse into a detected repo.
                dirnames.clear()

    return sources


def _repos_from_paths(
    changed_paths: list[str], workspace_roots: list[str]
) -> set[str]:
    """Determine workspace_roots to restrict to based on changed file paths.

    For each changed path, walks up to find the nearest ancestor that is
    itself a workspace_root, then returns the set of affected workspaces.

    Args:
        changed_paths: Absolute file paths that changed.
        workspace_roots: Configured workspace roots.

    Returns:
        Set of workspace root strings that contain at least one changed path.
    """
    roots_set = {str(Path(r).resolve()) for r in workspace_roots}
    affected: set[str] = set()

    for cp in changed_paths:
        cp_path = Path(cp).resolve()
        for root in roots_set:
            try:
                cp_path.relative_to(root)
                affected.add(root)
                break
            except ValueError:
                pass

    # Fall back to all roots if no overlap found.
    return affected if affected else roots_set


def _read_file_safe(path: Path, max_chars: int) -> str:
    """Read a file, truncating to max_chars, returning '' on any error.

    Args:
        path:      File to read.
        max_chars: Maximum characters to return.

    Returns:
        File contents (possibly truncated), or empty string on error.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def _build_repo_summary(source: dict) -> dict:
    """Build the repo_summary dict expected by CodeUnderstanding.analyze_repo().

    Reads README, representative source files, and manifest files from the
    repo path.  All reads are best-effort; missing files are silently skipped.

    Args:
        source: Source dict with at minimum a 'path' key.

    Returns:
        Dict with keys: name, path, purpose, tech_stack, depends_on,
        key_files, readme_content, sample_code.
    """
    repo_path = Path(source.get("path", ""))
    readme_content = ""
    sample_code: list[str] = []
    manifest_content = ""

    # README (first 5000 chars).
    for readme_name in ("README.md", "README.rst", "README.txt", "readme.md"):
        readme_file = repo_path / readme_name
        if readme_file.exists():
            readme_content = _read_file_safe(readme_file, 5000)
            break

    # Up to 3 representative source files (3000 chars each).
    for candidate in _ENTRY_FILE_CANDIDATES:
        if len(sample_code) >= 3:
            break
        candidate_path = repo_path / candidate
        if candidate_path.exists():
            content = _read_file_safe(candidate_path, 3000)
            if content:
                sample_code.append(f"// {candidate}\n{content}")

    # Manifest files (full content — they are small).
    manifest_parts: list[str] = []
    for mf in _MANIFEST_FILE_CANDIDATES:
        mf_path = repo_path / mf
        if mf_path.exists():
            content = _read_file_safe(mf_path, 4096)
            if content:
                manifest_parts.append(f"# {mf}\n{content}")
    manifest_content = "\n\n".join(manifest_parts)

    return {
        "name": source.get("name", repo_path.name),
        "path": str(repo_path),
        "purpose": source.get("purpose", readme_content[:500]),
        "tech_stack": source.get("tech_stack", []),
        "depends_on": source.get("depends_on", []),
        "key_files": source.get("key_files", []),
        "readme_content": readme_content,
        "sample_code": "\n\n".join(sample_code),
        "manifest_content": manifest_content,
    }

"""Tests for cap.lib.intelligent_indexer.

Covers:
  - IndexerConfig and IndexerStats default values
  - IntelligentIndexer initialisation (with mocked dependencies)
  - Change detection helpers (_get_repo_head_sha, _get_last_indexed_sha,
    _update_indexed_sha, _detect_changed_repos)
  - _discover_repos_from_roots fallback discovery
  - _build_repo_summary file-reading logic
  - _repos_from_paths workspace scoping
  - run_sync happy path (all phases succeed, stats populated)
  - run_sync error path (a phase raises, errors recorded, run continues)
  - get_status with and without sync_state data
  - run_daemon stop_event exits cleanly
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import init_knowledge_db
from cap.lib.config import PlatformConfig
from cap.lib.intelligent_indexer import (
    IndexerConfig,
    IndexerStats,
    IntelligentIndexer,
    _build_repo_summary,
    _discover_repos_from_roots,
    _repos_from_paths,
    _sync_state_key,
    _utcnow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """Temporary data directory with an initialised knowledge.db."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_knowledge_db(data_dir)
    return data_dir


@pytest.fixture()
def platform_cfg(tmp_data_dir: Path) -> PlatformConfig:
    """PlatformConfig pointing at the temporary data directory."""
    cfg = PlatformConfig()
    cfg._data_dir = tmp_data_dir
    cfg.__class__.data_dir = property(lambda self: tmp_data_dir)  # type: ignore[attr-defined]
    return cfg


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Workspace with two fake git repos."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    for repo_name in ("service-a", "service-b"):
        repo = ws / repo_name
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "README.md").write_text(f"# {repo_name}\nA service.", encoding="utf-8")
        (repo / "main.py").write_text("# entrypoint\nprint('hello')", encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "' + repo_name + '"\n', encoding="utf-8"
        )
    return ws


@pytest.fixture()
def indexer(tmp_data_dir: Path, workspace: Path) -> IntelligentIndexer:
    """IntelligentIndexer with LLM and embedding skipped (no Bedrock needed)."""
    cfg = IndexerConfig(
        workspace_roots=[str(workspace)],
        skip_llm_analysis=True,
        skip_embedding=True,
        full_reindex=True,
    )

    platform_cfg = PlatformConfig()

    # Patch data_dir property to point at our temp dir.
    with patch.object(
        type(platform_cfg), "data_dir", new_callable=lambda: property(lambda self: tmp_data_dir)
    ):
        idx = IntelligentIndexer(config=cfg, platform_config=platform_cfg)
        # Replace the platform_config after construction so _db is already open.
        yield idx
        idx.close()


# ---------------------------------------------------------------------------
# Unit tests — dataclasses
# ---------------------------------------------------------------------------


class TestIndexerConfig:
    def test_defaults(self):
        cfg = IndexerConfig()
        assert cfg.workspace_roots == []
        assert cfg.full_reindex is False
        assert cfg.skip_llm_analysis is False
        assert cfg.skip_embedding is False
        assert cfg.max_repos == 100
        assert cfg.incremental is True
        assert cfg.parallel_analysis == 3
        assert cfg.budget_limit_usd == 2.0
        assert cfg.include_file_level is True
        assert cfg.daemon_mode is False
        assert cfg.daemon_interval_minutes == 60

    def test_custom_values(self):
        cfg = IndexerConfig(max_repos=5, skip_llm_analysis=True, budget_limit_usd=0.5)
        assert cfg.max_repos == 5
        assert cfg.skip_llm_analysis is True
        assert cfg.budget_limit_usd == 0.5


class TestIndexerStats:
    def test_defaults(self):
        s = IndexerStats()
        assert s.repos_discovered == 0
        assert s.llm_cost_usd == 0.0
        assert s.errors == []
        assert s.phase_durations == {}

    def test_error_list_is_independent(self):
        a = IndexerStats()
        b = IndexerStats()
        a.errors.append("boom")
        assert b.errors == []


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestSyncStateKey:
    def test_deterministic(self):
        k1 = _sync_state_key("/ws", "repo-a")
        k2 = _sync_state_key("/ws", "repo-a")
        assert k1 == k2

    def test_different_repos_produce_different_keys(self):
        k1 = _sync_state_key("/ws", "repo-a")
        k2 = _sync_state_key("/ws", "repo-b")
        assert k1 != k2

    def test_different_workspaces_produce_different_keys(self):
        k1 = _sync_state_key("/ws1", "repo-a")
        k2 = _sync_state_key("/ws2", "repo-a")
        assert k1 != k2


class TestDiscoverReposFromRoots:
    def test_finds_git_repos(self, workspace: Path):
        sources = _discover_repos_from_roots([str(workspace)])
        names = {s["name"] for s in sources}
        assert "service-a" in names
        assert "service-b" in names

    def test_sets_workspace_key(self, workspace: Path):
        sources = _discover_repos_from_roots([str(workspace)])
        assert all(s["workspace"] == str(workspace) for s in sources)

    def test_empty_for_missing_root(self, tmp_path: Path):
        sources = _discover_repos_from_roots([str(tmp_path / "nonexistent")])
        assert sources == []

    def test_does_not_recurse_into_repos(self, tmp_path: Path):
        """Nested .git directories should not produce duplicate entries."""
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / ".git").mkdir()
        inner = outer / "inner"
        inner.mkdir()
        (inner / ".git").mkdir()
        sources = _discover_repos_from_roots([str(tmp_path)])
        # outer is found; inner is under outer's .git subtree exclusion
        names = [s["name"] for s in sources]
        assert names.count("outer") == 1


class TestReposFromPaths:
    def test_returns_matching_workspace(self, workspace: Path):
        changed = [str(workspace / "service-a" / "main.py")]
        affected = _repos_from_paths(changed, [str(workspace)])
        assert str(workspace.resolve()) in affected

    def test_returns_all_roots_when_no_match(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        changed = ["/completely/unrelated/file.py"]
        affected = _repos_from_paths(changed, [str(workspace)])
        # Falls back to all roots.
        assert str(workspace.resolve()) in affected

    def test_empty_changed_paths_returns_all_roots(self, workspace: Path):
        affected = _repos_from_paths([], [str(workspace)])
        assert str(workspace.resolve()) in affected


class TestBuildRepoSummary:
    def test_reads_readme(self, workspace: Path):
        source = {
            "path": str(workspace / "service-a"),
            "name": "service-a",
            "workspace": str(workspace),
        }
        summary = _build_repo_summary(source)
        assert "service-a" in summary["readme_content"]

    def test_reads_main_py(self, workspace: Path):
        source = {
            "path": str(workspace / "service-a"),
            "name": "service-a",
            "workspace": str(workspace),
        }
        summary = _build_repo_summary(source)
        assert "entrypoint" in summary["sample_code"]

    def test_reads_pyproject_toml(self, workspace: Path):
        source = {
            "path": str(workspace / "service-a"),
            "name": "service-a",
            "workspace": str(workspace),
        }
        summary = _build_repo_summary(source)
        assert "pyproject.toml" in summary["manifest_content"]

    def test_missing_files_produce_empty_strings(self, tmp_path: Path):
        empty_repo = tmp_path / "empty-repo"
        empty_repo.mkdir()
        source = {"path": str(empty_repo), "name": "empty-repo", "workspace": str(tmp_path)}
        summary = _build_repo_summary(source)
        assert summary["readme_content"] == ""
        assert summary["sample_code"] == ""
        assert summary["manifest_content"] == ""

    def test_readme_truncated_to_5000_chars(self, tmp_path: Path):
        repo = tmp_path / "big-repo"
        repo.mkdir()
        (repo / "README.md").write_text("x" * 10_000, encoding="utf-8")
        source = {"path": str(repo), "name": "big-repo", "workspace": str(tmp_path)}
        summary = _build_repo_summary(source)
        assert len(summary["readme_content"]) == 5000


# ---------------------------------------------------------------------------
# Integration tests — IntelligentIndexer with real DB
# ---------------------------------------------------------------------------


class TestChangeDetectionHelpers:
    def test_get_last_indexed_sha_returns_none_when_not_set(self, indexer: IntelligentIndexer, workspace: Path):
        sha = indexer._get_last_indexed_sha(str(workspace), "service-a")
        assert sha is None

    def test_update_and_retrieve_indexed_sha(self, indexer: IntelligentIndexer, workspace: Path):
        indexer._update_indexed_sha(str(workspace), "service-a", "abc123")
        sha = indexer._get_last_indexed_sha(str(workspace), "service-a")
        assert sha == "abc123"

    def test_update_is_idempotent(self, indexer: IntelligentIndexer, workspace: Path):
        indexer._update_indexed_sha(str(workspace), "service-a", "abc123")
        indexer._update_indexed_sha(str(workspace), "service-a", "def456")
        sha = indexer._get_last_indexed_sha(str(workspace), "service-a")
        assert sha == "def456"

    def test_get_repo_head_sha_returns_none_for_non_repo(self, tmp_path: Path, indexer: IntelligentIndexer):
        empty_dir = tmp_path / "not-a-repo"
        empty_dir.mkdir()
        sha = indexer._get_repo_head_sha(str(empty_dir))
        assert sha is None

    def test_detect_changed_repos_includes_never_indexed(
        self, indexer: IntelligentIndexer, workspace: Path
    ):
        sources = [
            {"path": str(workspace / "service-a"), "workspace": str(workspace), "name": "service-a"},
        ]
        # Never indexed → should be in changed list.
        with patch.object(indexer, "_get_repo_head_sha", return_value="sha-new"):
            changed = indexer._detect_changed_repos(sources)
        assert len(changed) == 1

    def test_detect_changed_repos_excludes_unchanged(
        self, indexer: IntelligentIndexer, workspace: Path
    ):
        indexer._update_indexed_sha(str(workspace), "service-a", "same-sha")
        sources = [
            {"path": str(workspace / "service-a"), "workspace": str(workspace), "name": "service-a"},
        ]
        with patch.object(indexer, "_get_repo_head_sha", return_value="same-sha"):
            changed = indexer._detect_changed_repos(sources)
        assert len(changed) == 0


class TestGetStatus:
    def test_returns_dict_with_expected_keys(self, indexer: IntelligentIndexer):
        status = indexer.get_status()
        assert "last_run_at" in status
        assert "repos_indexed" in status
        assert "graph_nodes" in status
        assert "graph_edges" in status
        assert "stale_nodes_count" in status
        assert "errors" in status

    def test_no_errors_on_fresh_db(self, indexer: IntelligentIndexer):
        status = indexer.get_status()
        assert status["graph_nodes"] == 0
        assert status["graph_edges"] == 0

    def test_last_run_at_populated_after_sha_update(
        self, indexer: IntelligentIndexer, workspace: Path
    ):
        indexer._update_indexed_sha(str(workspace), "service-a", "abc")
        status = indexer.get_status()
        assert status["last_run_at"] is not None


class TestRunSync:
    def test_happy_path_returns_stats(self, indexer: IntelligentIndexer):
        stats = indexer.run_sync()
        assert isinstance(stats, IndexerStats)
        assert stats.repos_discovered >= 2  # service-a, service-b
        assert stats.completed_at != ""
        assert stats.duration_seconds > 0

    def test_progress_callback_is_called(self, indexer: IntelligentIndexer):
        calls: list[tuple] = []
        indexer.run_sync(progress_callback=lambda m, c, t: calls.append((m, c, t)))
        assert len(calls) >= 2
        # Last call should be phase 8/8.
        assert calls[-1][1] == 8
        assert calls[-1][2] == 8

    def test_progress_callback_error_does_not_abort_run(
        self, indexer: IntelligentIndexer
    ):
        def bad_callback(m, c, t):
            raise RuntimeError("callback boom")

        # Should not raise.
        stats = indexer.run_sync(progress_callback=bad_callback)
        assert stats is not None

    def test_discovery_error_recorded_in_stats(
        self, indexer: IntelligentIndexer
    ):
        with patch.object(
            indexer,
            "_phase_discovery",
            side_effect=RuntimeError("discovery exploded"),
        ):
            stats = indexer.run_sync()
        assert any("discovery" in e for e in stats.errors)
        # Run should still complete (not raise).
        assert stats.completed_at != ""

    def test_repo_extraction_error_recorded_in_stats(
        self, indexer: IntelligentIndexer
    ):
        with patch.object(
            indexer,
            "_phase_repo_extraction",
            side_effect=RuntimeError("extraction boom"),
        ):
            stats = indexer.run_sync()
        assert any("repo_extraction" in e for e in stats.errors)

    def test_full_reindex_skips_change_filter(
        self, indexer: IntelligentIndexer, workspace: Path
    ):
        # Pre-populate SHA so change detection would skip both repos.
        for name in ("service-a", "service-b"):
            indexer._update_indexed_sha(str(workspace), name, "current-sha")

        with patch.object(indexer, "_get_repo_head_sha", return_value="current-sha"):
            stats = indexer.run_sync()

        # full_reindex=True (set in fixture) means repos_changed == repos_discovered.
        assert stats.repos_changed == stats.repos_discovered


class TestRunIncremental:
    def test_with_changed_paths_restricts_workspace(
        self, indexer: IntelligentIndexer, workspace: Path
    ):
        changed = [str(workspace / "service-a" / "main.py")]
        stats = asyncio.run(indexer.run_incremental(changed_paths=changed))
        assert isinstance(stats, IndexerStats)

    def test_without_changed_paths_falls_back_to_git_diff(
        self, indexer: IntelligentIndexer
    ):
        # full_reindex is reset to False inside run_incremental.
        stats = asyncio.run(indexer.run_incremental())
        assert isinstance(stats, IndexerStats)


class TestRunDaemon:
    def test_stop_event_halts_daemon(self, indexer: IntelligentIndexer):
        stop = asyncio.Event()

        async def _run():
            stop.set()  # signal immediately
            await indexer.run_daemon(stop_event=stop)

        # Should complete without hanging.
        asyncio.run(_run())

    def test_daemon_logs_stats_per_run(self, indexer: IntelligentIndexer):
        """Daemon calls run_incremental at least once before stopping."""
        call_count = {"n": 0}
        original = indexer.run_incremental

        async def counting_run(*a, **kw):
            call_count["n"] += 1
            return IndexerStats()

        indexer.run_incremental = counting_run  # type: ignore[method-assign]

        stop = asyncio.Event()

        async def _run():
            # Override interval to 0 minutes for speed, then stop after first run.
            indexer._config.daemon_interval_minutes = 0
            # Let one iteration complete then stop.
            task = asyncio.create_task(indexer.run_daemon(stop_event=stop))
            # Give the daemon one event-loop cycle to start and run.
            await asyncio.sleep(0.05)
            stop.set()
            await task

        asyncio.run(_run())
        assert call_count["n"] >= 1

        # Restore.
        indexer.run_incremental = original  # type: ignore[method-assign]

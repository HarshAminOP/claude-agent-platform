"""Tests for the `cap index` CLI command group.

Covers:
- cap index run      — happy path, no-workspace error, quiet flag, errors list
- cap index status   — output fields, missing-field defaults
- cap index deps     — named repo, --reverse flag, overview (no repo)
- cap index graph    — results, empty results, type/limit forwarding
- cap index daemon   — --stop with/without lock, start no-roots, start KeyboardInterrupt
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cap.cli.main import cli


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_index_stats(**overrides):
    """Return a SimpleNamespace mimicking IntelligentIndexer.run() stats."""
    defaults = dict(
        repos_discovered=3,
        repos_analyzed=3,
        dependencies_resolved=10,
        dependencies_unresolved=2,
        graph_nodes_created=15,
        graph_edges_created=20,
        files_indexed=100,
        llm_cost_usd=0.0123,
        duration_seconds=4.5,
        errors=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_indexer_module(indexer_instance):
    """Build a fake cap.lib.intelligent_indexer module with mock classes."""
    mod = ModuleType("cap.lib.intelligent_indexer")
    mod.IntelligentIndexer = MagicMock(return_value=indexer_instance)
    mod.IndexerConfig = MagicMock()
    return mod


def _fake_config_module(platform_config):
    """Build a fake cap.lib.config module."""
    mod = ModuleType("cap.lib.config")
    mod.load_config = MagicMock(return_value=platform_config)
    return mod


def _fake_harness_config_module(harness_cfg):
    """Build a fake cap.lib.harness_config module."""
    mod = ModuleType("cap.lib.harness_config")
    mod.load_harness_config = MagicMock(return_value=harness_cfg)
    return mod


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def mock_platform_config(tmp_path: Path):
    cfg = MagicMock()
    cfg.locks_dir = tmp_path / "locks"
    cfg.locks_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# ── cap index run ─────────────────────────────────────────────────────────────


class TestIndexRun:
    def _inject_modules(self, indexer_instance, platform_config, harness_cfg=None):
        """Return sys.modules patch dict for a full index_run invocation."""
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(indexer_instance),
            "cap.lib.config": _fake_config_module(platform_config),
        }
        if harness_cfg is not None:
            mods["cap.lib.harness_config"] = _fake_harness_config_module(harness_cfg)
        return mods

    def test_run_happy_path(self, runner, mock_platform_config):
        stats = _make_index_stats()
        mock_indexer = MagicMock()
        mock_indexer.run = AsyncMock(return_value=stats)
        mods = self._inject_modules(mock_indexer, mock_platform_config)

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "run", "--workspace", "/tmp/myrepo"])

        assert result.exit_code == 0, result.output
        assert "Indexing complete" in result.output
        assert "Repos discovered: 3" in result.output
        assert "Files indexed:    100" in result.output
        assert "$0.0123" in result.output

    def test_run_quiet_suppresses_progress(self, runner, mock_platform_config):
        stats = _make_index_stats()
        mock_indexer = MagicMock()
        mock_indexer.run = AsyncMock(return_value=stats)
        mods = self._inject_modules(mock_indexer, mock_platform_config)

        with patch.dict(sys.modules, mods):
            result = runner.invoke(
                cli, ["index", "run", "--workspace", "/tmp/myrepo", "--quiet"]
            )

        assert result.exit_code == 0, result.output
        assert "Indexing complete" in result.output

    def test_run_no_workspace_exits_nonzero(self, runner, mock_platform_config):
        mods = {
            "cap.lib.config": _fake_config_module(mock_platform_config),
            "cap.lib.harness_config": _fake_harness_config_module({}),
            "cap.lib.intelligent_indexer": _fake_indexer_module(MagicMock()),
        }
        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "run"])

        assert result.exit_code == 1
        assert "No workspace specified" in result.output

    def test_run_falls_back_to_harness_config_roots(self, runner, mock_platform_config):
        stats = _make_index_stats()
        mock_indexer = MagicMock()
        mock_indexer.run = AsyncMock(return_value=stats)
        harness_cfg = {"knowledge": {"indexed_roots": ["/data/repos"]}}
        mods = self._inject_modules(mock_indexer, mock_platform_config, harness_cfg)

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "run"])

        assert result.exit_code == 0, result.output
        assert "Indexing complete" in result.output

    def test_run_shows_errors_when_present(self, runner, mock_platform_config):
        stats = _make_index_stats(errors=["repo A failed", "repo B timed out"])
        mock_indexer = MagicMock()
        mock_indexer.run = AsyncMock(return_value=stats)
        mods = self._inject_modules(mock_indexer, mock_platform_config)

        with patch.dict(sys.modules, mods):
            result = runner.invoke(
                cli, ["index", "run", "--workspace", "/tmp/myrepo"]
            )

        assert result.exit_code == 0, result.output
        assert "Errors: 2" in result.output
        assert "repo A failed" in result.output


# ── cap index status ──────────────────────────────────────────────────────────


class TestIndexStatus:
    def test_status_prints_all_fields(self, runner, mock_platform_config):
        status_data = {
            "last_run_at": "2026-06-30T12:00:00Z",
            "repos_tracked": 12,
            "graph_nodes": 450,
            "graph_edges": 800,
            "stale_nodes": 3,
            "total_cost_usd": 0.25,
        }
        mock_indexer = MagicMock()
        mock_indexer.get_status.return_value = status_data
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "status"])

        assert result.exit_code == 0, result.output
        assert "Intelligent Indexer Status" in result.output
        assert "2026-06-30T12:00:00Z" in result.output
        assert "12" in result.output
        assert "450" in result.output
        assert "$0.2500" in result.output

    def test_status_defaults_for_missing_fields(self, runner, mock_platform_config):
        mock_indexer = MagicMock()
        mock_indexer.get_status.return_value = {}
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "status"])

        assert result.exit_code == 0, result.output
        assert "never" in result.output
        assert "$0.0000" in result.output


# ── cap index deps ────────────────────────────────────────────────────────────


class TestIndexDeps:
    def _make_indexer(self, deps=None, dependents=None, stats=None):
        mock_graph = MagicMock()
        mock_graph.get_dependencies.return_value = deps or []
        mock_graph.get_dependents.return_value = dependents or []
        mock_graph.get_stats.return_value = stats or {"nodes_by_type": {}, "total_edges": 0}
        mock_indexer = MagicMock()
        mock_indexer._knowledge_graph = mock_graph
        return mock_indexer

    def test_deps_for_specific_repo(self, runner, mock_platform_config):
        deps = [{"entity_name": "lib-common", "entity_type": "library"}]
        mock_indexer = self._make_indexer(deps=deps)
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "deps", "my-service"])

        assert result.exit_code == 0, result.output
        assert "Dependencies of my-service" in result.output
        assert "lib-common" in result.output
        assert "[library]" in result.output

    def test_deps_reverse_flag(self, runner, mock_platform_config):
        dependents = [{"entity_name": "api-gateway", "entity_type": "service"}]
        mock_indexer = self._make_indexer(dependents=dependents)
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "deps", "--reverse", "lib-common"])

        assert result.exit_code == 0, result.output
        assert "Repos that depend on lib-common" in result.output
        assert "api-gateway" in result.output

    def test_deps_overview_no_repo(self, runner, mock_platform_config):
        stats = {"nodes_by_type": {"service": 5, "library": 2}, "total_edges": 20}
        mock_indexer = self._make_indexer(stats=stats)
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "deps"])

        assert result.exit_code == 0, result.output
        assert "Dependency Overview" in result.output
        assert "service: 5" in result.output
        assert "Total edges: 20" in result.output


# ── cap index graph ───────────────────────────────────────────────────────────


class TestIndexGraph:
    def test_graph_search_with_results(self, runner, mock_platform_config):
        results = [
            {
                "entity_type": "service",
                "entity_name": "auth-service",
                "metadata": {"summary": "Handles user authentication"},
            }
        ]
        mock_indexer = MagicMock()
        mock_indexer._knowledge_graph.search.return_value = results
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "graph", "auth"])

        assert result.exit_code == 0, result.output
        assert "Graph search: 'auth'" in result.output
        assert "1 results" in result.output
        assert "auth-service" in result.output
        assert "[service]" in result.output
        assert "Handles user authentication" in result.output

    def test_graph_search_no_results(self, runner, mock_platform_config):
        mock_indexer = MagicMock()
        mock_indexer._knowledge_graph.search.return_value = []
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "graph", "nonexistent"])

        assert result.exit_code == 0, result.output
        assert "0 results" in result.output

    def test_graph_search_respects_type_and_limit(self, runner, mock_platform_config):
        mock_indexer = MagicMock()
        mock_indexer._knowledge_graph.search.return_value = []
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(mock_platform_config),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(
                cli, ["index", "graph", "payment", "--type", "service", "--limit", "5"]
            )

        assert result.exit_code == 0, result.output
        mock_indexer._knowledge_graph.search.assert_called_once_with(
            "payment", node_type="service", limit=5
        )


# ── cap index daemon ──────────────────────────────────────────────────────────


class TestIndexDaemon:
    def test_daemon_stop_with_lock_file(self, runner, tmp_path):
        cfg = MagicMock()
        lock_file = tmp_path / "intelligent_indexer.lock"
        lock_file.touch()
        cfg.locks_dir = tmp_path
        mods = {"cap.lib.config": _fake_config_module(cfg)}

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "daemon", "--stop"])

        assert result.exit_code == 0, result.output
        assert "Daemon stop signal sent" in result.output
        assert not lock_file.exists()

    def test_daemon_stop_no_lock_file(self, runner, tmp_path):
        cfg = MagicMock()
        cfg.locks_dir = tmp_path  # lock file does NOT exist
        mods = {"cap.lib.config": _fake_config_module(cfg)}

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "daemon", "--stop"])

        assert result.exit_code == 0, result.output
        assert "No daemon running" in result.output

    def test_daemon_start_no_roots_configured(self, runner, tmp_path):
        cfg = MagicMock()
        cfg.locks_dir = tmp_path
        mock_indexer = MagicMock()
        mock_indexer.run_daemon = AsyncMock()
        mods = {
            "cap.lib.intelligent_indexer": _fake_indexer_module(mock_indexer),
            "cap.lib.config": _fake_config_module(cfg),
            "cap.lib.harness_config": _fake_harness_config_module({}),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "daemon", "--interval", "30"])

        assert result.exit_code == 1
        assert "No workspace roots configured" in result.output

    def test_daemon_start_keyboard_interrupt_exits_cleanly(self, runner, tmp_path):
        cfg = MagicMock()
        cfg.locks_dir = tmp_path
        harness_cfg = {"knowledge": {"indexed_roots": ["/data/repos"]}}
        mock_indexer = MagicMock()
        mock_indexer.run_daemon = AsyncMock(side_effect=KeyboardInterrupt)

        fake_indexer_mod = _fake_indexer_module(mock_indexer)
        # Ensure IndexerConfig returns a mutable object so workspace_roots can be set
        fake_config_instance = MagicMock()
        fake_config_instance.workspace_roots = []
        fake_indexer_mod.IndexerConfig = MagicMock(return_value=fake_config_instance)

        mods = {
            "cap.lib.intelligent_indexer": fake_indexer_mod,
            "cap.lib.config": _fake_config_module(cfg),
            "cap.lib.harness_config": _fake_harness_config_module(harness_cfg),
        }

        with patch.dict(sys.modules, mods):
            result = runner.invoke(cli, ["index", "daemon", "--interval", "30"])

        assert result.exit_code == 0, result.output
        assert "Daemon stopped" in result.output

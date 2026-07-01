"""Tests for cap.harness.daemon.CapDaemon."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.daemon import CapDaemon


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def daemon() -> CapDaemon:
    return CapDaemon(interval_seconds=60)


# ── Construction ──────────────────────────────────────────────────────────────


def test_init_defaults():
    d = CapDaemon()
    assert d.interval == 21600
    assert d.running is False
    assert d.last_run is None


def test_init_custom_interval():
    d = CapDaemon(interval_seconds=300)
    assert d.interval == 300


# ── run_once returns structured dict ──────────────────────────────────────────


def _make_consolidation_result():
    """Return a minimal ConsolidationResult-like object."""
    r = MagicMock()
    r.expired_deleted = 3
    r.duplicates_removed = 1
    return r


def test_run_once_all_success(daemon):
    with (
        patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")),
        patch("cap.lib.db_init.init_knowledge_db", return_value=MagicMock()),
        patch("cap.lib.db_init.init_sessions_db", return_value=MagicMock()),
        patch("cap.lib.consolidator.consolidate", return_value=_make_consolidation_result()),
        patch("cap.harness.agent_store.cleanup_stale", return_value=2),
        patch("cap.harness.daemon.CapDaemon._run_pattern_embedding", return_value={"embedded": 5}),
        patch("cap.learning.engine.compute_thresholds_from_session_events", return_value={"sample_count": 4, "success_rate": 0.75, "avg_duration": 10.0}),
        patch("cap.harness.daemon.CapDaemon._run_retention", return_value={"skipped": "retention_module_unavailable"}),
        patch("cap.harness.governance.write_manifest"),
    ):
        results = daemon.run_once()

    assert "consolidation" in results
    assert "stale_agents" in results
    assert "pattern_embedding" in results
    assert "learning" in results
    assert "retention" in results
    assert "manifest" in results
    assert daemon.last_run is results


def test_run_once_sets_last_run(daemon):
    with patch.object(daemon, "_run_consolidation", return_value={"expired": 0, "deduped": 0}), \
         patch.object(daemon, "_run_stale_cleanup", return_value={"terminated": 0}), \
         patch.object(daemon, "_run_pattern_embedding", return_value={"skipped": "embedder_unavailable"}), \
         patch.object(daemon, "_run_learning", return_value={"sample_count": 0}), \
         patch.object(daemon, "_run_retention", return_value={"skipped": "retention_module_unavailable"}), \
         patch.object(daemon, "_run_manifest", return_value={"refreshed": True}):
        results = daemon.run_once()

    assert daemon.last_run is results


# ── Individual task runners ───────────────────────────────────────────────────


def test_run_consolidation_maps_fields(daemon):
    mock_result = MagicMock()
    mock_result.expired_deleted = 7
    mock_result.duplicates_removed = 2

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_knowledge_db", return_value=MagicMock()), \
         patch("cap.lib.consolidator.consolidate", return_value=mock_result):
        r = daemon._run_consolidation()

    assert r == {"expired": 7, "deduped": 2}


def test_run_consolidation_handles_error(daemon):
    with patch("cap.harness.daemon.CapDaemon._data_dir", side_effect=RuntimeError("no config")):
        r = daemon._run_consolidation()
    assert "error" in r
    assert "no config" in r["error"]


def test_run_stale_cleanup_returns_count(daemon):
    with patch("cap.harness.agent_store.cleanup_stale", return_value=3):
        r = daemon._run_stale_cleanup()
    assert r == {"terminated": 3}


def test_run_stale_cleanup_handles_error(daemon):
    with patch("cap.harness.agent_store.cleanup_stale", side_effect=Exception("db locked")):
        r = daemon._run_stale_cleanup()
    assert "error" in r


def test_run_pattern_embedding_unavailable(daemon):
    mock_pe = MagicMock()
    mock_pe.is_available = False

    with patch("cap.harness.vector_patterns.PatternEmbedder", return_value=mock_pe):
        r = daemon._run_pattern_embedding()

    assert r == {"skipped": "embedder_unavailable"}


def test_run_pattern_embedding_available(daemon):
    mock_pe = MagicMock()
    mock_pe.is_available = True
    mock_pe.bulk_embed_missing.return_value = 10

    with patch("cap.harness.vector_patterns.PatternEmbedder", return_value=mock_pe):
        r = daemon._run_pattern_embedding()

    assert r == {"embedded": 10}
    mock_pe.bulk_embed_missing.assert_called_once_with(batch_size=50)


def test_run_pattern_embedding_handles_error(daemon):
    with patch("cap.harness.vector_patterns.PatternEmbedder", side_effect=Exception("import error")):
        r = daemon._run_pattern_embedding()
    assert "error" in r


def test_run_retention_missing_module(daemon):
    """When retention module does not exist, returns skipped."""
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "cap.harness.retention":
            raise ImportError("No module named 'cap.harness.retention'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        r = daemon._run_retention()

    assert r == {"skipped": "retention_module_unavailable"}


def test_run_manifest_success(daemon):
    with patch("cap.harness.governance.write_manifest") as mock_wm:
        r = daemon._run_manifest()
    assert r == {"refreshed": True}
    mock_wm.assert_called_once_with(Path.cwd())


def test_run_manifest_handles_error(daemon):
    with patch("cap.harness.governance.write_manifest", side_effect=PermissionError("read-only")):
        r = daemon._run_manifest()
    assert "error" in r
    assert "read-only" in r["error"]


# ── Learning task runner ──────────────────────────────────────────────────────


def test_run_learning_success(daemon):
    expected = {"sample_count": 2, "success_rate": 1.0, "avg_duration": 5.0}

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_sessions_db", return_value=MagicMock()), \
         patch("cap.lib.db_init.init_knowledge_db", return_value=MagicMock()), \
         patch("cap.learning.engine.compute_thresholds_from_session_events", return_value=expected):
        r = daemon._run_learning()

    assert r == expected


def test_run_learning_handles_error(daemon):
    with patch("cap.harness.daemon.CapDaemon._data_dir", side_effect=Exception("config missing")):
        r = daemon._run_learning()
    assert "error" in r


# ── CLI smoke test ────────────────────────────────────────────────────────────


def test_cli_daemon_once():
    """cap daemon --once should print JSON and exit cleanly."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()

    with patch("cap.harness.daemon.CapDaemon.run_once", return_value={"consolidation": {"expired": 0, "deduped": 0}}):
        result = runner.invoke(cli, ["daemon", "--once"])

    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert "consolidation" in data


def test_cli_daemon_registered():
    """daemon command must be registered on the main CLI group."""
    from cap.cli.main import cli
    assert "daemon" in cli.commands

"""Tests for cap.harness.daemon — the CAP platform background operator.

Covers:
  - Health check logic (MCP server PID checking, restart)
  - Budget pause trigger when limit exceeded
  - Stale agent cleanup
  - Service file generation (macOS plist, Linux systemd)
  - PID lifecycle (write, read, remove, is_running)
  - Workspace detection
  - Legacy run_once backward compat
  - CLI commands
"""

from __future__ import annotations

import os
import platform
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.daemon import (
    CapDaemon,
    write_pid,
    remove_pid,
    read_pid,
    is_daemon_running,
    setup_logging,
    _cap_home,
    _pid_path,
    _log_path,
    _pending_workspaces_path,
    INTERVAL_HEALTH_CHECK,
    INTERVAL_BUDGET_CHECK,
    INTERVAL_REEMBED,
    INTERVAL_CLEANUP_STALE,
    INTERVAL_COMPACT_VECTORS,
    INTERVAL_WORKSPACE_DETECT,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def daemon() -> CapDaemon:
    return CapDaemon(interval_seconds=60)


@pytest.fixture()
def tmp_cap_home(tmp_path):
    """Set CAP_HOME to a temporary directory for tests."""
    cap_home = tmp_path / ".claude-platform"
    cap_home.mkdir()
    (cap_home / "run").mkdir()
    (cap_home / "logs").mkdir()
    (cap_home / "data").mkdir()
    with patch.dict(os.environ, {"CAP_HOME": str(cap_home)}):
        yield cap_home


# ── Constants ────────────────────────────────────────────────────────────────


def test_interval_constants():
    """Verify task intervals are as specified."""
    assert INTERVAL_HEALTH_CHECK == 60
    assert INTERVAL_BUDGET_CHECK == 300
    assert INTERVAL_REEMBED == 1800
    assert INTERVAL_CLEANUP_STALE == 3600
    assert INTERVAL_COMPACT_VECTORS == 21600
    assert INTERVAL_WORKSPACE_DETECT == 60


# ── Construction ──────────────────────────────────────────────────────────────


def test_init_defaults():
    d = CapDaemon()
    assert d.interval == 21600
    assert d.running is False
    assert d.last_run is None
    assert d.uptime_seconds == 0.0


def test_init_custom_interval():
    d = CapDaemon(interval_seconds=300)
    assert d.interval == 300


# ── PID Lifecycle ────────────────────────────────────────────────────────────


def test_write_and_read_pid(tmp_cap_home):
    write_pid()
    pid = read_pid()
    assert pid == os.getpid()


def test_remove_pid(tmp_cap_home):
    write_pid()
    assert read_pid() is not None
    remove_pid()
    assert read_pid() is None


def test_read_pid_no_file(tmp_cap_home):
    assert read_pid() is None


def test_read_pid_invalid_content(tmp_cap_home):
    pid_path = tmp_cap_home / "run" / "daemon.pid"
    pid_path.write_text("not_a_number")
    assert read_pid() is None


def test_is_daemon_running_true(tmp_cap_home):
    """Current process PID should be running."""
    write_pid()
    assert is_daemon_running() is True


def test_is_daemon_running_false_no_pid(tmp_cap_home):
    assert is_daemon_running() is False


def test_is_daemon_running_false_dead_pid(tmp_cap_home):
    """A non-existent PID should return False."""
    pid_path = tmp_cap_home / "run" / "daemon.pid"
    pid_path.write_text("99999999")
    assert is_daemon_running() is False


# ── Health Check ─────────────────────────────────────────────────────────────


def test_health_check_no_servers(daemon):
    """Health check with empty fleet returns zeros."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = []

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_fleet_db", return_value=mock_db):
        result = daemon.health_check()

    assert result["checked"] == 0
    assert result["alive"] == 0
    assert result["restarted"] == 0


def test_health_check_alive_server(daemon):
    """Health check detects a live server."""
    mock_db = MagicMock()
    # Simulate one running server with current PID
    mock_db.execute.return_value.fetchall.return_value = [
        ("test-server", os.getpid(), "running", "node server.js", 0, 5)
    ]

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_fleet_db", return_value=mock_db):
        result = daemon.health_check()

    assert result["checked"] == 1
    assert result["alive"] == 1
    assert result["restarted"] == 0


def test_health_check_dead_server_restarts(daemon):
    """Health check restarts a dead server."""
    mock_db = MagicMock()
    # Server with dead PID
    mock_db.execute.return_value.fetchall.return_value = [
        ("test-server", 99999999, "running", "echo hello", 0, 5)
    ]

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_fleet_db", return_value=mock_db), \
         patch("subprocess.Popen", return_value=mock_proc):
        result = daemon.health_check()

    assert result["checked"] == 1
    assert result["alive"] == 0
    assert result["restarted"] == 1


def test_health_check_max_restarts_exceeded(daemon):
    """Health check does not restart if max_restarts reached."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = [
        ("test-server", 99999999, "running", "echo hello", 5, 5)
    ]

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.db_init.init_fleet_db", return_value=mock_db):
        result = daemon.health_check()

    assert result["checked"] == 1
    assert result["restarted"] == 0


def test_health_check_handles_error(daemon):
    """Health check gracefully handles exceptions."""
    with patch("cap.harness.daemon.CapDaemon._data_dir", side_effect=RuntimeError("no config")):
        result = daemon.health_check()

    assert "errors" in result
    assert len(result["errors"]) > 0


# ── Budget Check ─────────────────────────────────────────────────────────────


def test_budget_check_under_limit(daemon):
    """Budget check when under limit does nothing."""
    mock_harness_cfg = {"budget": {"daily_limit_usd": 5.0}}
    mock_spend = {"total_spend_usd": 2.0, "date": "2026-01-01", "execution_count": 10}

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.harness_config.load_harness_config", return_value=mock_harness_cfg), \
         patch("cap.lib.budget_manager.init_budget_log_table"), \
         patch("cap.lib.budget_manager.get_today_spend", return_value=mock_spend), \
         patch("cap.lib.budget_manager.is_budget_paused", return_value=False), \
         patch("sqlite3.connect") as mock_conn:
        mock_conn.return_value.execute = MagicMock()
        result = daemon.budget_check()

    assert result["action"] == "none"
    assert result["today_spend_usd"] == 2.0
    assert result["percentage"] == 40.0


def test_budget_check_exceeds_limit_triggers_pause(daemon):
    """Budget check pauses when spend exceeds limit."""
    mock_harness_cfg = {"budget": {"daily_limit_usd": 5.0}}
    mock_spend = {"total_spend_usd": 5.50, "date": "2026-01-01", "execution_count": 50}

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.harness_config.load_harness_config", return_value=mock_harness_cfg), \
         patch("cap.lib.budget_manager.init_budget_log_table"), \
         patch("cap.lib.budget_manager.get_today_spend", return_value=mock_spend), \
         patch("cap.lib.budget_manager.is_budget_paused", return_value=False), \
         patch("cap.lib.budget_manager.pause_budget") as mock_pause, \
         patch("sqlite3.connect") as mock_conn:
        mock_conn.return_value.execute = MagicMock()
        result = daemon.budget_check()

    assert result["action"] == "paused"
    mock_pause.assert_called_once()


def test_budget_check_already_paused_no_action(daemon):
    """Budget check does nothing if already paused."""
    mock_harness_cfg = {"budget": {"daily_limit_usd": 5.0}}
    mock_spend = {"total_spend_usd": 10.0, "date": "2026-01-01", "execution_count": 100}

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")), \
         patch("cap.lib.harness_config.load_harness_config", return_value=mock_harness_cfg), \
         patch("cap.lib.budget_manager.init_budget_log_table"), \
         patch("cap.lib.budget_manager.get_today_spend", return_value=mock_spend), \
         patch("cap.lib.budget_manager.is_budget_paused", return_value=True), \
         patch("sqlite3.connect") as mock_conn:
        mock_conn.return_value.execute = MagicMock()
        result = daemon.budget_check()

    assert result["action"] == "none"
    assert result["paused"] is True


def test_budget_check_handles_error(daemon):
    """Budget check gracefully handles exceptions."""
    with patch("cap.harness.daemon.CapDaemon._data_dir", side_effect=RuntimeError("no data")):
        result = daemon.budget_check()
    assert "error" in result


# ── Stale Cleanup ────────────────────────────────────────────────────────────


def test_cleanup_stale_returns_count(daemon):
    """Cleanup stale returns terminated count."""
    with patch("cap.harness.agent_store.cleanup_stale", return_value=3):
        result = daemon.cleanup_stale()
    assert result == {"terminated": 3}


def test_cleanup_stale_handles_error(daemon):
    """Cleanup stale handles errors gracefully."""
    with patch("cap.harness.agent_store.cleanup_stale", side_effect=Exception("db locked")):
        result = daemon.cleanup_stale()
    assert "error" in result


# ── Reembed Patterns ─────────────────────────────────────────────────────────


def test_reembed_patterns_available(daemon):
    """Reembed patterns when embedder is available."""
    mock_pe = MagicMock()
    mock_pe.is_available = True
    mock_pe.bulk_embed_missing.return_value = 10

    with patch("cap.harness.vector_patterns.PatternEmbedder", return_value=mock_pe):
        result = daemon.reembed_patterns()

    assert result == {"embedded": 10}


def test_reembed_patterns_unavailable(daemon):
    """Reembed patterns when embedder is unavailable."""
    mock_pe = MagicMock()
    mock_pe.is_available = False

    with patch("cap.harness.vector_patterns.PatternEmbedder", return_value=mock_pe):
        result = daemon.reembed_patterns()

    assert result == {"skipped": "embedder_unavailable"}


# ── Compact Vectors ──────────────────────────────────────────────────────────


def test_compact_vectors_no_dir(daemon):
    """Compact vectors when vectors dir doesn't exist."""
    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/nonexistent")):
        result = daemon.compact_vectors()

    assert result["compacted"] is False


def test_compact_vectors_with_lancedb(daemon, tmp_path):
    """Compact vectors when LanceDB is available."""
    vectors_dir = tmp_path / "vectors"
    vectors_dir.mkdir()

    mock_table = MagicMock()
    mock_lance = MagicMock()
    mock_lance.table_names.return_value = ["test_table"]
    mock_lance.open_table.return_value = mock_table

    with patch("cap.harness.daemon.CapDaemon._data_dir", return_value=tmp_path), \
         patch("lancedb.connect", return_value=mock_lance):
        result = daemon.compact_vectors()

    assert result["compacted"] is True
    mock_table.compact_files.assert_called_once()


# ── Workspace Detection ──────────────────────────────────────────────────────


def test_workspace_detect_no_file(daemon, tmp_cap_home):
    """Workspace detect with no pending file returns zeros."""
    result = daemon.workspace_detect()
    assert result["new_workspaces"] == 0


def test_workspace_detect_empty_file(daemon, tmp_cap_home):
    """Workspace detect with empty pending file returns zeros."""
    pending = tmp_cap_home / "run" / "pending_workspaces"
    pending.write_text("")
    result = daemon.workspace_detect()
    assert result["new_workspaces"] == 0


def test_workspace_detect_with_paths(daemon, tmp_cap_home, tmp_path):
    """Workspace detect indexes new workspaces."""
    # Create a real directory to index
    ws_dir = tmp_path / "my-project"
    ws_dir.mkdir()

    pending = tmp_cap_home / "run" / "pending_workspaces"
    pending.write_text(f"{ws_dir}\n")

    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = (0,)  # Not yet indexed

    with patch("cap.lib.config.load_config") as mock_config, \
         patch("cap.lib.db_init.init_knowledge_db", return_value=mock_db), \
         patch("cap.lib.sync_engine.sync_workspace") as mock_sync:
        mock_config.return_value.data_dir = tmp_cap_home / "data"
        result = daemon.workspace_detect()

    assert result["new_workspaces"] == 1
    assert result["indexed"] == 1
    mock_sync.assert_called_once()


# ── Legacy run_once ──────────────────────────────────────────────────────────


def test_run_once_all_success(daemon):
    """Legacy run_once runs all tasks and returns structured dict."""
    with (
        patch("cap.harness.daemon.CapDaemon._data_dir", return_value=Path("/tmp")),
        patch("cap.lib.db_init.init_knowledge_db", return_value=MagicMock()),
        patch("cap.lib.db_init.init_sessions_db", return_value=MagicMock()),
        patch("cap.lib.consolidator.consolidate", return_value=MagicMock(expired_deleted=3, duplicates_removed=1)),
        patch("cap.harness.agent_store.cleanup_stale", return_value=2),
        patch("cap.harness.daemon.CapDaemon._run_pattern_embedding", return_value={"embedded": 5}),
        patch("cap.learning.engine.compute_thresholds_from_session_events", return_value={"sample_count": 4}),
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


# ── Service File Generation ──────────────────────────────────────────────────


class TestServiceGeneration:
    """Test OS service file generation."""

    def test_plist_generation(self, tmp_cap_home):
        """macOS plist contains required fields."""
        from cap.cli.daemon_service import _generate_plist, SERVICE_LABEL

        plist = _generate_plist()
        assert SERVICE_LABEL in plist
        assert "RunAtLoad" in plist
        assert "KeepAlive" in plist
        assert "cap.harness.daemon" in plist
        assert "ThrottleInterval" in plist
        assert str(tmp_cap_home) in plist

    def test_systemd_unit_generation(self, tmp_cap_home):
        """Linux systemd unit contains required fields."""
        from cap.cli.daemon_service import _generate_systemd_unit, SERVICE_DESCRIPTION

        unit = _generate_systemd_unit()
        assert SERVICE_DESCRIPTION in unit
        assert "cap.harness.daemon" in unit
        assert "Restart=on-failure" in unit
        assert "RestartSec=10" in unit
        assert str(tmp_cap_home) in unit
        assert "WantedBy=default.target" in unit

    def test_install_service_macos(self, tmp_cap_home, tmp_path):
        """install_service on macOS writes plist file."""
        from cap.cli.daemon_service import install_service, is_service_installed

        with patch("platform.system", return_value="Darwin"), \
             patch("cap.cli.daemon_service._launchagent_dir", return_value=tmp_path), \
             patch("cap.cli.daemon_service._launchagent_path", return_value=tmp_path / "com.cap.daemon.plist"), \
             patch("os.system"):
            path = install_service()

        assert Path(path).exists()
        content = Path(path).read_text()
        assert "cap.harness.daemon" in content

    def test_install_service_linux(self, tmp_cap_home, tmp_path):
        """install_service on Linux writes systemd unit file."""
        from cap.cli.daemon_service import install_service

        service_path = tmp_path / "cap-daemon.service"
        with patch("platform.system", return_value="Linux"), \
             patch("cap.cli.daemon_service._systemd_dir", return_value=tmp_path), \
             patch("cap.cli.daemon_service._systemd_path", return_value=service_path), \
             patch("os.system"):
            path = install_service()

        assert Path(path).exists()
        content = Path(path).read_text()
        assert "Restart=on-failure" in content

    def test_uninstall_service_not_installed(self, tmp_cap_home):
        """uninstall_service returns False when not installed."""
        from cap.cli.daemon_service import uninstall_service

        with patch("platform.system", return_value="Darwin"), \
             patch("cap.cli.daemon_service._launchagent_path", return_value=Path("/nonexistent/file.plist")):
            result = uninstall_service()
        assert result is False

    def test_is_service_installed_false(self, tmp_cap_home):
        """is_service_installed returns False when no file."""
        from cap.cli.daemon_service import is_service_installed

        with patch("platform.system", return_value="Darwin"), \
             patch("cap.cli.daemon_service._launchagent_path", return_value=Path("/nonexistent/file.plist")):
            assert is_service_installed() is False

    def test_is_service_installed_true(self, tmp_cap_home, tmp_path):
        """is_service_installed returns True when file exists."""
        from cap.cli.daemon_service import is_service_installed

        plist = tmp_path / "com.cap.daemon.plist"
        plist.write_text("<plist/>")

        with patch("platform.system", return_value="Darwin"), \
             patch("cap.cli.daemon_service._launchagent_path", return_value=plist):
            assert is_service_installed() is True

    def test_unsupported_platform(self, tmp_cap_home):
        """install_service raises on unsupported platform."""
        from cap.cli.daemon_service import install_service

        with patch("platform.system", return_value="Windows"), \
             pytest.raises(RuntimeError, match="Unsupported platform"):
            install_service()


# ── CLI Integration ──────────────────────────────────────────────────────────


def test_cli_daemon_status():
    """cap daemon status runs without error."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()
    with patch("cap.harness.daemon.read_pid", return_value=None), \
         patch("cap.harness.daemon.is_daemon_running", return_value=False):
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_cli_daemon_start_once():
    """cap daemon start --once should print JSON and exit."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()
    with patch("cap.harness.daemon.CapDaemon.run_once", return_value={"consolidation": {"expired": 0}}):
        result = runner.invoke(cli, ["daemon", "start", "--once"])

    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert "consolidation" in data


def test_cli_daemon_start_already_running():
    """cap daemon start when already running shows warning."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()
    with patch("cap.harness.daemon.is_daemon_running", return_value=True), \
         patch("cap.harness.daemon.read_pid", return_value=12345):
        result = runner.invoke(cli, ["daemon", "start"])

    assert result.exit_code == 0
    assert "already running" in result.output.lower()


def test_cli_daemon_stop_not_running():
    """cap daemon stop when not running shows message."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()
    with patch("cap.harness.daemon.read_pid", return_value=None), \
         patch("cap.harness.daemon.is_daemon_running", return_value=False):
        result = runner.invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_cli_daemon_logs_no_file():
    """cap daemon logs when no log file shows message."""
    from click.testing import CliRunner
    from cap.cli.main import cli

    runner = CliRunner()
    with patch("cap.harness.daemon._log_path", return_value=Path("/nonexistent/daemon.log")):
        result = runner.invoke(cli, ["daemon", "logs"])

    assert result.exit_code == 0
    assert "no log file" in result.output.lower()


def test_cli_daemon_group_registered():
    """daemon command group must be registered on the main CLI."""
    from cap.cli.main import cli
    assert "daemon" in cli.commands


def test_cli_daemon_subcommands_registered():
    """All daemon subcommands must be registered."""
    from cap.cli.main import daemon_group

    subcommands = list(daemon_group.commands.keys())
    assert "status" in subcommands
    assert "start" in subcommands
    assert "stop" in subcommands
    assert "restart" in subcommands
    assert "logs" in subcommands
    assert "install" in subcommands
    assert "uninstall" in subcommands


# ── Logging Setup ────────────────────────────────────────────────────────────


def test_setup_logging_creates_dir(tmp_cap_home):
    """setup_logging creates log directory if needed."""
    log_dir = tmp_cap_home / "logs"
    if log_dir.exists():
        import shutil
        shutil.rmtree(log_dir)

    setup_logging()
    assert log_dir.exists()


# ── Status Info ──────────────────────────────────────────────────────────────


def test_status_info_not_running(daemon, tmp_cap_home):
    """status_info returns None PID when not started."""
    info = daemon.status_info()
    assert info["pid"] is None
    assert info["running"] is False
    assert info["uptime_seconds"] == 0.0


def test_status_info_with_pid(daemon, tmp_cap_home):
    """status_info reads PID from file."""
    write_pid()
    info = daemon.status_info()
    assert info["pid"] == os.getpid()

"""
E2E Scenario: First Install (cap init in a new workspace)

USER DOES:
  pip install claude-agent-platform  (or uv tool install)
  cap init

SHOULD HAPPEN:
  - ~/.claude-platform/ directory tree created
  - 4 databases initialized (platform.db, knowledge.db, sessions.db, fleet.db)
  - ~/.claude/settings.json updated with hook entries
  - ~/.claude.json updated with MCP server registrations
  - Originals backed up to ~/.claude-platform/backups/
  - Hook scripts generated as thin wrappers (not inline logic)
  - cap status exits 0 and shows all databases healthy
  - cap doctor exits 0 (no issues)

FAILURE MODES:
  - init fails if ~/.claude.json already exists and cannot be backed up
  - init fails if target directory is read-only
  - init with --minimal skips MCP registration but still creates DBs
  - re-running cap init is idempotent (no duplicate MCP entries)

VERIFY:
  - All 4 DB files exist and have correct table counts
  - settings.json hooks point to generated scripts
  - Backup directory contains originals
  - Hook scripts contain thin-wrapper pattern (no inline sqlite3)
  - cap status output contains all required sections
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Completely isolated home directory for install testing."""
    home = tmp_path / "home"
    home.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir()

    # Minimal pre-existing claude.json (user already has claude code)
    claude_json = home / ".claude.json"
    claude_json.write_text(json.dumps({
        "numStartups": 5,
        "mcpServers": {}
    }))

    # Empty settings.json
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"permissions": {}}))

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CAP_HOME", str(home / ".claude-platform"))
    return home


class TestFirstInstallDatabaseInit:
    """Databases are created with correct schema on cap init."""

    def test_platform_db_created(self, isolated_home, tmp_path):
        from cap.lib.db_init import init_platform_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = init_platform_db(data_dir)
        assert conn is not None
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        # Core workflow tables must exist
        assert "workflows" in tables
        assert "workflow_events" in tables
        assert "budget_ledger" in tables
        conn.close()

    def test_knowledge_db_created(self, isolated_home, tmp_path):
        from cap.lib.db_init import init_knowledge_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = init_knowledge_db(data_dir)
        assert conn is not None
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "knowledge_entries" in tables
        assert "knowledge_graph_nodes" in tables
        assert "knowledge_graph_edges" in tables
        assert "business_knowledge" in tables
        assert "embedding_queue" in tables
        conn.close()

    def test_sessions_db_created(self, isolated_home, tmp_path):
        from cap.lib.db_init import init_sessions_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = init_sessions_db(data_dir)
        assert conn is not None
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "sessions" in tables
        assert "learnings" in tables
        assert "corrections" in tables
        assert "decisions" in tables
        conn.close()

    def test_fleet_db_created(self, isolated_home, tmp_path):
        from cap.lib.db_init import init_fleet_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = init_fleet_db(data_dir)
        assert conn is not None
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "fleet_servers" in tables
        assert "fleet_events" in tables
        conn.close()

    def test_databases_have_wal_mode(self, tmp_path):
        """All databases must use WAL mode for concurrent access."""
        from cap.lib.db_init import init_platform_db, init_knowledge_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        for init_fn in [init_platform_db, init_knowledge_db]:
            conn = init_fn(data_dir)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"{init_fn.__name__} should use WAL mode, got {mode}"
            conn.close()

    def test_init_is_idempotent(self, tmp_path):
        """Running init twice does not fail or create duplicate tables."""
        from cap.lib.db_init import init_knowledge_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn1 = init_knowledge_db(data_dir)
        count1 = conn1.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        conn1.close()

        conn2 = init_knowledge_db(data_dir)
        count2 = conn2.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        conn2.close()
        assert count1 == count2


class TestHookGeneration:
    """Hook scripts are generated as thin wrappers, not with inline logic."""

    def test_generated_hooks_use_thin_wrapper_pattern(self, tmp_path):
        from cap.cli.lifecycle import _generate_hook_scripts
        claude_dir = tmp_path / ".claude"
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _generate_hook_scripts(claude_dir, data_dir)

        for hook in ("pretool.py", "posttool.py"):
            content = (claude_dir / hook).read_text()
            assert "from cap.hooks." in content, f"{hook} must use thin-wrapper import"
            assert "sqlite3" not in content, f"{hook} must not have inline DB logic"
            assert "except ImportError:" in content, f"{hook} must handle missing package gracefully"

    def test_hooks_are_executable(self, tmp_path):
        from cap.cli.lifecycle import _generate_hook_scripts
        claude_dir = tmp_path / ".claude"
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _generate_hook_scripts(claude_dir, data_dir)

        for hook in ("pretool.py", "posttool.py"):
            path = claude_dir / hook
            assert path.stat().st_mode & 0o111, f"{hook} must be executable"

    def test_reinit_does_not_duplicate_hooks(self, tmp_path):
        """Running init a second time overwrites hooks, not appends."""
        from cap.cli.lifecycle import _generate_hook_scripts
        claude_dir = tmp_path / ".claude"
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _generate_hook_scripts(claude_dir, data_dir)
        content_after_first = (claude_dir / "pretool.py").read_text()

        _generate_hook_scripts(claude_dir, data_dir)
        content_after_second = (claude_dir / "pretool.py").read_text()

        # Content must be identical, not doubled
        assert content_after_first == content_after_second


class TestInstallBackup:
    """Pre-existing user configs are backed up before modification."""

    def test_backup_file_creates_timestamped_copy(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _backup_file, _backups_dir
        # Point CAP_HOME at tmp_path so backups land there
        monkeypatch.setenv("CAP_HOME", str(tmp_path / ".cap"))

        config_file = tmp_path / ".claude.json"
        config_file.write_text('{"numStartups": 3}')

        backup_path = _backup_file(config_file, "claude-json")

        assert backup_path is not None
        assert backup_path.exists()
        assert "claude-json.backup." in backup_path.name

    def test_backup_preserves_original_content(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _backup_file
        monkeypatch.setenv("CAP_HOME", str(tmp_path / ".cap"))

        original_content = '{"numStartups": 3, "custom": "value"}'
        config_file = tmp_path / ".claude.json"
        config_file.write_text(original_content)

        backup_path = _backup_file(config_file, "claude-json")

        assert backup_path is not None
        assert backup_path.read_text() == original_content


class TestInstallMCPRegistration:
    """MCP server config list is built correctly and does not duplicate entries."""

    def test_get_cap_mcp_servers_returns_list(self, tmp_path):
        """_get_cap_mcp_servers returns a list of server dicts with name and command."""
        from cap.cli.lifecycle import _get_cap_mcp_servers
        cap_home = tmp_path / ".cap"
        cap_home.mkdir(parents=True)
        data_dir = cap_home / "data"
        data_dir.mkdir()

        servers = _get_cap_mcp_servers(cap_home, data_dir)
        assert isinstance(servers, list)
        assert len(servers) > 0
        for srv in servers:
            assert "name" in srv, f"Server entry missing 'name': {srv}"
            assert "command" in srv or "cmd" in srv or "args" in srv, \
                f"Server entry missing command/args: {srv}"

    def test_cap_server_names_start_with_cap(self, tmp_path):
        """All CAP-managed MCP servers use 'cap-' prefix."""
        from cap.cli.lifecycle import _get_cap_mcp_servers
        cap_home = tmp_path / ".cap"
        cap_home.mkdir(parents=True)
        data_dir = cap_home / "data"
        data_dir.mkdir()

        servers = _get_cap_mcp_servers(cap_home, data_dir)
        for srv in servers:
            assert srv["name"].startswith("cap-"), \
                f"Expected cap- prefix, got: {srv['name']}"

    def test_mcp_server_exists_returns_false_for_unknown(self, tmp_path, monkeypatch):
        """_mcp_server_exists returns False when server is not in ~/.claude.json."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": {}}))

        from cap.cli.lifecycle import _mcp_server_exists
        assert _mcp_server_exists("cap-knowledge") is False

    def test_mcp_server_exists_returns_true_when_present(self, tmp_path, monkeypatch):
        """_mcp_server_exists returns True when server is already registered."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {"cap-knowledge": {"command": "cap-knowledge"}}
        }))

        from cap.cli.lifecycle import _mcp_server_exists
        assert _mcp_server_exists("cap-knowledge") is True

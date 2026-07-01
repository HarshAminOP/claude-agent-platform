"""Tests for cap init UX improvements (workspace auto-detect, minimal mode,
error messages, post-init verification, PATH check)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _import_lifecycle():
    import importlib
    import cap.cli.lifecycle as lc
    return lc


# ── 1. Workspace auto-detection ───────────────────────────────────────────────

class TestResolveWorkspace:
    def test_explicit_arg_returned_as_is(self, tmp_path):
        from cap.cli.lifecycle import _resolve_workspace
        result = _resolve_workspace(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_no_arg_git_root_in_cwd(self, tmp_path, monkeypatch):
        """When CWD is inside a git repo, returns the git root."""
        from cap.cli.lifecycle import _resolve_workspace
        git_root = tmp_path / "myrepo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        subdir = git_root / "src" / "pkg"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        result = _resolve_workspace(None)
        assert result == git_root

    def test_no_arg_no_git_falls_back_to_cwd(self, tmp_path, monkeypatch):
        """When no .git is found walking up, returns CWD."""
        from cap.cli.lifecycle import _resolve_workspace
        bare_dir = tmp_path / "nogit"
        bare_dir.mkdir()
        monkeypatch.chdir(bare_dir)
        result = _resolve_workspace(None)
        assert result == bare_dir

    def test_no_arg_cwd_is_git_root(self, tmp_path, monkeypatch):
        """When CWD itself is the git root, returns CWD."""
        from cap.cli.lifecycle import _resolve_workspace
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = _resolve_workspace(None)
        assert result == tmp_path


# ── 2. Minimal MCP server filter ──────────────────────────────────────────────

class TestFilterCapServersMinimal:
    def test_only_knowledge_and_session_kept(self):
        from cap.cli.lifecycle import _filter_cap_servers_minimal
        servers = [
            {"name": "cap-knowledge"},
            {"name": "cap-session"},
            {"name": "cap-orchestrator"},
            {"name": "cap-code-intel"},
            {"name": "cap-fleet"},
            {"name": "cap-diagram"},
            {"name": "cap-backlog"},
            {"name": "cap-workflow-engine"},
        ]
        result = _filter_cap_servers_minimal(servers)
        names = {s["name"] for s in result}
        assert names == {"cap-knowledge", "cap-session"}

    def test_empty_input(self):
        from cap.cli.lifecycle import _filter_cap_servers_minimal
        assert _filter_cap_servers_minimal([]) == []

    def test_preserves_server_dict_content(self):
        from cap.cli.lifecycle import _filter_cap_servers_minimal
        srv = {"name": "cap-knowledge", "command": "python3", "args": ["server.py"], "env": []}
        result = _filter_cap_servers_minimal([srv])
        assert result == [srv]


# ── 3. Python version check ───────────────────────────────────────────────────

class TestCheckPythonVersion:
    def test_current_version_passes(self):
        from cap.cli.lifecycle import _check_python_version
        ok, msg = _check_python_version()
        assert ok is True
        assert "OK" in msg

    def test_old_version_fails(self, monkeypatch):
        from cap.cli.lifecycle import _check_python_version
        import cap.cli.lifecycle as lc
        monkeypatch.setattr(lc.sys, "version_info", (3, 9, 0, "final", 0))
        ok, msg = _check_python_version()
        assert ok is False
        assert "3.9" in msg
        assert "3.11" in msg

    def test_error_message_contains_upgrade_hint(self, monkeypatch):
        from cap.cli.lifecycle import _check_python_version
        import cap.cli.lifecycle as lc
        monkeypatch.setattr(lc.sys, "version_info", (3, 10, 0, "final", 0))
        ok, msg = _check_python_version()
        assert ok is False
        # Must tell user which version is needed and how to upgrade
        assert "3.11" in msg
        assert any(hint in msg for hint in ("brew", "python.org", "upgrade", "Upgrade"))


# ── 4. Claude settings file warning ──────────────────────────────────────────

class TestWarnIfSettingsMissing:
    def test_returns_none_when_settings_exist(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _warn_if_settings_missing
        import cap.cli.lifecycle as lc
        settings = tmp_path / "settings.json"
        settings.write_text("{}")
        monkeypatch.setattr(lc, "_settings_json_path", lambda: settings)
        assert _warn_if_settings_missing() is None

    def test_returns_message_with_path_when_missing(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _warn_if_settings_missing
        import cap.cli.lifecycle as lc
        missing = tmp_path / "nonexistent" / "settings.json"
        monkeypatch.setattr(lc, "_settings_json_path", lambda: missing)
        msg = _warn_if_settings_missing()
        assert msg is not None
        assert str(missing) in msg


# ── 5. Post-init verification ─────────────────────────────────────────────────

class TestRunPostInitVerification:
    def _make_knowledge_db(self, data_dir: Path):
        db = data_dir / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE knowledge_entries (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        return db

    def test_knowledge_db_yes_when_exists(self, tmp_path):
        from cap.cli.lifecycle import _run_post_init_verification
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        self._make_knowledge_db(data_dir)
        rows = _run_post_init_verification(data_dir)
        kb_row = next(r for r in rows if "knowledge" in r[0])
        assert kb_row[1] == "yes"

    def test_knowledge_db_no_when_missing(self, tmp_path):
        from cap.cli.lifecycle import _run_post_init_verification
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        kb_row = next(r for r in rows if "knowledge" in r[0])
        assert kb_row[1] == "no"
        # Detail must contain the expected path
        assert "knowledge.db" in kb_row[2]

    def test_mcp_count_from_claude_json(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _run_post_init_verification
        import cap.cli.lifecycle as lc
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {"cap-knowledge": {}, "cap-session": {}, "other": {}}
        }))
        monkeypatch.setattr(lc, "_claude_json_path", lambda: claude_json)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        mcp_row = next(r for r in rows if "MCP" in r[0])
        assert mcp_row[1] == "3"

    def test_mcp_zero_when_claude_json_missing(self, tmp_path, monkeypatch):
        from cap.cli.lifecycle import _run_post_init_verification
        import cap.cli.lifecycle as lc
        monkeypatch.setattr(lc, "_claude_json_path", lambda: tmp_path / "no.json")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        mcp_row = next(r for r in rows if "MCP" in r[0])
        assert mcp_row[1] == "0"
        # Detail must tell user the expected path
        assert str(tmp_path / "no.json") in mcp_row[2]

    def test_cap_importable_yes(self, tmp_path):
        from cap.cli.lifecycle import _run_post_init_verification
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        cap_row = next(r for r in rows if "cap importable" in r[0])
        # In a dev install, cap is importable
        assert cap_row[1] == "yes"

    def test_returns_three_rows(self, tmp_path):
        from cap.cli.lifecycle import _run_post_init_verification
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = _run_post_init_verification(data_dir)
        assert len(rows) == 3


# ── 6. PATH check ─────────────────────────────────────────────────────────────

class TestCapOnPath:
    def test_returns_true_when_cap_found(self, monkeypatch):
        import shutil
        import cap.cli.lifecycle as lc
        monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/local/bin/cap" if cmd == "cap" else None)
        assert lc._cap_on_path() is True

    def test_returns_false_when_cap_not_found(self, monkeypatch):
        import shutil
        import cap.cli.lifecycle as lc
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        assert lc._cap_on_path() is False

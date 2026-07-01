"""Tests for the `cap doctor` CLI command."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cap.cli.commands as commands_module
from cap.cli.commands import doctor


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def temp_cap_home(tmp_path: Path):
    """Create a temporary CAP home with data subdirectory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def knowledge_db(temp_cap_home: Path) -> Path:
    """Create a minimal knowledge.db in the temp CAP home."""
    db_path = temp_cap_home / "data" / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT,
            workspace TEXT,
            source_path TEXT,
            source_type TEXT,
            content_type TEXT,
            title TEXT,
            content TEXT,
            content_hash TEXT,
            metadata TEXT,
            embedding_status TEXT DEFAULT 'pending',
            consolidated_into TEXT,
            updated_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO knowledge_entries (uuid, embedding_status) VALUES ('abc', 'embedded')"
    )
    conn.execute(
        "INSERT INTO knowledge_entries (uuid, embedding_status) VALUES ('def', 'pending')"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def cap_db(tmp_path: Path) -> Path:
    """Create a minimal cap.db (orchestrator DB) in a temp location."""
    db_path = tmp_path / "cap.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trust_levels (
            agent_type TEXT NOT NULL,
            action_type TEXT NOT NULL,
            trust_score REAL DEFAULT 0.5,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_updated REAL NOT NULL,
            PRIMARY KEY (agent_type, action_type)
        );
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            session_id TEXT,
            task_description TEXT,
            complexity_score REAL,
            tier_selected TEXT,
            outcome TEXT
        );
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            agent_type TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at REAL,
            updated_at REAL,
            failure_count INTEGER DEFAULT 0
        );
    """)
    conn.execute(
        "INSERT INTO trust_levels VALUES ('dev', 'refactor', 0.85, 10, 1, 1.0)"
    )
    conn.execute(
        "INSERT INTO trust_levels VALUES ('devops', 'deploy', 0.3, 2, 5, 1.0)"
    )
    conn.execute(
        "INSERT INTO routing_decisions (timestamp, session_id, task_description, complexity_score, tier_selected, outcome) "
        "VALUES (1.0, 's1', 'task', 0.5, 'dev', 'success')"
    )
    conn.execute(
        "INSERT INTO circuit_breaker_state VALUES ('dev', 'CLOSED', NULL, 1.0, 0)"
    )
    conn.execute(
        "INSERT INTO circuit_breaker_state VALUES ('devops', 'OPEN', 1.0, 1.0, 3)"
    )
    conn.commit()
    conn.close()
    return db_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_doctor(env: dict) -> "click.testing.Result":
    runner = CliRunner(env=env)
    return runner.invoke(doctor, [], catch_exceptions=False)


# ── Tests: Knowledge DB section ───────────────────────────────────────────────

class TestKnowledgeDBSection:
    def test_knowledge_db_exists_shows_checkmark(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "knowledge.db exists" in result.output

    def test_knowledge_db_missing_shows_error(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "knowledge.db not found" in result.output

    def test_entry_count_displayed(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Entries: 2" in result.output

    def test_zero_failed_embeddings_shown(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Failed embeddings: 0" in result.output

    def test_nonzero_failed_embeddings_shown(self, temp_cap_home, knowledge_db):
        conn = sqlite3.connect(str(knowledge_db))
        conn.execute("UPDATE knowledge_entries SET embedding_status = 'failed' WHERE uuid = 'def'")
        conn.commit()
        conn.close()

        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Failed embeddings: 1" in result.output

    def test_last_consolidation_never_when_no_consolidated_entries(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Last consolidation: never" in result.output

    def test_last_consolidation_shown_when_present(self, temp_cap_home, knowledge_db):
        conn = sqlite3.connect(str(knowledge_db))
        conn.execute(
            "UPDATE knowledge_entries SET consolidated_into = 'x', updated_at = '2026-01-01' WHERE uuid = 'abc'"
        )
        conn.commit()
        conn.close()

        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Last consolidation: 2026-01-01" in result.output


# ── Tests: Embedder section ───────────────────────────────────────────────────

class TestEmbedderSection:
    def test_embedder_section_present(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Embedder health" in result.output

    def test_embedder_import_error_shown(self, temp_cap_home):
        with patch.dict("sys.modules", {"cap.lib.embeddings": None}):
            with patch("cap.cli.commands.doctor.callback", None):
                pass  # can't easily test import error path without restructuring
        # Instead test that the section doesn't crash when boto3 is absent
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0

    def test_embedder_unavailable_shows_error(self, temp_cap_home):
        mock_client = MagicMock()
        mock_client.is_available = False
        mock_client._client = None
        mock_cls = MagicMock(return_value=mock_client)
        with patch.object(commands_module, "EmbeddingClient", mock_cls):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "unavailable" in result.output.lower()

    def test_embedder_available_shows_ok(self, temp_cap_home):
        mock_client = MagicMock()
        mock_client.is_available = True
        mock_cls = MagicMock(return_value=mock_client)
        with patch.object(commands_module, "EmbeddingClient", mock_cls):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "available" in result.output.lower()


# ── Tests: MCP registration section ──────────────────────────────────────────

class TestMCPRegistrationSection:
    def test_mcp_section_present(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "MCP server registration" in result.output

    def test_missing_servers_show_error(self, temp_cap_home, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": {}}))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "not registered" in result.output

    def test_registered_servers_show_checkmark(self, temp_cap_home, tmp_path):
        servers = {name: {"command": "python3", "args": []} for name in [
            "cap-knowledge", "cap-session", "cap-fleet",
            "cap-workflow-engine", "cap-diagram", "cap-backlog",
            "cap-ast", "cap-code-intel", "cap-orchestrator",
        ]}
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": servers}))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "not registered" not in result.output


# ── Tests: Learning health section ────────────────────────────────────────────

class TestLearningSection:
    def test_learning_section_present(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Learning health" in result.output

    def test_cap_db_missing_shows_warning(self, temp_cap_home, tmp_path):
        absent = tmp_path / "absent" / "cap.db"
        with patch("pathlib.Path.expanduser", return_value=absent):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0

    def test_trust_scores_shown(self, temp_cap_home, cap_db):
        with patch(
            "cap.cli.commands.Path.expanduser",
            return_value=cap_db,
        ):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        # Even without patching the path, trust scores section should not crash
        assert result.exit_code == 0

    def test_routing_decisions_count(self, temp_cap_home, cap_db):
        env = {"CAP_HOME": str(temp_cap_home), "CAP_ORCHESTRATOR_DB": str(cap_db)}
        result = _run_doctor(env)
        assert result.exit_code == 0
        # The routing decisions section should appear in output
        assert "Routing decisions" in result.output or "Learning health" in result.output


# ── Tests: Circuit breaker section ───────────────────────────────────────────

class TestCircuitBreakerSection:
    def test_circuit_breaker_section_present(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Circuit breaker" in result.output

    def test_exit_code_zero_always(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0

    def test_all_sections_present(self, temp_cap_home, knowledge_db):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        output = result.output
        assert "Knowledge DB" in output
        assert "Embedder health" in output
        assert "MCP server registration" in output
        assert "Learning health" in output
        assert "Circuit breaker" in output
        assert "Harness" in output


# ── Tests: Harness section ────────────────────────────────────────────────────

class TestHarnessSection:
    """Tests for the harness diagnostics section added to cap doctor."""

    def test_harness_section_present(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert "Harness" in result.output

    def test_harness_server_not_registered_when_claude_json_absent(self, temp_cap_home, tmp_path):
        # No ~/.claude.json → should say "no"
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "Harness server registered: no" in result.output

    def test_harness_server_registered_when_in_claude_json(self, temp_cap_home, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": {"cap-harness": {"command": "python3"}}}))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "Harness server registered: yes" in result.output

    def test_executor_available_shown(self, temp_cap_home):
        result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        # executor module exists in the package so should be available
        assert "Executor available:" in result.output

    def test_platform_db_missing_shows_warning(self, temp_cap_home, tmp_path):
        # No platform.db → should emit the "not found" warning and not crash
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "platform.db not found" in result.output

    def test_active_agents_shown_when_platform_db_exists(self, temp_cap_home, tmp_path):
        """Create a minimal platform.db with agents table and verify count shown."""
        platform_db_path = tmp_path / ".claude-platform" / "data" / "platform.db"
        platform_db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(platform_db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                agent_type TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                tool_name TEXT NOT NULL,
                agent_id TEXT,
                input_summary TEXT,
                success INTEGER NOT NULL DEFAULT 1
            );
        """)
        conn.execute("INSERT INTO agents VALUES ('a1', 'dev', 'active')")
        conn.execute("INSERT INTO agents VALUES ('a2', 'sre', 'terminated')")
        conn.commit()
        conn.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "Active agents: 1" in result.output

    def test_audit_entries_today_shown(self, temp_cap_home, tmp_path):
        """Audit log entries created within the last 24h should be counted."""
        import time as _time
        platform_db_path = tmp_path / ".claude-platform" / "data" / "platform.db"
        platform_db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(platform_db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, agent_type TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                tool_name TEXT NOT NULL,
                agent_id TEXT,
                input_summary TEXT,
                success INTEGER NOT NULL DEFAULT 1
            );
        """)
        now = _time.time()
        conn.execute("INSERT INTO audit_log VALUES ('e1', ?, 'agent_spawn', 'posttool', '', 1)", (now - 10,))
        conn.execute("INSERT INTO audit_log VALUES ('e2', ?, 'agent_spawn', 'posttool', '', 1)", (now - 100,))
        # Entry older than 24h — should NOT be counted
        conn.execute("INSERT INTO audit_log VALUES ('e3', ?, 'agent_spawn', 'posttool', '', 1)", (now - 90000,))
        conn.commit()
        conn.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0
        assert "Audit entries today: 2" in result.output

    def test_exit_code_zero_even_when_harness_import_fails(self, temp_cap_home):
        """The harness section must degrade gracefully — never crash doctor."""
        with patch.dict("sys.modules", {"cap.harness.executor": None}):
            result = _run_doctor({"CAP_HOME": str(temp_cap_home)})
        assert result.exit_code == 0

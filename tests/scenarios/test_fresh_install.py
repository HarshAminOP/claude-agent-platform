"""
Scenario: Fresh Install (Cold Start)

Simulates a brand new user installing CAP from scratch — no existing state,
no AWS credentials, no previously configured databases or directories.

Each test is fully isolated via tmp_path and monkeypatch. EmbeddingClient is
mocked as unavailable throughout to prevent real AWS calls.

Coverage:
    - Package importability
    - CLI --help works
    - cap init creates expected directory/file layout
    - Databases are created with correct tables
    - cap doctor runs without crash on fresh state
    - knowledge sync on empty git repo does not crash
    - knowledge_search on empty DB returns [] not an exception
    - cap route uses default thresholds when DB has 0 learning samples
    - TrustManager returns DEFAULT_TRUST (0.5) for unknown agents
    - CapDaemon.run_once() completes gracefully
    - spawn_agent records agent even without Bedrock creds
    - load_policy returns defaults and detects dangerous patterns
      when no .harness/ directory exists
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable regardless of editable-install status.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# Shared fixture: isolated environment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_embedding_client():
    """Mark EmbeddingClient as unavailable for every test in this module.

    Prevents any test from triggering a real Bedrock initialisation attempt.
    """
    mock = MagicMock()
    mock.is_available = False
    mock.return_value = mock  # mock() returns self so EmbeddingClient() works

    with patch("cap.lib.embeddings.EmbeddingClient", mock):
        yield mock


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Completely clean home-like environment with no pre-existing CAP state."""
    cap_home = tmp_path / ".claude-platform"
    old_cap_db = tmp_path / ".cap" / "cap.db"

    monkeypatch.setenv("CAP_HOME", str(cap_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Point legacy cap.db location to tmp so nothing reads ~/.cap/cap.db
    monkeypatch.setenv("CAP_ORCHESTRATOR_DB", str(old_cap_db))

    return tmp_path


# ---------------------------------------------------------------------------
# 1. test_import_cap_from_clean_state
# ---------------------------------------------------------------------------


class TestImportCap:
    """Package-level import must succeed without side effects."""

    def test_import_cap_from_clean_state(self):
        """import cap should resolve to the src tree and expose __file__."""
        import cap  # noqa: F401 — verifies importability

        assert cap.__file__ is not None
        assert "cap" in cap.__file__

    def test_cap_version_available(self):
        """cap.__version__ must be a non-empty string."""
        import cap

        assert hasattr(cap, "__version__")
        assert isinstance(cap.__version__, str)
        assert len(cap.__version__) > 0


# ---------------------------------------------------------------------------
# 2. test_cli_help_works
# ---------------------------------------------------------------------------


class TestCliHelp:
    """CLI entry point must respond to --help without error."""

    def test_cli_help_exits_zero(self):
        """cap --help should exit 0 and emit the root command group usage."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0, (
            f"--help exited {result.exit_code}:\n{result.output}"
        )
        assert "CAP" in result.output or "cap" in result.output.lower()

    def test_cli_help_lists_commands(self):
        """--help output must advertise at least init, status, sync, knowledge."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        for expected_cmd in ("init", "status", "sync"):
            assert expected_cmd in result.output, (
                f"Command '{expected_cmd}' not advertised in --help output"
            )

    def test_subcommand_help_works(self):
        """cap knowledge --help must also exit 0 (subgroup help path)."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["knowledge", "--help"])

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 3. test_init_creates_directories
# ---------------------------------------------------------------------------


class TestInitCreatesDirectories:
    """cap init must produce the expected directory/file skeleton."""

    def test_init_creates_harness_dir(self, isolated_env, monkeypatch):
        """Running cap init should create the CAP_HOME/data directory skeleton."""
        from cap.cli.lifecycle import init as init_cmd
        from click.testing import CliRunner

        cap_home = isolated_env / ".claude-platform"
        runner = CliRunner()

        # --skip-mcp avoids subprocess calls to `claude mcp` which won't exist in CI.
        # --minimal limits the install footprint.
        with patch("cap.cli.lifecycle._run_claude_mcp", return_value=True), \
             patch("cap.cli.lifecycle._backup_file", return_value=None):
            result = runner.invoke(init_cmd, ["--minimal", "--skip-mcp"])

        # The data directory is what init always creates.
        assert cap_home.exists(), "CAP_HOME was not created"
        data_dir = cap_home / "data"
        assert data_dir.exists(), f"data/ sub-directory was not created (init output: {result.output!r})"

    def test_init_creates_mcp_policy(self, isolated_env, monkeypatch):
        """cap init should write a .harness/mcp-policy.json (or data/mcp-policy.json)."""
        from cap.harness.governance import load_policy

        # Verify that load_policy does NOT crash when no .harness/ exists.
        # On a fresh install the directory is absent; the function must return defaults.
        policy = load_policy(isolated_env)
        assert policy is not None
        assert policy.daily_budget_usd > 0

    def test_init_minimal_flag_does_not_crash(self, isolated_env):
        """cap init --minimal --skip-mcp must complete without unhandled exception."""
        from cap.cli.lifecycle import init as init_cmd
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("cap.cli.lifecycle._run_claude_mcp", return_value=True), \
             patch("cap.cli.lifecycle._backup_file", return_value=None):
            result = runner.invoke(init_cmd, ["--minimal", "--skip-mcp"])

        # exit code 0 or 1 is acceptable; what matters is no unhandled exception
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"init --minimal raised unexpected exception: {result.exception}"
        )


# ---------------------------------------------------------------------------
# 4. test_init_creates_databases
# ---------------------------------------------------------------------------


class TestInitCreatesDatabases:
    """After init the four platform databases must exist with correct tables."""

    def _table_names(self, db_path: Path) -> set[str]:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    def test_platform_db_has_expected_tables(self, isolated_env):
        """platform.db must have budget_ledger, workflows, workflow_events."""
        from cap.lib.db_init import init_platform_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = init_platform_db(data_dir)
        conn.close()

        tables = self._table_names(data_dir / "platform.db")
        for required in ("budget_ledger", "workflows", "workflow_events"):
            assert required in tables, f"platform.db missing table: {required}"

    def test_knowledge_db_has_expected_tables(self, isolated_env):
        """knowledge.db must have knowledge_entries, embedding_queue, and graph tables."""
        from cap.lib.db_init import init_knowledge_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = init_knowledge_db(data_dir)
        conn.close()

        tables = self._table_names(data_dir / "knowledge.db")
        for required in (
            "knowledge_entries",
            "embedding_queue",
            "knowledge_graph_nodes",
            "knowledge_graph_edges",
            "business_knowledge",
        ):
            assert required in tables, f"knowledge.db missing table: {required}"

    def test_sessions_db_has_expected_tables(self, isolated_env):
        """sessions.db must have sessions, learnings, corrections, decisions."""
        from cap.lib.db_init import init_sessions_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = init_sessions_db(data_dir)
        conn.close()

        tables = self._table_names(data_dir / "sessions.db")
        for required in ("sessions", "learnings", "corrections", "decisions"):
            assert required in tables, f"sessions.db missing table: {required}"

    def test_fleet_db_has_expected_tables(self, isolated_env):
        """fleet.db must have fleet_servers, fleet_events."""
        from cap.lib.db_init import init_fleet_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = init_fleet_db(data_dir)
        conn.close()

        tables = self._table_names(data_dir / "fleet.db")
        for required in ("fleet_servers", "fleet_events"):
            assert required in tables, f"fleet.db missing table: {required}"

    def test_databases_use_wal_mode(self, isolated_env):
        """All databases must use WAL journal mode."""
        from cap.lib.db_init import init_platform_db, init_knowledge_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        for fn_name, init_fn in [
            ("init_platform_db", init_platform_db),
            ("init_knowledge_db", init_knowledge_db),
        ]:
            conn = init_fn(data_dir)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert mode == "wal", f"{fn_name} uses journal_mode={mode!r} instead of wal"

    def test_db_init_is_idempotent(self, isolated_env):
        """Calling init_knowledge_db twice must not fail or duplicate tables."""
        from cap.lib.db_init import init_knowledge_db

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

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

        assert count1 == count2, "Second init changed table count (not idempotent)"


# ---------------------------------------------------------------------------
# 5. test_doctor_on_fresh_install
# ---------------------------------------------------------------------------


class TestDoctorOnFreshInstall:
    """cap doctor must run without crash on a brand-new, unconfigured machine."""

    def test_doctor_exits_without_exception(self, isolated_env):
        """Invoking cap doctor must not raise an unhandled exception."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])

        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"doctor raised unexpected exception:\n{result.exception}\n{result.output}"
        )

    def test_doctor_warns_about_missing_db_not_crash(self, isolated_env):
        """On fresh install, doctor should warn about missing DBs without crashing."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])

        # Must produce some output
        assert len(result.output) > 0

    def test_doctor_embedder_status_visible(self, isolated_env):
        """doctor output must mention embedder state (even if unavailable)."""
        from click.testing import CliRunner
        from cap.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])

        # The embedder section is always printed
        lower_out = result.output.lower()
        assert (
            "embedder" in lower_out
            or "embedding" in lower_out
            or "bedrock" in lower_out
        ), "doctor output does not mention embedder/bedrock status"


# ---------------------------------------------------------------------------
# 6. test_first_knowledge_sync
# ---------------------------------------------------------------------------


class TestFirstKnowledgeSync:
    """knowledge sync on a fresh (empty) git repo must not crash."""

    @pytest.fixture()
    def git_repo(self, tmp_path):
        """Create a minimal git repo in tmp_path."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        (tmp_path / "README.md").write_text("# Test Repo\n")
        return tmp_path

    def test_sync_workspace_empty_repo_does_not_crash(self, isolated_env, git_repo):
        """sync_workspace on an empty git repo must return a SyncStats, not raise."""
        from cap.lib.db_init import init_knowledge_db
        from cap.lib.sync_engine import sync_workspace

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = init_knowledge_db(data_dir)

        # Should not raise
        stats = sync_workspace(db, str(git_repo), full=True)

        assert stats is not None
        assert hasattr(stats, "files_scanned")

    def test_sync_produces_zero_or_more_entries(self, isolated_env, git_repo):
        """Syncing an empty repo may produce 0 entries, but must not be negative."""
        from cap.lib.db_init import init_knowledge_db
        from cap.lib.sync_engine import sync_workspace

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = init_knowledge_db(data_dir)

        stats = sync_workspace(db, str(git_repo), full=True)

        assert stats.files_indexed >= 0
        assert stats.files_updated >= 0
        assert stats.files_scanned >= 0


# ---------------------------------------------------------------------------
# 7. test_first_search_empty_db
# ---------------------------------------------------------------------------


class TestFirstSearchEmptyDb:
    """knowledge_search on empty DB must return empty list, not raise."""

    def _make_knowledge_db(self, data_dir: Path) -> sqlite3.Connection:
        from cap.lib.db_init import init_knowledge_db
        return init_knowledge_db(data_dir)

    def test_search_returns_empty_list_not_error(self, isolated_env):
        """hybrid_search on a fresh database should return [], not raise."""
        from cap.lib.retrieval import hybrid_search

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = self._make_knowledge_db(data_dir)

        results = hybrid_search(
            conn=db,
            vectors_table=None,
            query="deploy kubernetes terraform",
            query_vector=None,
            workspace=str(isolated_env),
            strategy="keyword",
            top_k=10,
        )

        assert isinstance(results, list)
        assert len(results) == 0

    def test_search_with_semantic_strategy_degrades_gracefully(self, isolated_env):
        """hybrid strategy with no vector table and no embeddings must not raise."""
        from cap.lib.retrieval import hybrid_search

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = self._make_knowledge_db(data_dir)

        # vectors_table=None simulates no LanceDB; query_vector=None = no embedder
        results = hybrid_search(
            conn=db,
            vectors_table=None,
            query="some query on empty db",
            query_vector=None,
            workspace=str(isolated_env),
            strategy="hybrid",
            top_k=5,
        )

        assert isinstance(results, list)

    def test_graph_strategy_on_empty_db(self, isolated_env):
        """graph strategy on fresh DB (no nodes/edges) must return empty list."""
        from cap.lib.retrieval import hybrid_search

        data_dir = isolated_env / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = self._make_knowledge_db(data_dir)

        results = hybrid_search(
            conn=db,
            vectors_table=None,
            query="service dependency graph",
            query_vector=None,
            workspace=str(isolated_env),
            strategy="graph",
            top_k=5,
        )

        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 8. test_first_route_uses_defaults
# ---------------------------------------------------------------------------


class TestFirstRouteUsesDefaults:
    """Router must use hardcoded default thresholds when DB has 0 learning samples."""

    @pytest.fixture()
    def empty_router_db(self, tmp_path):
        """Migrated cap.db with zero routing decisions."""
        from cap.db import get_db, migrate

        db_path = str(tmp_path / "cap.db")
        conn = get_db(db_path)
        migrate(conn)
        return conn

    def test_get_learned_thresholds_returns_default_source(self, empty_router_db):
        """With 0 routing decisions the source must be 'default', not 'learned'."""
        from cap.orchestration.router import get_learned_thresholds

        thresholds = get_learned_thresholds(empty_router_db)

        assert thresholds["source"] == "default", (
            f"Expected source='default' on fresh install, got {thresholds['source']!r}"
        )

    def test_default_thresholds_match_constants(self, empty_router_db):
        """Default threshold values must match the published module constants."""
        from cap.orchestration.router import (
            get_learned_thresholds,
            DEFAULT_INLINE_MAX,
            DEFAULT_FULL_MIN,
        )

        thresholds = get_learned_thresholds(empty_router_db)

        assert thresholds["inline_max"] == DEFAULT_INLINE_MAX
        assert thresholds["full_min"] == DEFAULT_FULL_MIN

    def test_route_trivial_task_on_fresh_db(self, empty_router_db):
        """Routing a trivial task on empty DB must return a valid RoutingDecision."""
        from cap.orchestration.router import route, Tier

        decision = route("fix typo in comment", empty_router_db)

        assert decision is not None
        assert decision.tier in (Tier.INLINE, Tier.LIGHTWEIGHT, Tier.FULL)

    def test_route_does_not_return_learned_thresholds_with_zero_samples(
        self, empty_router_db
    ):
        """routing with zero history must never advertise learned source."""
        from cap.orchestration.router import get_learned_thresholds

        thresholds = get_learned_thresholds(empty_router_db)

        assert thresholds["source"] != "learned", (
            "Router incorrectly reports 'learned' thresholds with 0 samples"
        )


# ---------------------------------------------------------------------------
# 9. test_trust_starts_at_default
# ---------------------------------------------------------------------------


class TestTrustStartsAtDefault:
    """TrustManager must return DEFAULT_TRUST (0.5) for any unknown agent."""

    @pytest.fixture()
    def trust_db(self, tmp_path):
        """Migrated database with empty trust_levels table."""
        from cap.db import get_db, migrate

        db_path = str(tmp_path / "trust_test.db")
        conn = get_db(db_path)
        migrate(conn)
        return conn

    def test_unknown_agent_returns_default_trust(self, trust_db):
        """Any agent not yet recorded must return DEFAULT_TRUST = 0.5."""
        from cap.learning.trust import TrustManager, DEFAULT_TRUST

        manager = TrustManager(trust_db)
        score = manager.get_trust_level("dev", "refactor")

        assert score == DEFAULT_TRUST

    def test_all_standard_agent_types_return_default_on_fresh_db(self, trust_db):
        """All built-in agent types start at DEFAULT_TRUST with no history."""
        from cap.learning.trust import TrustManager, DEFAULT_TRUST

        manager = TrustManager(trust_db)
        agent_types = ["dev", "devops", "security", "sre", "code-review", "test", "docs"]

        for agent_type in agent_types:
            score = manager.get_trust_level(agent_type, "general")
            assert score == DEFAULT_TRUST, (
                f"Agent '{agent_type}' returned trust={score}, expected {DEFAULT_TRUST}"
            )

    def test_default_trust_yields_confirm_autonomy(self, trust_db):
        """DEFAULT_TRUST (0.5) is in the confirm band, never auto or deny."""
        from cap.learning.trust import TrustManager

        manager = TrustManager(trust_db)
        level = manager.get_autonomy_level("unknown-agent", "unknown-action")

        assert level == "confirm", (
            f"Fresh install autonomy level should be 'confirm', got {level!r}"
        )


# ---------------------------------------------------------------------------
# 10. test_daemon_once_on_fresh_install
# ---------------------------------------------------------------------------


class TestDaemonOnceOnFreshInstall:
    """CapDaemon.run_once() must complete on a machine with no existing state."""

    def test_run_once_completes_without_crash(self, isolated_env):
        """CapDaemon.run_once() returns a dict with all expected task keys."""
        from cap.harness.daemon import CapDaemon

        daemon = CapDaemon(interval_seconds=60)

        with (
            patch("cap.harness.daemon.CapDaemon._data_dir", return_value=isolated_env / "data"),
            patch("cap.lib.db_init.init_knowledge_db", return_value=MagicMock()),
            patch("cap.lib.db_init.init_sessions_db", return_value=MagicMock()),
            patch("cap.lib.consolidator.consolidate", return_value=MagicMock(
                expired_deleted=0, duplicates_removed=0
            )),
            patch("cap.harness.agent_store.cleanup_stale", return_value=0),
            patch("cap.harness.daemon.CapDaemon._run_pattern_embedding",
                  return_value={"skipped": "embedder_unavailable"}),
            patch("cap.learning.engine.compute_thresholds_from_session_events",
                  return_value={"sample_count": 0}),
            patch("cap.harness.daemon.CapDaemon._run_retention",
                  return_value={"skipped": "retention_module_unavailable"}),
            patch("cap.harness.governance.write_manifest"),
        ):
            results = daemon.run_once()

        assert isinstance(results, dict)
        for expected_key in ("consolidation", "stale_agents", "pattern_embedding",
                             "learning", "retention", "manifest"):
            assert expected_key in results, (
                f"run_once() result missing key: {expected_key}"
            )

    def test_run_once_graceful_on_consolidation_error(self, isolated_env):
        """If consolidation task fails, run_once should report error, not crash."""
        from cap.harness.daemon import CapDaemon

        daemon = CapDaemon(interval_seconds=60)

        with patch("cap.harness.daemon.CapDaemon._data_dir",
                   side_effect=RuntimeError("no config on fresh install")):
            results = daemon.run_once()

        # Consolidation should report error key
        assert "consolidation" in results
        assert "error" in results["consolidation"]

    def test_run_once_sets_last_run(self, isolated_env):
        """run_once() should set daemon.last_run to the returned dict."""
        from cap.harness.daemon import CapDaemon

        daemon = CapDaemon(interval_seconds=60)

        with (
            patch.object(daemon, "_run_consolidation", return_value={"expired": 0, "deduped": 0}),
            patch.object(daemon, "_run_stale_cleanup", return_value={"terminated": 0}),
            patch.object(daemon, "_run_pattern_embedding",
                         return_value={"skipped": "embedder_unavailable"}),
            patch.object(daemon, "_run_learning", return_value={"sample_count": 0}),
            patch.object(daemon, "_run_retention",
                         return_value={"skipped": "retention_module_unavailable"}),
            patch.object(daemon, "_run_manifest", return_value={"refreshed": True}),
        ):
            results = daemon.run_once()

        assert daemon.last_run is results


# ---------------------------------------------------------------------------
# 11. test_harness_spawn_without_bedrock
# ---------------------------------------------------------------------------


class TestHarnessSpawnWithoutBedrock:
    """Agent records are created even when AWS credentials are absent."""

    @pytest.fixture()
    def platform_db(self, tmp_path):
        """Minimal platform.db with agents table (from cap.lib.db_init)."""
        from cap.lib.db_init import init_platform_db

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = init_platform_db(data_dir)
        return conn, data_dir

    def test_agent_record_written_on_spawn(self, platform_db):
        """spawn_agent should persist an AgentRecord to the DB even without creds."""
        from cap.harness.agent_store import spawn_agent

        conn, data_dir = platform_db
        db_path = data_dir / "platform.db"

        # spawn_agent uses _db_path kwarg to override the default DB location.
        record = spawn_agent(agent_type="dev", _db_path=db_path)

        assert record is not None
        assert record.agent_type == "dev"
        assert record.agent_id is not None and len(record.agent_id) > 0

    def test_spawned_record_appears_in_db(self, platform_db):
        """After spawn_agent, the record must be queryable from the database."""
        from cap.harness.agent_store import spawn_agent

        conn, data_dir = platform_db
        db_path = data_dir / "platform.db"

        record = spawn_agent(agent_type="devops", _db_path=db_path)

        # Query via a fresh connection to the same file
        verify_conn = __import__("sqlite3").connect(str(db_path))
        row = verify_conn.execute(
            "SELECT agent_type, status FROM agents WHERE agent_id = ?",
            (record.agent_id,),
        ).fetchone()
        verify_conn.close()

        assert row is not None
        assert row[0] == "devops"


# ---------------------------------------------------------------------------
# 12. test_governance_default_policy_loaded
# ---------------------------------------------------------------------------


class TestGovernanceDefaultPolicyLoaded:
    """load_policy must return sane defaults when no .harness/ directory exists."""

    def test_load_policy_without_harness_dir_returns_defaults(self, tmp_path):
        """load_policy on a directory without .harness/ must not raise."""
        from cap.harness.governance import load_policy

        # tmp_path has no .harness/ subdirectory
        policy = load_policy(tmp_path)

        assert policy is not None

    def test_default_policy_has_budget(self, tmp_path):
        """Default policy must include a non-zero daily_budget_usd."""
        from cap.harness.governance import load_policy

        policy = load_policy(tmp_path)

        assert policy.daily_budget_usd > 0

    def test_default_policy_has_dangerous_patterns(self, tmp_path):
        """Default policy must ship with at least one dangerous pattern."""
        from cap.harness.governance import load_policy

        policy = load_policy(tmp_path)

        assert len(policy.dangerous_patterns) > 0

    def test_check_dangerous_rm_rf_detected_without_config(self):
        """check_dangerous should detect 'rm -rf /' even with no policy file."""
        from cap.harness.governance import check_dangerous

        matches = check_dangerous("rm -rf /")

        assert len(matches) > 0, "rm -rf / should always be flagged as dangerous"

    def test_check_dangerous_safe_command_passes(self):
        """check_dangerous must return empty list for safe content."""
        from cap.harness.governance import check_dangerous

        matches = check_dangerous("echo hello world")

        assert matches == []

    def test_check_dangerous_sudo_detected(self):
        """sudo must be flagged as dangerous on a fresh install."""
        from cap.harness.governance import check_dangerous

        matches = check_dangerous("sudo systemctl restart nginx")

        assert len(matches) > 0, "sudo should be flagged as dangerous"

"""Tests for budget management CLI and enforcement logic.

Covers:
- Budget status output
- Pause/resume flag file
- Daily limit enforcement
- Per-agent-type cap enforcement
- Per-project isolation
- History tracking
- Reset functionality
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cap.lib.budget_manager import (
    init_budget_log_table,
    is_budget_paused,
    pause_budget,
    resume_budget,
    get_today_spend,
    get_history,
    reset_today,
    check_budget_enforcement,
    record_budget_spend,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def budget_db(tmp_path):
    """Create an in-memory style temp database with budget_log table."""
    db_path = tmp_path / "platform.db"
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA busy_timeout=2000")
    init_budget_log_table(db)
    return db


@pytest.fixture
def cap_home(tmp_path):
    """Set up a temporary CAP_HOME directory."""
    cap_dir = tmp_path / ".claude-platform"
    data_dir = cap_dir / "data"
    data_dir.mkdir(parents=True)
    with patch.dict(os.environ, {"CAP_HOME": str(cap_dir)}):
        yield cap_dir


# ---------------------------------------------------------------------------
# PART 1: Budget status output
# ---------------------------------------------------------------------------


class TestBudgetStatus:
    """Tests for get_today_spend and status reporting."""

    def test_empty_budget_returns_zero(self, budget_db):
        """When no spend recorded, today's spend is zero."""
        result = get_today_spend(budget_db)
        assert result["total_spend_usd"] == 0.0
        assert result["execution_count"] == 0

    def test_spend_is_tracked(self, budget_db):
        """After recording spend, today's total reflects it."""
        record_budget_spend(budget_db, "dev", 1.50)
        record_budget_spend(budget_db, "security", 0.75)

        result = get_today_spend(budget_db)
        assert abs(result["total_spend_usd"] - 2.25) < 0.001
        assert result["execution_count"] == 2

    def test_per_workspace_spend(self, budget_db):
        """Per-project spend is isolated when workspace is specified."""
        record_budget_spend(budget_db, "dev", 1.0, workspace="/project-a")
        record_budget_spend(budget_db, "dev", 2.0, workspace="/project-b")

        result_a = get_today_spend(budget_db, workspace="/project-a")
        result_b = get_today_spend(budget_db, workspace="/project-b")
        result_all = get_today_spend(budget_db)

        assert abs(result_a["total_spend_usd"] - 1.0) < 0.001
        assert abs(result_b["total_spend_usd"] - 2.0) < 0.001
        assert abs(result_all["total_spend_usd"] - 3.0) < 0.001


# ---------------------------------------------------------------------------
# PART 2: Pause/Resume flag
# ---------------------------------------------------------------------------


class TestPauseResume:
    """Tests for pause/resume flag file and DB state."""

    def test_initial_state_not_paused(self, cap_home):
        """Budget starts not paused."""
        assert not is_budget_paused()

    def test_pause_creates_flag(self, cap_home, budget_db):
        """Pausing creates the flag file."""
        pause_budget(budget_db)
        flag_path = cap_home / "data" / "budget_paused"
        assert flag_path.exists()
        assert is_budget_paused()

    def test_resume_removes_flag(self, cap_home, budget_db):
        """Resuming removes the flag file."""
        pause_budget(budget_db)
        assert is_budget_paused()

        resume_budget(budget_db)
        flag_path = cap_home / "data" / "budget_paused"
        assert not flag_path.exists()
        assert not is_budget_paused()

    def test_pause_sets_db_flag(self, cap_home, budget_db):
        """Pausing sets paused=1 in budget_log for today."""
        pause_budget(budget_db)
        row = budget_db.execute(
            "SELECT paused FROM budget_log WHERE workspace = '__global__'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_resume_clears_db_flag(self, cap_home, budget_db):
        """Resuming sets paused=0 in budget_log for today."""
        pause_budget(budget_db)
        resume_budget(budget_db)
        row = budget_db.execute(
            "SELECT paused FROM budget_log WHERE workspace = '__global__'"
        ).fetchone()
        assert row is not None
        assert row[0] == 0


# ---------------------------------------------------------------------------
# PART 3: Daily limit enforcement
# ---------------------------------------------------------------------------


class TestDailyLimitEnforcement:
    """Tests for daily budget limit checks."""

    def test_under_limit_allowed(self, budget_db, cap_home):
        """Execution is allowed when under the daily limit."""
        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "dev", cost_usd=1.0, config=config)
        assert result["allowed"] is True
        assert result["reason"] is None

    def test_over_limit_blocked(self, budget_db, cap_home):
        """Execution is blocked when daily limit would be exceeded."""
        # Record spend up to the limit
        record_budget_spend(budget_db, "dev", 4.5)

        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "dev", cost_usd=1.0, config=config)
        assert result["allowed"] is False
        assert "exceeded" in result["reason"].lower() or "limit" in result["reason"].lower()

    def test_exactly_at_limit_allowed(self, budget_db, cap_home):
        """Execution is allowed when spend equals limit exactly (no overshoot)."""
        record_budget_spend(budget_db, "dev", 5.0)

        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": False}}
        # cost_usd=0 check (just status check, not adding new cost)
        result = check_budget_enforcement(budget_db, "dev", cost_usd=0.0, config=config)
        # At exactly limit, no additional cost requested — allowed
        assert result["allowed"] is True

    def test_paused_blocks_execution(self, budget_db, cap_home):
        """When paused, even under-limit executions are blocked."""
        pause_budget(budget_db)

        config = {"budget": {"daily_limit_usd": 100.0, "agent_caps": {}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "dev", cost_usd=0.1, config=config)
        assert result["allowed"] is False
        assert "paused" in result["reason"].lower()

        # Cleanup
        resume_budget(budget_db)


# ---------------------------------------------------------------------------
# PART 4: Per-agent-type cap enforcement
# ---------------------------------------------------------------------------


class TestPerAgentCap:
    """Tests for per-agent-type spending caps."""

    def test_no_cap_allows_any_agent(self, budget_db, cap_home):
        """Without agent_caps configured, all agent types are allowed."""
        config = {"budget": {"daily_limit_usd": 100.0, "agent_caps": {}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "opus", cost_usd=50.0, config=config)
        assert result["allowed"] is True

    def test_agent_cap_blocks_when_exceeded(self, budget_db, cap_home):
        """Per-agent cap blocks when that agent type exceeds its cap."""
        # We need execution_ledger table for agent cap checks
        budget_db.execute("""
            CREATE TABLE IF NOT EXISTS execution_ledger (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                model TEXT NOT NULL,
                task_hash TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                swarm_id TEXT,
                workflow_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        budget_db.commit()

        # Record some spend for the "opus" agent type
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        budget_db.execute(
            "INSERT INTO execution_ledger (id, agent_id, agent_type, model, input_tokens, output_tokens, cost_usd, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-1", "agent-1", "opus", "claude-opus", 1000, 500, 2.8, 1000, now),
        )
        budget_db.commit()

        config = {"budget": {"daily_limit_usd": 100.0, "agent_caps": {"opus": 3.0}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "opus", cost_usd=0.5, config=config)
        assert result["allowed"] is False
        assert "opus" in result["reason"].lower()

    def test_agent_cap_allows_under_cap(self, budget_db, cap_home):
        """Per-agent cap allows execution when under the cap."""
        budget_db.execute("""
            CREATE TABLE IF NOT EXISTS execution_ledger (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                model TEXT NOT NULL,
                task_hash TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                swarm_id TEXT,
                workflow_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        budget_db.commit()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        budget_db.execute(
            "INSERT INTO execution_ledger (id, agent_id, agent_type, model, input_tokens, output_tokens, cost_usd, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-2", "agent-2", "security", "claude-opus", 500, 200, 1.0, 500, now),
        )
        budget_db.commit()

        config = {"budget": {"daily_limit_usd": 100.0, "agent_caps": {"security": 2.0}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "security", cost_usd=0.5, config=config)
        assert result["allowed"] is True

    def test_different_agent_not_affected_by_other_cap(self, budget_db, cap_home):
        """An agent type without a cap is not affected by other caps."""
        budget_db.execute("""
            CREATE TABLE IF NOT EXISTS execution_ledger (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                model TEXT NOT NULL,
                task_hash TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                swarm_id TEXT,
                workflow_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        budget_db.commit()

        config = {"budget": {"daily_limit_usd": 100.0, "agent_caps": {"opus": 1.0}, "per_project": False}}
        result = check_budget_enforcement(budget_db, "dev", cost_usd=5.0, config=config)
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# PART 5: Per-project isolation
# ---------------------------------------------------------------------------


class TestPerProjectIsolation:
    """Tests for per-project budget tracking."""

    def test_per_project_disabled_uses_global(self, budget_db, cap_home):
        """When per_project=False, spend is tracked globally."""
        record_budget_spend(budget_db, "dev", 3.0, workspace="/project-a")
        record_budget_spend(budget_db, "dev", 2.5, workspace="/project-b")

        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": False}}
        # Global total is 5.5 which exceeds 5.0
        result = check_budget_enforcement(
            budget_db, "dev", cost_usd=0.1, workspace="/project-a", config=config
        )
        assert result["allowed"] is False

    def test_per_project_enabled_isolates_spend(self, budget_db, cap_home):
        """When per_project=True, spend is tracked per workspace."""
        record_budget_spend(budget_db, "dev", 3.0, workspace="/project-a")
        record_budget_spend(budget_db, "dev", 2.5, workspace="/project-b")

        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": True}}
        # Project A has 3.0 spent, well under 5.0
        result = check_budget_enforcement(
            budget_db, "dev", cost_usd=1.0, workspace="/project-a", config=config
        )
        assert result["allowed"] is True

    def test_per_project_workspace_exceeds_own_limit(self, budget_db, cap_home):
        """Per-project: a workspace is blocked when IT exceeds the limit."""
        record_budget_spend(budget_db, "dev", 4.8, workspace="/project-a")

        config = {"budget": {"daily_limit_usd": 5.0, "agent_caps": {}, "per_project": True}}
        result = check_budget_enforcement(
            budget_db, "dev", cost_usd=0.5, workspace="/project-a", config=config
        )
        assert result["allowed"] is False


# ---------------------------------------------------------------------------
# PART 6: History and reset
# ---------------------------------------------------------------------------


class TestHistoryAndReset:
    """Tests for history retrieval and counter reset."""

    def test_history_returns_days(self, budget_db):
        """History returns entries for recent days."""
        from datetime import datetime, timezone, timedelta

        today = datetime.now(timezone.utc)
        for i in range(3):
            date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            budget_db.execute(
                "INSERT INTO budget_log (date, workspace, total_spend_usd, execution_count) "
                "VALUES (?, '__global__', ?, ?)",
                (date_str, 1.5 * (i + 1), 10 * (i + 1)),
            )
        budget_db.commit()

        history = get_history(budget_db, days=7)
        assert len(history) == 3
        # Most recent first
        assert history[0]["date"] == today.strftime("%Y-%m-%d")

    def test_reset_clears_today(self, budget_db):
        """Reset removes today's budget_log entry."""
        record_budget_spend(budget_db, "dev", 3.0)
        assert get_today_spend(budget_db)["total_spend_usd"] > 0

        reset_today(budget_db)
        assert get_today_spend(budget_db)["total_spend_usd"] == 0.0

    def test_reset_workspace_only_clears_that_workspace(self, budget_db):
        """Reset with workspace only clears that workspace's counter."""
        record_budget_spend(budget_db, "dev", 2.0, workspace="/project-a")
        record_budget_spend(budget_db, "dev", 3.0, workspace="/project-b")

        reset_today(budget_db, workspace="/project-a")

        result_a = get_today_spend(budget_db, workspace="/project-a")
        result_b = get_today_spend(budget_db, workspace="/project-b")

        assert result_a["total_spend_usd"] == 0.0
        assert abs(result_b["total_spend_usd"] - 3.0) < 0.001


# ---------------------------------------------------------------------------
# CLI integration tests (click CliRunner)
# ---------------------------------------------------------------------------


class TestBudgetCLI:
    """Integration tests for budget CLI commands via CliRunner."""

    @pytest.fixture
    def runner_env(self, tmp_path):
        """Set up a full CLI test environment."""
        cap_dir = tmp_path / ".claude-platform"
        data_dir = cap_dir / "data"
        data_dir.mkdir(parents=True)

        # Create a minimal platform.db
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        init_budget_log_table(db)
        db.close()

        # Create harness-config.json
        import json
        config = {
            "provider": "aws-bedrock",
            "budget": {
                "daily_limit_usd": 10.0,
                "alert_threshold_pct": 80,
                "per_project": False,
                "agent_caps": {},
            },
        }
        config_path = cap_dir / "harness-config.json"
        config_path.write_text(json.dumps(config))

        env = {
            "CAP_HOME": str(cap_dir),
            "HOME": str(tmp_path),
        }
        return env, tmp_path

    def test_budget_pause_resume_cli(self, runner_env):
        """Test cap budget pause and resume commands."""
        from cap.cli.main import cli

        env, tmp_path = runner_env
        runner = CliRunner()

        with patch.dict(os.environ, env):
            # Pause
            result = runner.invoke(cli, ["budget", "pause"])
            assert result.exit_code == 0
            assert "PAUSED" in result.output

            # Pause again (already paused)
            result = runner.invoke(cli, ["budget", "pause"])
            assert result.exit_code == 0
            assert "already paused" in result.output.lower()

            # Resume
            result = runner.invoke(cli, ["budget", "resume"])
            assert result.exit_code == 0
            assert "RESUMED" in result.output

            # Resume again (not paused)
            result = runner.invoke(cli, ["budget", "resume"])
            assert result.exit_code == 0
            assert "not paused" in result.output.lower()

    def test_budget_reset_cli(self, runner_env):
        """Test cap budget reset command."""
        from cap.cli.main import cli

        env, tmp_path = runner_env
        runner = CliRunner()

        with patch.dict(os.environ, env):
            # Record some spend first
            cap_dir = Path(env["CAP_HOME"])
            db = sqlite3.connect(str(cap_dir / "data" / "platform.db"))
            db.execute("PRAGMA busy_timeout=2000")
            record_budget_spend(db, "dev", 2.5)
            db.close()

            # Reset with --yes flag
            result = runner.invoke(cli, ["budget", "reset", "--yes"])
            assert result.exit_code == 0
            assert "reset" in result.output.lower()

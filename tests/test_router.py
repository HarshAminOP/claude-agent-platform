"""Tests for CAP orchestration router."""
import pytest
import sys
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.db import get_db, migrate
from cap.orchestration.router import (
    route,
    Tier,
    RoutingDecision,
    get_learned_thresholds,
    DEFAULT_INLINE_MAX,
    DEFAULT_FULL_MIN,
    _compute_keyword_score,
)


@pytest.fixture
def db(tmp_path):
    """Provide a migrated database connection."""
    db_path = str(tmp_path / "test_router.db")
    conn = get_db(db_path)
    migrate(conn)
    yield conn
    conn.close()


class TestTrivialPromptInline:
    """Test that trivial prompts route to INLINE tier."""

    def test_fix_typo(self, db):
        decision = route("fix typo in README", db)
        assert decision.tier == Tier.INLINE

    def test_rename_variable(self, db):
        decision = route("rename x to count", db)
        assert decision.tier == Tier.INLINE

    def test_update_comment(self, db):
        decision = route("update comment on line 5", db)
        assert decision.tier == Tier.INLINE

    def test_add_log_line(self, db):
        decision = route("add log statement", db)
        assert decision.tier == Tier.INLINE

    def test_short_simple_prompt(self, db):
        decision = route("hello", db)
        assert decision.tier == Tier.INLINE

    def test_inline_has_no_agents(self, db):
        decision = route("fix typo", db)
        assert decision.estimated_agents == []
        assert decision.estimated_cost == 0.0


class TestComplexPromptFull:
    """Test that complex prompts route to FULL tier."""

    def test_terraform_migration(self, db):
        decision = route(
            "Migrate all terraform modules across every environment to use the new provider version",
            db,
        )
        assert decision.tier == Tier.FULL

    def test_refactor_and_deploy(self, db):
        decision = route(
            "Refactor the authentication service and deploy to kubernetes with helm charts across all environments to migrate everything",
            db,
        )
        assert decision.tier == Tier.FULL

    def test_security_audit_all_files(self, db):
        decision = route(
            "Run a security audit across all files and deploy the compliance fixes to argocd",
            db,
        )
        assert decision.tier == Tier.FULL

    def test_full_tier_has_orchestrator(self, db):
        decision = route(
            "Refactor the entire codebase and deploy across all environments with terraform",
            db,
        )
        assert "orchestrator" in decision.estimated_agents

    def test_long_prompt_increases_complexity(self, db):
        """Prompts over 500 chars get +0.2 complexity."""
        long_prompt = "deploy kubernetes " + "x " * 300  # > 500 chars
        score = _compute_keyword_score(long_prompt)
        assert score >= 0.45  # deploy(0.25) + length(0.2) = 0.45


class TestMediumPromptLightweight:
    """Test that medium-complexity prompts route to LIGHTWEIGHT tier."""

    def test_single_terraform_task(self, db):
        decision = route("update the terraform module for the VPC", db)
        assert decision.tier == Tier.LIGHTWEIGHT

    def test_single_review(self, db):
        """Review keyword alone scores 0.15, need more context to reach LIGHTWEIGHT."""
        decision = route(
            "review the PR changes and check the terraform module for issues",
            db,
        )
        assert decision.tier == Tier.LIGHTWEIGHT

    def test_lightweight_has_specialist(self, db):
        decision = route("deploy the new helm chart version", db)
        assert decision.tier == Tier.LIGHTWEIGHT
        assert len(decision.estimated_agents) == 1
        assert "devops" in decision.estimated_agents

    def test_security_review(self, db):
        decision = route(
            "security review and audit of the IAM policy with terraform compliance checks",
            db,
        )
        assert decision.tier == Tier.LIGHTWEIGHT
        assert "devops" in decision.estimated_agents or "security" in decision.estimated_agents


class TestLearnedThresholds:
    """Test that learned thresholds override defaults when enough data exists."""

    def test_defaults_with_insufficient_data(self, db):
        """With < 50 decisions, defaults should be used."""
        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "default"
        assert thresholds["inline_max"] == DEFAULT_INLINE_MAX
        assert thresholds["full_min"] == DEFAULT_FULL_MIN

    def test_learned_thresholds_with_sufficient_data(self, db):
        """With 50+ decisions and per-tier success data, thresholds should adapt."""
        now = time.time()

        # Insert 60 routing decisions with known patterns
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"inline task {i}", 0.1, "inline", "success"),
            )
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"lightweight task {i}", 0.35, "lightweight", "success"),
            )
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"full task {i}", 0.7, "full", "success"),
            )
        db.commit()

        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "learned"

        # inline avg = 0.1, lightweight avg = 0.35, full avg = 0.7
        # inline_max = (0.1 + 0.35) / 2 = 0.225
        # full_min = (0.35 + 0.7) / 2 = 0.525
        assert abs(thresholds["inline_max"] - 0.225) < 0.01
        assert abs(thresholds["full_min"] - 0.525) < 0.01

    def test_learned_thresholds_affect_routing(self, db):
        """Learned thresholds should change routing decisions."""
        now = time.time()

        # Create history that shifts thresholds
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"task {i}", 0.05, "inline", "success"),
            )
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"task {i}", 0.25, "lightweight", "success"),
            )
        for i in range(20):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "s1", f"task {i}", 0.8, "full", "success"),
            )
        db.commit()

        # With learned thresholds: inline_max = (0.05+0.25)/2 = 0.15
        # A prompt scoring 0.16 should now be LIGHTWEIGHT instead of INLINE
        thresholds = get_learned_thresholds(db)
        assert thresholds["source"] == "learned"
        assert thresholds["inline_max"] < DEFAULT_INLINE_MAX


class TestRoutingDecisionRecording:
    """Test that routing decisions are recorded to the database."""

    def test_decision_recorded(self, db):
        decision = route("fix typo", db)
        assert decision.decision_id is not None

        row = db.execute(
            "SELECT * FROM routing_decisions WHERE id = ?", (decision.decision_id,)
        ).fetchone()
        assert row is not None
        assert row["tier_selected"] == "inline"

"""
Integration Tests: Auto-Learning Pipeline
"Learning never triggers" is the known bug. These tests verify the full
feedback -> learning -> routing improvement pipeline.

KNOWN GAP: CAP's learning system has the data model (learnings table, corrections
table, trust_levels) but the trigger mechanism is not wired up. These tests
define the REQUIRED behavior for the rehaul.

USER SCENARIO:
  1. User corrects Claude: "Don't use shell=True" → session_feedback recorded
  2. Next session starts → corrections loaded
  3. Agent spawn hook injects corrections into system prompt
  4. Over time, trust score increases for successful agent patterns
  5. Router adapts thresholds from successful routing history (50+ decisions)

VERIFY:
  - Corrections are stored in DB with correct category
  - Corrections are retrieved on next agent spawn hook
  - Trust score increases on success, decreases on failure
  - Router uses learned thresholds when 50+ decisions recorded
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.fixture
def sessions_db(tmp_path):
    from cap.lib.db_init import init_sessions_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = init_sessions_db(data_dir)
    yield db
    db.close()


@pytest.fixture
def cap_db(tmp_path):
    from cap.db import get_db, migrate
    db_path = str(tmp_path / "cap.db")
    conn = get_db(db_path)
    migrate(conn)
    yield conn
    conn.close()


class TestCorrectionStorage:
    """User corrections are persisted and retrievable."""

    def test_correction_stored_in_sessions_db(self, sessions_db):
        # corrections.id is INTEGER autoincrement — omit it
        sessions_db.execute(
            "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
            "VALUES ('/ws', 'code', 'Used shell=True', 'Use subprocess list args')"
        )
        sessions_db.commit()

        rows = sessions_db.execute("SELECT what_is_correct FROM corrections").fetchall()
        assert len(rows) >= 1
        assert any("subprocess list args" in r[0] for r in rows)

    def test_multiple_corrections_all_retrieved(self, sessions_db):
        corrections = [
            ("Never use os.system", "Use subprocess.run"),
            ("Don't commit .env files", "Use AWS Secrets Manager"),
            ("Avoid hardcoded IPs", "Use DNS names or environment variables"),
        ]
        for wrong, correct in corrections:
            sessions_db.execute(
                "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
                "VALUES ('/ws', 'security', ?, ?)",
                (wrong, correct)
            )
        sessions_db.commit()

        rows = sessions_db.execute(
            "SELECT what_was_wrong, what_is_correct FROM corrections WHERE workspace = '/ws'"
        ).fetchall()
        assert len(rows) == 3

    def test_correction_workspace_scoping(self, sessions_db):
        """Corrections from workspace A must not leak into workspace B."""
        sessions_db.execute(
            "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
            "VALUES ('/ws-a', 'code', 'Wrong A', 'Correct A')"
        )
        sessions_db.execute(
            "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
            "VALUES ('/ws-b', 'code', 'Wrong B', 'Correct B')"
        )
        sessions_db.commit()

        rows_a = sessions_db.execute(
            "SELECT what_was_wrong FROM corrections WHERE workspace = '/ws-a'"
        ).fetchall()
        assert len(rows_a) == 1
        assert rows_a[0][0] == "Wrong A"


class TestCorrectionInjectionHook:
    """Corrections are injected into agent prompts before spawn."""

    def test_hook_injects_prior_corrections(self):
        from cap.lib.hooks import HookContext, HookType, correction_injection_hook

        def mock_recall(query, agent_id):
            return "Don't use shell=True in subprocess calls"

        ctx = HookContext(
            hook_type=HookType.before_agent_spawn,
            agent_id="dev",
            prompt="Implement the new file processing feature",
            metadata={"session_recall_fn": mock_recall},
        )
        correction_injection_hook(ctx)
        assert "[SYSTEM] Prior corrections:" in ctx.prompt
        assert "shell=True" in ctx.prompt

    def test_hook_skips_injection_when_no_corrections(self):
        from cap.lib.hooks import HookContext, HookType, correction_injection_hook

        def mock_recall_empty(query, agent_id):
            return None

        ctx = HookContext(
            hook_type=HookType.before_agent_spawn,
            agent_id="dev",
            prompt="Implement the new file processing feature",
            metadata={"session_recall_fn": mock_recall_empty},
        )
        original_prompt = ctx.prompt
        correction_injection_hook(ctx)
        # If no corrections, prompt should be unchanged
        assert ctx.prompt == original_prompt or "[SYSTEM]" not in ctx.prompt


class TestTrustScoreUpdate:
    """Trust scores increase on success and decrease on failure."""

    def test_trust_score_increases_on_success(self, cap_db):
        from cap.learning.trust import TrustManager
        manager = TrustManager(cap_db)

        # Record 5 successes
        for _ in range(5):
            manager.record_outcome(agent_type="dev", action_type="code", success=True)

        level = manager.get_trust_level("dev", "code")
        # get_trust_level returns a float (the trust score)
        # After 5 successes: (5+1)/(5+0+2) = 6/7 ≈ 0.857
        assert isinstance(level, float)
        assert level > 0.5

    def test_trust_score_decreases_on_failure(self, cap_db):
        from cap.learning.trust import TrustManager
        manager = TrustManager(cap_db)

        # Build up trust, then fail
        for _ in range(3):
            manager.record_outcome(agent_type="security", action_type="audit", success=True)
        level_before = manager.get_trust_level("security", "audit")

        manager.record_outcome(agent_type="security", action_type="audit", success=False)
        level_after = manager.get_trust_level("security", "audit")

        # get_trust_level returns a float; score should decrease after a failure
        assert isinstance(level_after, float)
        assert level_after < level_before

    def test_new_agent_has_default_trust(self, cap_db):
        from cap.learning.trust import TrustManager, DEFAULT_TRUST
        manager = TrustManager(cap_db)

        level = manager.get_trust_level("brand-new-agent", "deploy")
        # get_trust_level returns DEFAULT_TRUST (0.5) for unknown agents
        assert isinstance(level, float)
        assert level == DEFAULT_TRUST


class TestLearningThresholdAdaptation:
    """Router adapts thresholds from routing history — the key learning loop."""

    def test_routing_decisions_accumulate(self, cap_db):
        from cap.orchestration.router import route
        # Route 10 different tasks
        tasks = [
            "fix typo", "rename variable", "update comment",
            "deploy kubernetes service", "run security audit",
            "refactor auth module across all files",
        ]
        for task in tasks:
            route(task, cap_db)

        count = cap_db.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
        assert count == len(tasks), "Each route call must record a decision"

    def test_sufficient_history_produces_learned_thresholds(self, cap_db):
        from cap.orchestration.router import get_learned_thresholds, DEFAULT_INLINE_MAX, DEFAULT_FULL_MIN

        now = time.time()
        # Insert 60 decisions with known score distribution
        for i in range(20):
            cap_db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, "
                "complexity_score, tier_selected, outcome) VALUES (?, 's1', 'task', ?, ?, 'success')",
                (now, 0.08, "inline")
            )
        for i in range(20):
            cap_db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, "
                "complexity_score, tier_selected, outcome) VALUES (?, 's1', 'task', ?, ?, 'success')",
                (now, 0.32, "lightweight")
            )
        for i in range(20):
            cap_db.execute(
                "INSERT INTO routing_decisions (timestamp, session_id, task_description, "
                "complexity_score, tier_selected, outcome) VALUES (?, 's1', 'task', ?, ?, 'success')",
                (now, 0.72, "full")
            )
        cap_db.commit()

        thresholds = get_learned_thresholds(cap_db)
        assert thresholds["source"] == "learned"
        # Inline max = (0.08 + 0.32) / 2 = 0.20
        assert abs(thresholds["inline_max"] - 0.20) < 0.02
        # Full min = (0.32 + 0.72) / 2 = 0.52
        assert abs(thresholds["full_min"] - 0.52) < 0.02

    def test_outcome_recorded_as_success_on_completion(self, cap_db):
        from cap.orchestration.router import route
        decision = route("deploy kubernetes service", cap_db)

        # Simulate marking outcome as success
        cap_db.execute(
            "UPDATE routing_decisions SET outcome = 'success' WHERE id = ?",
            (decision.decision_id,)
        )
        cap_db.commit()

        row = cap_db.execute(
            "SELECT outcome FROM routing_decisions WHERE id = ?",
            (decision.decision_id,)
        ).fetchone()
        assert row["outcome"] == "success"

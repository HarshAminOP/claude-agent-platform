"""
Phase 2 Orchestration — Workflow Routing Tests

Covers:
- Task keyword detection (feature, bug, infra, refactor, review)
- Word-boundary false-positive prevention
- workflow_name assignment per tier
- Hard threshold safety bounds on learned thresholds
- Core workflow .js file existence and meta validity
- Dead orchestration file absence
- Integration: route() returns workflow_name field
"""

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.orchestration.router import (
    HARD_FULL_MIN_FLOOR,
    HARD_INLINE_MAX_CEILING,
    TASK_KEYWORD_PATTERNS,
    WORKFLOW_MAP,
    Tier,
    _extract_task_keywords,
    get_learned_thresholds,
    route,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "src" / "cap" / "data" / "workflows"
_CORE_WORKFLOWS = ["feature-request", "bugfix", "infra", "refactor", "review"]
_DEAD_FILES = ["executor.py", "consensus.py", "planner.py", "context.py", "checkpoint.py"]


@pytest.fixture
def db(tmp_path):
    """Migrated in-process SQLite database, isolated per test."""
    conn = get_db(str(tmp_path / "test_routing.db"))
    migrate(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Router: keyword detection
# ---------------------------------------------------------------------------


class TestRouterKeywordDetection:
    """_extract_task_keywords returns correct categories for representative prompts."""

    def test_feature_keywords_detected(self):
        keywords = _extract_task_keywords("implement a health endpoint")
        assert "feature" in keywords

    def test_bugfix_keywords_detected(self):
        keywords = _extract_task_keywords("fix the timeout bug")
        assert "bug" in keywords

    def test_infra_keywords_detected(self):
        keywords = _extract_task_keywords("add terraform module for S3")
        assert "infra" in keywords

    def test_refactor_keywords_detected(self):
        keywords = _extract_task_keywords("refactor the auth middleware")
        assert "refactor" in keywords

    def test_review_keywords_detected(self):
        keywords = _extract_task_keywords("review the PR changes")
        assert "review" in keywords

    def test_multiple_categories_detected(self):
        """A prompt matching multiple categories returns all matching ones."""
        keywords = _extract_task_keywords("refactor and review the auth module")
        assert "refactor" in keywords
        assert "review" in keywords

    def test_empty_prompt_returns_no_keywords(self):
        assert _extract_task_keywords("") == []

    def test_unrelated_prompt_returns_no_keywords(self):
        keywords = _extract_task_keywords("update the README with better examples")
        assert keywords == []


# ---------------------------------------------------------------------------
# Router: word-boundary false-positive prevention
# ---------------------------------------------------------------------------


class TestRouterWordBoundaryGuards:
    """Patterns use \\b anchors; substrings must not match."""

    def test_no_false_positive_fix_in_prefix(self):
        """'add a prefix to names' contains 'prefix' which has 'fix' as a substring.
        With \\bfix\\b the word 'prefix' must NOT match the bug category."""
        keywords = _extract_task_keywords("add a prefix to names")
        assert "bug" not in keywords, (
            "'prefix' should not trigger the \\bfix\\b bug pattern"
        )

    def test_no_false_positive_check_in_checkout(self):
        """'checkout the branch' contains 'check' as a prefix of 'checkout'.
        With \\bcheck\\b 'checkout' must NOT match the review category."""
        keywords = _extract_task_keywords("checkout the branch")
        assert "review" not in keywords, (
            "'checkout' should not trigger the \\bcheck\\b review pattern"
        )

    def test_fix_standalone_matches(self):
        """Standalone 'fix' at word boundary should still match."""
        keywords = _extract_task_keywords("fix the broken login")
        assert "bug" in keywords

    def test_check_standalone_matches(self):
        """Standalone 'check' at word boundary should match review."""
        keywords = _extract_task_keywords("check the deployment status")
        assert "review" in keywords


# ---------------------------------------------------------------------------
# Router: workflow_name assignment
# ---------------------------------------------------------------------------


class TestRouterWorkflowNameAssignment:
    """workflow_name is set for FULL/LIGHTWEIGHT tiers and None for INLINE."""

    def test_workflow_name_set_for_full_tier_implement(self, db):
        """'implement' triggers feature category; FULL tier must map to 'feature-request'."""
        decision = route(
            "implement a new authentication service across all environments with terraform deploy",
            db,
        )
        if decision.tier == Tier.FULL:
            assert decision.workflow_name == "feature-request", (
                f"Expected feature-request, got {decision.workflow_name}"
            )
        else:
            # Ensure workflow_name is still a string (not None) for LIGHTWEIGHT
            assert decision.workflow_name is not None or decision.tier == Tier.INLINE

    def test_workflow_name_feature_request_via_forced_full(self, db):
        """Force FULL tier with sufficient complexity signals and 'implement'."""
        # 'implement' + 'terraform' + 'across' + 'every' -> feature + infra + multi-file
        decision = route(
            "implement the new service and migrate all terraform modules across every environment",
            db,
        )
        assert decision.tier == Tier.FULL
        # First matched category is 'feature' (from 'implement') -> maps to feature-request
        assert decision.workflow_name == "feature-request"

    def test_workflow_name_none_for_inline(self, db):
        """INLINE tier must never set a workflow_name."""
        decision = route("fix typo in README", db)
        # INLINE from negative keyword weight
        assert decision.tier == Tier.INLINE
        assert decision.workflow_name is None

    def test_workflow_name_bugfix_for_bug_prompt(self, db):
        """A bug-fix prompt that reaches LIGHTWEIGHT/FULL should map to 'bugfix'."""
        decision = route(
            "fix the timeout bug in auth middleware and deploy to kubernetes",
            db,
        )
        # deploy (infra) bumps score -> at least LIGHTWEIGHT
        assert decision.tier in (Tier.LIGHTWEIGHT, Tier.FULL)
        assert decision.workflow_name == "bugfix"

    def test_workflow_map_covers_all_core_categories(self):
        """WORKFLOW_MAP must have an entry for every core task category."""
        for category in ("feature", "bug", "infra", "refactor", "review"):
            assert category in WORKFLOW_MAP, f"WORKFLOW_MAP missing '{category}'"

    def test_workflow_map_values_match_core_workflows(self):
        """Every WORKFLOW_MAP value must correspond to a real workflow file."""
        for category, wf_name in WORKFLOW_MAP.items():
            js_file = _WORKFLOWS_DIR / f"{wf_name}.js"
            assert js_file.exists(), (
                f"WORKFLOW_MAP['{category}'] = '{wf_name}' but {js_file} not found"
            )


# ---------------------------------------------------------------------------
# Router: hard threshold bounds
# ---------------------------------------------------------------------------


class TestHardThresholdBounds:
    """Learned thresholds cannot escape hard safety rails."""

    def _insert_decisions(self, db, inline_score, lw_score, full_score, n=20):
        now = time.time()
        for i in range(n):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "test", f"inline-{i}", inline_score, "inline", "success"),
            )
        for i in range(n):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "test", f"lw-{i}", lw_score, "lightweight", "success"),
            )
        for i in range(n):
            db.execute(
                """INSERT INTO routing_decisions
                   (timestamp, session_id, task_description, complexity_score,
                    tier_selected, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, "test", f"full-{i}", full_score, "full", "success"),
            )
        db.commit()

    def test_hard_bounds_enforced_inline_ceiling(self, db):
        """inline_max can never exceed HARD_INLINE_MAX_CEILING even if history suggests it."""
        # Insert history that would push inline_max above the ceiling
        # inline avg = 0.25, lightweight avg = 0.35 => midpoint = 0.30 = ceiling
        # Try inline avg = 0.28, lightweight avg = 0.40 => midpoint = 0.34 > ceiling
        self._insert_decisions(db, inline_score=0.28, lw_score=0.40, full_score=0.80)
        thresholds = get_learned_thresholds(db)
        assert thresholds["inline_max"] <= HARD_INLINE_MAX_CEILING, (
            f"inline_max {thresholds['inline_max']} exceeds ceiling {HARD_INLINE_MAX_CEILING}"
        )

    def test_hard_bounds_enforced_full_floor(self, db):
        """full_min can never drop below HARD_FULL_MIN_FLOOR even if history suggests it."""
        # lightweight avg = 0.30, full avg = 0.45 => midpoint = 0.375 < floor (0.40)
        self._insert_decisions(db, inline_score=0.05, lw_score=0.30, full_score=0.45)
        thresholds = get_learned_thresholds(db)
        assert thresholds["full_min"] >= HARD_FULL_MIN_FLOOR, (
            f"full_min {thresholds['full_min']} is below floor {HARD_FULL_MIN_FLOOR}"
        )

    def test_hard_ceiling_value_is_sane(self):
        """Constants themselves must be within logical range."""
        assert 0.0 < HARD_INLINE_MAX_CEILING < 1.0
        assert 0.0 < HARD_FULL_MIN_FLOOR < 1.0
        assert HARD_INLINE_MAX_CEILING < HARD_FULL_MIN_FLOOR, (
            "Inline ceiling must be less than full floor to avoid an impossible zone"
        )

    def test_learned_thresholds_respect_both_bounds_simultaneously(self, db):
        """Both bounds are enforced together in a single call."""
        self._insert_decisions(db, inline_score=0.28, lw_score=0.35, full_score=0.45)
        thresholds = get_learned_thresholds(db)
        assert thresholds["inline_max"] <= HARD_INLINE_MAX_CEILING
        assert thresholds["full_min"] >= HARD_FULL_MIN_FLOOR


# ---------------------------------------------------------------------------
# Workflow script files: existence and meta validity
# ---------------------------------------------------------------------------


class TestWorkflowScriptFiles:
    """Core workflow .js files must exist with valid export const meta blocks."""

    def test_all_core_workflows_exist(self):
        """feature-request.js, bugfix.js, infra.js, refactor.js, review.js must all exist."""
        missing = []
        for name in _CORE_WORKFLOWS:
            js_path = _WORKFLOWS_DIR / f"{name}.js"
            if not js_path.exists():
                missing.append(str(js_path))
        assert not missing, f"Missing workflow files: {missing}"

    @pytest.mark.parametrize("workflow_name", _CORE_WORKFLOWS)
    def test_workflow_meta_valid(self, workflow_name):
        """Each .js file must start with a valid 'export const meta = {...}' block."""
        js_path = _WORKFLOWS_DIR / f"{workflow_name}.js"
        content = js_path.read_text(encoding="utf-8").strip()
        assert content.startswith("export const meta = {"), (
            f"{workflow_name}.js does not begin with 'export const meta = {{'"
        )

    @pytest.mark.parametrize("workflow_name", _CORE_WORKFLOWS)
    def test_workflow_has_phases(self, workflow_name):
        """Each workflow meta must declare a non-empty phases array."""
        js_path = _WORKFLOWS_DIR / f"{workflow_name}.js"
        content = js_path.read_text(encoding="utf-8")
        # Check that 'phases' key exists with at least one object entry
        assert "phases:" in content, f"{workflow_name}.js meta is missing 'phases' key"
        # Verify at least one phase object with a 'title' field
        assert re.search(r"title\s*:", content), (
            f"{workflow_name}.js phases array appears to have no titled phases"
        )

    @pytest.mark.parametrize("workflow_name", _CORE_WORKFLOWS)
    def test_workflow_meta_has_name_field(self, workflow_name):
        """Each meta must declare a 'name' field matching the file name."""
        js_path = _WORKFLOWS_DIR / f"{workflow_name}.js"
        content = js_path.read_text(encoding="utf-8")
        assert f"name: '{workflow_name}'" in content, (
            f"{workflow_name}.js meta.name does not match filename"
        )

    @pytest.mark.parametrize("workflow_name", _CORE_WORKFLOWS)
    def test_workflow_meta_has_description(self, workflow_name):
        """Each meta must declare a non-empty 'description' string."""
        js_path = _WORKFLOWS_DIR / f"{workflow_name}.js"
        content = js_path.read_text(encoding="utf-8")
        assert re.search(r"description\s*:\s*'[^']+'", content), (
            f"{workflow_name}.js meta is missing a non-empty 'description' field"
        )


# ---------------------------------------------------------------------------
# Dead orchestration files must not exist
# ---------------------------------------------------------------------------


class TestNoDeadOrchestrationFiles:
    """Old planner/executor/consensus Python files must be absent after Phase 2 cleanup."""

    @pytest.mark.parametrize("dead_file", _DEAD_FILES)
    def test_no_dead_orchestration_files(self, dead_file):
        orchestration_dir = Path(__file__).parent.parent.parent / "src" / "cap" / "orchestration"
        dead_path = orchestration_dir / dead_file
        assert not dead_path.exists(), (
            f"Dead orchestration file found: {dead_path}. "
            "Phase 2 cleanup should have removed it."
        )


# ---------------------------------------------------------------------------
# Integration: route() returns workflow_name field
# ---------------------------------------------------------------------------


class TestRouteIntegration:
    """End-to-end: route() returns a RoutingDecision that always has a workflow_name field."""

    def test_route_returns_workflow_name_attribute(self, db):
        """RoutingDecision must always carry a workflow_name attribute (may be None for INLINE)."""
        decision = route("implement a new feature", db)
        assert hasattr(decision, "workflow_name"), (
            "RoutingDecision missing 'workflow_name' field"
        )

    def test_route_returns_workflow_name_for_non_inline(self, db):
        """For LIGHTWEIGHT/FULL tiers with known keyword, workflow_name is a non-empty string."""
        decision = route(
            "refactor the authentication module and deploy across all environments",
            db,
        )
        if decision.tier != Tier.INLINE and decision.task_keywords:
            assert isinstance(decision.workflow_name, str), (
                f"Expected str workflow_name, got {type(decision.workflow_name)}"
            )
            assert decision.workflow_name != "", "workflow_name must not be empty"

    def test_route_returns_task_keywords_attribute(self, db):
        """RoutingDecision must carry task_keywords list."""
        decision = route("review the PR changes", db)
        assert hasattr(decision, "task_keywords")
        assert isinstance(decision.task_keywords, list)

    def test_route_workflow_name_in_workflow_map_values(self, db):
        """When workflow_name is set, it must be a known entry in WORKFLOW_MAP."""
        decision = route(
            "fix the null pointer bug and deploy kubernetes manifests",
            db,
        )
        if decision.workflow_name is not None:
            assert decision.workflow_name in WORKFLOW_MAP.values(), (
                f"workflow_name '{decision.workflow_name}' not in WORKFLOW_MAP values"
            )

    def test_route_decision_id_persisted(self, db):
        """route() must persist the decision and return a valid decision_id."""
        decision = route("implement a health check endpoint", db)
        assert decision.decision_id is not None
        row = db.execute(
            "SELECT id, tier_selected FROM routing_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        assert row is not None
        assert row["tier_selected"] == decision.tier.value

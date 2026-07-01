"""Unit tests for cap.lib.task_decomposer.

Tests cover:
- TaskStep and TaskPlan data classes (topological_order, get_ready_steps)
- TaskDecomposer._classify_complexity
- TaskDecomposer._heuristic_decompose (all seven patterns + no-match)
- TaskDecomposer._build_steps_from_template (sequential dependency wiring)
- TaskDecomposer._compute_parallel_groups (BFS layers, cycle detection)
- TaskDecomposer._strip_markdown_fences
- TaskDecomposer.decompose (heuristic path, LLM path, LLM failure fallback)
- TaskDecomposer._persist_plan (SQLite round-trip)
- TaskDecomposer._minimal_fallback_plan
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the src layout importable in isolation
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.task_decomposer import (
    KNOWN_AGENT_TYPES,
    TaskDecomposer,
    TaskPlan,
    TaskStep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(step_id: str, agent_type: str = "dev", depends_on: list[str] | None = None) -> TaskStep:
    return TaskStep(
        id=step_id,
        agent_type=agent_type,
        task=f"Do something for {step_id}",
        depends_on=depends_on or [],
        estimated_tokens=1_000,
        priority=5,
    )


def _make_plan(steps: list[TaskStep]) -> TaskPlan:
    deps = {s.id: list(s.depends_on) for s in steps}
    td = TaskDecomposer()
    groups = td._compute_parallel_groups(steps)
    return TaskPlan(
        workflow_id="wf-test",
        original_task="test task",
        steps=steps,
        dependencies=deps,
        parallel_groups=groups,
    )




# ---------------------------------------------------------------------------
# TaskStep
# ---------------------------------------------------------------------------


class TestTaskStep:
    def test_defaults(self):
        step = TaskStep(id="step-1", agent_type="dev", task="Fix it")
        assert step.depends_on == []
        assert step.receives_from == []
        assert step.estimated_tokens == 0
        assert step.priority == 0

    def test_depends_on_is_independent_list(self):
        step = TaskStep(id="step-1", agent_type="dev", task="X", depends_on=["step-0"])
        step.depends_on.append("step-extra")
        # Verify it's a plain list, not a shared default
        step2 = TaskStep(id="step-2", agent_type="dev", task="Y")
        assert step2.depends_on == []


# ---------------------------------------------------------------------------
# TaskPlan
# ---------------------------------------------------------------------------


class TestTaskPlan:
    def test_step_count(self):
        plan = _make_plan([_make_step("step-1"), _make_step("step-2")])
        assert plan.step_count == 2

    def test_get_step_found(self):
        steps = [_make_step("step-1"), _make_step("step-2")]
        plan = _make_plan(steps)
        assert plan.get_step("step-1") is steps[0]
        assert plan.get_step("step-2") is steps[1]

    def test_get_step_not_found(self):
        plan = _make_plan([_make_step("step-1")])
        assert plan.get_step("step-99") is None

    def test_get_ready_steps_no_completed(self):
        steps = [
            _make_step("step-1"),
            _make_step("step-2", depends_on=["step-1"]),
        ]
        plan = _make_plan(steps)
        ready = plan.get_ready_steps(set())
        assert [s.id for s in ready] == ["step-1"]

    def test_get_ready_steps_after_first(self):
        steps = [
            _make_step("step-1"),
            _make_step("step-2", depends_on=["step-1"]),
        ]
        plan = _make_plan(steps)
        ready = plan.get_ready_steps({"step-1"})
        assert [s.id for s in ready] == ["step-2"]

    def test_get_ready_steps_all_completed(self):
        steps = [_make_step("step-1"), _make_step("step-2")]
        plan = _make_plan(steps)
        ready = plan.get_ready_steps({"step-1", "step-2"})
        assert ready == []

    def test_topological_order_linear(self):
        steps = [
            _make_step("step-1"),
            _make_step("step-2", depends_on=["step-1"]),
            _make_step("step-3", depends_on=["step-2"]),
        ]
        plan = _make_plan(steps)
        order = plan.topological_order()
        assert order == ["step-1", "step-2", "step-3"]

    def test_topological_order_parallel(self):
        # step-1 and step-2 are independent; step-3 depends on both
        steps = [
            _make_step("step-1"),
            _make_step("step-2"),
            _make_step("step-3", depends_on=["step-1", "step-2"]),
        ]
        plan = _make_plan(steps)
        order = plan.topological_order()
        assert order.index("step-3") > order.index("step-1")
        assert order.index("step-3") > order.index("step-2")

    def test_topological_order_cycle_raises(self):
        # Manually craft a cyclic plan (bypasses _compute_parallel_groups)
        s1 = _make_step("step-1", depends_on=["step-2"])
        s2 = _make_step("step-2", depends_on=["step-1"])
        plan = TaskPlan(
            workflow_id="wf-cycle",
            original_task="cycle",
            steps=[s1, s2],
            dependencies={"step-1": ["step-2"], "step-2": ["step-1"]},
            parallel_groups=[],
        )
        with pytest.raises(ValueError, match="Cycle detected"):
            plan.topological_order()


# ---------------------------------------------------------------------------
# TaskDecomposer._classify_complexity
# ---------------------------------------------------------------------------


class TestClassifyComplexity:
    def setup_method(self):
        self.td = TaskDecomposer()

    def test_simple_short_task(self):
        assert self.td._classify_complexity("Fix a typo in README") == "simple"

    def test_moderate_medium_task(self):
        assert self.td._classify_complexity("Refactor the auth module and add tests") == "moderate"

    def test_complex_long_task(self):
        long_task = " ".join(["word"] * 65)
        assert self.td._classify_complexity(long_task) == "complex"

    def test_complex_multi_step_keywords(self):
        task = "Deploy the service and then run security audit and also monitor"
        assert self.td._classify_complexity(task) == "complex"

    def test_complex_many_keyword_hits(self):
        task = "Security review, deploy, and performance test for the pipeline"
        assert self.td._classify_complexity(task) == "complex"


# ---------------------------------------------------------------------------
# TaskDecomposer._heuristic_decompose
# ---------------------------------------------------------------------------


class TestHeuristicDecompose:
    def setup_method(self):
        self.td = TaskDecomposer()

    def _decompose(self, task: str) -> TaskPlan | None:
        complexity = self.td._classify_complexity(task)
        return self.td._heuristic_decompose(task, complexity)

    def test_fix_bug_pattern(self):
        plan = self._decompose("Fix bug in payment service")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["explore", "dev", "test"]

    def test_implement_feature_pattern(self):
        plan = self._decompose("Implement new checkout endpoint")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["dev", "test", "code-review"]

    def test_security_audit_pattern(self):
        plan = self._decompose("Security audit the IAM roles")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["security", "dev", "security"]

    def test_deploy_pattern(self):
        plan = self._decompose("Deploy new service to Kubernetes")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["aws-architect", "devops", "security"]

    def test_refactor_pattern(self):
        plan = self._decompose("Refactor the database layer")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["explore", "dev", "test", "code-review"]

    def test_documentation_pattern(self):
        plan = self._decompose("Write documentation for the API")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["explore", "docs"]

    def test_performance_pattern(self):
        plan = self._decompose("Optimize query performance in analytics service")
        assert plan is not None
        types = [s.agent_type for s in plan.steps]
        assert types == ["explore", "optimization", "dev", "test"]

    def test_debug_pattern_alias(self):
        plan = self._decompose("Debug the memory leak in worker process")
        assert plan is not None
        assert plan.steps[0].agent_type == "explore"

    def test_no_match_returns_none(self):
        plan = self._decompose("Quarterly planning meeting agenda")
        assert plan is None

    def test_sequential_dependencies(self):
        plan = self._decompose("Fix bug in login flow")
        ids = [s.id for s in plan.steps]
        for i, step in enumerate(plan.steps):
            if i == 0:
                assert step.depends_on == []
            else:
                assert step.depends_on == [ids[i - 1]]

    def test_task_text_injected(self):
        task = "Fix bug in login flow"
        plan = self._decompose(task)
        for step in plan.steps:
            assert task in step.task

    def test_planning_cost_is_zero(self):
        plan = self._decompose("Fix bug in user service")
        assert plan.planning_cost_usd == 0.0

    def test_workflow_id_is_unique(self):
        plan1 = self._decompose("Fix bug in service A")
        plan2 = self._decompose("Fix bug in service B")
        assert plan1.workflow_id != plan2.workflow_id


# ---------------------------------------------------------------------------
# TaskDecomposer._compute_parallel_groups
# ---------------------------------------------------------------------------


class TestComputeParallelGroups:
    def setup_method(self):
        self.td = TaskDecomposer()

    def test_all_independent(self):
        steps = [_make_step("s1"), _make_step("s2"), _make_step("s3")]
        groups = self.td._compute_parallel_groups(steps)
        assert len(groups) == 1
        assert sorted(groups[0]) == ["s1", "s2", "s3"]

    def test_fully_sequential(self):
        steps = [
            _make_step("s1"),
            _make_step("s2", depends_on=["s1"]),
            _make_step("s3", depends_on=["s2"]),
        ]
        groups = self.td._compute_parallel_groups(steps)
        assert groups == [["s1"], ["s2"], ["s3"]]

    def test_fan_out_then_join(self):
        # s1 -> s2, s3 (parallel) -> s4
        steps = [
            _make_step("s1"),
            _make_step("s2", depends_on=["s1"]),
            _make_step("s3", depends_on=["s1"]),
            _make_step("s4", depends_on=["s2", "s3"]),
        ]
        groups = self.td._compute_parallel_groups(steps)
        assert groups[0] == ["s1"]
        assert sorted(groups[1]) == ["s2", "s3"]
        assert groups[2] == ["s4"]

    def test_cycle_returns_single_group_fallback(self):
        steps = [
            _make_step("s1", depends_on=["s2"]),
            _make_step("s2", depends_on=["s1"]),
        ]
        groups = self.td._compute_parallel_groups(steps)
        assert len(groups) == 1
        assert sorted(groups[0]) == ["s1", "s2"]

    def test_single_step(self):
        steps = [_make_step("s1")]
        groups = self.td._compute_parallel_groups(steps)
        assert groups == [["s1"]]

    def test_empty_steps(self):
        groups = self.td._compute_parallel_groups([])
        assert groups == []


# ---------------------------------------------------------------------------
# TaskDecomposer._strip_markdown_fences
# ---------------------------------------------------------------------------


class TestStripMarkdownFences:
    def test_json_fence(self):
        text = "```json\n{\"key\": 1}\n```"
        assert TaskDecomposer._strip_markdown_fences(text) == '{"key": 1}'

    def test_plain_fence(self):
        text = "```\n{\"key\": 2}\n```"
        assert TaskDecomposer._strip_markdown_fences(text) == '{"key": 2}'

    def test_no_fence(self):
        text = '{"key": 3}'
        assert TaskDecomposer._strip_markdown_fences(text) == '{"key": 3}'

    def test_fence_with_surrounding_text(self):
        text = "Here is the plan:\n```json\n{\"steps\": []}\n```\nDone."
        result = TaskDecomposer._strip_markdown_fences(text)
        assert result == '{"steps": []}'


# ---------------------------------------------------------------------------
# TaskDecomposer._parse_steps_from_llm
# ---------------------------------------------------------------------------


class TestParseStepsFromLlm:
    def setup_method(self):
        self.td = TaskDecomposer()

    def test_valid_steps(self):
        raw = [
            {"id": "step-1", "agent_type": "explore", "task": "Look around", "depends_on": []},
            {"id": "step-2", "agent_type": "dev", "task": "Fix it", "depends_on": ["step-1"]},
        ]
        steps = self.td._parse_steps_from_llm(raw)
        assert len(steps) == 2
        assert steps[0].agent_type == "explore"
        assert steps[1].depends_on == ["step-1"]

    def test_unknown_agent_type_replaced_with_dev(self):
        raw = [{"id": "step-1", "agent_type": "magic-agent", "task": "Do magic", "depends_on": []}]
        steps = self.td._parse_steps_from_llm(raw)
        assert steps[0].agent_type == "dev"

    def test_missing_id_auto_generated(self):
        raw = [{"agent_type": "dev", "task": "Something", "depends_on": []}]
        steps = self.td._parse_steps_from_llm(raw)
        assert steps[0].id == "step-1"

    def test_missing_task_gets_default(self):
        raw = [{"id": "step-1", "agent_type": "dev", "depends_on": []}]
        steps = self.td._parse_steps_from_llm(raw)
        assert "step-1" in steps[0].task

    def test_duplicate_ids_deduped(self):
        raw = [
            {"id": "step-1", "agent_type": "dev", "task": "A", "depends_on": []},
            {"id": "step-1", "agent_type": "test", "task": "B", "depends_on": []},
        ]
        steps = self.td._parse_steps_from_llm(raw)
        assert len({s.id for s in steps}) == 2


# ---------------------------------------------------------------------------
# TaskDecomposer._minimal_fallback_plan
# ---------------------------------------------------------------------------


class TestMinimalFallbackPlan:
    def setup_method(self):
        self.td = TaskDecomposer()

    def test_single_dev_step(self):
        plan = self.td._minimal_fallback_plan("Some unknown task")
        assert plan.step_count == 1
        assert plan.steps[0].agent_type == "dev"
        assert plan.steps[0].task == "Some unknown task"

    def test_parallel_groups_is_single_group(self):
        plan = self.td._minimal_fallback_plan("Task")
        assert plan.parallel_groups == [["step-1"]]

    def test_topological_order_works(self):
        plan = self.td._minimal_fallback_plan("Task")
        assert plan.topological_order() == ["step-1"]


# ---------------------------------------------------------------------------
# TaskDecomposer._persist_plan (SQLite round-trip)
# ---------------------------------------------------------------------------


class TestPersistPlan:
    def _make_db(self) -> tuple[sqlite3.Connection, str]:
        tmp = tempfile.mkdtemp()
        db_path = str(Path(tmp) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            CREATE TABLE task_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                critical_path TEXT,
                parallelism_factor REAL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE task_steps (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                description TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                depends_on TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                started_at REAL,
                completed_at REAL,
                result_json TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_steps_workflow ON task_steps(workflow_id, state)")
        conn.commit()
        return conn, db_path

    def test_plan_and_steps_written(self):
        conn, db_path = self._make_db()
        conn.close()

        steps = [
            _make_step("step-1"),
            _make_step("step-2", depends_on=["step-1"]),
        ]
        plan = _make_plan(steps)
        plan.workflow_id = "wf-persist-test"

        td = TaskDecomposer(db_path=db_path)

        # Patch get_db to return a fresh connection to our test DB
        with patch("cap.lib.task_decomposer.TaskDecomposer._persist_plan") as mock_persist:
            # Call the real _write_plan via get_db directly
            mock_persist.side_effect = lambda p: None

        # Now test _write_plan directly
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        td._write_plan(conn2, plan)
        conn2.commit()

        row = conn2.execute(
            "SELECT * FROM task_plans WHERE workflow_id = ?", ("wf-persist-test",)
        ).fetchone()
        assert row is not None
        assert row["workflow_id"] == "wf-persist-test"

        step_rows = conn2.execute(
            "SELECT * FROM task_steps WHERE workflow_id = ?", ("wf-persist-test",)
        ).fetchall()
        assert len(step_rows) == 2
        step_ids = {r["id"] for r in step_rows}
        assert "wf-persist-test:step-1" in step_ids
        assert "wf-persist-test:step-2" in step_ids

        conn2.close()

    def test_persist_plan_swallows_db_error(self):
        """_persist_plan must not raise even when the DB is unavailable."""
        td = TaskDecomposer(db_path="/nonexistent/path/that/cannot/be/created.db")
        steps = [_make_step("step-1")]
        plan = _make_plan(steps)
        # Should not raise
        td._persist_plan(plan)


# ---------------------------------------------------------------------------
# TaskDecomposer.decompose — heuristic path
# ---------------------------------------------------------------------------


class TestDecomposeHeuristicPath:
    @pytest.mark.asyncio
    async def test_simple_bug_fix_uses_heuristic(self):
        td = TaskDecomposer(db_path=":memory:")
        with patch.object(td, "_persist_plan"):
            plan = await td.decompose("Fix bug in auth handler")
        types = [s.agent_type for s in plan.steps]
        assert types == ["explore", "dev", "test"]
        assert plan.planning_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_documentation_task(self):
        td = TaskDecomposer(db_path=":memory:")
        with patch.object(td, "_persist_plan"):
            plan = await td.decompose("Write documentation for the REST API")
        types = [s.agent_type for s in plan.steps]
        assert "docs" in types

    @pytest.mark.asyncio
    async def test_persist_plan_called(self):
        td = TaskDecomposer(db_path=":memory:")
        with patch.object(td, "_persist_plan") as mock_persist:
            await td.decompose("Fix bug in scheduler")
        mock_persist.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_task_raises_value_error(self):
        td = TaskDecomposer()
        with pytest.raises(ValueError, match="non-empty"):
            await td.decompose("")


# ---------------------------------------------------------------------------
# TaskDecomposer.decompose — LLM path
# ---------------------------------------------------------------------------


class TestDecomposeLlmPath:
    def _make_mock_executor(self, response_json: dict) -> MagicMock:
        mock = MagicMock()
        result = MagicMock()
        result.error = None
        result.response = json.dumps(response_json)
        result.total_cost_usd = 0.001
        mock.execute.return_value = result
        return mock

    @pytest.mark.asyncio
    async def test_llm_path_used_for_complex_task(self):
        llm_response = {
            "steps": [
                {"id": "step-1", "agent_type": "explore", "task": "Explore codebase", "depends_on": []},
                {"id": "step-2", "agent_type": "aws-architect", "task": "Design arch", "depends_on": ["step-1"]},
                {"id": "step-3", "agent_type": "devops", "task": "Deploy infra", "depends_on": ["step-2"]},
                {"id": "step-4", "agent_type": "security", "task": "Review security", "depends_on": ["step-2"]},
            ],
            "parallel_groups": [["step-1"], ["step-2"], ["step-3", "step-4"]],
            "estimated_cost_usd": 0.10,
            "complexity": "complex",
        }
        executor = self._make_mock_executor(llm_response)
        # Use a task that won't match the deploy heuristic cleanly AND is long/complex
        task = " ".join([
            "End-to-end multi-service migration to AWS EKS with Terraform infrastructure,",
            "GitOps pipeline via ArgoCD, security hardening of all IAM roles,",
            "and performance benchmarking of all critical paths",
        ])
        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_persist_plan"):
            plan = await td.decompose(task, context="Migration project")

        assert plan.step_count == 4
        assert plan.complexity == "complex"
        assert plan.planning_cost_usd == pytest.approx(0.001)
        executor.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_path_strips_markdown_fences(self):
        raw = '```json\n{"steps": [{"id": "step-1", "agent_type": "dev", "task": "Do it", "depends_on": []}], "estimated_cost_usd": 0.01, "complexity": "simple"}\n```'
        executor = MagicMock()
        result = MagicMock()
        result.error = None
        result.response = raw
        result.total_cost_usd = 0.0005
        executor.execute.return_value = result

        # Force LLM path by making heuristic return None
        task = " ".join(["word"] * 70)  # Very long task -> complex
        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_heuristic_decompose", return_value=None):
            with patch.object(td, "_persist_plan"):
                plan = await td.decompose(task)
        assert plan.step_count == 1

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_heuristic(self):
        executor = MagicMock()
        result = MagicMock()
        result.error = "ThrottlingException"
        result.response = None
        result.total_cost_usd = 0.0
        executor.execute.return_value = result

        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_persist_plan"):
            plan = await td.decompose("Fix bug in payment processor", context="ctx")
        # Should fall back to heuristic (fix bug pattern)
        assert plan.step_count >= 1
        assert plan.steps[0].agent_type == "explore"

    @pytest.mark.asyncio
    async def test_llm_failure_no_heuristic_falls_back_to_minimal(self):
        executor = MagicMock()
        result = MagicMock()
        result.error = "ModelNotReadyException"
        result.response = None
        result.total_cost_usd = 0.0
        executor.execute.return_value = result

        task = " ".join(["word"] * 70)  # complex, no heuristic pattern
        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_heuristic_decompose", return_value=None):
            with patch.object(td, "_persist_plan"):
                plan = await td.decompose(task)
        assert plan.step_count == 1
        assert plan.steps[0].agent_type == "dev"

    @pytest.mark.asyncio
    async def test_llm_invalid_json_falls_back(self):
        executor = MagicMock()
        result = MagicMock()
        result.error = None
        result.response = "not json at all { broken"
        result.total_cost_usd = 0.0
        executor.execute.return_value = result

        task = " ".join(["word"] * 70)
        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_heuristic_decompose", return_value=None):
            with patch.object(td, "_persist_plan"):
                plan = await td.decompose(task)
        # Fell back to minimal
        assert plan.step_count == 1

    @pytest.mark.asyncio
    async def test_parallel_groups_recomputed_from_steps(self):
        """Even if LLM gives wrong parallel_groups, we recompute from dependency graph."""
        llm_response = {
            "steps": [
                {"id": "step-1", "agent_type": "explore", "task": "T1", "depends_on": []},
                {"id": "step-2", "agent_type": "dev",     "task": "T2", "depends_on": []},
                {"id": "step-3", "agent_type": "test",    "task": "T3", "depends_on": ["step-1", "step-2"]},
            ],
            # Wrong parallel_groups from LLM (all sequential)
            "parallel_groups": [["step-1"], ["step-2"], ["step-3"]],
            "estimated_cost_usd": 0.05,
            "complexity": "moderate",
        }
        executor = self._make_mock_executor(llm_response)
        task = " ".join(["word"] * 70)
        td = TaskDecomposer(executor=executor, db_path=":memory:")
        with patch.object(td, "_heuristic_decompose", return_value=None):
            with patch.object(td, "_persist_plan"):
                plan = await td.decompose(task)

        # step-1 and step-2 should be in the same parallel group
        first_group = plan.parallel_groups[0]
        assert sorted(first_group) == ["step-1", "step-2"]
        assert plan.parallel_groups[1] == ["step-3"]

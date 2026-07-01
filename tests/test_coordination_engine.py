"""Tests for cap.lib.coordination_engine.CoordinationEngine.

Covers:
- Happy path: linear chain, parallel siblings
- Failure cascading (dependents become skipped)
- Budget exhaustion mid-plan
- Cyclic plan rejection
- Context passing from completed steps to dependents
- SharedState updates after step completion
- DB persistence (task_steps rows)
- get_plan_status query
- _extract_summary truncation
- _build_step_context with and without dependency results
- _synthesize_response with critical-path highlighting
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.orchestration.dag import TaskDAG, TaskStep, StepState
from cap.lib.agent_context import SharedState
from cap.db import get_db, migrate
from cap.lib.coordination_engine import (
    CoordinationEngine,
    CoordinationResult,
    StepResult,
    TaskPlan,
    _is_budget_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(*step_specs: tuple) -> TaskDAG:
    """Build a TaskDAG from (id, agent_type, depends_on) tuples."""
    dag = TaskDAG()
    for step_id, agent_type, deps in step_specs:
        dag.steps[step_id] = TaskStep(
            id=step_id,
            description=f"Do {step_id}",
            agent_type=agent_type,
            depends_on=deps,
        )
    return dag


def _mock_executor(responses: dict[str, str], errors: Optional[dict[str, str]] = None):
    """Return a mock ConverseExecutor whose execute() returns canned responses.

    Args:
        responses: Map of agent_type → response text for successful calls.
        errors: Optional map of agent_type → error string for failing calls.
    """
    from cap.harness.converse_executor import ConversationResult

    call_count: dict[str, int] = {}

    def execute(agent_id, agent_type, prompt, model=None, max_tokens=8192, context=None):
        call_count[agent_type] = call_count.get(agent_type, 0) + 1
        error_str = (errors or {}).get(agent_type)
        response_str = responses.get(agent_type, f"output from {agent_type}")
        return ConversationResult(
            agent_id=agent_id,
            agent_type=agent_type,
            model="haiku",
            response=None if error_str else response_str,
            error=error_str,
            total_input_tokens=10,
            total_output_tokens=20,
            total_cost_usd=0.001,
            duration_ms=50,
            turns=1,
        )

    executor = MagicMock()
    executor.execute.side_effect = execute
    executor._call_counts = call_count
    return executor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_coord.db")
    # Run migrations so task_steps (and all other CAP tables) exist.
    db = get_db(path)
    migrate(db)
    db.close()
    return path


@pytest.fixture
def shared(db_path):
    return SharedState(session_id="test-session", db_path=db_path)


def _engine(executor, shared, db_path, max_parallel=4):
    return CoordinationEngine(
        executor=executor,
        shared=shared,
        max_parallel=max_parallel,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Unit tests: _is_budget_error
# ---------------------------------------------------------------------------


class TestIsBudgetError:
    def test_daily_budget_exceeded(self):
        assert _is_budget_error("daily budget exceeded (limit=$5.0)")

    def test_budget_paused(self):
        assert _is_budget_error("budget paused — executions blocked.")

    def test_per_agent_cap(self):
        assert _is_budget_error("per-agent cap exceeded for 'dev': $0.50 spent of $0.50 cap.")

    def test_normal_error_not_budget(self):
        assert not _is_budget_error("validation error in model call")

    def test_none_is_not_budget(self):
        assert not _is_budget_error(None)

    def test_empty_is_not_budget(self):
        assert not _is_budget_error("")


# ---------------------------------------------------------------------------
# Unit tests: _extract_summary
# ---------------------------------------------------------------------------


class TestExtractSummary:
    def test_short_text_unchanged(self):
        text = "Hello world"
        assert CoordinationEngine._extract_summary(text, max_chars=100) == "Hello world"

    def test_long_text_truncated(self):
        text = "x" * 600
        result = CoordinationEngine._extract_summary(text, max_chars=500)
        assert len(result) <= 504  # 500 chars + " ..."
        assert result.endswith(" ...")

    def test_exact_length_not_truncated(self):
        text = "a" * 500
        result = CoordinationEngine._extract_summary(text, max_chars=500)
        assert result == text
        assert not result.endswith(" ...")

    def test_strips_surrounding_whitespace(self):
        text = "  hello  "
        assert CoordinationEngine._extract_summary(text, max_chars=100) == "hello"


# ---------------------------------------------------------------------------
# Unit tests: _build_step_context
# ---------------------------------------------------------------------------


class TestBuildStepContext:
    def test_no_deps_returns_empty(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        step = TaskStep(id="b", description="B", agent_type="dev", depends_on=[])
        result = engine._build_step_context(step, {})
        assert result == ""

    def test_missing_dep_result_skipped(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        step = TaskStep(id="b", description="B", agent_type="dev", depends_on=["a"])
        result = engine._build_step_context(step, {})
        assert result == ""

    def test_dep_with_empty_summary_skipped(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        step = TaskStep(id="b", description="B", agent_type="dev", depends_on=["a"])
        completed = {
            "a": StepResult(step_id="a", agent_type="dev", status="completed", output_summary="")
        }
        result = engine._build_step_context(step, completed)
        assert result == ""

    def test_dep_summary_included(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        step = TaskStep(id="b", description="B", agent_type="sre", depends_on=["a"])
        completed = {
            "a": StepResult(
                step_id="a",
                agent_type="dev",
                status="completed",
                output_summary="Found 3 bugs.",
            )
        }
        ctx = engine._build_step_context(step, completed)
        assert "Found 3 bugs." in ctx
        assert "dev" in ctx
        assert "step a" in ctx

    def test_multiple_deps_all_included(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        step = TaskStep(id="c", description="C", agent_type="sre", depends_on=["a", "b"])
        completed = {
            "a": StepResult(step_id="a", agent_type="dev", status="completed", output_summary="A out"),
            "b": StepResult(step_id="b", agent_type="test", status="completed", output_summary="B out"),
        }
        ctx = engine._build_step_context(step, completed)
        assert "A out" in ctx
        assert "B out" in ctx


# ---------------------------------------------------------------------------
# Unit tests: _synthesize_response
# ---------------------------------------------------------------------------


class TestSynthesizeResponse:
    @pytest.mark.asyncio
    async def test_empty_results_returns_no_steps_message(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        plan = _make_plan(("a", "dev", []))
        result = await engine._synthesize_response(plan, {})
        assert "No steps completed" in result

    @pytest.mark.asyncio
    async def test_completed_step_included_in_output(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        plan = _make_plan(("a", "dev", []))
        results = {
            "a": StepResult(step_id="a", agent_type="dev", status="completed", response="All done.")
        }
        output = await engine._synthesize_response(plan, results)
        assert "All done." in output
        assert "DEV" in output

    @pytest.mark.asyncio
    async def test_critical_path_step_marked(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        plan = _make_plan(("a", "dev", []), ("b", "sre", ["a"]))
        results = {
            "a": StepResult(step_id="a", agent_type="dev", status="completed", response="A out"),
            "b": StepResult(step_id="b", agent_type="sre", status="completed", response="B out"),
        }
        output = await engine._synthesize_response(plan, results)
        assert "[critical path]" in output

    @pytest.mark.asyncio
    async def test_cost_footer_present(self, db_path, shared):
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)
        plan = _make_plan(("a", "dev", []))
        results = {
            "a": StepResult(
                step_id="a", agent_type="dev", status="completed",
                response="Done", cost_usd=0.005,
            )
        }
        output = await engine._synthesize_response(plan, results)
        assert "Total cost:" in output
        assert "$0.0050" in output


# ---------------------------------------------------------------------------
# Integration tests: execute_plan
# ---------------------------------------------------------------------------


class TestExecutePlanLinearChain:
    """Linear A -> B -> C plan."""

    @pytest.mark.asyncio
    async def test_all_steps_complete(self, db_path, shared):
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", ["a"]),
            ("c", "test", ["b"]),
        )
        executor = _mock_executor({"dev": "dev done", "sre": "sre done", "test": "test done"})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan, workspace="/tmp")

        assert result.status == "completed"
        assert len(result.completed_steps) == 3
        assert len(result.failed_steps) == 0
        assert result.total_cost_usd == pytest.approx(0.003, abs=1e-6)

    @pytest.mark.asyncio
    async def test_final_response_synthesised(self, db_path, shared):
        plan = _make_plan(("a", "dev", []), ("b", "sre", ["a"]))
        executor = _mock_executor({"dev": "dev output", "sre": "sre output"})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        assert result.final_response is not None
        assert "dev output" in result.final_response
        assert "sre output" in result.final_response

    @pytest.mark.asyncio
    async def test_context_forwarded_to_dependent(self, db_path, shared):
        """The prompt context passed to step B must contain step A's output_summary."""
        plan = _make_plan(("a", "dev", []), ("b", "sre", ["a"]))
        executor = _mock_executor({"dev": "A findings: found 2 issues", "sre": "resolved"})
        engine = _engine(executor, shared, db_path)

        await engine.execute_plan(plan)

        # The second call (sre) should have received context containing A's summary.
        calls = executor.execute.call_args_list
        assert len(calls) == 2
        # Find the sre call.
        sre_calls = [c for c in calls if c.kwargs.get("agent_type") == "sre"
                     or (c.args and c.args[1] == "sre")]
        assert len(sre_calls) == 1
        sre_context = sre_calls[0].kwargs.get("context") or (
            sre_calls[0].args[5] if len(sre_calls[0].args) > 5 else None
        )
        assert sre_context is not None
        assert "A findings" in sre_context


class TestExecutePlanParallel:
    """Plan where A and B have no deps (should run in parallel), then C depends on both."""

    @pytest.mark.asyncio
    async def test_parallel_steps_both_complete(self, db_path, shared):
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", []),
            ("c", "test", ["a", "b"]),
        )
        executor = _mock_executor({"dev": "dev ok", "sre": "sre ok", "test": "test ok"})
        engine = _engine(executor, shared, db_path, max_parallel=2)

        result = await engine.execute_plan(plan)

        assert result.status == "completed"
        assert len(result.completed_steps) == 3

    @pytest.mark.asyncio
    async def test_max_parallel_respected(self, db_path, shared):
        """All 4 independent steps with max_parallel=2 — should still complete."""
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", []),
            ("c", "test", []),
            ("d", "devops", []),
        )
        executor = _mock_executor({"dev": "ok", "sre": "ok", "test": "ok", "devops": "ok"})
        engine = _engine(executor, shared, db_path, max_parallel=2)

        result = await engine.execute_plan(plan)

        assert result.status == "completed"
        assert len(result.completed_steps) == 4


class TestExecutePlanFailureCascade:
    """When a step fails, dependents must be skipped."""

    @pytest.mark.asyncio
    async def test_failing_step_marks_dependents_skipped(self, db_path, shared):
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", ["a"]),
            ("c", "test", ["b"]),
        )
        executor = _mock_executor({}, errors={"dev": "model error: timeout"})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        assert result.status == "failed"
        step_statuses = {sr.step_id: sr.status for sr in result.steps}
        assert step_statuses["a"] == "failed"
        assert step_statuses["b"] == "skipped"
        assert step_statuses["c"] == "skipped"

    @pytest.mark.asyncio
    async def test_partial_when_some_branches_fail(self, db_path, shared):
        """Parallel plan: A succeeds, B fails. C depends only on A (should complete)."""
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", []),
            ("c", "test", ["a"]),
        )
        executor = _mock_executor({"dev": "ok", "test": "ok"}, errors={"sre": "sre failed"})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        assert result.status == "partial"
        step_statuses = {sr.step_id: sr.status for sr in result.steps}
        assert step_statuses["a"] == "completed"
        assert step_statuses["b"] == "failed"
        assert step_statuses["c"] == "completed"

    @pytest.mark.asyncio
    async def test_errors_list_populated_on_failure(self, db_path, shared):
        plan = _make_plan(("a", "dev", []))
        executor = _mock_executor({}, errors={"dev": "validation error"})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        assert len(result.errors) == 1
        assert "validation error" in result.errors[0]


class TestExecutePlanBudgetExhaustion:
    """Budget errors cause remaining steps to be skipped."""

    @pytest.mark.asyncio
    async def test_budget_error_skips_remaining(self, db_path, shared):
        plan = _make_plan(
            ("a", "dev", []),
            ("b", "sre", ["a"]),
        )
        executor = _mock_executor(
            {},
            errors={"dev": "daily budget exceeded (limit=$5.0)"},
        )
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        step_statuses = {sr.step_id: sr.status for sr in result.steps}
        assert step_statuses["a"] == "failed"
        assert step_statuses["b"] == "skipped"


class TestExecutePlanCycleRejection:
    @pytest.mark.asyncio
    async def test_cyclic_plan_returns_failed(self, db_path, shared):
        plan = _make_plan(
            ("a", "dev", ["c"]),
            ("b", "sre", ["a"]),
            ("c", "test", ["b"]),
        )
        executor = _mock_executor({})
        engine = _engine(executor, shared, db_path)

        result = await engine.execute_plan(plan)

        assert result.status == "failed"
        assert any("cycle" in e.lower() for e in result.errors)
        # Executor must NOT have been called.
        executor.execute.assert_not_called()


class TestExecutePlanSharedState:
    """SharedState must be populated after each step."""

    @pytest.mark.asyncio
    async def test_shared_state_has_step_result(self, db_path, shared):
        plan = _make_plan(("a", "dev", []))
        executor = _mock_executor({"dev": "my output"})
        engine = _engine(executor, shared, db_path)

        await engine.execute_plan(plan)

        value = await shared.get("step.a.result")
        assert value is not None
        assert value["step_id"] == "a"
        assert value["status"] == "completed"

    @pytest.mark.asyncio
    async def test_findings_key_set(self, db_path, shared):
        plan = _make_plan(("a", "dev", []))
        executor = _mock_executor({"dev": "dev output"})
        engine = _engine(executor, shared, db_path)

        await engine.execute_plan(plan)

        findings = await shared.get("findings.dev")
        assert findings is not None
        assert findings["agent_type"] == "dev"


class TestGetPlanStatus:
    @pytest.mark.asyncio
    async def test_unknown_workflow_returns_none(self, db_path, shared):
        engine = _engine(_mock_executor({}), shared, db_path)
        status = await engine.get_plan_status("does-not-exist")
        assert status is None

    @pytest.mark.asyncio
    async def test_returns_status_after_execute(self, db_path, shared):
        plan = _make_plan(("a", "dev", []))
        # Force workflow_id so we can look it up.
        plan.workflow_id = "test-workflow-001"  # type: ignore[attr-defined]

        executor = _mock_executor({"dev": "done"})
        engine = _engine(executor, shared, db_path)

        await engine.execute_plan(plan)

        status = await engine.get_plan_status("test-workflow-001")
        assert status is not None
        assert status["workflow_id"] == "test-workflow-001"
        assert len(status["steps"]) == 1
        assert status["counts"].get("completed", 0) == 1


class TestConstructorValidation:
    def test_none_executor_raises(self, db_path, shared):
        with pytest.raises(ValueError, match="executor"):
            CoordinationEngine(executor=None, shared=shared, db_path=db_path)

    def test_zero_max_parallel_raises(self, db_path, shared):
        with pytest.raises(ValueError, match="max_parallel"):
            CoordinationEngine(
                executor=_mock_executor({}),
                shared=shared,
                max_parallel=0,
                db_path=db_path,
            )

    def test_negative_max_parallel_raises(self, db_path, shared):
        with pytest.raises(ValueError, match="max_parallel"):
            CoordinationEngine(
                executor=_mock_executor({}),
                shared=shared,
                max_parallel=-1,
                db_path=db_path,
            )


class TestTaskPlanAlias:
    def test_task_plan_is_task_dag(self):
        assert TaskPlan is TaskDAG

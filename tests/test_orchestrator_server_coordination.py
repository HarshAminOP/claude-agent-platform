"""Unit tests for the coordination wiring in cap.servers.orchestrator_server.

Covers:
- _handle_plan using TaskDecomposer (heuristic and LLM paths)
- _handle_orchestrate complexity routing (simple → single-agent, moderate/complex → coordination)
- _handle_orchestrate fallback to single-agent when CoordinationEngine raises
- _handle_coordinate explicit multi-step path
- _build_dag_from_decomposer_plan field mapping
- _get_decomposer singleton initialisation
- cap_coordinate and updated cap_plan tools registered in list_tools()

All tests are fully offline — no AWS credentials needed.
ConverseExecutor.execute(), TaskDecomposer.decompose(), and
CoordinationEngine.execute_plan() are patched throughout.
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Minimal stubs so we can import the server without real AWS / DB
# ---------------------------------------------------------------------------

@dataclass
class _FakeExecResult:
    """Minimal stub matching the interface used by _handle_execute."""
    agent_id: str
    model: str = "sonnet"
    total_input_tokens: int = 10
    total_output_tokens: int = 20
    total_cost_usd: float = 0.001
    duration_ms: int = 100
    response: Optional[str] = "ok"
    error: Optional[str] = None
    turns: int = 1
    tool_calls: list = field(default_factory=list)

    def to_execution_result(self):
        return self


@dataclass
class _FakeTaskStep:
    id: str
    agent_type: str
    task: str
    depends_on: list = field(default_factory=list)
    receives_from: list = field(default_factory=list)
    estimated_tokens: int = 6000
    priority: int = 0


@dataclass
class _FakeTaskPlan:
    workflow_id: str
    original_task: str
    steps: list
    dependencies: dict
    parallel_groups: list
    estimated_cost_usd: float = 0.01
    complexity: str = "moderate"
    planning_cost_usd: float = 0.0001

    @property
    def step_count(self):
        return len(self.steps)


def _two_step_plan(workflow_id: str = "wf-test123") -> _FakeTaskPlan:
    steps = [
        _FakeTaskStep(id="step-1", agent_type="explore", task="Explore the codebase"),
        _FakeTaskStep(id="step-2", agent_type="dev", task="Fix the bug", depends_on=["step-1"]),
    ]
    return _FakeTaskPlan(
        workflow_id=workflow_id,
        original_task="fix the authentication bug",
        steps=steps,
        dependencies={"step-1": [], "step-2": ["step-1"]},
        parallel_groups=[["step-1"], ["step-2"]],
        complexity="moderate",
    )


def _single_step_plan(workflow_id: str = "wf-single") -> _FakeTaskPlan:
    steps = [_FakeTaskStep(id="step-1", agent_type="dev", task="do the thing")]
    return _FakeTaskPlan(
        workflow_id=workflow_id,
        original_task="do the thing",
        steps=steps,
        dependencies={"step-1": []},
        parallel_groups=[["step-1"]],
        complexity="simple",
    )


@dataclass
class _FakeStepResult:
    step_id: str
    agent_type: str
    status: str = "completed"
    response: Optional[str] = "step response"
    error: Optional[str] = None
    cost_usd: float = 0.002
    duration_ms: int = 50


@dataclass
class _FakeCoordResult:
    workflow_id: str
    status: str = "completed"
    steps: list = field(default_factory=list)
    total_cost_usd: float = 0.005
    total_duration_ms: int = 200
    final_response: Optional[str] = "coordinated response"
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """Reset module-level singletons between tests to prevent state leakage."""
    import importlib
    import cap.servers.orchestrator_server as srv
    original_decomposer = srv._task_decomposer
    original_router = srv._embedding_router
    yield
    srv._task_decomposer = original_decomposer
    srv._embedding_router = original_router


@pytest.fixture()
def _patch_db(tmp_path):
    """Patch the module-level DB to use a temp path."""
    import cap.db as db_mod
    db_path = str(tmp_path / "test_orch.db")
    conn = db_mod.get_db(db_path)
    db_mod.migrate(conn)
    with patch("cap.servers.orchestrator_server.db", conn), \
         patch("cap.servers.orchestrator_server.DB_PATH", db_path):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# _get_decomposer
# ---------------------------------------------------------------------------

class TestGetDecomposer:
    def test_lazy_init_creates_instance(self):
        """_get_decomposer() returns a TaskDecomposer on first call."""
        import cap.servers.orchestrator_server as srv
        srv._task_decomposer = None

        fake_decomposer = MagicMock()
        with patch("cap.lib.task_decomposer.TaskDecomposer", return_value=fake_decomposer):
            result = srv._get_decomposer()

        assert result is fake_decomposer
        assert srv._task_decomposer is fake_decomposer

    def test_singleton_not_recreated(self):
        """_get_decomposer() returns the same instance on subsequent calls."""
        import cap.servers.orchestrator_server as srv
        first_instance = MagicMock()
        srv._task_decomposer = first_instance

        result = srv._get_decomposer()

        assert result is first_instance


# ---------------------------------------------------------------------------
# _build_dag_from_decomposer_plan
# ---------------------------------------------------------------------------

class TestBuildDagFromDecomposerPlan:
    def test_steps_are_mapped(self):
        """Each TaskDecomposer step becomes a DAG step with description=step.task."""
        import cap.servers.orchestrator_server as srv
        plan = _two_step_plan()

        dag = srv._build_dag_from_decomposer_plan(plan)

        assert "step-1" in dag.steps
        assert "step-2" in dag.steps
        assert dag.steps["step-1"].description == "Explore the codebase"
        assert dag.steps["step-2"].description == "Fix the bug"

    def test_agent_type_preserved(self):
        """Agent types from the decomposer plan are preserved in the DAG."""
        import cap.servers.orchestrator_server as srv
        plan = _two_step_plan()

        dag = srv._build_dag_from_decomposer_plan(plan)

        assert dag.steps["step-1"].agent_type == "explore"
        assert dag.steps["step-2"].agent_type == "dev"

    def test_depends_on_copied(self):
        """depends_on lists from the decomposer plan are copied into DAG steps."""
        import cap.servers.orchestrator_server as srv
        plan = _two_step_plan()

        dag = srv._build_dag_from_decomposer_plan(plan)

        assert dag.steps["step-1"].depends_on == []
        assert dag.steps["step-2"].depends_on == ["step-1"]

    def test_empty_plan_returns_empty_dag(self):
        """A plan with no steps produces an empty DAG without raising."""
        import cap.servers.orchestrator_server as srv
        empty_plan = _FakeTaskPlan(
            workflow_id="wf-empty",
            original_task="nothing",
            steps=[],
            dependencies={},
            parallel_groups=[],
        )

        dag = srv._build_dag_from_decomposer_plan(empty_plan)

        assert dag.steps == {}


# ---------------------------------------------------------------------------
# _handle_plan
# ---------------------------------------------------------------------------

class TestHandlePlan:
    @pytest.mark.asyncio
    async def test_returns_plan_fields(self, _patch_db):
        """_handle_plan returns workflow_id, steps, parallel_groups, complexity."""
        import cap.servers.orchestrator_server as srv

        fake_plan = _two_step_plan()
        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value=fake_plan)

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer):
            result = await srv._handle_plan({"task_description": "fix the authentication bug"})

        payload = json.loads(result[0].text)
        assert payload["workflow_id"] == "wf-test123"
        assert len(payload["steps"]) == 2
        assert payload["steps"][0]["id"] == "step-1"
        assert payload["steps"][0]["agent_type"] == "explore"
        assert payload["steps"][1]["depends_on"] == ["step-1"]
        assert payload["complexity"] == "moderate"
        assert "estimated_cost_usd" in payload
        assert "planning_cost_usd" in payload

    @pytest.mark.asyncio
    async def test_context_dict_serialised(self, _patch_db):
        """Context dict is serialised to JSON string before being passed to decomposer."""
        import cap.servers.orchestrator_server as srv

        fake_plan = _single_step_plan()
        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value=fake_plan)

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer):
            await srv._handle_plan({
                "task_description": "do the thing",
                "context": {"workspace": "/tmp/foo"},
            })

        _call = mock_decomposer.decompose.call_args
        context_arg = _call.kwargs.get("context") or _call.args[1]
        assert isinstance(context_arg, str)
        parsed = json.loads(context_arg)
        assert parsed["workspace"] == "/tmp/foo"

    @pytest.mark.asyncio
    async def test_decomposer_exception_returns_error(self, _patch_db):
        """When TaskDecomposer.decompose() raises, _handle_plan returns an error payload."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(side_effect=RuntimeError("LLM call timed out"))

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer):
            result = await srv._handle_plan({"task_description": "anything"})

        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "LLM call timed out" in payload["error"]


# ---------------------------------------------------------------------------
# _handle_orchestrate — complexity routing
# ---------------------------------------------------------------------------

class TestHandleOrchestrateRouting:
    @pytest.mark.asyncio
    async def test_simple_task_uses_single_agent_path(self, _patch_db):
        """For simple tasks, _handle_orchestrate uses the existing single-agent path."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = MagicMock()
        mock_decomposer._classify_complexity.return_value = "simple"

        single_agent_response = [
            MagicMock(text=json.dumps({
                "agent_id": "agt-abc",
                "agent_type": "dev",
                "model": "sonnet",
                "response": "I fixed it",
                "error": None,
                "total_input_tokens": 5,
                "total_output_tokens": 10,
                "cost_usd": 0.001,
                "duration_ms": 80,
                "turns": 1,
                "tool_calls": [],
            }))
        ]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_handle_execute", new=AsyncMock(return_value=single_agent_response)), \
             patch("cap.harness.hooks.hooks_route", return_value={"recommended_model": "sonnet", "tier": "lightweight", "confidence": 0.8, "reason": "ok"}), \
             patch("cap.harness.agentdb.agentdb_semantic_route", return_value={"recommended_agent_type": "dev", "confidence": 0.7, "alternatives": [], "based_on_patterns": 0}):
            result = await srv._handle_orchestrate({"task": "list files"})

        payload = json.loads(result[0].text)
        # Single-agent path enriches with routing key but no "steps" array
        assert "routing" in payload
        assert payload["routing"]["routing_method"] in ("keyword", "embedding")
        # CoordinationEngine result has "steps" list; single-agent does not
        assert "steps" not in payload or "step_id" not in (payload.get("steps") or [{}])[0]

    @pytest.mark.asyncio
    async def test_moderate_task_uses_coordination_path(self, _patch_db):
        """For moderate tasks, _handle_orchestrate delegates to _run_coordination."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = MagicMock()
        mock_decomposer._classify_complexity.return_value = "moderate"

        coord_result_payload = {
            "workflow_id": "wf-test123",
            "status": "completed",
            "response": "done",
            "steps": [{"step_id": "step-1", "agent_type": "dev", "status": "completed", "cost_usd": 0.001, "duration_ms": 50, "error": None}],
            "total_cost_usd": 0.001,
            "total_duration_ms": 50,
            "errors": [],
            "routing": {"method": "coordinated", "complexity": "moderate", "step_count": 2},
        }
        coord_contents = [MagicMock(text=json.dumps(coord_result_payload))]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_run_coordination", new=AsyncMock(return_value=coord_contents)):
            result = await srv._handle_orchestrate({"task": "fix bug and deploy and run tests"})

        payload = json.loads(result[0].text)
        assert payload["routing"]["method"] == "coordinated"
        assert payload["routing"]["complexity"] == "moderate"

    @pytest.mark.asyncio
    async def test_complex_task_uses_coordination_path(self, _patch_db):
        """For complex tasks, _handle_orchestrate delegates to _run_coordination."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = MagicMock()
        mock_decomposer._classify_complexity.return_value = "complex"

        coord_result_payload = {
            "workflow_id": "wf-complex",
            "status": "completed",
            "response": "all done",
            "steps": [],
            "total_cost_usd": 0.01,
            "total_duration_ms": 500,
            "errors": [],
            "routing": {"method": "coordinated", "complexity": "complex", "step_count": 5},
        }
        coord_contents = [MagicMock(text=json.dumps(coord_result_payload))]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_run_coordination", new=AsyncMock(return_value=coord_contents)):
            result = await srv._handle_orchestrate({"task": "security audit and deploy and test and document multiple services end-to-end"})

        payload = json.loads(result[0].text)
        assert payload["routing"]["method"] == "coordinated"

    @pytest.mark.asyncio
    async def test_coordination_failure_falls_back_to_single_agent(self, _patch_db):
        """When CoordinationEngine raises, _handle_orchestrate falls back to single-agent."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = MagicMock()
        mock_decomposer._classify_complexity.return_value = "moderate"

        single_agent_response = [
            MagicMock(text=json.dumps({
                "agent_id": "agt-fallback",
                "agent_type": "dev",
                "model": "sonnet",
                "response": "fallback response",
                "error": None,
                "total_input_tokens": 5,
                "total_output_tokens": 10,
                "cost_usd": 0.001,
                "duration_ms": 80,
                "turns": 1,
                "tool_calls": [],
            }))
        ]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_run_coordination", new=AsyncMock(side_effect=RuntimeError("Bedrock down"))), \
             patch.object(srv, "_handle_execute", new=AsyncMock(return_value=single_agent_response)), \
             patch("cap.harness.hooks.hooks_route", return_value={"recommended_model": "sonnet", "tier": "lightweight", "confidence": 0.8, "reason": "ok"}), \
             patch("cap.harness.agentdb.agentdb_semantic_route", return_value={"recommended_agent_type": "dev", "confidence": 0.7, "alternatives": [], "based_on_patterns": 0}):
            result = await srv._handle_orchestrate({"task": "fix bug and deploy"})

        payload = json.loads(result[0].text)
        # Fell back: no "coordinated" routing, has single-agent fields
        assert payload.get("routing", {}).get("routing_method") in ("keyword", "embedding")

    @pytest.mark.asyncio
    async def test_complexity_check_failure_defaults_to_simple(self, _patch_db):
        """When _classify_complexity raises, complexity defaults to simple (single-agent)."""
        import cap.servers.orchestrator_server as srv

        mock_decomposer = MagicMock()
        mock_decomposer._classify_complexity.side_effect = ImportError("module gone")

        single_agent_response = [
            MagicMock(text=json.dumps({
                "agent_id": "agt-x",
                "agent_type": "dev",
                "model": "sonnet",
                "response": "ok",
                "error": None,
                "total_input_tokens": 1,
                "total_output_tokens": 2,
                "cost_usd": 0.0001,
                "duration_ms": 10,
                "turns": 1,
                "tool_calls": [],
            }))
        ]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_handle_execute", new=AsyncMock(return_value=single_agent_response)), \
             patch("cap.harness.hooks.hooks_route", return_value={"recommended_model": "sonnet", "tier": "lightweight", "confidence": 0.8, "reason": "ok"}), \
             patch("cap.harness.agentdb.agentdb_semantic_route", return_value={"recommended_agent_type": "dev", "confidence": 0.7, "alternatives": [], "based_on_patterns": 0}):
            result = await srv._handle_orchestrate({"task": "simple task"})

        # Should have returned without error (single-agent fallback worked)
        payload = json.loads(result[0].text)
        assert "error" not in payload or payload["error"] is None


# ---------------------------------------------------------------------------
# _run_coordination
# ---------------------------------------------------------------------------

class TestRunCoordination:
    @pytest.mark.asyncio
    async def test_multi_step_runs_engine(self, _patch_db):
        """_run_coordination calls CoordinationEngine.execute_plan for multi-step plans."""
        import cap.servers.orchestrator_server as srv

        plan = _two_step_plan()
        coord_result = _FakeCoordResult(
            workflow_id="wf-test123",
            steps=[
                _FakeStepResult("step-1", "explore"),
                _FakeStepResult("step-2", "dev"),
            ],
        )

        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value=plan)

        mock_engine = AsyncMock()
        mock_engine.execute_plan = AsyncMock(return_value=coord_result)

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch("cap.servers.orchestrator_server.CoordinationEngine", return_value=mock_engine, create=True), \
             patch("cap.lib.coordination_engine.CoordinationEngine", return_value=mock_engine), \
             patch("cap.servers.orchestrator_server.ConverseExecutor", MagicMock(), create=True), \
             patch("cap.lib.agent_context.SharedState", MagicMock()), \
             patch("cap.lib.agent_bus.AgentBus", MagicMock()):
            # We patch _run_coordination's local imports inline via patch context:
            with patch("cap.harness.converse_executor.ConverseExecutor", MagicMock()), \
                 patch.object(srv, "_build_dag_from_decomposer_plan", wraps=srv._build_dag_from_decomposer_plan):

                # Directly patch the imports inside _run_coordination
                with patch.dict("sys.modules", {
                    "cap.harness.converse_executor": MagicMock(ConverseExecutor=MagicMock()),
                    "cap.lib.agent_context": MagicMock(SharedState=MagicMock(return_value=MagicMock())),
                    "cap.lib.agent_bus": MagicMock(AgentBus=MagicMock(return_value=MagicMock())),
                    "cap.lib.coordination_engine": MagicMock(CoordinationEngine=MagicMock(return_value=mock_engine)),
                }):
                    result = await srv._run_coordination("fix the bug", "", "")

        payload = json.loads(result[0].text)
        assert payload["workflow_id"] == "wf-test123"
        assert payload["status"] == "completed"
        assert len(payload["steps"]) == 2
        assert payload["routing"]["method"] == "coordinated"

    @pytest.mark.asyncio
    async def test_single_step_fallback_to_execute(self, _patch_db):
        """When the decomposer returns a single-step plan, _run_coordination falls back to _handle_execute."""
        import cap.servers.orchestrator_server as srv

        plan = _single_step_plan()
        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value=plan)

        execute_response = [MagicMock(text=json.dumps({
            "agent_id": "agt-single",
            "agent_type": "dev",
            "model": "sonnet",
            "response": "single step done",
            "error": None,
            "total_input_tokens": 5,
            "total_output_tokens": 10,
            "cost_usd": 0.001,
            "duration_ms": 80,
            "turns": 1,
            "tool_calls": [],
        }))]

        with patch.object(srv, "_get_decomposer", return_value=mock_decomposer), \
             patch.object(srv, "_handle_execute", new=AsyncMock(return_value=execute_response)):
            result = await srv._run_coordination("simple task", "", "")

        payload = json.loads(result[0].text)
        assert payload["agent_id"] == "agt-single"
        assert payload["response"] == "single step done"


# ---------------------------------------------------------------------------
# _handle_coordinate
# ---------------------------------------------------------------------------

class TestHandleCoordinate:
    @pytest.mark.asyncio
    async def test_delegates_to_run_coordination(self, _patch_db):
        """_handle_coordinate passes task/context/workspace to _run_coordination."""
        import cap.servers.orchestrator_server as srv

        coord_payload = {
            "workflow_id": "wf-coord",
            "status": "completed",
            "response": "all done",
            "steps": [],
            "total_cost_usd": 0.01,
            "total_duration_ms": 200,
            "errors": [],
            "routing": {"method": "coordinated", "complexity": "moderate", "step_count": 2},
        }
        coord_contents = [MagicMock(text=json.dumps(coord_payload))]

        with patch.object(srv, "_run_coordination", new=AsyncMock(return_value=coord_contents)) as mock_rc:
            result = await srv._handle_coordinate({
                "task": "fix and deploy",
                "context": "some context",
                "workspace": "/tmp/ws",
            })

        mock_rc.assert_awaited_once_with("fix and deploy", "some context", "/tmp/ws")
        payload = json.loads(result[0].text)
        assert payload["workflow_id"] == "wf-coord"

    @pytest.mark.asyncio
    async def test_missing_task_returns_error(self, _patch_db):
        """_handle_coordinate returns an error payload when task is absent."""
        import cap.servers.orchestrator_server as srv

        result = await srv._handle_coordinate({})

        payload = json.loads(result[0].text)
        assert "error" in payload
        assert payload["error"] == "task is required"

    @pytest.mark.asyncio
    async def test_run_coordination_exception_returns_error(self, _patch_db):
        """When _run_coordination raises, _handle_coordinate returns an error payload."""
        import cap.servers.orchestrator_server as srv

        with patch.object(srv, "_run_coordination", new=AsyncMock(side_effect=RuntimeError("engine exploded"))):
            result = await srv._handle_coordinate({"task": "something"})

        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "engine exploded" in payload["error"]

    @pytest.mark.asyncio
    async def test_defaults_context_and_workspace(self, _patch_db):
        """Missing context and workspace default to empty strings."""
        import cap.servers.orchestrator_server as srv

        coord_contents = [MagicMock(text=json.dumps({"workflow_id": "x", "status": "completed"}))]

        with patch.object(srv, "_run_coordination", new=AsyncMock(return_value=coord_contents)) as mock_rc:
            await srv._handle_coordinate({"task": "do something"})

        mock_rc.assert_awaited_once_with("do something", "", "")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_cap_coordinate_tool_registered(self):
        """cap_coordinate is included in the tools returned by list_tools()."""
        import cap.servers.orchestrator_server as srv

        tools = await srv.list_tools()
        tool_names = {t.name for t in tools}
        assert "cap_coordinate" in tool_names

    @pytest.mark.asyncio
    async def test_cap_plan_still_registered(self):
        """cap_plan remains registered after the refactor."""
        import cap.servers.orchestrator_server as srv

        tools = await srv.list_tools()
        tool_names = {t.name for t in tools}
        assert "cap_plan" in tool_names

    @pytest.mark.asyncio
    async def test_all_original_tools_still_registered(self):
        """The refactor does not remove any pre-existing tools."""
        import cap.servers.orchestrator_server as srv

        tools = await srv.list_tools()
        tool_names = {t.name for t in tools}
        expected = {"cap_route", "cap_plan", "cap_status", "cap_dlq_list", "cap_health", "cap_execute", "cap_orchestrate", "cap_resume"}
        assert expected.issubset(tool_names)

    @pytest.mark.asyncio
    async def test_cap_coordinate_schema_has_required_task(self):
        """cap_coordinate schema lists task as a required field."""
        import cap.servers.orchestrator_server as srv

        tools = await srv.list_tools()
        coord_tool = next(t for t in tools if t.name == "cap_coordinate")
        assert "task" in coord_tool.inputSchema.get("required", [])

    @pytest.mark.asyncio
    async def test_call_tool_routes_cap_coordinate(self, _patch_db):
        """call_tool dispatches cap_coordinate to _handle_coordinate."""
        import cap.servers.orchestrator_server as srv

        coord_result = [MagicMock(text=json.dumps({"workflow_id": "wf-x", "status": "completed"}))]

        with patch.object(srv, "_handle_coordinate", new=AsyncMock(return_value=coord_result)) as mock_hc:
            result = await srv.call_tool("cap_coordinate", {"task": "do it"})

        mock_hc.assert_awaited_once_with({"task": "do it"})
        payload = json.loads(result[0].text)
        assert payload["workflow_id"] == "wf-x"

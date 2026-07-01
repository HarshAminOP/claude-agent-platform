"""Harness layer scenario tests — agent_store, executor, cost_meter, harness_server.

Covers the full agent lifecycle exposed via MCP tools: spawn, execute, status,
terminate, health, pool. All tests run offline with in-memory SQLite and mocked
boto3. No AWS credentials required.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.harness.agent_store import (
    AgentRecord,
    spawn_agent,
    get_agent,
    list_agents,
    terminate_agent,
    record_execution as store_record_execution,
    cleanup_stale,
    _VALID_AGENT_TYPES,
    _VALID_MODELS,
)
from cap.harness.executor import (
    MODEL_ALIASES,
    MODEL_PRICING,
    AgentExecutor,
    ExecutionResult,
    _compute_cost,
    _resolve_model,
)
from cap.harness.cost_meter import (
    AgentCostSummary,
    WorkflowCostSummary,
    _ensure_schema,
    budget_remaining,
    get_agent_cost,
    get_model_breakdown,
    get_workflow_cost,
    record_execution as cost_record_execution,
    top_spenders,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def db_path(tmp_path):
    """Isolated platform.db for agent_store tests."""
    return tmp_path / "platform.db"


@pytest.fixture
def cost_db():
    """In-memory SQLite with full CAP schema + execution_ledger for cost tests."""
    conn = get_db(":memory:")
    migrate(conn)
    _ensure_schema(conn)
    yield conn
    conn.close()


def _exec_result(
    agent_id="agent-1",
    model="sonnet",
    input_tokens=100,
    output_tokens=50,
    cost_usd=0.001275,
    duration_ms=420,
    error=None,
) -> ExecutionResult:
    return ExecutionResult(
        agent_id=agent_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        response=None if error else "ok",
        error=error,
        timestamp=datetime.now(timezone.utc),
    )


def _bedrock_response(text: str, input_tokens: int = 10, output_tokens: int = 20) -> dict:
    body = json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    ).encode()
    return {"body": BytesIO(body)}


# ===========================================================================
# AGENT STORE SCENARIOS
# ===========================================================================


class TestSpawnAgentCreatesRecord:
    """test_spawn_agent_creates_record — spawn persists a retrievable record."""

    def test_creates_retrievable_record(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert isinstance(rec, AgentRecord)
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched is not None
        assert fetched.agent_id == rec.agent_id
        assert fetched.agent_type == "dev"
        assert fetched.status == "idle"

    def test_record_has_created_timestamp(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert isinstance(rec.created_at, datetime)
        assert rec.created_at.tzinfo is not None


class TestSpawnAgentAutoSelectsModel:
    """test_spawn_agent_auto_selects_model — dev->sonnet, security->opus."""

    def test_dev_gets_sonnet(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert rec.model == "claude-sonnet-4-6"

    def test_security_gets_opus(self, db_path):
        rec = spawn_agent("security", _db_path=db_path)
        assert rec.model == "claude-opus-4-6"

    def test_optimization_gets_haiku(self, db_path):
        rec = spawn_agent("optimization", _db_path=db_path)
        assert rec.model == "claude-haiku-4-5"

    def test_explicit_model_overrides_default(self, db_path):
        rec = spawn_agent("dev", model="claude-opus-4-6", _db_path=db_path)
        assert rec.model == "claude-opus-4-6"


class TestGetAgentReturnsNoneForMissing:
    """test_get_agent_returns_none_for_missing — unknown ID yields None."""

    def test_returns_none(self, db_path):
        result = get_agent("00000000-0000-0000-0000-000000000000", _db_path=db_path)
        assert result is None

    def test_returns_none_random_string(self, db_path):
        result = get_agent("nonexistent-agent-xyz", _db_path=db_path)
        assert result is None


class TestListAgentsFiltersByStatus:
    """test_list_agents_filters_by_status — terminated agents excluded."""

    def test_filter_idle_excludes_terminated(self, db_path):
        a = spawn_agent("dev", _db_path=db_path)
        b = spawn_agent("devops", _db_path=db_path)
        terminate_agent(a.agent_id, _db_path=db_path)
        idle = list_agents(status="idle", _db_path=db_path)
        assert len(idle) == 1
        assert idle[0].agent_id == b.agent_id

    def test_filter_terminated_only(self, db_path):
        a = spawn_agent("dev", _db_path=db_path)
        spawn_agent("devops", _db_path=db_path)
        terminate_agent(a.agent_id, _db_path=db_path)
        terminated = list_agents(status="terminated", _db_path=db_path)
        assert len(terminated) == 1
        assert terminated[0].agent_id == a.agent_id


class TestListAgentsFiltersByType:
    """test_list_agents_filters_by_type — filters by agent_type field."""

    def test_only_dev_returned(self, db_path):
        spawn_agent("dev", _db_path=db_path)
        spawn_agent("dev", _db_path=db_path)
        spawn_agent("security", _db_path=db_path)
        devs = list_agents(agent_type="dev", _db_path=db_path)
        assert len(devs) == 2
        assert all(r.agent_type == "dev" for r in devs)

    def test_empty_when_no_match(self, db_path):
        spawn_agent("dev", _db_path=db_path)
        result = list_agents(agent_type="security", _db_path=db_path)
        assert result == []


class TestTerminateAgentSetsStatus:
    """test_terminate_agent_sets_status — status becomes 'terminated'."""

    def test_status_changes_to_terminated(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminated = terminate_agent(rec.agent_id, _db_path=db_path)
        assert terminated.status == "terminated"

    def test_persists_after_terminate(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminate_agent(rec.agent_id, _db_path=db_path)
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched.status == "terminated"

    def test_stores_reason_in_metadata(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminated = terminate_agent(rec.agent_id, reason="task complete", _db_path=db_path)
        assert terminated.metadata.get("termination_reason") == "task complete"


class TestRecordExecutionUpdatesTotals:
    """test_record_execution_updates_totals — tokens and cost accumulate."""

    def test_increments_task_count(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        store_record_execution(rec.agent_id, 100, 200, 0.001, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.task_count == 1

    def test_accumulates_tokens_and_cost(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        store_record_execution(rec.agent_id, 100, 200, 0.01, _db_path=db_path)
        store_record_execution(rec.agent_id, 50, 75, 0.005, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.total_input_tokens == 150
        assert updated.total_output_tokens == 275
        assert updated.task_count == 2
        assert abs(updated.total_cost_usd - 0.015) < 1e-9


class TestCleanupStaleTerminatesOldAgents:
    """test_cleanup_stale_terminates_old_agents — idle agents past threshold cleaned."""

    def _age_agent(self, agent_id: str, hours: int, db_path: Path):
        import sqlite3
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE agents SET last_active = ? WHERE agent_id = ?",
            (old_ts, agent_id),
        )
        conn.commit()
        conn.close()

    def test_terminates_stale_agent(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        self._age_agent(rec.agent_id, hours=25, db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 1
        assert get_agent(rec.agent_id, _db_path=db_path).status == "terminated"

    def test_does_not_touch_fresh_agents(self, db_path):
        spawn_agent("dev", _db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 0


# ===========================================================================
# EXECUTOR SCENARIOS
# ===========================================================================


class TestExecutorInitGracefulWithoutCreds:
    """test_executor_init_graceful_without_creds — is_available=False without AWS."""

    def test_no_credentials_sets_unavailable(self):
        from botocore.exceptions import NoCredentialsError

        with patch("boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.client.side_effect = NoCredentialsError()
            ex = AgentExecutor()
            ex._ensure_client()
            assert ex.is_available is False

    def test_execute_returns_error_when_unavailable(self):
        ex = AgentExecutor()
        ex._available = False
        r = ex.execute("a", "prompt")
        assert r.error is not None
        assert r.response is None
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.cost_usd == 0.0


class TestModelResolution:
    """test_model_resolution — sonnet -> full model ID."""

    def test_sonnet_resolves(self):
        assert _resolve_model("sonnet") == MODEL_ALIASES["sonnet"]

    def test_haiku_resolves(self):
        assert _resolve_model("haiku") == MODEL_ALIASES["haiku"]

    def test_opus_resolves(self):
        assert _resolve_model("opus") == MODEL_ALIASES["opus"]

    def test_none_defaults_to_sonnet(self):
        assert _resolve_model(None) == MODEL_ALIASES["sonnet"]

    def test_full_id_passes_through(self):
        fq = "us.anthropic.claude-sonnet-4-6-20250514"
        assert _resolve_model(fq) == fq


class TestExecutionResultDataclassFields:
    """test_execution_result_dataclass_fields — all expected fields present."""

    def test_all_fields_accessible(self):
        r = ExecutionResult(
            agent_id="a1",
            model="sonnet",
            input_tokens=5,
            output_tokens=10,
            cost_usd=0.0001,
            duration_ms=250,
            response="hello",
            error=None,
        )
        assert r.agent_id == "a1"
        assert r.model == "sonnet"
        assert r.input_tokens == 5
        assert r.output_tokens == 10
        assert r.cost_usd == 0.0001
        assert r.duration_ms == 250
        assert r.response == "hello"
        assert r.error is None
        assert isinstance(r.timestamp, datetime)
        assert r.timestamp.tzinfo is not None


class TestCostCalculation:
    """test_cost_calculation — known tokens -> expected USD."""

    def test_sonnet_1m_tokens(self):
        # 1M input @ $3.00 + 1M output @ $15.00 = $18.00
        cost = _compute_cost(MODEL_ALIASES["sonnet"], 1_000_000, 1_000_000)
        assert abs(cost - 18.00) < 1e-9

    def test_haiku_1m_tokens(self):
        # 1M input @ $0.80 + 1M output @ $4.00 = $4.80
        cost = _compute_cost(MODEL_ALIASES["haiku"], 1_000_000, 1_000_000)
        assert abs(cost - 4.80) < 1e-9

    def test_opus_1m_tokens(self):
        # 1M input @ $15.00 + 1M output @ $75.00 = $90.00
        cost = _compute_cost(MODEL_ALIASES["opus"], 1_000_000, 1_000_000)
        assert abs(cost - 90.00) < 1e-9

    def test_proportional_small_count(self):
        cost = _compute_cost(MODEL_ALIASES["sonnet"], 1000, 500)
        expected = 1000 * 3.00 / 1_000_000 + 500 * 15.00 / 1_000_000
        assert abs(cost - expected) < 1e-12

    def test_unknown_model_zero_cost(self):
        assert _compute_cost("unknown-model", 500_000, 500_000) == 0.0

    def test_zero_tokens_zero_cost(self):
        assert _compute_cost(MODEL_ALIASES["sonnet"], 0, 0) == 0.0


# ===========================================================================
# COST METER SCENARIOS
# ===========================================================================


class TestRecordExecutionInsertsLedgerRow:
    """test_record_execution_inserts_ledger_row — row appears in DB."""

    def test_inserts_row(self, cost_db):
        entry_id = cost_record_execution(_exec_result(), "dev", db=cost_db)
        row = cost_db.execute(
            "SELECT * FROM execution_ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row is not None
        assert row["agent_id"] == "agent-1"
        assert row["agent_type"] == "dev"
        assert row["model"] == "sonnet"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

    def test_returns_uuid(self, cost_db):
        entry_id = cost_record_execution(_exec_result(), "dev", db=cost_db)
        assert isinstance(entry_id, str)
        assert len(entry_id) == 36


class TestGetAgentCostAggregates:
    """test_get_agent_cost_aggregates — sums cost across executions."""

    def test_sums_multiple_executions(self, cost_db):
        cost_record_execution(_exec_result(agent_id="ag-1", cost_usd=0.01), "dev", db=cost_db)
        cost_record_execution(_exec_result(agent_id="ag-1", cost_usd=0.02), "dev", db=cost_db)
        summary = get_agent_cost("ag-1", db=cost_db)
        assert isinstance(summary, AgentCostSummary)
        assert summary.execution_count == 2
        assert abs(summary.total_cost_usd - 0.03) < 1e-6

    def test_unknown_agent_returns_zeros(self, cost_db):
        summary = get_agent_cost("ghost", db=cost_db)
        assert summary.execution_count == 0
        assert summary.total_cost_usd == 0.0


class TestGetWorkflowCostGroupsByAgentType:
    """test_get_workflow_cost_groups_by_agent_type — breakdowns correct."""

    def test_groups_by_agent_type_and_model(self, cost_db):
        cost_record_execution(
            _exec_result(agent_id="d1", model="haiku", cost_usd=0.001),
            "dev", workflow_id="wf-A", db=cost_db,
        )
        cost_record_execution(
            _exec_result(agent_id="s1", model="sonnet", cost_usd=0.010),
            "sre", workflow_id="wf-A", db=cost_db,
        )
        summary = get_workflow_cost("wf-A", db=cost_db)
        assert isinstance(summary, WorkflowCostSummary)
        assert abs(summary.total_cost_usd - 0.011) < 1e-6
        assert "dev" in summary.by_agent_type
        assert "haiku" in summary.by_model

    def test_empty_workflow_returns_zero(self, cost_db):
        summary = get_workflow_cost("wf-nonexistent", db=cost_db)
        assert summary.total_cost_usd == 0.0


class TestGetModelBreakdownPercentages:
    """test_get_model_breakdown_percentages — percentages sum to 100."""

    def test_percentages_sum_to_100(self, cost_db):
        cost_record_execution(_exec_result(model="haiku", cost_usd=0.001), "dev", db=cost_db)
        cost_record_execution(_exec_result(model="sonnet", cost_usd=0.009), "dev", db=cost_db)
        breakdown = get_model_breakdown(db=cost_db)
        total_pct = sum(e.pct_of_total for e in breakdown.values())
        assert abs(total_pct - 100.0) < 0.01

    def test_empty_db_returns_empty(self, cost_db):
        assert get_model_breakdown(db=cost_db) == {}


class TestBudgetRemainingSubtractsTodaySpend:
    """test_budget_remaining_subtracts_today_spend — decreases after spend."""

    def test_full_budget_when_nothing_spent(self, cost_db):
        remaining = budget_remaining(daily_limit_usd=5.0, db=cost_db)
        assert abs(remaining - 5.0) < 1e-6

    def test_decreases_after_spend(self, cost_db):
        cost_record_execution(_exec_result(cost_usd=1.0), "dev", db=cost_db)
        remaining = budget_remaining(daily_limit_usd=5.0, db=cost_db)
        assert abs(remaining - 4.0) < 1e-4

    def test_negative_when_over_budget(self, cost_db):
        cost_record_execution(_exec_result(cost_usd=6.0), "dev", db=cost_db)
        remaining = budget_remaining(daily_limit_usd=5.0, db=cost_db)
        assert remaining < 0


class TestTopSpendersOrderedByCost:
    """test_top_spenders_ordered_by_cost — highest spender first."""

    def test_ordered_descending(self, cost_db):
        cost_record_execution(_exec_result(agent_id="cheap", cost_usd=0.001), "dev", db=cost_db)
        cost_record_execution(_exec_result(agent_id="pricey", cost_usd=0.999), "dev", db=cost_db)
        spenders = top_spenders(n=10, db=cost_db)
        assert spenders[0].agent_id == "pricey"
        assert spenders[1].agent_id == "cheap"

    def test_respects_n_limit(self, cost_db):
        for i in range(5):
            cost_record_execution(
                _exec_result(agent_id=f"ag-{i}", cost_usd=float(i)), "dev", db=cost_db
            )
        spenders = top_spenders(n=3, db=cost_db)
        assert len(spenders) == 3

    def test_empty_returns_empty(self, cost_db):
        assert top_spenders(db=cost_db) == []


# ===========================================================================
# HARNESS SERVER SCENARIOS
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_store_for_server(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite DB so agent IDs don't bleed across tests."""
    import cap.harness.agent_store as store_mod
    db_path = tmp_path / "platform.db"
    conn = store_mod._open_db(db_path)
    monkeypatch.setattr(store_mod, "_conn", conn)
    yield
    conn.close()


def _make_exec_result(agent_id: str, response: str = "ok", error: str | None = None):
    return ExecutionResult(
        agent_id=agent_id,
        model="us.anthropic.claude-sonnet-4-6-20250514",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.00035,
        duration_ms=120,
        response=response,
        error=error,
        timestamp=datetime.now(timezone.utc),
    )


class TestAgentSpawnToolReturnsAgentId:
    """test_agent_spawn_tool_returns_agent_id — MCP spawn returns agent_id."""

    @pytest.mark.asyncio
    async def test_returns_agent_id_in_response(self):
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({"agent_type": "dev"})
        data = json.loads(result[0].text)
        assert "agent_id" in data
        assert data["agent_type"] == "dev"
        assert data["status"] == "idle"

    @pytest.mark.asyncio
    async def test_invalid_type_returns_error(self):
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({"agent_type": "unknown-robot"})
        data = json.loads(result[0].text)
        assert "error" in data


class TestAgentExecuteRecordsCost:
    """test_agent_execute_records_cost — execution updates cost tracking."""

    @pytest.mark.asyncio
    async def test_execute_returns_cost_fields(self):
        from cap.servers.harness_server import _handle_spawn, _handle_execute

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        exec_result = _make_exec_result(agent_id, response="done")

        with patch("cap.servers.harness_server._get_executor") as mock_get:
            mock_exec = MagicMock()
            mock_exec.execute.return_value = exec_result
            mock_get.return_value = mock_exec

            with patch("cap.servers.harness_server._cost_meter.record_execution"):
                result = await _handle_execute({
                    "agent_id": agent_id,
                    "prompt": "do something",
                })

        data = json.loads(result[0].text)
        assert data["agent_id"] == agent_id
        assert data["cost_usd"] == pytest.approx(0.00035)
        assert data["input_tokens"] == 10
        assert data["output_tokens"] == 20
        assert data["duration_ms"] == 120


class TestAgentStatusListsActive:
    """test_agent_status_lists_active — status endpoint shows active agents."""

    @pytest.mark.asyncio
    async def test_lists_active_agents_only(self):
        from cap.servers.harness_server import _handle_spawn, _handle_status, _handle_terminate

        await _handle_spawn({"agent_type": "dev"})
        spawn_result = await _handle_spawn({"agent_type": "docs"})
        docs_id = json.loads(spawn_result[0].text)["agent_id"]

        await _handle_terminate({"agent_id": docs_id})

        result = await _handle_status({})
        data = json.loads(result[0].text)
        assert data["count"] == 1
        for a in data["agents"]:
            assert a["status"] != "terminated"


class TestAgentTerminateMarksTerminated:
    """test_agent_terminate_marks_terminated — terminate tool sets status."""

    @pytest.mark.asyncio
    async def test_terminate_sets_status(self):
        from cap.servers.harness_server import _handle_spawn, _handle_terminate

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        result = await _handle_terminate({"agent_id": agent_id, "reason": "done"})
        data = json.loads(result[0].text)
        assert data["status"] == "terminated"
        assert data["reason"] == "done"

    @pytest.mark.asyncio
    async def test_terminate_not_found_returns_error(self):
        from cap.servers.harness_server import _handle_terminate
        result = await _handle_terminate({"agent_id": "ghost"})
        data = json.loads(result[0].text)
        assert "error" in data


class TestAgentHealthShowsAvailability:
    """test_agent_health_shows_availability — health endpoint reports executor state."""

    @pytest.mark.asyncio
    async def test_shows_executor_available(self):
        from cap.servers.harness_server import _handle_health

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=5.0):
            with patch("cap.servers.harness_server._get_executor") as mock_get:
                mock_exec = MagicMock()
                mock_exec.is_available = True
                mock_get.return_value = mock_exec
                result = await _handle_health({})

        data = json.loads(result[0].text)
        assert data["executor_available"] is True
        assert data["circuit_breaker"] == "closed"

    @pytest.mark.asyncio
    async def test_shows_executor_unavailable(self):
        from cap.servers.harness_server import _handle_health

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=5.0):
            with patch("cap.servers.harness_server._get_executor") as mock_get:
                mock_exec = MagicMock()
                mock_exec.is_available = False
                mock_get.return_value = mock_exec
                result = await _handle_health({})

        data = json.loads(result[0].text)
        assert data["executor_available"] is False
        assert data["circuit_breaker"] == "open"


class TestAgentPoolSpawnCreatesMultiple:
    """test_agent_pool_spawn_creates_multiple — pool spawn N agents."""

    @pytest.mark.asyncio
    async def test_spawns_requested_count(self):
        from cap.servers.harness_server import _handle_pool
        result = await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 3})
        data = json.loads(result[0].text)
        assert data["action"] == "spawn"
        assert len(data["spawned"]) == 3

    @pytest.mark.asyncio
    async def test_pool_status_reflects_spawned(self):
        from cap.servers.harness_server import _handle_pool
        await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 2})
        result = await _handle_pool({"action": "status"})
        data = json.loads(result[0].text)
        assert data["total_active"] == 2

    @pytest.mark.asyncio
    async def test_pool_drain_terminates_idle(self):
        from cap.servers.harness_server import _handle_pool
        await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 3})
        result = await _handle_pool({"action": "drain", "agent_type": "dev"})
        data = json.loads(result[0].text)
        assert data["terminated"] == 3

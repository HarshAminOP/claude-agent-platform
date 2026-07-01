"""Unit tests for cap.servers.harness_server.

All tests are fully offline — no AWS credentials needed.
The agent store uses a temp SQLite file; executor.execute() is patched.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Patch boto3 before any cap imports so the executor can be imported cleanly
# even in environments without AWS credentials.
import boto3  # noqa: F401  — ensure it can be patched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_exec_result(agent_id: str, response: str = "ok", error: str | None = None):
    from cap.harness.executor import ExecutionResult
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite DB so agent IDs don't bleed across tests."""
    import cap.harness.agent_store as store_mod
    db_path = tmp_path / "platform.db"
    conn = store_mod._open_db(db_path)
    # Patch global connection so all store calls use our isolated DB
    monkeypatch.setattr(store_mod, "_conn", conn)
    yield
    conn.close()


@pytest.fixture()
def db(tmp_path):
    """In-memory SQLite with full CAP schema (for cost_meter calls)."""
    from cap.db import get_db, migrate
    conn = get_db(":memory:")
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Import-level smoke test
# ---------------------------------------------------------------------------

def test_import_mcp_object():
    """Server module must export `mcp` with the correct name."""
    from cap.servers.harness_server import mcp
    assert mcp.name == "cap-harness"


# ---------------------------------------------------------------------------
# agent_spawn
# ---------------------------------------------------------------------------

class TestAgentSpawn:
    @pytest.mark.asyncio
    async def test_spawn_known_type(self):
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({"agent_type": "dev"})
        data = json.loads(result[0].text)
        assert "agent_id" in data
        assert data["agent_type"] == "dev"
        assert data["status"] == "idle"
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_spawn_explicit_model(self):
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({"agent_type": "dev", "model": "claude-haiku-4-5"})
        data = json.loads(result[0].text)
        assert data["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_spawn_auto_model_complex(self):
        """Complex keywords should trigger opus selection."""
        from cap.servers.harness_server import _handle_spawn
        # Use an opus-default agent type so auto-selection overrides won't be blocked
        result = await _handle_spawn({
            "agent_type": "security",
            "task_description": "audit the security posture of this service",
        })
        data = json.loads(result[0].text)
        # security type defaults to opus; task_description also triggers opus selection
        assert "opus" in data["model"]

    @pytest.mark.asyncio
    async def test_spawn_auto_model_simple(self):
        """Simple keywords should trigger haiku selection."""
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({
            "agent_type": "dev",
            "task_description": "rename a file",
        })
        data = json.loads(result[0].text)
        assert "haiku" in data["model"]

    @pytest.mark.asyncio
    async def test_spawn_unknown_type_returns_error(self):
        from cap.servers.harness_server import _handle_spawn
        result = await _handle_spawn({"agent_type": "unknown-robot"})
        data = json.loads(result[0].text)
        assert "error" in data


# ---------------------------------------------------------------------------
# agent_execute
# ---------------------------------------------------------------------------

class TestAgentExecute:
    @pytest.mark.asyncio
    async def test_execute_success(self, db):
        from cap.servers.harness_server import _handle_spawn, _handle_execute

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        exec_result = _make_exec_result(agent_id, response="hello world")

        with patch("cap.servers.harness_server._get_executor") as mock_get:
            mock_exec = MagicMock()
            mock_exec.execute.return_value = exec_result
            mock_get.return_value = mock_exec

            with patch("cap.servers.harness_server._cost_meter.record_execution"):
                result = await _handle_execute({
                    "agent_id": agent_id,
                    "prompt": "say hello",
                })

        data = json.loads(result[0].text)
        assert data["agent_id"] == agent_id
        assert data["response"] == "hello world"
        assert data["input_tokens"] == 10
        assert data["output_tokens"] == 20
        assert data["cost_usd"] == pytest.approx(0.00035)
        assert data["duration_ms"] == 120
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_execute_agent_not_found(self):
        from cap.servers.harness_server import _handle_execute
        result = await _handle_execute({"agent_id": "nonexistent-id", "prompt": "hi"})
        data = json.loads(result[0].text)
        assert data["error"] == "agent not found"

    @pytest.mark.asyncio
    async def test_execute_bedrock_unavailable(self):
        from cap.servers.harness_server import _handle_spawn, _handle_execute

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        degraded_result = _make_exec_result(agent_id, response=None, error="bedrock unavailable: client not initialised")

        with patch("cap.servers.harness_server._get_executor") as mock_get:
            mock_exec = MagicMock()
            mock_exec.execute.return_value = degraded_result
            mock_get.return_value = mock_exec

            result = await _handle_execute({"agent_id": agent_id, "prompt": "hi"})

        data = json.loads(result[0].text)
        assert data.get("degraded") is True
        assert "error" in data


# ---------------------------------------------------------------------------
# agent_status
# ---------------------------------------------------------------------------

class TestAgentStatus:
    @pytest.mark.asyncio
    async def test_status_single(self):
        from cap.servers.harness_server import _handle_spawn, _handle_status

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        result = await _handle_status({"agent_id": agent_id})
        data = json.loads(result[0].text)
        assert data["agent_id"] == agent_id
        assert data["status"] == "idle"

    @pytest.mark.asyncio
    async def test_status_not_found(self):
        from cap.servers.harness_server import _handle_status
        result = await _handle_status({"agent_id": "no-such-agent"})
        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_status_all_active(self):
        from cap.servers.harness_server import _handle_spawn, _handle_status, _handle_terminate

        await _handle_spawn({"agent_type": "dev"})
        spawn_result = await _handle_spawn({"agent_type": "docs"})
        agent_id2 = json.loads(spawn_result[0].text)["agent_id"]

        # Terminate one; it should not appear in the listing
        await _handle_terminate({"agent_id": agent_id2})

        result = await _handle_status({})
        data = json.loads(result[0].text)
        assert data["count"] == 1
        for a in data["agents"]:
            assert a["status"] != "terminated"


# ---------------------------------------------------------------------------
# agent_terminate
# ---------------------------------------------------------------------------

class TestAgentTerminate:
    @pytest.mark.asyncio
    async def test_terminate_success(self):
        from cap.servers.harness_server import _handle_spawn, _handle_terminate

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]

        result = await _handle_terminate({"agent_id": agent_id, "reason": "test done"})
        data = json.loads(result[0].text)
        assert data["status"] == "terminated"
        assert data["reason"] == "test done"

    @pytest.mark.asyncio
    async def test_terminate_not_found(self):
        from cap.servers.harness_server import _handle_terminate
        result = await _handle_terminate({"agent_id": "ghost"})
        data = json.loads(result[0].text)
        assert "error" in data


# ---------------------------------------------------------------------------
# agent_cost
# ---------------------------------------------------------------------------

class TestAgentCost:
    @pytest.mark.asyncio
    async def test_cost_today_summary(self):
        from cap.servers.harness_server import _handle_cost

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=4.5):
            with patch("cap.servers.harness_server._cost_meter.get_model_breakdown", return_value={}):
                result = await _handle_cost({})

        data = json.loads(result[0].text)
        assert "today_spent_usd" in data
        assert "budget_remaining_usd" in data
        assert data["budget_remaining_usd"] == pytest.approx(4.5)

    @pytest.mark.asyncio
    async def test_cost_by_agent_id(self):
        from cap.servers.harness_server import _handle_cost
        from cap.harness.cost_meter import AgentCostSummary

        mock_summary = AgentCostSummary(
            agent_id="abc",
            agent_type="dev",
            total_cost_usd=0.01,
            total_tokens=1000,
            execution_count=2,
        )
        with patch("cap.servers.harness_server._cost_meter.get_agent_cost", return_value=mock_summary):
            result = await _handle_cost({"agent_id": "abc"})

        data = json.loads(result[0].text)
        assert data["agent_id"] == "abc"
        assert data["total_cost_usd"] == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_cost_by_workflow_id(self):
        from cap.servers.harness_server import _handle_cost
        from cap.harness.cost_meter import WorkflowCostSummary

        mock_summary = WorkflowCostSummary(
            workflow_id="wf-123",
            total_cost_usd=0.05,
            by_agent_type={"dev": 0.03},
            by_model={"sonnet": 0.05},
        )
        with patch("cap.servers.harness_server._cost_meter.get_workflow_cost", return_value=mock_summary):
            result = await _handle_cost({"workflow_id": "wf-123"})

        data = json.loads(result[0].text)
        assert data["workflow_id"] == "wf-123"
        assert data["total_cost_usd"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# agent_health
# ---------------------------------------------------------------------------

class TestAgentHealth:
    @pytest.mark.asyncio
    async def test_health_no_agents(self):
        from cap.servers.harness_server import _handle_health

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=5.0):
            with patch("cap.servers.harness_server._get_executor") as mock_get:
                mock_exec = MagicMock()
                mock_exec.is_available = None
                mock_get.return_value = mock_exec

                result = await _handle_health({})

        data = json.loads(result[0].text)
        assert "executor_available" in data
        assert data["active_agent_count"] == 0
        assert data["circuit_breaker"] == "unknown"

    @pytest.mark.asyncio
    async def test_health_executor_available(self):
        from cap.servers.harness_server import _handle_health

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=3.0):
            with patch("cap.servers.harness_server._get_executor") as mock_get:
                mock_exec = MagicMock()
                mock_exec.is_available = True
                mock_get.return_value = mock_exec

                result = await _handle_health({})

        data = json.loads(result[0].text)
        assert data["executor_available"] is True
        assert data["circuit_breaker"] == "closed"

    @pytest.mark.asyncio
    async def test_health_executor_unavailable(self):
        from cap.servers.harness_server import _handle_health

        with patch("cap.servers.harness_server._cost_meter.budget_remaining", return_value=5.0):
            with patch("cap.servers.harness_server._get_executor") as mock_get:
                mock_exec = MagicMock()
                mock_exec.is_available = False
                mock_get.return_value = mock_exec

                result = await _handle_health({})

        data = json.loads(result[0].text)
        assert data["circuit_breaker"] == "open"


# ---------------------------------------------------------------------------
# agent_pool
# ---------------------------------------------------------------------------

class TestAgentPool:
    @pytest.mark.asyncio
    async def test_pool_status_empty(self):
        from cap.servers.harness_server import _handle_pool
        result = await _handle_pool({"action": "status"})
        data = json.loads(result[0].text)
        assert data["action"] == "status"
        assert data["total_active"] == 0

    @pytest.mark.asyncio
    async def test_pool_spawn(self):
        from cap.servers.harness_server import _handle_pool
        result = await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 3})
        data = json.loads(result[0].text)
        assert data["action"] == "spawn"
        assert len(data["spawned"]) == 3

    @pytest.mark.asyncio
    async def test_pool_spawn_missing_agent_type(self):
        from cap.servers.harness_server import _handle_pool
        result = await _handle_pool({"action": "spawn"})
        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_pool_drain(self):
        from cap.servers.harness_server import _handle_pool

        # Spawn some agents first
        await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 2})

        result = await _handle_pool({"action": "drain", "agent_type": "dev"})
        data = json.loads(result[0].text)
        assert data["action"] == "drain"
        assert data["terminated"] == 2

    @pytest.mark.asyncio
    async def test_pool_status_after_spawn(self):
        from cap.servers.harness_server import _handle_pool

        await _handle_pool({"action": "spawn", "agent_type": "dev", "count": 2})
        await _handle_pool({"action": "spawn", "agent_type": "docs", "count": 1})

        result = await _handle_pool({"action": "status"})
        data = json.loads(result[0].text)
        assert data["total_active"] == 3
        assert data["by_type"]["dev"]["total"] == 2
        assert data["by_type"]["docs"]["total"] == 1

    @pytest.mark.asyncio
    async def test_pool_unknown_action(self):
        from cap.servers.harness_server import _handle_pool
        result = await _handle_pool({"action": "explode"})
        data = json.loads(result[0].text)
        assert "error" in data


# ---------------------------------------------------------------------------
# auto_select_model
# ---------------------------------------------------------------------------

class TestAutoSelectModel:
    def test_complex_keyword(self):
        from cap.servers.harness_server import _auto_select_model
        assert "opus" in _auto_select_model("audit the security configuration")

    def test_simple_keyword(self):
        from cap.servers.harness_server import _auto_select_model
        assert "haiku" in _auto_select_model("format the output file")

    def test_neutral_defaults_to_sonnet(self):
        from cap.servers.harness_server import _auto_select_model
        assert "sonnet" in _auto_select_model("implement a new feature")


# ---------------------------------------------------------------------------
# agent_logs
# ---------------------------------------------------------------------------

class TestAgentLogs:
    @pytest.mark.asyncio
    async def test_agent_logs_empty(self):
        from cap.servers.harness_server import _handle_agent_logs
        with patch("cap.servers.harness_server.get_db") as mock_get_db:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_get_db.return_value = mock_conn
            # Patch _ensure_schema so it's a no-op
            with patch("cap.harness.cost_meter._ensure_schema"):
                result = await _handle_agent_logs({"agent_id": "abc-123", "limit": 10})
        data = json.loads(result[0].text)
        assert data["agent_id"] == "abc-123"
        assert data["count"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_agent_logs_returns_entries(self):
        from cap.servers.harness_server import _handle_agent_logs
        fake_rows = [
            ("2026-06-30T10:00:00", "claude-sonnet-4-6", 100, 200, 0.005, 300, 1, None),
            ("2026-06-30T09:00:00", "claude-sonnet-4-6", 50, 80, 0.002, 150, 0, "timeout"),
        ]
        with patch("cap.servers.harness_server.get_db") as mock_get_db:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = fake_rows
            mock_get_db.return_value = mock_conn
            with patch("cap.harness.cost_meter._ensure_schema"):
                result = await _handle_agent_logs({"agent_id": "abc-123"})
        data = json.loads(result[0].text)
        assert data["count"] == 2
        assert data["entries"][0]["model"] == "claude-sonnet-4-6"
        assert data["entries"][0]["success"] is True
        assert data["entries"][1]["success"] is False
        assert data["entries"][1]["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_agent_logs_limit_capped(self):
        """Limit above 1000 is silently capped."""
        from cap.servers.harness_server import _handle_agent_logs
        with patch("cap.servers.harness_server.get_db") as mock_get_db:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_get_db.return_value = mock_conn
            with patch("cap.harness.cost_meter._ensure_schema"):
                result = await _handle_agent_logs({"agent_id": "x", "limit": 9999})
        data = json.loads(result[0].text)
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# audit_trail
# ---------------------------------------------------------------------------

class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_audit_trail_empty(self):
        from cap.servers.harness_server import _handle_audit_trail
        with patch("cap.servers.harness_server._get_audit_conn") as mock_conn_fn:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_conn_fn.return_value = mock_conn
            result = await _handle_audit_trail({})
        data = json.loads(result[0].text)
        assert data["count"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_audit_trail_with_entries(self):
        from cap.servers.harness_server import _handle_audit_trail
        import time
        ts = time.time()
        fake_rows = [
            (ts, "agent_spawn", "dev-agent-1", "spawn dev", 1),
            (ts - 60, "agent_execute", "dev-agent-1", "say hello", 1),
        ]
        with patch("cap.servers.harness_server._get_audit_conn") as mock_conn_fn:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = fake_rows
            mock_conn_fn.return_value = mock_conn
            result = await _handle_audit_trail({"agent_id": "dev-agent-1"})
        data = json.loads(result[0].text)
        assert data["count"] == 2
        assert data["entries"][0]["tool_name"] == "agent_spawn"
        assert data["entries"][0]["agent_id"] == "dev-agent-1"
        assert data["entries"][0]["success"] is True

    @pytest.mark.asyncio
    async def test_audit_trail_invalid_since(self):
        from cap.servers.harness_server import _handle_audit_trail
        result = await _handle_audit_trail({"since": "not-a-date"})
        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_audit_trail_since_filter(self):
        from cap.servers.harness_server import _handle_audit_trail
        with patch("cap.servers.harness_server._get_audit_conn") as mock_conn_fn:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_conn_fn.return_value = mock_conn
            result = await _handle_audit_trail({"since": "2026-06-01T00:00:00Z"})
        data = json.loads(result[0].text)
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# agent_execute content guardrail
# ---------------------------------------------------------------------------

class TestExecuteGuardrail:
    @pytest.mark.asyncio
    async def test_execute_no_warnings_on_safe_response(self, db):
        from cap.servers.harness_server import _handle_spawn, _handle_execute

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]
        exec_result = _make_exec_result(agent_id, response="here is your refactored code")

        with patch("cap.servers.harness_server._get_executor") as mock_get:
            mock_exec = MagicMock()
            mock_exec.execute.return_value = exec_result
            mock_get.return_value = mock_exec
            with patch("cap.servers.harness_server._cost_meter.record_execution"):
                result = await _handle_execute({"agent_id": agent_id, "prompt": "refactor"})

        data = json.loads(result[0].text)
        assert "warnings" not in data

    @pytest.mark.asyncio
    async def test_execute_warnings_on_dangerous_response(self, db):
        from cap.servers.harness_server import _handle_spawn, _handle_execute

        spawn_result = await _handle_spawn({"agent_type": "dev"})
        agent_id = json.loads(spawn_result[0].text)["agent_id"]
        # Response contains a dangerous pattern
        exec_result = _make_exec_result(agent_id, response="run: rm -rf /tmp/data")

        with patch("cap.servers.harness_server._get_executor") as mock_get:
            mock_exec = MagicMock()
            mock_exec.execute.return_value = exec_result
            mock_get.return_value = mock_exec
            with patch("cap.servers.harness_server._cost_meter.record_execution"):
                with patch("cap.servers.harness_server._record_audit"):
                    result = await _handle_execute({"agent_id": agent_id, "prompt": "cleanup"})

        data = json.loads(result[0].text)
        # Response is NOT blocked — still present
        assert data["response"] == "run: rm -rf /tmp/data"
        # But warnings field is populated
        assert "warnings" in data
        assert len(data["warnings"]) > 0

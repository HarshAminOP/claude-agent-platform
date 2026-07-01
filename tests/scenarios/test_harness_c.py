"""Phase C Harness verification tests — Swarm, Coordination, and Integration.

Tests cover three areas matching Ruflo's swarm-tools.ts + coordination-tools.ts:
1. Swarm — init, validate topology, status with agents, health metrics, shutdown, list
2. Coordination — assign picks idle, assign spawns, assign queues when full, release, balance, consensus
3. Integration — full lifecycle, public API imports, doctor section

All tests are offline — no credentials, no network, isolated temp DBs.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.harness.swarm import (
    swarm_init,
    swarm_status,
    swarm_health,
    swarm_shutdown,
    swarm_list,
    _VALID_TOPOLOGIES,
)
from cap.harness.coordination import (
    coordination_assign,
    coordination_release,
    coordination_balance,
    coordination_consensus,
)
from cap.harness.agent_store import (
    spawn_agent,
    get_agent,
    list_agents,
    update_agent,
    terminate_agent,
    record_execution,
)
from cap.harness.cost_meter import record_execution as record_cost


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "platform_c.db"


# ===========================================================================
# 1. SWARM TESTS
# ===========================================================================


class TestSwarmInitCreatesRecord:
    """swarm_init persists a swarm and returns its ID."""

    def test_swarm_init_creates_record(self, db_path):
        result = swarm_init("test-swarm", topology="mesh", max_agents=4, _db_path=db_path)
        assert "swarm_id" in result
        assert result["name"] == "test-swarm"
        assert result["topology"] == "mesh"
        assert result["status"] == "running"
        assert result["max_agents"] == 4


class TestSwarmInitValidatesTopology:
    """swarm_init rejects invalid topology values."""

    def test_swarm_init_validates_topology(self, db_path):
        with pytest.raises(ValueError, match="Invalid topology"):
            swarm_init("bad", topology="ring", _db_path=db_path)

        # All valid topologies should pass
        for topo in _VALID_TOPOLOGIES:
            result = swarm_init(f"swarm-{topo}", topology=topo, _db_path=db_path)
            assert result["topology"] == topo


class TestSwarmStatusIncludesAgents:
    """swarm_status returns agent list and counts."""

    def test_swarm_status_includes_agents(self, db_path):
        result = swarm_init("with-agents", _db_path=db_path)
        swarm_id = result["swarm_id"]

        # Spawn agents in this swarm
        spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        spawn_agent("devops", swarm_id=swarm_id, _db_path=db_path)

        status = swarm_status(swarm_id, _db_path=db_path)
        assert status["swarm_id"] == swarm_id
        assert status["agent_count"] == 2
        assert status["active_count"] == 2  # both idle counts as active
        assert len(status["agents"]) == 2
        assert all("agent_id" in a for a in status["agents"])
        assert all("agent_type" in a for a in status["agents"])


class TestSwarmHealthComputesMetrics:
    """swarm_health computes utilization, cost, and failure metrics."""

    def test_swarm_health_computes_metrics(self, db_path):
        result = swarm_init("health-test", _db_path=db_path)
        swarm_id = result["swarm_id"]

        # Spawn 2 agents: one idle, one busy
        a1 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        a2 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(a2.agent_id, status="busy", _db_path=db_path)

        health = swarm_health(swarm_id, _db_path=db_path)
        assert health["healthy"] is True
        assert health["total_agents"] == 2
        assert health["busy_agents"] == 1
        assert health["agent_utilization"] == pytest.approx(0.5, abs=0.01)
        assert health["failed_count"] == 0
        assert "total_cost_usd" in health


class TestSwarmShutdownTerminatesAgents:
    """swarm_shutdown terminates all active agents and updates swarm status."""

    def test_swarm_shutdown_terminates_agents(self, db_path):
        result = swarm_init("shutdown-test", _db_path=db_path)
        swarm_id = result["swarm_id"]

        a1 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        a2 = spawn_agent("devops", swarm_id=swarm_id, _db_path=db_path)
        update_agent(a2.agent_id, status="busy", _db_path=db_path)

        shutdown = swarm_shutdown(swarm_id, reason="completed", _db_path=db_path)
        assert shutdown["swarm_id"] == swarm_id
        assert shutdown["status"] == "completed"
        assert shutdown["agents_terminated"] == 2

        # Verify agents are terminated
        agent1 = get_agent(a1.agent_id, _db_path=db_path)
        agent2 = get_agent(a2.agent_id, _db_path=db_path)
        assert agent1.status == "terminated"
        assert agent2.status == "terminated"


class TestSwarmListFiltersByStatus:
    """swarm_list returns swarms filtered by status."""

    def test_swarm_list_filters_by_status(self, db_path):
        s1 = swarm_init("running-swarm", _db_path=db_path)
        s2 = swarm_init("completed-swarm", _db_path=db_path)
        swarm_shutdown(s2["swarm_id"], reason="completed", _db_path=db_path)

        all_swarms = swarm_list(_db_path=db_path)
        assert len(all_swarms) == 2

        running = swarm_list(status="running", _db_path=db_path)
        assert len(running) == 1
        assert running[0]["name"] == "running-swarm"

        completed = swarm_list(status="completed", _db_path=db_path)
        assert len(completed) == 1
        assert completed[0]["name"] == "completed-swarm"


# ===========================================================================
# 2. COORDINATION TESTS
# ===========================================================================


class TestAssignPicksIdleAgent:
    """coordination_assign picks an idle agent of the preferred type."""

    def test_assign_picks_idle_agent(self, db_path):
        s = swarm_init("assign-test", _db_path=db_path)
        swarm_id = s["swarm_id"]

        a = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)

        result = coordination_assign(
            swarm_id, "implement feature", preferred_agent_type="dev", _db_path=db_path
        )
        assert result["assigned"] is True
        assert result["agent_id"] == a.agent_id
        assert result["spawned"] is False

        # Agent should now be busy
        agent = get_agent(a.agent_id, _db_path=db_path)
        assert agent.status == "busy"


class TestAssignSpawnsWhenNoneIdle:
    """coordination_assign spawns a new agent when no idle agents exist."""

    def test_assign_spawns_when_none_idle(self, db_path):
        s = swarm_init("spawn-test", max_agents=4, _db_path=db_path)
        swarm_id = s["swarm_id"]

        # No agents exist yet — should spawn one
        result = coordination_assign(
            swarm_id, "deploy service", preferred_agent_type="devops", _db_path=db_path
        )
        assert result["assigned"] is True
        assert result["spawned"] is True
        assert result["agent_type"] == "devops"


class TestAssignQueuesWhenFull:
    """coordination_assign queues the task when swarm is at capacity."""

    def test_assign_queues_when_full(self, db_path):
        s = swarm_init("full-test", max_agents=1, _db_path=db_path)
        swarm_id = s["swarm_id"]

        # Spawn one agent and mark it busy
        a = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(a.agent_id, status="busy", _db_path=db_path)

        # Should queue because max_agents=1 and one is already busy
        result = coordination_assign(
            swarm_id, "another task", preferred_agent_type="dev", _db_path=db_path
        )
        assert result["queued"] is True
        assert "swarm full" in result["reason"]


class TestReleaseSetIdle:
    """coordination_release sets the agent back to idle."""

    def test_release_sets_idle(self, db_path):
        s = swarm_init("release-test", _db_path=db_path)
        swarm_id = s["swarm_id"]

        a = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(a.agent_id, status="busy", _db_path=db_path)

        result = coordination_release(a.agent_id, _db_path=db_path)
        assert result["status"] == "idle"
        assert result["agent_id"] == a.agent_id

        agent = get_agent(a.agent_id, _db_path=db_path)
        assert agent.status == "idle"


class TestBalanceIdentifiesBottlenecks:
    """coordination_balance detects agent types that are all busy (bottleneck)."""

    def test_balance_identifies_bottlenecks(self, db_path):
        s = swarm_init("balance-test", max_agents=8, _db_path=db_path)
        swarm_id = s["swarm_id"]

        # Create 2 dev agents, both busy (bottleneck)
        d1 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        d2 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(d1.agent_id, status="busy", _db_path=db_path)
        update_agent(d2.agent_id, status="busy", _db_path=db_path)

        # Create 2 devops agents, both idle (over-provisioned)
        o1 = spawn_agent("devops", swarm_id=swarm_id, _db_path=db_path)
        o2 = spawn_agent("devops", swarm_id=swarm_id, _db_path=db_path)

        balance = coordination_balance(swarm_id, _db_path=db_path)
        assert balance["balanced"] is False
        assert "dev" in balance["bottlenecks"]
        assert "devops" in balance["over_provisioned"]
        assert "Spawn more agents" in balance["recommendation"]


class TestConsensusMajorityApprove:
    """coordination_consensus approves when majority votes approve."""

    def test_consensus_majority_approve(self, db_path):
        s = swarm_init("consensus-test", _db_path=db_path)
        swarm_id = s["swarm_id"]

        votes = {
            "agent-1": "approve",
            "agent-2": "approve",
            "agent-3": "reject",
        }
        result = coordination_consensus(
            swarm_id, "deploy to production", votes=votes, _db_path=db_path
        )
        assert result["outcome"] == "approved"
        assert result["votes_for"] == 2
        assert result["votes_against"] == 1
        assert result["total"] == 3


class TestConsensusMajorityReject:
    """coordination_consensus rejects when majority votes reject (or tie)."""

    def test_consensus_majority_reject(self, db_path):
        s = swarm_init("reject-test", _db_path=db_path)
        swarm_id = s["swarm_id"]

        # Tie case should reject
        votes_tie = {
            "agent-1": "approve",
            "agent-2": "reject",
        }
        result_tie = coordination_consensus(
            swarm_id, "risky change", votes=votes_tie, _db_path=db_path
        )
        assert result_tie["outcome"] == "rejected"

        # Majority reject
        votes_reject = {
            "agent-1": "reject",
            "agent-2": "reject",
            "agent-3": "approve",
        }
        result_reject = coordination_consensus(
            swarm_id, "delete data", votes=votes_reject, _db_path=db_path
        )
        assert result_reject["outcome"] == "rejected"
        assert result_reject["votes_for"] == 1
        assert result_reject["votes_against"] == 2


# ===========================================================================
# 3. INTEGRATION TESTS
# ===========================================================================


class TestFullHarnessLifecycle:
    """End-to-end: spawn swarm -> assign task -> execute -> record cost -> shutdown."""

    def test_full_harness_lifecycle(self, db_path):
        # 1. Init swarm
        swarm = swarm_init("lifecycle-test", topology="hierarchical", max_agents=4, _db_path=db_path)
        swarm_id = swarm["swarm_id"]
        assert swarm["status"] == "running"

        # 2. Assign a task (spawns a new agent since none exist)
        assign_result = coordination_assign(
            swarm_id, "implement OAuth flow", preferred_agent_type="dev", _db_path=db_path
        )
        assert assign_result["assigned"] is True
        assert assign_result["spawned"] is True
        agent_id = assign_result["agent_id"]

        # 3. Verify agent is busy
        agent = get_agent(agent_id, _db_path=db_path)
        assert agent.status == "busy"
        assert agent.agent_type == "dev"

        # 4. Simulate execution completion via agent_store.record_execution
        record_execution(
            agent_id,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0045,
            result="OAuth flow implemented with PKCE",
            _db_path=db_path,
        )

        # 5. Agent should be back to idle with updated stats
        agent = get_agent(agent_id, _db_path=db_path)
        assert agent.status == "idle"
        assert agent.task_count == 1
        assert agent.total_cost_usd == pytest.approx(0.0045, abs=1e-8)
        assert agent.total_input_tokens == 1000
        assert agent.total_output_tokens == 500

        # 6. Check swarm health shows the cost
        health = swarm_health(swarm_id, _db_path=db_path)
        assert health["healthy"] is True
        assert health["total_cost_usd"] == pytest.approx(0.0045, abs=1e-8)
        assert health["total_agents"] == 1

        # 7. Shutdown
        shutdown = swarm_shutdown(swarm_id, reason="completed", _db_path=db_path)
        assert shutdown["status"] == "completed"
        assert shutdown["agents_terminated"] == 1
        assert shutdown["final_cost_usd"] == pytest.approx(0.0045, abs=1e-8)

        # 8. Verify final state
        agent = get_agent(agent_id, _db_path=db_path)
        assert agent.status == "terminated"


class TestHarnessImportsAllPublicAPI:
    """All __all__ exports from cap.harness are importable."""

    def test_harness_imports_all_public_api(self):
        import cap.harness

        expected = [
            # Executor
            "AgentExecutor", "ExecutionResult",
            # Agent store
            "AgentRecord", "spawn_agent", "get_agent", "list_agents",
            "update_agent", "terminate_agent", "record_execution", "cleanup_stale",
            # Cost meter
            "record_cost",
            # Hooks
            "hooks_route", "hooks_pre_task", "hooks_post_task",
            # Swarm
            "swarm_init", "swarm_status", "swarm_shutdown",
            # Coordination
            "coordination_assign", "coordination_release",
            # Governance
            "HarnessPolicy", "load_policy", "check_dangerous", "enforce_budget",
            "generate_manifest", "write_manifest", "verify_manifest", "record_audit",
            # Validation
            "validate_identifier", "validate_text", "validate_path", "sanitize_for_storage",
        ]

        for name in expected:
            assert hasattr(cap.harness, name), f"cap.harness missing export: {name}"
            assert name in cap.harness.__all__, f"{name} not in __all__"


class TestDoctorShowsHarnessSection:
    """cap doctor output includes the Harness section (section 6)."""

    def test_doctor_shows_harness_section(self):
        from click.testing import CliRunner
        from cap.cli.commands import doctor

        runner = CliRunner()
        result = runner.invoke(doctor)

        # Doctor should run without crashing (exit 0 or status checks)
        # and contain the harness section header
        assert "6. Harness" in result.output
        assert "Executor available" in result.output
        assert "Harness server registered" in result.output

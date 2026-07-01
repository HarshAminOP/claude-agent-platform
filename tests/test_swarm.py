"""Tests for cap.harness.swarm."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.swarm import (
    swarm_init,
    swarm_status,
    swarm_health,
    swarm_shutdown,
    swarm_list,
    _VALID_TOPOLOGIES,
)
from cap.harness.agent_store import spawn_agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Isolated platform.db for each test."""
    return tmp_path / "platform.db"


# ---------------------------------------------------------------------------
# swarm_init
# ---------------------------------------------------------------------------

class TestSwarmInit:
    def test_returns_dict_with_swarm_id(self, db_path):
        result = swarm_init("my-swarm", _db_path=db_path)
        assert "swarm_id" in result
        assert len(result["swarm_id"]) == 36  # UUID

    def test_default_topology_hierarchical(self, db_path):
        result = swarm_init("s1", _db_path=db_path)
        assert result["topology"] == "hierarchical"

    def test_default_status_running(self, db_path):
        result = swarm_init("s1", _db_path=db_path)
        assert result["status"] == "running"

    def test_default_max_agents(self, db_path):
        result = swarm_init("s1", _db_path=db_path)
        assert result["max_agents"] == 8

    def test_custom_topology(self, db_path):
        for topo in _VALID_TOPOLOGIES:
            result = swarm_init(f"swarm-{topo}", topology=topo, _db_path=db_path)
            assert result["topology"] == topo

    def test_custom_max_agents(self, db_path):
        result = swarm_init("s1", max_agents=16, _db_path=db_path)
        assert result["max_agents"] == 16

    def test_config_stored(self, db_path):
        cfg = {"consensus_mechanism": "raft", "auto_scaling": True}
        result = swarm_init("s1", config=cfg, _db_path=db_path)
        # Verify it roundtrips via swarm_status
        status = swarm_status(result["swarm_id"], _db_path=db_path)
        assert status["config"]["consensus_mechanism"] == "raft"

    def test_invalid_topology_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid topology"):
            swarm_init("s1", topology="ring", _db_path=db_path)

    def test_empty_name_raises(self, db_path):
        with pytest.raises(ValueError, match="non-empty"):
            swarm_init("", _db_path=db_path)

    def test_whitespace_name_raises(self, db_path):
        with pytest.raises(ValueError, match="non-empty"):
            swarm_init("   ", _db_path=db_path)

    def test_unique_swarm_ids(self, db_path):
        ids = {swarm_init(f"s{i}", _db_path=db_path)["swarm_id"] for i in range(5)}
        assert len(ids) == 5

    def test_returns_name(self, db_path):
        result = swarm_init("  my swarm  ", _db_path=db_path)
        assert result["name"] == "my swarm"


# ---------------------------------------------------------------------------
# swarm_status
# ---------------------------------------------------------------------------

class TestSwarmStatus:
    def test_returns_swarm_record(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        status = swarm_status(init["swarm_id"], _db_path=db_path)
        assert status["swarm_id"] == init["swarm_id"]
        assert status["topology"] == "hierarchical"
        assert status["status"] == "running"

    def test_agents_empty_initially(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        status = swarm_status(init["swarm_id"], _db_path=db_path)
        assert status["agents"] == []
        assert status["agent_count"] == 0
        assert status["active_count"] == 0

    def test_agents_included_after_spawn(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        spawn_agent("dev", swarm_id=init["swarm_id"], _db_path=db_path)
        spawn_agent("test", swarm_id=init["swarm_id"], _db_path=db_path)
        status = swarm_status(init["swarm_id"], _db_path=db_path)
        assert status["agent_count"] == 2
        assert status["active_count"] == 2  # idle counts as active

    def test_agent_shape(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        spawn_agent("dev", swarm_id=init["swarm_id"], _db_path=db_path)
        status = swarm_status(init["swarm_id"], _db_path=db_path)
        agent = status["agents"][0]
        assert "agent_id" in agent
        assert "agent_type" in agent
        assert "status" in agent

    def test_not_found_raises(self, db_path):
        with pytest.raises(KeyError):
            swarm_status("non-existent-id", _db_path=db_path)

    def test_agents_not_in_other_swarm_excluded(self, db_path):
        s1 = swarm_init("s1", _db_path=db_path)
        s2 = swarm_init("s2", _db_path=db_path)
        spawn_agent("dev", swarm_id=s1["swarm_id"], _db_path=db_path)
        status = swarm_status(s2["swarm_id"], _db_path=db_path)
        assert status["agent_count"] == 0


# ---------------------------------------------------------------------------
# swarm_health
# ---------------------------------------------------------------------------

class TestSwarmHealth:
    def test_healthy_running_no_failures(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        assert health["healthy"] is True

    def test_utilization_zero_no_agents(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        assert health["agent_utilization"] == 0.0

    def test_total_cost_zero_no_agents(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        assert health["total_cost_usd"] == 0.0

    def test_failed_count_zero_no_failures(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        assert health["failed_count"] == 0

    def test_not_found_raises(self, db_path):
        with pytest.raises(KeyError):
            swarm_health("missing-id", _db_path=db_path)

    def test_has_required_keys(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        required = {"healthy", "agent_utilization", "total_cost_usd", "failed_count", "avg_task_duration_ms"}
        assert required.issubset(health.keys())

    def test_avg_duration_none_no_executions(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        health = swarm_health(init["swarm_id"], _db_path=db_path)
        assert health["avg_task_duration_ms"] is None


# ---------------------------------------------------------------------------
# swarm_shutdown
# ---------------------------------------------------------------------------

class TestSwarmShutdown:
    def test_sets_status_completed(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], reason="completed", _db_path=db_path)
        assert result["status"] == "completed"

    def test_non_completed_reason_sets_terminated(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], reason="abort", _db_path=db_path)
        assert result["status"] == "terminated"

    def test_returns_agents_terminated_count(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        spawn_agent("dev", swarm_id=init["swarm_id"], _db_path=db_path)
        spawn_agent("test", swarm_id=init["swarm_id"], _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], _db_path=db_path)
        assert result["agents_terminated"] == 2

    def test_returns_final_cost_usd(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], _db_path=db_path)
        assert "final_cost_usd" in result
        assert result["final_cost_usd"] >= 0.0

    def test_not_found_raises(self, db_path):
        with pytest.raises(KeyError):
            swarm_shutdown("missing-id", _db_path=db_path)

    def test_swarm_status_updated_after_shutdown(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        swarm_shutdown(init["swarm_id"], _db_path=db_path)
        status = swarm_status(init["swarm_id"], _db_path=db_path)
        assert status["status"] == "completed"

    def test_agents_terminated_after_shutdown(self, db_path):
        from cap.harness.agent_store import get_agent
        init = swarm_init("s1", _db_path=db_path)
        agent = spawn_agent("dev", swarm_id=init["swarm_id"], _db_path=db_path)
        swarm_shutdown(init["swarm_id"], _db_path=db_path)
        updated = get_agent(agent.agent_id, _db_path=db_path)
        assert updated.status == "terminated"

    def test_zero_agents_terminated_empty_swarm(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], _db_path=db_path)
        assert result["agents_terminated"] == 0

    def test_returns_swarm_id(self, db_path):
        init = swarm_init("s1", _db_path=db_path)
        result = swarm_shutdown(init["swarm_id"], _db_path=db_path)
        assert result["swarm_id"] == init["swarm_id"]


# ---------------------------------------------------------------------------
# swarm_list
# ---------------------------------------------------------------------------

class TestSwarmList:
    def test_empty_initially(self, db_path):
        result = swarm_list(_db_path=db_path)
        assert result == []

    def test_lists_all_swarms(self, db_path):
        swarm_init("s1", _db_path=db_path)
        swarm_init("s2", _db_path=db_path)
        result = swarm_list(_db_path=db_path)
        assert len(result) == 2

    def test_filter_by_status(self, db_path):
        init1 = swarm_init("s1", _db_path=db_path)
        swarm_init("s2", _db_path=db_path)
        swarm_shutdown(init1["swarm_id"], _db_path=db_path)

        running = swarm_list(status="running", _db_path=db_path)
        assert len(running) == 1
        assert running[0]["status"] == "running"

        completed = swarm_list(status="completed", _db_path=db_path)
        assert len(completed) == 1
        assert completed[0]["status"] == "completed"

    def test_invalid_status_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid status"):
            swarm_list(status="unknown", _db_path=db_path)

    def test_result_shape(self, db_path):
        swarm_init("s1", _db_path=db_path)
        result = swarm_list(_db_path=db_path)
        s = result[0]
        for key in ("swarm_id", "name", "topology", "status", "max_agents", "created_at"):
            assert key in s

    def test_ordered_by_created_at_desc(self, db_path):
        swarm_init("first", _db_path=db_path)
        swarm_init("second", _db_path=db_path)
        result = swarm_list(_db_path=db_path)
        # Most recently created should come first
        assert result[0]["name"] == "second"
        assert result[1]["name"] == "first"

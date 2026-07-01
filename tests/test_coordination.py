"""Tests for cap.harness.coordination."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.coordination import (
    coordination_assign,
    coordination_release,
    coordination_balance,
    coordination_consensus,
)
from cap.harness.swarm import swarm_init
from cap.harness.agent_store import spawn_agent, update_agent, get_agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Isolated platform.db for each test."""
    return tmp_path / "platform.db"


@pytest.fixture
def running_swarm(db_path):
    """A running swarm with max_agents=4."""
    return swarm_init("test-swarm", max_agents=4, _db_path=db_path)


# ---------------------------------------------------------------------------
# coordination_assign
# ---------------------------------------------------------------------------

class TestCoordinationAssign:
    def test_assign_with_preferred_type_spawns_agent(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        result = coordination_assign(
            swarm_id=swarm_id,
            task="write unit tests",
            preferred_agent_type="dev",
            _db_path=db_path,
        )
        assert result["assigned"] is True
        assert result["agent_type"] == "dev"
        assert "agent_id" in result
        assert "model" in result

    def test_assign_marks_agent_busy(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        result = coordination_assign(
            swarm_id=swarm_id,
            task="deploy service",
            preferred_agent_type="devops",
            _db_path=db_path,
        )
        agent = get_agent(result["agent_id"], _db_path=db_path)
        assert agent.status == "busy"

    def test_assign_picks_idle_before_spawn(self, db_path, running_swarm):
        """Pre-existing idle agent of the right type should be reused."""
        swarm_id = running_swarm["swarm_id"]
        # Pre-spawn a dev agent into the swarm.
        pre = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        result = coordination_assign(
            swarm_id=swarm_id,
            task="fix bug",
            preferred_agent_type="dev",
            _db_path=db_path,
        )
        assert result["assigned"] is True
        assert result["agent_id"] == pre.agent_id
        assert result["spawned"] is False

    def test_assign_spawns_when_all_busy(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        # Spawn a dev agent and mark it busy manually.
        existing = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(existing.agent_id, status="busy", _db_path=db_path)

        result = coordination_assign(
            swarm_id=swarm_id,
            task="add feature",
            preferred_agent_type="dev",
            _db_path=db_path,
        )
        assert result["assigned"] is True
        assert result["agent_id"] != existing.agent_id
        assert result["spawned"] is True

    def test_assign_queued_when_swarm_full(self, db_path):
        """max_agents=1 swarm with one busy agent should queue."""
        swarm = swarm_init("tiny", max_agents=1, _db_path=db_path)
        swarm_id = swarm["swarm_id"]
        agent = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(agent.agent_id, status="busy", _db_path=db_path)

        result = coordination_assign(
            swarm_id=swarm_id,
            task="anything",
            preferred_agent_type="dev",
            _db_path=db_path,
        )
        assert result["queued"] is True
        assert result["reason"] == "swarm full"

    def test_assign_unknown_swarm_raises(self, db_path):
        with pytest.raises(KeyError):
            coordination_assign(
                swarm_id="00000000-0000-0000-0000-000000000000",
                task="something",
                _db_path=db_path,
            )

    def test_assign_empty_task_raises(self, db_path, running_swarm):
        with pytest.raises(ValueError):
            coordination_assign(
                swarm_id=running_swarm["swarm_id"],
                task="",
                _db_path=db_path,
            )

    def test_assign_without_preferred_type_uses_routing(self, db_path, running_swarm):
        """When no preferred type given, semantic routing picks a valid type."""
        result = coordination_assign(
            swarm_id=running_swarm["swarm_id"],
            task="write unit tests for the authentication module",
            _db_path=db_path,
        )
        assert result["assigned"] is True
        assert result["agent_type"] in {
            "dev", "devops", "security", "sre", "code-review",
            "test", "docs", "optimization", "aws-architect",
        }


# ---------------------------------------------------------------------------
# coordination_release
# ---------------------------------------------------------------------------

class TestCoordinationRelease:
    def test_release_sets_idle(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        agent = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(agent.agent_id, status="busy", _db_path=db_path)

        result = coordination_release(agent.agent_id, _db_path=db_path)
        assert result["status"] == "idle"
        assert result["agent_id"] == agent.agent_id

        # Verify DB was updated.
        refreshed = get_agent(agent.agent_id, _db_path=db_path)
        assert refreshed.status == "idle"

    def test_release_unknown_agent_raises(self, db_path):
        with pytest.raises(KeyError):
            coordination_release(
                "00000000-0000-0000-0000-000000000000",
                _db_path=db_path,
            )

    def test_release_already_idle_is_idempotent(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        agent = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        # agent starts idle — releasing again should still return idle.
        result = coordination_release(agent.agent_id, _db_path=db_path)
        assert result["status"] == "idle"


# ---------------------------------------------------------------------------
# coordination_balance
# ---------------------------------------------------------------------------

class TestCoordinationBalance:
    def test_empty_swarm_is_not_balanced(self, db_path, running_swarm):
        result = coordination_balance(running_swarm["swarm_id"], _db_path=db_path)
        assert result["swarm_id"] == running_swarm["swarm_id"]
        assert result["bottlenecks"] == []
        assert result["over_provisioned"] == []
        # No agents — not "balanced" but no bottlenecks either.
        assert isinstance(result["recommendation"], str)

    def test_all_idle_is_over_provisioned(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)

        result = coordination_balance(swarm_id, _db_path=db_path)
        assert "dev" in result["over_provisioned"]
        assert result["balanced"] is False

    def test_all_busy_is_bottleneck(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        agent = spawn_agent("test", swarm_id=swarm_id, _db_path=db_path)
        update_agent(agent.agent_id, status="busy", _db_path=db_path)

        result = coordination_balance(swarm_id, _db_path=db_path)
        assert "test" in result["bottlenecks"]
        assert result["balanced"] is False

    def test_mixed_is_balanced(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        a1 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        a2 = spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)
        update_agent(a1.agent_id, status="busy", _db_path=db_path)
        # a2 remains idle — mix of busy and idle for "dev"

        result = coordination_balance(swarm_id, _db_path=db_path)
        assert "dev" not in result["bottlenecks"]
        assert "dev" not in result["over_provisioned"]
        assert result["balanced"] is True

    def test_unknown_swarm_raises(self, db_path):
        with pytest.raises(KeyError):
            coordination_balance("00000000-0000-0000-0000-000000000000", _db_path=db_path)

    def test_by_type_structure(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        spawn_agent("dev", swarm_id=swarm_id, _db_path=db_path)

        result = coordination_balance(swarm_id, _db_path=db_path)
        assert "dev" in result["by_type"]
        entry = result["by_type"]["dev"]
        assert "idle" in entry
        assert "busy" in entry
        assert "total" in entry


# ---------------------------------------------------------------------------
# coordination_consensus
# ---------------------------------------------------------------------------

class TestCoordinationConsensus:
    def test_phase1_creates_pending_proposal(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        result = coordination_consensus(swarm_id, "Upgrade to Python 3.13", _db_path=db_path)
        assert result["status"] == "pending"
        assert "proposal_id" in result
        assert len(result["proposal_id"]) == 36  # UUID

    def test_phase2_majority_approve(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        votes = {"agent-1": "approve", "agent-2": "approve", "agent-3": "reject"}
        result = coordination_consensus(swarm_id, "Add retry logic", votes=votes, _db_path=db_path)
        assert result["outcome"] == "approved"
        assert result["votes_for"] == 2
        assert result["votes_against"] == 1
        assert result["total"] == 3

    def test_phase2_majority_reject(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        votes = {"a": "reject", "b": "reject", "c": "approve"}
        result = coordination_consensus(swarm_id, "Remove caching", votes=votes, _db_path=db_path)
        assert result["outcome"] == "rejected"

    def test_phase2_tie_is_rejected(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        votes = {"a": "approve", "b": "reject"}
        result = coordination_consensus(swarm_id, "Tie proposal", votes=votes, _db_path=db_path)
        assert result["outcome"] == "rejected"

    def test_phase2_unanimous_approve(self, db_path, running_swarm):
        swarm_id = running_swarm["swarm_id"]
        votes = {"x": "approve", "y": "approve"}
        result = coordination_consensus(swarm_id, "All agree", votes=votes, _db_path=db_path)
        assert result["outcome"] == "approved"

    def test_invalid_vote_value_raises(self, db_path, running_swarm):
        with pytest.raises(ValueError, match="Invalid vote"):
            coordination_consensus(
                running_swarm["swarm_id"],
                "Bad vote",
                votes={"agent-1": "maybe"},
                _db_path=db_path,
            )

    def test_unknown_swarm_raises(self, db_path):
        with pytest.raises(KeyError):
            coordination_consensus(
                "00000000-0000-0000-0000-000000000000",
                "proposal text",
                _db_path=db_path,
            )

    def test_empty_proposal_raises(self, db_path, running_swarm):
        with pytest.raises(ValueError):
            coordination_consensus(running_swarm["swarm_id"], "", _db_path=db_path)

    def test_result_contains_proposal_id(self, db_path, running_swarm):
        votes = {"a": "approve", "b": "approve", "c": "approve"}
        result = coordination_consensus(
            running_swarm["swarm_id"], "Major refactor", votes=votes, _db_path=db_path
        )
        assert "proposal_id" in result
        assert result["proposal_id"]

    def test_single_approve_vote(self, db_path, running_swarm):
        votes = {"solo": "approve"}
        result = coordination_consensus(
            running_swarm["swarm_id"], "Solo decision", votes=votes, _db_path=db_path
        )
        assert result["outcome"] == "approved"
        assert result["total"] == 1

    def test_single_reject_vote(self, db_path, running_swarm):
        votes = {"solo": "reject"}
        result = coordination_consensus(
            running_swarm["swarm_id"], "Solo rejection", votes=votes, _db_path=db_path
        )
        assert result["outcome"] == "rejected"

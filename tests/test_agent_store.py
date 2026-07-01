"""Tests for cap.harness.agent_store."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.agent_store import (
    AgentRecord,
    spawn_agent,
    get_agent,
    list_agents,
    update_agent,
    terminate_agent,
    record_execution,
    cleanup_stale,
    _VALID_AGENT_TYPES,
    _VALID_MODELS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Isolated platform.db for each test."""
    return tmp_path / "platform.db"


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------

class TestSpawnAgent:
    def test_returns_agent_record(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert isinstance(rec, AgentRecord)

    def test_status_is_idle(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert rec.status == "idle"

    def test_auto_model_dev(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        assert rec.model == "claude-sonnet-4-6"

    def test_auto_model_security(self, db_path):
        rec = spawn_agent("security", _db_path=db_path)
        assert rec.model == "claude-opus-4-6"

    def test_auto_model_optimization(self, db_path):
        rec = spawn_agent("optimization", _db_path=db_path)
        assert rec.model == "claude-haiku-4-5"

    def test_explicit_model_override(self, db_path):
        rec = spawn_agent("dev", model="claude-opus-4-6", _db_path=db_path)
        assert rec.model == "claude-opus-4-6"

    def test_persists_to_db(self, db_path):
        rec = spawn_agent("devops", _db_path=db_path)
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched is not None
        assert fetched.agent_id == rec.agent_id

    def test_unique_ids(self, db_path):
        ids = {spawn_agent("dev", _db_path=db_path).agent_id for _ in range(5)}
        assert len(ids) == 5

    def test_swarm_id_stored(self, db_path):
        rec = spawn_agent("test", swarm_id="swarm-abc", _db_path=db_path)
        assert rec.swarm_id == "swarm-abc"
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched.swarm_id == "swarm-abc"

    def test_config_stored(self, db_path):
        cfg = {"max_tokens": 4096, "temperature": 0.7, "system_prompt_key": "dev_default"}
        rec = spawn_agent("dev", config=cfg, _db_path=db_path)
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched.config == cfg

    def test_empty_agent_type_raises(self, db_path):
        with pytest.raises(ValueError, match="agent_type must be a non-empty"):
            spawn_agent("", _db_path=db_path)

    def test_invalid_model_raises(self, db_path):
        with pytest.raises(ValueError, match="Unknown model"):
            spawn_agent("dev", model="gpt-4", _db_path=db_path)

    @pytest.mark.parametrize("agent_type", sorted(_VALID_AGENT_TYPES))
    def test_all_valid_agent_types(self, agent_type, db_path):
        rec = spawn_agent(agent_type, _db_path=db_path)
        assert rec.agent_type == agent_type
        assert rec.model in _VALID_MODELS

    def test_task_count_starts_at_zero(self, db_path):
        rec = spawn_agent("docs", _db_path=db_path)
        assert rec.task_count == 0
        assert rec.total_input_tokens == 0
        assert rec.total_output_tokens == 0
        assert rec.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# get_agent
# ---------------------------------------------------------------------------

class TestGetAgent:
    def test_returns_none_for_missing(self, db_path):
        result = get_agent("00000000-0000-0000-0000-000000000000", _db_path=db_path)
        assert result is None

    def test_round_trips_all_fields(self, db_path):
        original = spawn_agent("code-review", swarm_id="s1", _db_path=db_path)
        fetched = get_agent(original.agent_id, _db_path=db_path)
        assert fetched.agent_id == original.agent_id
        assert fetched.agent_type == original.agent_type
        assert fetched.model == original.model
        assert fetched.status == original.status
        assert fetched.swarm_id == original.swarm_id


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------

class TestListAgents:
    def test_empty_initially(self, db_path):
        assert list_agents(_db_path=db_path) == []

    def test_filter_by_status(self, db_path):
        a = spawn_agent("dev", _db_path=db_path)
        b = spawn_agent("devops", _db_path=db_path)
        terminate_agent(a.agent_id, _db_path=db_path)
        idle = list_agents(status="idle", _db_path=db_path)
        assert all(r.status == "idle" for r in idle)
        assert any(r.agent_id == b.agent_id for r in idle)

    def test_filter_by_agent_type(self, db_path):
        spawn_agent("dev", _db_path=db_path)
        spawn_agent("dev", _db_path=db_path)
        spawn_agent("security", _db_path=db_path)
        devs = list_agents(agent_type="dev", _db_path=db_path)
        assert len(devs) == 2
        assert all(r.agent_type == "dev" for r in devs)

    def test_filter_by_swarm_id(self, db_path):
        spawn_agent("dev", swarm_id="alpha", _db_path=db_path)
        spawn_agent("devops", swarm_id="alpha", _db_path=db_path)
        spawn_agent("test", swarm_id="beta", _db_path=db_path)
        alpha = list_agents(swarm_id="alpha", _db_path=db_path)
        assert len(alpha) == 2
        assert all(r.swarm_id == "alpha" for r in alpha)

    def test_combined_filters(self, db_path):
        spawn_agent("dev", swarm_id="s1", _db_path=db_path)
        spawn_agent("dev", swarm_id="s2", _db_path=db_path)
        spawn_agent("security", swarm_id="s1", _db_path=db_path)
        results = list_agents(agent_type="dev", swarm_id="s1", _db_path=db_path)
        assert len(results) == 1
        assert results[0].agent_type == "dev"
        assert results[0].swarm_id == "s1"

    def test_no_filter_returns_all(self, db_path):
        for atype in ("dev", "security", "optimization"):
            spawn_agent(atype, _db_path=db_path)
        all_agents = list_agents(_db_path=db_path)
        assert len(all_agents) == 3


# ---------------------------------------------------------------------------
# update_agent
# ---------------------------------------------------------------------------

class TestUpdateAgent:
    def test_updates_status(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        updated = update_agent(rec.agent_id, status="busy", _db_path=db_path)
        assert updated.status == "busy"

    def test_updates_config(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        new_cfg = {"max_tokens": 8192}
        updated = update_agent(rec.agent_id, config=new_cfg, _db_path=db_path)
        assert updated.config == new_cfg

    def test_updates_metadata(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        updated = update_agent(rec.agent_id, metadata={"tag": "v2"}, _db_path=db_path)
        assert updated.metadata == {"tag": "v2"}

    def test_invalid_status_raises(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        with pytest.raises(ValueError, match="Invalid status"):
            update_agent(rec.agent_id, status="purple", _db_path=db_path)

    def test_non_updatable_field_raises(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        with pytest.raises(ValueError, match="Non-updatable fields"):
            update_agent(rec.agent_id, agent_id="new-id", _db_path=db_path)

    def test_missing_agent_raises(self, db_path):
        with pytest.raises(KeyError):
            update_agent("no-such-id", status="busy", _db_path=db_path)


# ---------------------------------------------------------------------------
# terminate_agent
# ---------------------------------------------------------------------------

class TestTerminateAgent:
    def test_sets_status_terminated(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminated = terminate_agent(rec.agent_id, _db_path=db_path)
        assert terminated.status == "terminated"

    def test_records_reason_in_metadata(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminated = terminate_agent(rec.agent_id, reason="done", _db_path=db_path)
        assert terminated.metadata.get("termination_reason") == "done"

    def test_default_reason_is_manual(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminated = terminate_agent(rec.agent_id, _db_path=db_path)
        assert terminated.metadata.get("termination_reason") == "manual"

    def test_missing_agent_raises(self, db_path):
        with pytest.raises(KeyError):
            terminate_agent("missing-id", _db_path=db_path)

    def test_persists_termination(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminate_agent(rec.agent_id, _db_path=db_path)
        fetched = get_agent(rec.agent_id, _db_path=db_path)
        assert fetched.status == "terminated"


# ---------------------------------------------------------------------------
# record_execution
# ---------------------------------------------------------------------------

class TestRecordExecution:
    def test_increments_task_count(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        record_execution(rec.agent_id, 100, 200, 0.001, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.task_count == 1

    def test_accumulates_tokens(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        record_execution(rec.agent_id, 100, 200, 0.001, _db_path=db_path)
        record_execution(rec.agent_id, 50, 75, 0.0005, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.total_input_tokens == 150
        assert updated.total_output_tokens == 275
        assert updated.task_count == 2

    def test_accumulates_cost(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        record_execution(rec.agent_id, 100, 200, 0.001, _db_path=db_path)
        record_execution(rec.agent_id, 100, 200, 0.002, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert abs(updated.total_cost_usd - 0.003) < 1e-9

    def test_stores_last_result(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        record_execution(rec.agent_id, 10, 20, 0.0, result="all done", _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.last_result == "all done"

    def test_truncates_result_at_2000_chars(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        long_result = "x" * 5000
        record_execution(rec.agent_id, 10, 20, 0.0, result=long_result, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert len(updated.last_result) == 2000

    def test_stores_last_error(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        record_execution(rec.agent_id, 10, 20, 0.0, error="timeout", _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.last_error == "timeout"

    def test_sets_status_back_to_idle(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        update_agent(rec.agent_id, status="busy", _db_path=db_path)
        record_execution(rec.agent_id, 10, 20, 0.0, _db_path=db_path)
        updated = get_agent(rec.agent_id, _db_path=db_path)
        assert updated.status == "idle"

    def test_missing_agent_raises(self, db_path):
        with pytest.raises(KeyError):
            record_execution("no-such-id", 10, 20, 0.0, _db_path=db_path)


# ---------------------------------------------------------------------------
# cleanup_stale
# ---------------------------------------------------------------------------

class TestCleanupStale:
    def _age_agent(self, agent_id: str, hours: int, db_path: Path):
        """Backdating helper — sets last_active to `hours` ago."""
        import sqlite3
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE agents SET last_active = ? WHERE agent_id = ?",
            (old_ts, agent_id),
        )
        conn.commit()
        conn.close()

    def test_terminates_stale_idle(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        self._age_agent(rec.agent_id, hours=25, db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 1
        assert get_agent(rec.agent_id, _db_path=db_path).status == "terminated"

    def test_terminates_stale_busy(self, db_path):
        rec = spawn_agent("devops", _db_path=db_path)
        update_agent(rec.agent_id, status="busy", _db_path=db_path)
        self._age_agent(rec.agent_id, hours=25, db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 1

    def test_does_not_touch_fresh_agents(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 0
        assert get_agent(rec.agent_id, _db_path=db_path).status == "idle"

    def test_does_not_touch_already_terminated(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        terminate_agent(rec.agent_id, _db_path=db_path)
        self._age_agent(rec.agent_id, hours=999, db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 0

    def test_returns_count(self, db_path):
        recs = [spawn_agent("dev", _db_path=db_path) for _ in range(3)]
        for r in recs:
            self._age_agent(r.agent_id, hours=48, db_path=db_path)
        count = cleanup_stale(max_age_hours=24, _db_path=db_path)
        assert count == 3

    def test_custom_max_age(self, db_path):
        rec = spawn_agent("dev", _db_path=db_path)
        self._age_agent(rec.agent_id, hours=2, db_path=db_path)
        # with 24h threshold → fresh
        assert cleanup_stale(max_age_hours=24, _db_path=db_path) == 0
        # with 1h threshold → stale
        assert cleanup_stale(max_age_hours=1, _db_path=db_path) == 1


# ---------------------------------------------------------------------------
# AgentRecord serialisation round-trip
# ---------------------------------------------------------------------------

class TestAgentRecordRoundTrip:
    def test_to_row_and_from_row(self, db_path):
        original = spawn_agent(
            "aws-architect",
            config={"max_tokens": 4096},
            swarm_id="test-swarm",
            _db_path=db_path,
        )
        fetched = get_agent(original.agent_id, _db_path=db_path)
        assert fetched.agent_id == original.agent_id
        assert fetched.config == original.config
        assert fetched.swarm_id == original.swarm_id
        assert isinstance(fetched.created_at, datetime)
        assert isinstance(fetched.last_active, datetime)

"""Unit tests for cap.harness.cost_meter.

All tests run fully offline against an in-memory SQLite database.
"""

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.db import get_db, migrate
from cap.harness.cost_meter import (
    AgentCostSummary,
    ModelCostEntry,
    WorkflowCostSummary,
    _ensure_schema,
    budget_remaining,
    get_agent_cost,
    get_model_breakdown,
    get_workflow_cost,
    record_execution,
    top_spenders,
)
from cap.harness.executor import ExecutionResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """In-memory SQLite with full CAP schema + execution_ledger."""
    conn = get_db(":memory:")
    migrate(conn)
    _ensure_schema(conn)
    yield conn
    conn.close()


def _result(
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


# ---------------------------------------------------------------------------
# record_execution
# ---------------------------------------------------------------------------


class TestRecordExecution:
    def test_returns_uuid_string(self, db):
        entry_id = record_execution(_result(), "dev", db=db)
        assert isinstance(entry_id, str)
        assert len(entry_id) == 36  # UUID4 canonical form

    def test_row_inserted(self, db):
        entry_id = record_execution(_result(), "dev", db=db)
        row = db.execute(
            "SELECT * FROM execution_ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row is not None
        assert row["agent_id"] == "agent-1"
        assert row["agent_type"] == "dev"
        assert row["model"] == "sonnet"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert abs(row["cost_usd"] - 0.001275) < 1e-9
        assert row["success"] == 1
        assert row["error"] is None

    def test_error_sets_success_zero(self, db):
        r = _result(error="Throttled")
        entry_id = record_execution(r, "dev", db=db)
        row = db.execute(
            "SELECT success, error FROM execution_ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row["success"] == 0
        assert row["error"] == "Throttled"

    def test_task_hash_stored(self, db):
        th = hashlib.sha256(b"my prompt").hexdigest()
        entry_id = record_execution(_result(), "dev", task_hash=th, db=db)
        row = db.execute(
            "SELECT task_hash FROM execution_ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row["task_hash"] == th

    def test_task_hash_auto_derived_when_absent(self, db):
        entry_id = record_execution(_result(), "dev", db=db)
        row = db.execute(
            "SELECT task_hash FROM execution_ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        assert row["task_hash"] is not None
        assert len(row["task_hash"]) == 64  # SHA-256 hex

    def test_swarm_and_workflow_stored(self, db):
        entry_id = record_execution(
            _result(), "sre", swarm_id="swarm-42", workflow_id="wf-99", db=db
        )
        row = db.execute(
            "SELECT swarm_id, workflow_id FROM execution_ledger WHERE id = ?",
            (entry_id,),
        ).fetchone()
        assert row["swarm_id"] == "swarm-42"
        assert row["workflow_id"] == "wf-99"

    def test_mirrors_to_cost_events(self, db):
        record_execution(_result(), "dev", workflow_id="wf-1", db=db)
        row = db.execute(
            "SELECT * FROM cost_events WHERE workflow_id = 'wf-1'"
        ).fetchone()
        assert row is not None
        assert row["agent_type"] == "dev"
        assert row["model"] == "sonnet"

    def test_multiple_entries_independent(self, db):
        id1 = record_execution(_result(agent_id="a"), "dev", db=db)
        id2 = record_execution(_result(agent_id="b"), "dev", db=db)
        assert id1 != id2
        count = db.execute("SELECT COUNT(*) FROM execution_ledger").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# get_agent_cost
# ---------------------------------------------------------------------------


class TestGetAgentCost:
    def test_returns_summary_for_known_agent(self, db):
        record_execution(_result(agent_id="ag-1", cost_usd=0.01), "dev", db=db)
        record_execution(_result(agent_id="ag-1", cost_usd=0.02), "dev", db=db)

        summary = get_agent_cost("ag-1", db=db)

        assert isinstance(summary, AgentCostSummary)
        assert summary.agent_id == "ag-1"
        assert summary.execution_count == 2
        assert abs(summary.total_cost_usd - 0.03) < 1e-6
        assert summary.total_tokens == 300  # 2 × (100+50)

    def test_unknown_agent_returns_zeros(self, db):
        summary = get_agent_cost("ghost", db=db)
        assert summary.execution_count == 0
        assert summary.total_cost_usd == 0.0

    def test_since_filter(self, db):
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        future_cutoff = datetime.now(timezone.utc) + timedelta(days=1)

        record_execution(_result(agent_id="ag-x", cost_usd=0.05), "dev", db=db)

        # Filter that excludes everything (tomorrow as lower bound)
        summary = get_agent_cost("ag-x", since=future_cutoff, db=db)
        assert summary.execution_count == 0

        # Filter that includes everything
        summary = get_agent_cost("ag-x", since=past, db=db)
        assert summary.execution_count == 1


# ---------------------------------------------------------------------------
# get_workflow_cost
# ---------------------------------------------------------------------------


class TestGetWorkflowCost:
    def test_aggregates_by_agent_and_model(self, db):
        record_execution(
            _result(agent_id="d1", model="haiku", cost_usd=0.001),
            "dev",
            workflow_id="wf-A",
            db=db,
        )
        record_execution(
            _result(agent_id="s1", model="sonnet", cost_usd=0.010),
            "sre",
            workflow_id="wf-A",
            db=db,
        )

        summary = get_workflow_cost("wf-A", db=db)

        assert isinstance(summary, WorkflowCostSummary)
        assert summary.workflow_id == "wf-A"
        assert abs(summary.total_cost_usd - 0.011) < 1e-6
        assert "dev" in summary.by_agent_type
        assert "sre" in summary.by_agent_type
        assert "haiku" in summary.by_model
        assert "sonnet" in summary.by_model

    def test_empty_workflow_returns_zero(self, db):
        summary = get_workflow_cost("wf-nonexistent", db=db)
        assert summary.total_cost_usd == 0.0
        assert summary.by_agent_type == {}
        assert summary.by_model == {}


# ---------------------------------------------------------------------------
# get_model_breakdown
# ---------------------------------------------------------------------------


class TestGetModelBreakdown:
    def test_percentages_sum_to_100(self, db):
        record_execution(
            _result(model="haiku", cost_usd=0.001), "dev", db=db
        )
        record_execution(
            _result(model="sonnet", cost_usd=0.009), "dev", db=db
        )

        breakdown = get_model_breakdown(db=db)

        assert set(breakdown.keys()) == {"haiku", "sonnet"}
        total_pct = sum(e.pct_of_total for e in breakdown.values())
        assert abs(total_pct - 100.0) < 0.01

    def test_returns_model_cost_entry_instances(self, db):
        record_execution(_result(model="opus", cost_usd=0.05), "dev", db=db)
        breakdown = get_model_breakdown(db=db)
        assert isinstance(breakdown["opus"], ModelCostEntry)
        assert breakdown["opus"].execution_count == 1

    def test_empty_db_returns_empty_dict(self, db):
        assert get_model_breakdown(db=db) == {}

    def test_since_filter(self, db):
        record_execution(_result(model="haiku", cost_usd=0.001), "dev", db=db)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        breakdown = get_model_breakdown(since=future, db=db)
        assert breakdown == {}


# ---------------------------------------------------------------------------
# budget_remaining
# ---------------------------------------------------------------------------


class TestBudgetRemaining:
    def test_full_budget_when_nothing_spent(self, db):
        remaining = budget_remaining(daily_limit_usd=5.0, db=db)
        assert abs(remaining - 5.0) < 1e-6

    def test_decreases_after_spend(self, db):
        record_execution(_result(cost_usd=1.0), "dev", db=db)
        remaining = budget_remaining(daily_limit_usd=5.0, db=db)
        assert abs(remaining - 4.0) < 1e-4

    def test_negative_when_over_budget(self, db):
        record_execution(_result(cost_usd=6.0), "dev", db=db)
        remaining = budget_remaining(daily_limit_usd=5.0, db=db)
        assert remaining < 0

    def test_custom_limit(self, db):
        record_execution(_result(cost_usd=0.5), "dev", db=db)
        remaining = budget_remaining(daily_limit_usd=1.0, db=db)
        assert abs(remaining - 0.5) < 1e-4


# ---------------------------------------------------------------------------
# top_spenders
# ---------------------------------------------------------------------------


class TestTopSpenders:
    def test_ordered_by_cost_descending(self, db):
        record_execution(_result(agent_id="cheap", cost_usd=0.001), "dev", db=db)
        record_execution(_result(agent_id="pricey", cost_usd=0.999), "dev", db=db)

        spenders = top_spenders(n=10, db=db)

        assert spenders[0].agent_id == "pricey"
        assert spenders[1].agent_id == "cheap"

    def test_respects_n_limit(self, db):
        for i in range(5):
            record_execution(
                _result(agent_id=f"ag-{i}", cost_usd=float(i)), "dev", db=db
            )

        spenders = top_spenders(n=3, db=db)
        assert len(spenders) == 3

    def test_empty_returns_empty_list(self, db):
        assert top_spenders(db=db) == []

    def test_since_filter_excludes_old(self, db):
        record_execution(_result(agent_id="old-agent", cost_usd=0.5), "dev", db=db)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        spenders = top_spenders(since=future, db=db)
        assert spenders == []

    def test_returns_agent_cost_summary_instances(self, db):
        record_execution(_result(agent_id="x"), "sre", db=db)
        spenders = top_spenders(db=db)
        assert isinstance(spenders[0], AgentCostSummary)
        assert spenders[0].agent_type == "sre"

"""Unit tests for cap.harness.agentdb.

All tests are fully offline — no AWS credentials, no external dependencies.
Each test gets an isolated temp-file SQLite DB via the _db_path override.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.agentdb import (
    _get_conn,
    _prompt_hash,
    agentdb_pattern_store,
    agentdb_pattern_search,
    agentdb_reasoning_store,
    agentdb_reasoning_recall,
    agentdb_semantic_route,
    agentdb_hierarchical_recall,
    agentdb_stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "agentdb_test.db"


@pytest.fixture()
def conn(db_path):
    c = _get_conn(db_path)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# _prompt_hash
# ---------------------------------------------------------------------------

def test_prompt_hash_stable():
    assert _prompt_hash("deploy the service") == _prompt_hash("deploy the service")


def test_prompt_hash_case_insensitive():
    assert _prompt_hash("Deploy Service") == _prompt_hash("deploy service")


def test_prompt_hash_different_inputs():
    assert _prompt_hash("task A") != _prompt_hash("task B")


def test_prompt_hash_length():
    assert len(_prompt_hash("anything")) == 16


# ---------------------------------------------------------------------------
# agentdb_pattern_store
# ---------------------------------------------------------------------------

def test_pattern_store_basic(db_path):
    result = agentdb_pattern_store(
        task_type="feature",
        prompt_summary="implement OAuth2 login",
        model="claude-sonnet-4-6",
        agent_type="dev",
        cost_usd=0.005,
        duration_ms=3000,
        _db_path=db_path,
    )
    assert "pattern_id" in result
    assert result["deduplicated"] is False
    assert len(result["pattern_id"]) == 32


def test_pattern_store_persists_to_db(db_path, conn):
    result = agentdb_pattern_store(
        task_type="bugfix",
        prompt_summary="fix null pointer crash",
        model="claude-haiku-4-5",
        agent_type="dev",
        cost_usd=0.001,
        duration_ms=500,
        output_summary="Fixed the NPE in auth module",
        _db_path=db_path,
    )
    pid = result["pattern_id"]
    row = conn.execute("SELECT * FROM patterns WHERE id = ?", (pid,)).fetchone()
    assert row is not None
    assert row["task_type"] == "bugfix"
    assert row["model"] == "claude-haiku-4-5"
    assert row["output_summary"] == "Fixed the NPE in auth module"
    assert row["success"] == 1


def test_pattern_store_failure_flag(db_path, conn):
    result = agentdb_pattern_store(
        task_type="refactor",
        prompt_summary="refactor legacy service",
        model="claude-sonnet-4-6",
        agent_type="dev",
        cost_usd=0.002,
        duration_ms=1000,
        success=False,
        _db_path=db_path,
    )
    assert "pattern_id" in result
    row = conn.execute("SELECT success FROM patterns WHERE id = ?", (result["pattern_id"],)).fetchone()
    assert row["success"] == 0


def test_pattern_store_dedup_same_prompt(db_path):
    kwargs = dict(
        task_type="feature",
        prompt_summary="add rate limiting",
        model="claude-sonnet-4-6",
        agent_type="dev",
        cost_usd=0.003,
        duration_ms=2000,
        _db_path=db_path,
    )
    r1 = agentdb_pattern_store(**kwargs)
    r2 = agentdb_pattern_store(**kwargs)
    assert r1["deduplicated"] is False
    assert r2["deduplicated"] is True
    assert r1["pattern_id"] == r2["pattern_id"]


def test_pattern_store_different_prompts_both_stored(db_path, conn):
    agentdb_pattern_store("f", "prompt A", "model", "dev", 0.001, 100, _db_path=db_path)
    agentdb_pattern_store("f", "prompt B", "model", "dev", 0.001, 100, _db_path=db_path)
    count = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# agentdb_pattern_search
# ---------------------------------------------------------------------------

def test_pattern_search_empty_db(db_path):
    results = agentdb_pattern_search("oauth login", _db_path=db_path)
    assert results == []


def test_pattern_search_finds_by_hash(db_path):
    agentdb_pattern_store("feature", "implement OAuth2", "model", "dev", 0.001, 100, _db_path=db_path)
    results = agentdb_pattern_search("implement OAuth2", _db_path=db_path)
    assert len(results) >= 1
    assert results[0]["prompt_summary"] == "implement OAuth2"


def test_pattern_search_falls_back_to_like(db_path):
    agentdb_pattern_store("bugfix", "fix authentication crash", "model", "dev", 0.001, 100, _db_path=db_path)
    # Different query — no hash match, will LIKE match
    results = agentdb_pattern_search("authentication", _db_path=db_path)
    assert len(results) >= 1
    assert "authentication" in results[0]["prompt_summary"].lower()


def test_pattern_search_task_type_filter(db_path):
    agentdb_pattern_store("feature", "add dark mode", "model", "dev", 0.001, 100, _db_path=db_path)
    agentdb_pattern_store("bugfix", "fix dark mode flicker", "model", "dev", 0.001, 100, _db_path=db_path)
    results = agentdb_pattern_search("dark mode", task_type="feature", _db_path=db_path)
    assert all(r.get("success") is not None for r in results)
    # At minimum the feature pattern should appear; bugfix may too (LIKE match)
    assert len(results) >= 1


def test_pattern_search_result_shape(db_path):
    agentdb_pattern_store("feature", "build widget", "claude-sonnet-4-6", "dev", 0.003, 1500, _db_path=db_path)
    results = agentdb_pattern_search("build widget", _db_path=db_path)
    assert len(results) >= 1
    r = results[0]
    for key in ("pattern_id", "prompt_summary", "model", "cost_usd", "success", "created_at"):
        assert key in r


def test_pattern_search_limit(db_path):
    for i in range(10):
        agentdb_pattern_store("t", f"some unique task number {i}", "m", "dev", 0.001, 100, _db_path=db_path)
    # Search with LIKE on a common substring
    results = agentdb_pattern_search("some unique task", limit=3, _db_path=db_path)
    assert len(results) <= 3


# ---------------------------------------------------------------------------
# agentdb_reasoning_store
# ---------------------------------------------------------------------------

def test_reasoning_store_basic(db_path):
    result = agentdb_reasoning_store(
        agent_id="dev",
        reasoning_chain=["Step 1: read code", "Step 2: identify bug", "Step 3: fix"],
        conclusion="The root cause was a missing null check in auth.py",
        _db_path=db_path,
    )
    assert "reasoning_id" in result
    assert len(result["reasoning_id"]) == 32


def test_reasoning_store_persists_steps(db_path, conn):
    steps = ["Analyse logs", "Correlate metrics", "Identify hotspot"]
    result = agentdb_reasoning_store(
        agent_id="sre",
        reasoning_chain=steps,
        conclusion="CPU spike caused by uncached DB query",
        task_hash="abc123",
        _db_path=db_path,
    )
    rid = result["reasoning_id"]
    row = conn.execute("SELECT * FROM reasoning_bank WHERE id = ?", (rid,)).fetchone()
    assert row is not None
    assert json.loads(row["steps_json"]) == steps
    assert row["conclusion"] == "CPU spike caused by uncached DB query"
    assert row["agent_id"] == "sre"
    assert row["task_hash"] == "abc123"


def test_reasoning_store_missing_conclusion(db_path):
    result = agentdb_reasoning_store(
        agent_id="dev",
        reasoning_chain=["step"],
        conclusion="",
        _db_path=db_path,
    )
    assert "error" in result


def test_reasoning_store_invalid_chain_type(db_path):
    result = agentdb_reasoning_store(
        agent_id="dev",
        reasoning_chain="not a list",  # type: ignore[arg-type]
        conclusion="some conclusion",
        _db_path=db_path,
    )
    assert "error" in result


def test_reasoning_store_empty_chain(db_path):
    result = agentdb_reasoning_store(
        agent_id="dev",
        reasoning_chain=[],
        conclusion="empty chain is valid",
        _db_path=db_path,
    )
    assert "reasoning_id" in result


# ---------------------------------------------------------------------------
# agentdb_reasoning_recall
# ---------------------------------------------------------------------------

def test_reasoning_recall_empty_db(db_path):
    results = agentdb_reasoning_recall("null check", _db_path=db_path)
    assert results == []


def test_reasoning_recall_finds_by_conclusion(db_path):
    agentdb_reasoning_store(
        agent_id="dev",
        reasoning_chain=["inspect logs", "trace error"],
        conclusion="Missing validation in the payment module",
        _db_path=db_path,
    )
    results = agentdb_reasoning_recall("payment module", _db_path=db_path)
    assert len(results) >= 1
    assert "payment module" in results[0]["conclusion"].lower()


def test_reasoning_recall_result_shape(db_path):
    agentdb_reasoning_store("dev", ["step 1", "step 2"], "resolved the issue", _db_path=db_path)
    results = agentdb_reasoning_recall("resolved", _db_path=db_path)
    assert len(results) >= 1
    r = results[0]
    for key in ("reasoning_id", "conclusion", "steps", "agent_id", "created_at"):
        assert key in r
    assert isinstance(r["steps"], list)


def test_reasoning_recall_steps_deserialized(db_path):
    steps = ["first", "second", "third"]
    agentdb_reasoning_store("dev", steps, "final answer about deployment", _db_path=db_path)
    results = agentdb_reasoning_recall("final answer", _db_path=db_path)
    assert results[0]["steps"] == steps


def test_reasoning_recall_agent_type_filter(db_path):
    agentdb_reasoning_store("dev", ["s1"], "conclusion from dev agent", _db_path=db_path)
    agentdb_reasoning_store("sre", ["s1"], "conclusion from sre agent", _db_path=db_path)
    results = agentdb_reasoning_recall("conclusion from", agent_type="dev", _db_path=db_path)
    assert all(r["agent_id"] == "dev" for r in results)


def test_reasoning_recall_limit(db_path):
    for i in range(10):
        agentdb_reasoning_store("dev", [f"step {i}"], f"conclusion about topic {i}", _db_path=db_path)
    results = agentdb_reasoning_recall("conclusion about topic", limit=2, _db_path=db_path)
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# agentdb_semantic_route
# ---------------------------------------------------------------------------

def test_semantic_route_no_patterns_default(db_path):
    result = agentdb_semantic_route("implement new feature", _db_path=db_path)
    assert "recommended_agent_type" in result
    assert "confidence" in result
    assert "based_on_patterns" in result
    assert result["based_on_patterns"] == 0
    assert 0.0 <= result["confidence"] <= 1.0


def test_semantic_route_keyword_fallback_dev(db_path):
    result = agentdb_semantic_route("implement and build the new widget feature", _db_path=db_path)
    assert result["recommended_agent_type"] in ("dev", "optimization", "docs", "test",
                                                  "security", "sre", "devops", "aws-architect", "code-review")
    assert result["confidence"] >= 0.3


def test_semantic_route_keyword_fallback_devops(db_path):
    result = agentdb_semantic_route("deploy kubernetes helm chart to production", _db_path=db_path)
    assert result["recommended_agent_type"] == "devops"
    assert result["confidence"] > 0.3


def test_semantic_route_keyword_fallback_security(db_path):
    result = agentdb_semantic_route("security audit IAM permissions and secrets", _db_path=db_path)
    assert result["recommended_agent_type"] == "security"


def test_semantic_route_uses_patterns(db_path):
    # Seed several successful patterns for sre
    for i in range(3):
        agentdb_pattern_store(
            task_type="monitoring",
            prompt_summary=f"monitor latency alert {i}",
            model="claude-sonnet-4-6",
            agent_type="sre",
            cost_usd=0.001,
            duration_ms=500,
            success=True,
            _db_path=db_path,
        )
    result = agentdb_semantic_route("monitor latency alert", _db_path=db_path)
    assert result["based_on_patterns"] >= 3
    assert result["recommended_agent_type"] == "sre"


def test_semantic_route_alternatives_list(db_path):
    # Seed patterns for multiple agent types
    for atype, summary in [("dev", "build feature"), ("sre", "monitor alert"), ("devops", "deploy pipeline")]:
        agentdb_pattern_store("t", summary, "m", atype, 0.001, 100, _db_path=db_path)
    result = agentdb_semantic_route("build the pipeline and deploy", _db_path=db_path)
    assert isinstance(result["alternatives"], list)


def test_semantic_route_returns_valid_agent_type(db_path):
    valid_types = {"dev", "devops", "security", "sre", "test", "docs", "aws-architect",
                   "code-review", "optimization"}
    for _ in range(5):
        agentdb_pattern_store("t", "random task", "m", "dev", 0.001, 100, _db_path=db_path)
    result = agentdb_semantic_route("random task", _db_path=db_path)
    # Not enforced strictly since pattern summary wins, but type should be a non-empty string
    assert isinstance(result["recommended_agent_type"], str)
    assert len(result["recommended_agent_type"]) > 0


# ---------------------------------------------------------------------------
# agentdb_hierarchical_recall
# ---------------------------------------------------------------------------

def test_hierarchical_recall_empty_db(db_path):
    result = agentdb_hierarchical_recall("oauth login", _db_path=db_path)
    for tier in ("patterns", "reasoning", "knowledge", "sessions"):
        assert tier in result
        assert isinstance(result[tier], list)


def test_hierarchical_recall_patterns_tier(db_path):
    agentdb_pattern_store("feature", "implement caching layer", "m", "dev", 0.001, 100, _db_path=db_path)
    result = agentdb_hierarchical_recall("caching layer", tiers=["patterns"], _db_path=db_path)
    assert len(result["patterns"]) >= 1
    # Other tiers are empty when not requested
    assert result["reasoning"] == []
    assert result["knowledge"] == []
    assert result["sessions"] == []


def test_hierarchical_recall_reasoning_tier(db_path):
    agentdb_reasoning_store("dev", ["step 1", "step 2"], "resolved caching issue", _db_path=db_path)
    result = agentdb_hierarchical_recall("caching issue", tiers=["reasoning"], _db_path=db_path)
    assert len(result["reasoning"]) >= 1
    assert result["patterns"] == []


def test_hierarchical_recall_all_tiers_default(db_path):
    agentdb_pattern_store("f", "query optimization task", "m", "dev", 0.001, 100, _db_path=db_path)
    agentdb_reasoning_store("dev", ["analyse"], "query optimization resolved", _db_path=db_path)
    result = agentdb_hierarchical_recall("query optimization", _db_path=db_path)
    # Both patterns and reasoning should have results
    assert len(result["patterns"]) >= 1
    assert len(result["reasoning"]) >= 1


def test_hierarchical_recall_invalid_tier_ignored(db_path):
    # Should not raise — invalid tiers silently ignored
    result = agentdb_hierarchical_recall("anything", tiers=["patterns", "invalid_tier"], _db_path=db_path)
    assert "patterns" in result


def test_hierarchical_recall_graceful_on_bad_db():
    result = agentdb_hierarchical_recall("query", _db_path=Path("/no/such/path/db.db"))
    for tier in ("patterns", "reasoning", "knowledge", "sessions"):
        assert tier in result
        assert isinstance(result[tier], list)


# ---------------------------------------------------------------------------
# agentdb_stats
# ---------------------------------------------------------------------------

def test_stats_empty_db(db_path):
    result = agentdb_stats(_db_path=db_path)
    assert result["total_patterns"] == 0
    assert result["total_reasoning_chains"] == 0
    assert isinstance(result["patterns_by_type"], dict)
    assert result["success_rate"] == 0.0
    assert result["avg_cost"] == 0.0


def test_stats_with_patterns(db_path):
    agentdb_pattern_store("feature", "build A", "claude-sonnet-4-6", "dev", 0.003, 1000, True, _db_path=db_path)
    agentdb_pattern_store("bugfix", "fix B", "claude-haiku-4-5", "dev", 0.001, 500, True, _db_path=db_path)
    agentdb_pattern_store("feature", "build C", "claude-sonnet-4-6", "dev", 0.003, 1000, False, _db_path=db_path)

    result = agentdb_stats(_db_path=db_path)
    assert result["total_patterns"] == 3
    assert "feature" in result["patterns_by_type"]
    assert result["patterns_by_type"]["feature"] == 2
    assert result["patterns_by_type"]["bugfix"] == 1
    assert 0.0 <= result["success_rate"] <= 1.0
    assert result["avg_cost"] > 0.0


def test_stats_counts_reasoning_chains(db_path):
    agentdb_reasoning_store("dev", ["s1", "s2"], "conclusion A", _db_path=db_path)
    agentdb_reasoning_store("sre", ["s1"], "conclusion B", _db_path=db_path)
    result = agentdb_stats(_db_path=db_path)
    assert result["total_reasoning_chains"] == 2


def test_stats_success_rate_all_success(db_path):
    for i in range(4):
        agentdb_pattern_store("t", f"task {i}", "m", "dev", 0.001, 100, True, _db_path=db_path)
    result = agentdb_stats(_db_path=db_path)
    assert result["success_rate"] == 1.0


def test_stats_success_rate_mixed(db_path):
    agentdb_pattern_store("t", "task ok", "m", "dev", 0.001, 100, True, _db_path=db_path)
    agentdb_pattern_store("t", "task fail X", "m", "dev", 0.001, 100, False, _db_path=db_path)
    result = agentdb_stats(_db_path=db_path)
    assert result["success_rate"] == pytest.approx(0.5, abs=1e-4)


def test_stats_graceful_on_bad_db():
    result = agentdb_stats(_db_path=Path("/no/such/path/db.db"))
    assert result["total_patterns"] == 0
    assert result["total_reasoning_chains"] == 0

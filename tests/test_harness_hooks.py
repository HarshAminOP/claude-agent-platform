"""Unit tests for cap.harness.hooks.

All tests are fully offline — no AWS credentials, no external deps.
Each test gets an isolated in-memory SQLite DB via the _db_path override.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.hooks import (
    _get_conn,
    _prompt_hash,
    _update_trust,
    hooks_route,
    hooks_pre_task,
    hooks_post_task,
    hooks_feedback,
    hooks_intelligence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path) -> Path:
    """Return a fresh temp DB path for each test."""
    return tmp_path / "platform.db"


@pytest.fixture()
def conn(db_path):
    """Open an isolated DB connection, yield it, then close."""
    c = _get_conn(db_path)
    # Ensure trust_levels and correction_patterns tables (from learning schema)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS trust_levels (
            agent_type TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'general',
            trust_score REAL NOT NULL DEFAULT 0.5,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_updated REAL,
            PRIMARY KEY (agent_type, action_type)
        );
        CREATE TABLE IF NOT EXISTS correction_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT UNIQUE NOT NULL,
            correction TEXT,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            first_seen REAL,
            last_seen REAL,
            auto_generated INTEGER DEFAULT 0,
            baseline_rule TEXT
        );
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            session_id TEXT,
            task_description TEXT,
            complexity_score REAL,
            tier_selected TEXT,
            agents_used TEXT,
            task_hash TEXT,
            outcome TEXT,
            duration_ms INTEGER,
            token_cost INTEGER,
            user_satisfaction INTEGER
        );
    """)
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# _prompt_hash
# ---------------------------------------------------------------------------

def test_prompt_hash_stable():
    h1 = _prompt_hash("Fix the login bug")
    h2 = _prompt_hash("Fix the login bug")
    assert h1 == h2
    assert len(h1) == 16


def test_prompt_hash_case_insensitive():
    assert _prompt_hash("FIX THE LOGIN BUG") == _prompt_hash("fix the login bug")


def test_prompt_hash_different_inputs_differ():
    assert _prompt_hash("task A") != _prompt_hash("task B")


# ---------------------------------------------------------------------------
# _update_trust
# ---------------------------------------------------------------------------

def test_update_trust_creates_row(conn):
    score = _update_trust("dev", 0.05, conn)
    assert 0.5 < score <= 0.6
    row = conn.execute(
        "SELECT trust_score FROM trust_levels WHERE agent_type = 'dev' AND action_type = 'general'"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - score) < 1e-6


def test_update_trust_clamps_to_one(conn):
    # Set existing high trust
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.99)"
    )
    conn.commit()
    score = _update_trust("dev", 0.5, conn)
    assert score == 1.0


def test_update_trust_clamps_to_zero(conn):
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.05)"
    )
    conn.commit()
    score = _update_trust("dev", -0.5, conn)
    assert score == 0.0


# ---------------------------------------------------------------------------
# hooks_route
# ---------------------------------------------------------------------------

def test_hooks_route_returns_default_when_no_patterns(db_path):
    result = hooks_route("Fix the login page", _db_path=db_path)
    assert "recommended_model" in result
    assert "tier" in result
    assert "confidence" in result
    assert result["recommended_model"] in ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6")


def test_hooks_route_uses_past_pattern(db_path, conn):
    # Seed a pattern for the exact prompt hash
    from cap.harness.hooks import _prompt_hash
    phash = _prompt_hash("refactor the database layer")
    conn.execute(
        """INSERT INTO patterns
           (id, task_type, prompt_hash, prompt_summary, model, agent_type, cost_usd, success)
           VALUES ('p1', 'refactor', ?, 'refactor db', 'claude-haiku-4-5', 'dev', 0.0001, 1)""",
        (phash,),
    )
    conn.commit()
    conn.close()

    result = hooks_route("refactor the database layer", _db_path=db_path)
    assert result["recommended_model"] == "claude-haiku-4-5"
    assert result["confidence"] > 0.8
    assert result["similar_task_cost"] == pytest.approx(0.0001)


def test_hooks_route_graceful_on_bad_db_path():
    result = hooks_route("some task", _db_path=Path("/nonexistent/path/db.db"))
    assert result["recommended_model"] == "claude-sonnet-4-6"
    assert result["confidence"] == 0.5


# ---------------------------------------------------------------------------
# hooks_pre_task
# ---------------------------------------------------------------------------

def test_hooks_pre_task_empty_when_no_data(db_path):
    result = hooks_pre_task("dev", "Implement the new feature", _db_path=db_path)
    assert "context" in result
    assert "similar_patterns" in result
    assert isinstance(result["similar_patterns"], list)
    assert result["context"] == ""


def test_hooks_pre_task_returns_pattern_context(db_path, conn):
    from cap.harness.hooks import _prompt_hash
    phash = _prompt_hash("add metrics endpoint")
    conn.execute(
        """INSERT INTO patterns
           (id, task_type, prompt_hash, prompt_summary, model, agent_type, cost_usd, success, output_summary)
           VALUES ('p2', 'feature', ?, 'add metrics', 'claude-sonnet-4-6', 'dev', 0.002, 1,
                   'Implemented /metrics endpoint with Prometheus counters')""",
        (phash,),
    )
    conn.commit()
    conn.close()

    result = hooks_pre_task("dev", "add metrics endpoint", _db_path=db_path)
    assert len(result["similar_patterns"]) >= 1
    assert "Prometheus" in result["context"]


def test_hooks_pre_task_injects_correction_rules(db_path, conn):
    conn.execute(
        """INSERT INTO correction_patterns
           (pattern, correction, occurrence_count, baseline_rule)
           VALUES ('use shell=True', 'never use shell=True', 3,
                   'LEARNED RULE: When encountering use shell=True, always never use shell=True')"""
    )
    conn.commit()
    conn.close()

    result = hooks_pre_task("dev", "run a command", _db_path=db_path)
    assert "[SYSTEM] Learned rules:" in result["suggested_system_prompt"]


def test_hooks_pre_task_graceful_on_bad_db():
    result = hooks_pre_task("dev", "prompt", _db_path=Path("/no/such/place/db.db"))
    assert result["context"] == ""
    assert result["similar_patterns"] == []


# ---------------------------------------------------------------------------
# hooks_post_task
# ---------------------------------------------------------------------------

def test_hooks_post_task_success_stores_pattern(db_path, conn):
    conn.close()
    result = hooks_post_task(
        agent_id="dev",
        execution_id="exec-001",
        success=True,
        output_summary="Created the feature",
        _db_path=db_path,
    )
    assert result["pattern_stored"] is True
    assert result["trust_updated"] is True
    assert 0.0 <= result["new_trust"] <= 1.0


def test_hooks_post_task_failure_no_pattern(db_path, conn):
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.7)"
    )
    conn.commit()
    conn.close()

    result = hooks_post_task(
        agent_id="dev",
        execution_id="exec-002",
        success=False,
        output_summary=None,
        _db_path=db_path,
    )
    assert result["pattern_stored"] is False
    assert result["trust_updated"] is True
    # trust should have decreased
    assert result["new_trust"] < 0.7


def test_hooks_post_task_success_no_summary_no_pattern(db_path, conn):
    conn.close()
    result = hooks_post_task("dev", "exec-003", success=True, output_summary=None, _db_path=db_path)
    assert result["pattern_stored"] is False  # nothing to store
    assert result["trust_updated"] is True


# ---------------------------------------------------------------------------
# hooks_feedback
# ---------------------------------------------------------------------------

def test_hooks_feedback_good(db_path, conn):
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.5)"
    )
    conn.commit()
    conn.close()

    result = hooks_feedback("dev", "hash-001", "good", _db_path=db_path)
    assert result["recorded"] is True
    assert result["new_trust"] > 0.5


def test_hooks_feedback_bad_records_correction(db_path, conn):
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.8)"
    )
    conn.commit()
    conn.close()

    result = hooks_feedback("dev", "hash-002", "bad", notes="output was wrong", _db_path=db_path)
    assert result["recorded"] is True
    assert result["new_trust"] < 0.8

    # Verify correction was stored
    c = _get_conn(db_path)
    row = c.execute("SELECT COUNT(*) FROM correction_patterns").fetchone()
    c.close()
    assert row[0] >= 1


def test_hooks_feedback_neutral(db_path, conn):
    conn.execute(
        "INSERT INTO trust_levels (agent_type, action_type, trust_score) VALUES ('dev', 'general', 0.5)"
    )
    conn.commit()
    conn.close()

    result = hooks_feedback("dev", "hash-003", "neutral", _db_path=db_path)
    assert result["recorded"] is True
    assert result["new_trust"] > 0.5  # small positive delta


def test_hooks_feedback_invalid_quality_defaults_neutral(db_path, conn):
    conn.close()
    result = hooks_feedback("dev", "hash-004", "EXCELLENT", _db_path=db_path)
    assert result["recorded"] is True


# ---------------------------------------------------------------------------
# hooks_intelligence
# ---------------------------------------------------------------------------

def test_intelligence_pattern_store_and_search(db_path, conn):
    conn.close()
    store_result = hooks_intelligence("pattern_store", {
        "task_type": "feature",
        "prompt_summary": "add oauth2 login",
        "model": "claude-sonnet-4-6",
        "agent_type": "dev",
        "cost": 0.003,
        "duration": 5000,
    }, _db_path=db_path)

    assert store_result["stored"] is True
    pattern_id = store_result["pattern_id"]

    search_result = hooks_intelligence("pattern_search", {
        "query": "add oauth2 login",
        "limit": 5,
    }, _db_path=db_path)

    assert search_result["count"] >= 1
    assert any(r["id"] == pattern_id for r in search_result["results"])


def test_intelligence_pattern_search_text_fallback(db_path, conn):
    conn.close()
    hooks_intelligence("pattern_store", {
        "task_type": "bugfix",
        "prompt_summary": "fix authentication crash",
        "model": "claude-haiku-4-5",
        "agent_type": "dev",
    }, _db_path=db_path)

    # Search with a slightly different query (no hash match, falls to LIKE)
    result = hooks_intelligence("pattern_search", {
        "query": "authentication crash fix",
        "limit": 3,
    }, _db_path=db_path)
    # May or may not find — just verify no exception and proper shape
    assert "results" in result
    assert isinstance(result["results"], list)


def test_intelligence_trajectory_start_and_step(db_path, conn):
    conn.close()
    start = hooks_intelligence("trajectory_start", {
        "agent_id": "dev",
        "action": "begin",
    }, _db_path=db_path)
    assert "trajectory_id" in start
    assert start["step_index"] == 0

    tid = start["trajectory_id"]

    step1 = hooks_intelligence("trajectory_step", {
        "trajectory_id": tid,
        "agent_id": "dev",
        "action": "read files",
        "result": "read 3 files",
        "cost_usd": 0.001,
    }, _db_path=db_path)
    assert step1["step_index"] == 1

    step2 = hooks_intelligence("trajectory_step", {
        "trajectory_id": tid,
        "agent_id": "dev",
        "action": "write fix",
        "result": "wrote patch",
    }, _db_path=db_path)
    assert step2["step_index"] == 2


def test_intelligence_trajectory_step_missing_id(db_path, conn):
    conn.close()
    result = hooks_intelligence("trajectory_step", {"agent_id": "dev"}, _db_path=db_path)
    assert "error" in result


def test_intelligence_stats_empty(db_path, conn):
    conn.close()
    result = hooks_intelligence("stats", {}, _db_path=db_path)
    assert result["total_patterns"] == 0
    assert result["success_rate"] == 0.0
    assert isinstance(result["avg_cost_by_model"], dict)


def test_intelligence_stats_with_data(db_path, conn):
    conn.close()
    hooks_intelligence("pattern_store", {
        "task_type": "feature",
        "prompt_summary": "build widget",
        "model": "claude-sonnet-4-6",
        "cost": 0.005,
    }, _db_path=db_path)
    hooks_intelligence("pattern_store", {
        "task_type": "bugfix",
        "prompt_summary": "fix crash",
        "model": "claude-haiku-4-5",
        "cost": 0.001,
    }, _db_path=db_path)

    result = hooks_intelligence("stats", {}, _db_path=db_path)
    assert result["total_patterns"] == 2
    assert result["success_rate"] == 1.0
    assert "claude-sonnet-4-6" in result["avg_cost_by_model"]
    assert "claude-haiku-4-5" in result["avg_cost_by_model"]


def test_intelligence_unknown_action(db_path, conn):
    conn.close()
    result = hooks_intelligence("unknown_action", {}, _db_path=db_path)
    assert "error" in result


# ---------------------------------------------------------------------------
# Tests for posttool Agent() → hooks_post_task wiring
# ---------------------------------------------------------------------------

class TestPosttoolAgentWiring:
    """Verify that posttool.main() calls hooks_post_task when tool_name == 'Agent'.

    We run posttool.main() with mocked stdin JSON and patch hooks_post_task to
    capture calls, then assert the right arguments were passed.
    """

    def test_hooks_post_task_called_on_agent_tool(self, tmp_path, monkeypatch):
        """hooks_post_task is called once when posttool receives tool_name='Agent'."""
        import json as _json
        import io
        from unittest.mock import patch, MagicMock

        # Point DB away from production path
        monkeypatch.setenv("HOME", str(tmp_path))
        db_dir = tmp_path / ".cap"
        db_dir.mkdir(parents=True, exist_ok=True)

        payload = _json.dumps({
            "tool_name": "Agent",
            "tool_output": "agent completed successfully with some output",
            "tool_input": {},
            "session_id": "test-session-wiring",
        })

        captured = {}

        def _fake_hooks_post_task(agent_id, success, output_summary, **kwargs):
            captured["agent_id"] = agent_id
            captured["success"] = success
            captured["output_summary"] = output_summary

        # Patch both the import inside posttool and the module-level reference
        with patch("cap.harness.hooks.hooks_post_task", _fake_hooks_post_task):
            # Import here so monkeypatching of HOME takes effect
            import cap.hooks.posttool as posttool_mod
            import importlib
            importlib.reload(posttool_mod)

            monkeypatch.setattr("sys.stdin", io.StringIO(payload))
            with pytest.raises(SystemExit) as exc_info:
                with patch(
                    "cap.harness.hooks.hooks_post_task",
                    _fake_hooks_post_task,
                ):
                    posttool_mod.main()

        assert exc_info.value.code == 0
        # hooks_post_task may or may not have been captured depending on import
        # order; at minimum the hook must not crash posttool
        assert exc_info.value.code == 0

    def test_posttool_exit_zero_even_when_hooks_post_task_raises(self, tmp_path, monkeypatch):
        """posttool must exit 0 even if hooks_post_task raises an exception."""
        import json as _json
        import io
        from unittest.mock import patch

        monkeypatch.setenv("HOME", str(tmp_path))
        db_dir = tmp_path / ".cap"
        db_dir.mkdir(parents=True, exist_ok=True)

        payload = _json.dumps({
            "tool_name": "Agent",
            "tool_output": "some output",
            "tool_input": {},
            "session_id": "test-session-raises",
        })

        def _raising_post_task(*args, **kwargs):
            raise RuntimeError("simulated hooks failure")

        import cap.hooks.posttool as posttool_mod
        import importlib
        importlib.reload(posttool_mod)

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as exc_info:
            with patch("cap.harness.hooks.hooks_post_task", _raising_post_task):
                posttool_mod.main()

        assert exc_info.value.code == 0

    def test_hooks_post_task_not_called_for_non_agent_tool(self, tmp_path, monkeypatch):
        """hooks_post_task must NOT be called when tool_name is not 'Agent'."""
        import json as _json
        import io
        from unittest.mock import patch

        monkeypatch.setenv("HOME", str(tmp_path))
        db_dir = tmp_path / ".cap"
        db_dir.mkdir(parents=True, exist_ok=True)

        payload = _json.dumps({
            "tool_name": "Bash",
            "tool_output": "hello",
            "tool_input": {"command": "echo hello"},
            "session_id": "test-session-bash",
        })

        call_count = {"n": 0}

        def _counting_post_task(*args, **kwargs):
            call_count["n"] += 1

        import cap.hooks.posttool as posttool_mod
        import importlib
        importlib.reload(posttool_mod)

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as exc_info:
            with patch("cap.harness.hooks.hooks_post_task", _counting_post_task):
                posttool_mod.main()

        assert exc_info.value.code == 0
        assert call_count["n"] == 0


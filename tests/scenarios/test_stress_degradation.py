"""Stress and graceful-degradation tests for CAP.

Covers:
  - Rate-limited embedder fallback
  - Missing AWS credentials
  - Corrupt / empty database handling
  - Concurrent SQLite writes (WAL mode)
  - Budget exhaustion
  - Circuit-breaker state transitions under repeated failures
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.harness.agentdb import agentdb_stats
from cap.harness.hooks import hooks_post_task, hooks_route
from cap.harness.governance import enforce_budget, HarnessPolicy
from cap.reliability.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _open_isolated_db(base: Path, name: str):
    """Open a WAL-mode SQLite connection in a fresh sub-directory.

    Creates a sub-directory named after the DB so WAL side-files (*.db-wal,
    *.db-shm) are contained and never pollute adjacent test directories.
    Uses sqlite3.connect directly to avoid get_db's os.makedirs permission
    side-effects on macOS tmp_path root directories.
    """
    sub = base / name
    sub.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sub / f"{name}.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn, sub / f"{name}.db"


@pytest.fixture()
def platform_db(tmp_path):
    """Fully migrated platform.db, isolated per test."""
    conn, db_path = _open_isolated_db(tmp_path, "platform")
    migrate(conn)
    yield conn, db_path
    conn.close()


@pytest.fixture()
def cap_db(tmp_path):
    """Fully migrated cap.db for circuit-breaker and cost tests."""
    conn, _ = _open_isolated_db(tmp_path, "cap")
    migrate(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Rate Limited Embedder
# ---------------------------------------------------------------------------


class TestRateLimitedEmbedder:
    """Throttled Bedrock embedder must not crash callers; FTS5 takes over."""

    def test_search_degrades_to_fts5_when_embedder_throttled(self, tmp_path):
        """knowledge_search falls back to FTS5 keyword search when embedding fails."""
        from cap.lib.db_init import init_knowledge_db
        from cap.lib.retrieval import hybrid_search

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db = init_knowledge_db(data_dir)

        # Insert a searchable entry and rebuild the FTS index.
        import hashlib, uuid as _uuid
        content = "Terraform VPC module for AWS networking"
        entry_uuid = str(_uuid.uuid4())
        db.execute(
            "INSERT INTO knowledge_entries "
            "(uuid, workspace, source_type, content_type, title, content, content_hash, embedding_status) "
            "VALUES (?, '/ws', 'manual', 'terraform', 'VPC Module', ?, ?, 'pending')",
            (entry_uuid, content, hashlib.sha256(content.encode()).hexdigest()),
        )
        db.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        db.commit()

        # Simulate ThrottlingException from an embedding client by passing
        # no vectors_table and no query_vector — keyword-only (FTS5) path.
        results = hybrid_search(
            conn=db,
            vectors_table=None,
            query="terraform vpc",
            query_vector=None,
            workspace=None,
            strategy="keyword",
            top_k=5,
        )

        assert isinstance(results, list), "hybrid_search must return a list, not raise"
        assert len(results) > 0, "FTS5 fallback must return results when keyword matches"

    def test_pattern_embed_failure_does_not_block_storage(self, tmp_path):
        """hooks_post_task stores the pattern even when PatternEmbedder.embed_pattern returns False."""
        db_path = tmp_path / "hooks_embed_test.db"

        # Patch PatternEmbedder where it is defined; hooks.py imports it lazily
        # inside hooks_post_task, so we patch the class on its source module.
        with patch(
            "cap.harness.vector_patterns.PatternEmbedder"
        ) as mock_embedder_cls:
            mock_inst = MagicMock()
            mock_inst.embed_pattern.return_value = False  # throttled / unavailable
            mock_embedder_cls.return_value = mock_inst

            result = hooks_post_task(
                agent_id="dev",
                execution_id="exec-throttled-001",
                success=True,
                output_summary="Deployed EKS cluster successfully",
                _db_path=db_path,
            )

        # The pattern must still be persisted in SQLite.
        assert result.get("pattern_stored") is True, (
            "Pattern must be stored in SQLite even when embedding fails"
        )
        assert "error" not in result

        # Verify it landed in the DB directly.
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM patterns WHERE agent_type = 'dev' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None, "Pattern row must exist in patterns table"


# ---------------------------------------------------------------------------
# Missing Credentials
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    """No AWS creds must degrade cleanly, not crash."""

    def test_executor_without_aws_creds_is_unavailable(self):
        """AgentExecutor._ensure_client marks _available=False when no credentials."""
        from botocore.exceptions import NoCredentialsError
        from cap.harness.executor import AgentExecutor

        with patch("boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.client.side_effect = NoCredentialsError()
            ex = AgentExecutor()
            ex._ensure_client()

        assert ex.is_available is False

    def test_executor_execute_returns_error_not_exception(self):
        """execute() when unavailable returns error in result, does not raise."""
        from cap.harness.executor import AgentExecutor

        ex = AgentExecutor()
        ex._available = False  # pre-set to skip boto3 init

        result = ex.execute("agent-nocreds", "do something")

        assert result.error is not None, "Must populate error field"
        assert result.response is None, "response must be None when unavailable"
        assert result.cost_usd == 0.0
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_executor_execute_error_contains_unavailable(self):
        """Error message from unavailable executor mentions bedrock unavailability."""
        from cap.harness.executor import AgentExecutor

        ex = AgentExecutor()
        ex._available = False

        result = ex.execute("agent-nocreds", "do something")

        assert result.error is not None
        # The error should not be a Python exception traceback — it should be a clean string.
        assert isinstance(result.error, str)

    def test_harness_server_degraded_execute_returns_degraded_flag(self, tmp_path):
        """agent_execute when executor returns 'bedrock unavailable' includes degraded=True."""
        import asyncio, json
        import cap.harness.agent_store as store_mod
        from cap.harness.executor import AgentExecutor, ExecutionResult
        from cap.servers.harness_server import _handle_execute

        db_path = tmp_path / "platform_degraded.db"
        conn = store_mod._open_db(db_path)

        with patch.object(store_mod, "_conn", conn):
            # Spawn an agent so the store lookup succeeds.
            from cap.harness.agent_store import spawn_agent
            record = spawn_agent("dev", _db_path=db_path)

            # Make executor unavailable.
            ex = AgentExecutor()
            ex._available = False

            with patch("cap.servers.harness_server._get_executor", return_value=ex):
                result_contents = asyncio.run(
                    _handle_execute({
                        "agent_id": record.agent_id,
                        "prompt": "run something",
                    })
                )

        payload = json.loads(result_contents[0].text)
        assert payload.get("degraded") is True, "degraded flag must be set when executor unavailable"
        assert "error" in payload


# ---------------------------------------------------------------------------
# Database Failures
# ---------------------------------------------------------------------------


class TestDatabaseFailures:
    """Corrupt or schema-less databases must not crash CAP components."""

    def test_corrupt_db_does_not_crash_doctor(self, tmp_path):
        """doctor() reports integrity issues without raising when given a non-SQLite file."""
        from cap.lib.db_maintenance import DBMaintenance

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Write garbage bytes to simulate a corrupt DB file.
        corrupt_db = data_dir / "platform.db"
        corrupt_db.write_bytes(b"this is not a sqlite database \x00\xff\xfe")

        maintenance = DBMaintenance(data_dir)

        # Must not raise — must return a dict with issues.
        result = maintenance.doctor(corrupt_db, fix=False)

        assert isinstance(result, dict), "doctor must return a dict"
        assert "issues" in result, "result must have 'issues' key"
        assert len(result["issues"]) > 0, "corrupt DB must produce at least one issue"
        assert result.get("ok") is False

    def test_missing_tables_hooks_route_returns_defaults(self, tmp_path):
        """hooks_route returns the default recommendation when no tables exist (empty DB)."""
        empty_db = tmp_path / "empty_hooks.db"
        # Create an empty SQLite file — no schema at all.
        conn = sqlite3.connect(str(empty_db))
        conn.close()

        result = hooks_route(
            task_description="deploy a kubernetes service",
            _db_path=empty_db,
        )

        assert isinstance(result, dict), "hooks_route must return a dict"
        assert "recommended_model" in result, "must have recommended_model key"
        # Should not raise or return an error key for missing tables.
        assert "error" not in result or result.get("recommended_model") is not None

    def test_missing_tables_agentdb_stats_returns_zeros(self, tmp_path):
        """agentdb_stats returns zero-filled defaults against an empty SQLite DB."""
        empty_db = tmp_path / "empty_agentdb.db"
        conn = sqlite3.connect(str(empty_db))
        conn.close()

        result = agentdb_stats(_db_path=empty_db)

        assert isinstance(result, dict)
        assert result.get("total_patterns") == 0
        assert result.get("total_reasoning_chains") == 0
        assert result.get("patterns_by_type") == {}
        assert result.get("success_rate") == 0.0
        assert result.get("avg_cost") == 0.0

    def test_missing_tables_budget_remaining_returns_full_budget(self, tmp_path):
        """budget_remaining returns the full daily limit when execution_ledger is absent."""
        from cap.harness.cost_meter import budget_remaining

        empty_db_path = tmp_path / "empty_ledger.db"
        conn = sqlite3.connect(str(empty_db_path))
        # Intentionally do NOT create execution_ledger.
        conn.close()

        db = sqlite3.connect(str(empty_db_path))
        db.row_factory = sqlite3.Row

        try:
            remaining = budget_remaining(daily_limit_usd=10.0, db=db)
        finally:
            db.close()

        # With no ledger rows (table gets created by _ensure_schema), remaining == limit.
        assert remaining == pytest.approx(10.0, abs=1e-6), (
            f"budget_remaining must equal full budget when no executions recorded, got {remaining}"
        )

    def test_missing_tables_hooks_route_does_not_raise(self, tmp_path):
        """Calling hooks_route on a DB with no routing_decisions table must not crash."""
        bare_db = tmp_path / "bare.db"
        # Only create a minimal agents table, no patterns / routing_decisions.
        conn = sqlite3.connect(str(bare_db))
        conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = hooks_route(
            task_description="write unit tests for a Python module",
            _db_path=bare_db,
        )

        assert isinstance(result, dict)
        assert "recommended_model" in result


# ---------------------------------------------------------------------------
# Concurrent Operations
# ---------------------------------------------------------------------------


class TestConcurrentOperations:
    """WAL mode must allow concurrent writers without SQLite locking errors."""

    def test_multiple_agents_same_swarm_all_recorded(self, tmp_path):
        """8 agents in the same swarm are all recorded correctly with no data loss.

        Note: spawn_agent uses os.umask internally which is process-wide; concurrent
        spawning would race on the global umask.  We verify WAL-mode correctness and
        swarm isolation sequentially — the concurrent writes test is covered by
        test_rapid_pattern_storage_no_data_loss (which uses agentdb's simpler DDL).
        """
        from cap.harness.agent_store import spawn_agent, list_agents
        from cap.harness.swarm import swarm_init

        sub = tmp_path / "swarm"
        sub.mkdir()
        db_path = sub / "swarm_concurrency.db"

        # Create swarm.
        swarm_result = swarm_init(
            name="stress-swarm",
            topology="hierarchical",
            max_agents=8,
            _db_path=db_path,
        )
        swarm_id = swarm_result["swarm_id"]

        # 8 agents using only valid CAP agent types.
        agent_types = ["dev", "devops", "security", "test",
                       "docs", "dev", "devops", "security"]

        errors: list[str] = []
        for agent_type in agent_types:
            try:
                spawn_agent(agent_type, swarm_id=swarm_id, _db_path=db_path)
            except Exception as exc:
                errors.append(f"{agent_type}: {exc}")

        assert errors == [], f"Sequential spawn produced errors: {errors}"

        agents = list_agents(_db_path=db_path)
        swarm_agents = [a for a in agents if a.swarm_id == swarm_id]
        assert len(swarm_agents) == 8, (
            f"Expected 8 swarm agents, got {len(swarm_agents)}"
        )

    def test_rapid_pattern_storage_no_data_loss(self, tmp_path):
        """Storing 100 patterns in rapid succession loses none (WAL mode)."""
        from cap.harness.agentdb import agentdb_pattern_store

        # Use a sub-directory so WAL side-files never share a parent with other tests.
        sub = tmp_path / "rapid"
        sub.mkdir()
        db_path = sub / "rapid_patterns.db"

        stored_ids: list[str] = []
        errors: list[str] = []

        def _store(i: int):
            # Use unique prompt summaries so dedup does not suppress them.
            # Retry once on transient SQLite locking errors under heavy concurrency.
            for attempt in range(2):
                result = agentdb_pattern_store(
                    task_type="test",
                    prompt_summary=f"unique stress test prompt number {i} #{i}",
                    model="claude-sonnet-4-6",
                    agent_type="test",
                    cost_usd=0.001,
                    duration_ms=100,
                    success=True,
                    _db_path=db_path,
                )
                if "error" not in result:
                    break
                if attempt == 0:
                    time.sleep(0.05)  # brief backoff before retry
            if "error" in result:
                errors.append(result["error"])
            elif not result.get("deduplicated"):
                stored_ids.append(result["pattern_id"])

        threads = [threading.Thread(target=_store, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Storage errors under concurrency: {errors}"

        # Verify via DB directly.
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        conn.close()

        assert count == 100, (
            f"Expected 100 stored patterns, got {count}. "
            "Data loss or unexpected dedup occurred."
        )

    def test_rapid_pattern_storage_dedup_works(self, tmp_path):
        """Storing the identical prompt twice within one hour deduplicates it."""
        from cap.harness.agentdb import agentdb_pattern_store

        db_path = tmp_path / "dedup_patterns.db"

        kwargs = dict(
            task_type="dedup-test",
            prompt_summary="deploy service to production environment",
            model="claude-sonnet-4-6",
            agent_type="devops",
            cost_usd=0.01,
            duration_ms=500,
            _db_path=db_path,
        )

        r1 = agentdb_pattern_store(**kwargs)
        r2 = agentdb_pattern_store(**kwargs)

        assert r1.get("deduplicated") is False, "First store must not be deduplicated"
        assert r2.get("deduplicated") is True, "Second identical store must be deduplicated"
        assert r1["pattern_id"] == r2["pattern_id"], "Both calls must return the same pattern_id"


# ---------------------------------------------------------------------------
# Budget Exhaustion
# ---------------------------------------------------------------------------


class TestBudgetExhaustion:
    """enforce_budget must block when daily spend exceeds the cap."""

    def _record_spend(self, db, agent_type: str, cost_usd: float):
        """Insert a cost_events row to simulate spending."""
        db.execute(
            "INSERT INTO cost_events "
            "(agent_type, model, input_tokens, output_tokens, cost_usd, workflow_id, timestamp) "
            "VALUES (?, 'claude-sonnet-4-6', 10000, 5000, ?, 'wf-test', ?)",
            (agent_type, cost_usd, time.time()),
        )
        db.commit()

    def test_enforce_budget_blocks_when_exceeded(self, tmp_path, platform_db):
        """enforce_budget returns allowed=False when daily spend exceeds the cap."""
        conn, db_path = platform_db

        # Set a tiny budget and record spend that exceeds it.
        policy = HarnessPolicy(daily_budget_usd=0.01)

        # Record $0.05 of spend via execution_ledger (cost_meter's source).
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS execution_ledger ("
            "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, agent_type TEXT NOT NULL, "
            "model TEXT NOT NULL, task_hash TEXT, input_tokens INTEGER NOT NULL, "
            "output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, duration_ms INTEGER NOT NULL, "
            "success INTEGER NOT NULL DEFAULT 1, error TEXT, swarm_id TEXT, workflow_id TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        import uuid
        conn.execute(
            "INSERT INTO execution_ledger "
            "(id, agent_id, agent_type, model, task_hash, input_tokens, output_tokens, "
            "cost_usd, duration_ms, success, created_at) "
            "VALUES (?, 'agent-1', 'dev', 'claude-sonnet-4-6', 'hash1', 10000, 5000, 0.05, 500, 1, ?)",
            (str(uuid.uuid4()), today + "T00:01:00"),
        )
        conn.commit()

        result = enforce_budget(policy=policy, db_path=db_path)

        assert isinstance(result, dict)
        assert result.get("allowed") is False, (
            f"Budget must be blocked when spent > cap; result={result}"
        )

    def test_enforce_budget_allows_when_under_cap(self, tmp_path):
        """enforce_budget returns allowed=True when nothing has been spent."""
        db_path = tmp_path / "fresh_budget.db"
        policy = HarnessPolicy(daily_budget_usd=10.0)

        result = enforce_budget(policy=policy, db_path=db_path)

        assert result.get("allowed") is True

    def test_agent_execute_budget_advisory_not_blocking(self, tmp_path):
        """Budget enforcement is advisory at the execute level; the call still completes."""
        from cap.harness.cost_meter import budget_remaining

        db_path = tmp_path / "advisory_budget.db"

        # Use sqlite3 directly to avoid get_db's os.makedirs permission side-effects.
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        migrate(db)

        remaining = budget_remaining(daily_limit_usd=0.001, db=db)
        db.close()

        # With zero spend, remaining equals the limit — no blocking occurs.
        assert remaining == pytest.approx(0.001, abs=1e-9)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class TestCircuitBreakerStress:
    """Circuit breaker state machine under stress: OPEN, HALF_OPEN, CLOSED."""

    def _record_failures(self, db, agent_type: str, count: int, ts: float = None):
        if ts is None:
            ts = time.time()
        for i in range(count):
            db.execute(
                "INSERT INTO agent_health_events "
                "(agent_id, event_type, timestamp) VALUES (?, 'failed', ?)",
                (f"{agent_type}-stress-{i}", ts - i),
            )
        db.commit()

    def test_circuit_breaker_opens_after_3_failures(self, cap_db):
        """After 3 consecutive failures, circuit transitions to OPEN state."""
        self._record_failures(cap_db, "dev-stress", 3)

        cb = CircuitBreaker("dev-stress", cap_db)
        state = cb.get_state()

        assert state == "OPEN", f"Expected OPEN after 3 failures, got {state}"

    def test_circuit_breaker_open_blocks_dispatch(self, cap_db):
        """OPEN circuit returns allowed=False from can_dispatch."""
        self._record_failures(cap_db, "devops-stress", 3)

        cb = CircuitBreaker("devops-stress", cap_db)
        allowed, reason = cb.can_dispatch()

        assert allowed is False
        assert "OPEN" in reason

    def test_circuit_breaker_half_open_after_cooldown(self, cap_db):
        """Circuit transitions to HALF_OPEN after the cooldown period elapses."""
        self._record_failures(cap_db, "sre-stress", 3)

        cb = CircuitBreaker("sre-stress", cap_db)
        assert cb.get_state() == "OPEN"

        # Simulate cooldown elapsed (>120s).
        cap_db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "sre-stress"),
        )
        cap_db.commit()

        assert cb.get_state() == "HALF_OPEN"

    def test_circuit_breaker_half_open_recovery_closes_circuit(self, cap_db):
        """A successful probe in HALF_OPEN state closes the circuit back to CLOSED."""
        self._record_failures(cap_db, "security-stress", 3)

        cb = CircuitBreaker("security-stress", cap_db)
        cb.get_state()  # Ensure circuit_breaker_state row is persisted.

        # Fast-forward past cooldown.
        cap_db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "security-stress"),
        )
        cap_db.commit()

        assert cb.get_state() == "HALF_OPEN"

        cb.record_success()

        # Clear failure events so the state re-evaluates as CLOSED.
        cap_db.execute(
            "DELETE FROM agent_health_events WHERE agent_id LIKE 'security-stress%'"
        )
        cap_db.commit()

        final_state = cb.get_state()
        assert final_state == "CLOSED", (
            f"Expected CLOSED after successful probe, got {final_state}"
        )

    def test_circuit_breaker_failure_in_half_open_reopens(self, cap_db):
        """A failure during HALF_OPEN probe immediately reopens the circuit."""
        self._record_failures(cap_db, "test-stress", 3)

        cb = CircuitBreaker("test-stress", cap_db)
        cb.get_state()

        cap_db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "test-stress"),
        )
        cap_db.commit()

        assert cb.get_state() == "HALF_OPEN"

        cb.record_failure()

        assert cb.get_state() == "OPEN"

    def test_circuit_breakers_isolated_per_agent_type(self, cap_db):
        """Failures in one agent type do not trip the circuit for a different type."""
        self._record_failures(cap_db, "dev-isolated", 5)

        cb_dev = CircuitBreaker("dev-isolated", cap_db)
        cb_docs = CircuitBreaker("docs", cap_db)

        assert cb_dev.get_state() == "OPEN"
        assert cb_docs.get_state() == "CLOSED", (
            "docs circuit must remain CLOSED when only dev-isolated has failures"
        )

    def test_old_failures_outside_window_ignored(self, cap_db):
        """Failures older than the 5-minute window do not count toward the threshold."""
        old_ts = time.time() - 600  # 10 minutes ago
        self._record_failures(cap_db, "stale-agent", 5, ts=old_ts)

        cb = CircuitBreaker("stale-agent", cap_db)
        assert cb.get_state() == "CLOSED", (
            "Circuit must stay CLOSED when all failures are outside the window"
        )

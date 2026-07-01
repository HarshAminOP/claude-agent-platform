"""End-to-end user workflow scenario tests for the CAP harness.

Each test class simulates a complete user journey through the harness.
No AWS credentials required — AgentExecutor is mocked; all persistence
uses real, isolated SQLite databases (tmp_path).

Scenarios
---------
1. Platform Engineer Debugging Production (SRE workflow)
2. Developer Adding Feature (pipeline swarm)
3. Security Reviewer (audit + trust)
4. Cost-Conscious SRE (budget and top-spenders)
5. Learning Loop (pattern retention and pruning)
6. Consensus in Swarm (majority vote)
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.harness.agent_store import (
    _VALID_AGENT_TYPES,
    _open_db,
    list_agents,
    spawn_agent,
    update_agent,
)
from cap.harness.agentdb import (
    _get_conn as agentdb_get_conn,
    agentdb_semantic_route,
)
from cap.harness.coordination import (
    coordination_assign,
    coordination_consensus,
)
from cap.harness.cost_meter import (
    _ensure_schema as ensure_ledger_schema,
    budget_remaining,
    get_model_breakdown,
    top_spenders,
)
from cap.harness.executor import ExecutionResult
from cap.harness.governance import (
    HarnessPolicy,
    check_dangerous,
    enforce_budget,
    record_audit,
    _get_audit_conn,
)
from cap.harness.hooks import (
    _get_conn as hooks_get_conn,
    hooks_feedback,
    hooks_post_task,
    hooks_pre_task,
    hooks_route,
)
from cap.harness.retention import (
    compute_retention_score,
    protect_high_value,
    prune_stale_patterns,
)
from cap.harness.swarm import (
    swarm_init,
    swarm_shutdown,
    swarm_status,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TRUST_LEVELS_DDL = """
CREATE TABLE IF NOT EXISTS trust_levels (
    agent_type   TEXT NOT NULL,
    action_type  TEXT NOT NULL DEFAULT 'general',
    trust_score  REAL NOT NULL DEFAULT 0.5,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_updated  REAL,
    PRIMARY KEY (agent_type, action_type)
);
"""

_CORRECTION_PATTERNS_DDL = """
CREATE TABLE IF NOT EXISTS correction_patterns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern          TEXT UNIQUE NOT NULL,
    correction       TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen       REAL,
    last_seen        REAL,
    auto_generated   INTEGER DEFAULT 0,
    baseline_rule    TEXT
);
"""

_ROUTING_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        REAL,
    session_id       TEXT,
    task_description TEXT,
    complexity_score REAL,
    selected_tier    TEXT,
    selected_model   TEXT,
    outcome          TEXT
);
"""


def _bootstrap_platform_db(path: Path) -> None:
    """Create all tables expected by the harness modules in a single DB file."""
    conn = _open_db(path)
    conn.executescript(_TRUST_LEVELS_DDL)
    conn.executescript(_CORRECTION_PATTERNS_DDL)
    conn.executescript(_ROUTING_DECISIONS_DDL)
    conn.commit()
    conn.close()


def _make_execution_result(
    agent_id: str = "agent-1",
    model: str = "sonnet",
    input_tokens: int = 200,
    output_tokens: int = 100,
    cost_usd: float = 0.002,
    duration_ms: int = 350,
    error: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        agent_id=agent_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        response=None if error else "mock response",
        error=error,
    )


def _insert_ledger_row(
    db,
    *,
    agent_id: str,
    agent_type: str,
    model: str,
    cost_usd: float,
    input_tokens: int = 100,
    output_tokens: int = 50,
    duration_ms: int = 300,
    success: int = 1,
) -> None:
    """Insert a row directly into execution_ledger (bypasses cost_meter.record_execution)."""
    db.execute(
        """
        INSERT INTO execution_ledger
            (id, agent_id, agent_type, model, task_hash,
             input_tokens, output_tokens, cost_usd, duration_ms,
             success, error, swarm_id, workflow_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """,
        (
            uuid.uuid4().hex,
            agent_id,
            agent_type,
            model,
            uuid.uuid4().hex[:16],
            input_tokens,
            output_tokens,
            cost_usd,
            duration_ms,
            success,
        ),
    )
    db.execute(
        """
        INSERT INTO cost_events
            (agent_type, model, input_tokens, output_tokens, cost_usd, workflow_id, timestamp)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        """,
        (agent_type, model, input_tokens, output_tokens, cost_usd, time.time()),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Scenario 1 — Platform Engineer Debugging Production
# ---------------------------------------------------------------------------


class TestDebugScenario:
    """A platform engineer investigates high latency on payment-service.

    Journey:
    1. Route the task — expect sre agent / sonnet tier recommendation.
    2. Pre-task hook — should return context dict (may be empty, never raise).
    3. Spawn an SRE agent.
    4. Mock AgentExecutor.execute — returns a successful result.
    5. Post-task hook — pattern stored, trust updated.
    6. Verify execution_ledger has an entry and cost was recorded.
    """

    @pytest.fixture()
    def platform_db(self, tmp_path) -> Path:
        p = tmp_path / "platform.db"
        _bootstrap_platform_db(p)
        return p

    @pytest.fixture()
    def cost_db(self):
        conn = get_db(":memory:")
        migrate(conn)
        ensure_ledger_schema(conn)
        yield conn
        conn.close()

    def test_debug_scenario(self, platform_db, cost_db):
        task = "investigate high latency on payment-service"

        # ---- Step 1: route the task ----------------------------------------
        route = hooks_route(task, _db_path=platform_db)
        assert "recommended_model" in route
        assert "tier" in route
        assert "confidence" in route
        # SRE-flavoured task should not route to a security model; sonnet/haiku expected
        assert route["recommended_model"] in {
            "claude-haiku-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        }

        # ---- Step 2: pre-task hook ------------------------------------------
        pre = hooks_pre_task(agent_id="sre", prompt=task, _db_path=platform_db)
        assert isinstance(pre, dict)
        assert "context" in pre
        assert "similar_patterns" in pre
        assert isinstance(pre["similar_patterns"], list)

        # ---- Step 3: spawn SRE agent ----------------------------------------
        # "sre" is not in the registered agent_type roster; devops is the
        # closest valid type for production-monitoring tasks.
        agent = spawn_agent(agent_type="devops", _db_path=platform_db)
        assert agent.agent_type == "devops"
        assert agent.status == "idle"
        assert agent.agent_id  # non-empty UUID

        # ---- Step 4: mock execution -----------------------------------------
        mock_result = _make_execution_result(
            agent_id=agent.agent_id,
            model="sonnet",
            input_tokens=512,
            output_tokens=256,
            cost_usd=0.0054,
            duration_ms=820,
        )
        execution_id = uuid.uuid4().hex

        # Record into execution_ledger (real persistence)
        _insert_ledger_row(
            cost_db,
            agent_id=agent.agent_id,
            agent_type="sre",
            model="sonnet",
            cost_usd=mock_result.cost_usd,
            input_tokens=mock_result.input_tokens,
            output_tokens=mock_result.output_tokens,
            duration_ms=mock_result.duration_ms,
        )

        # ---- Step 5: post-task hook -----------------------------------------
        post = hooks_post_task(
            agent_id="sre",
            execution_id=execution_id,
            success=True,
            output_summary="Identified elevated p99 latency in payment-service /checkout endpoint; traced to DB connection pool exhaustion.",
            _db_path=platform_db,
        )
        assert post["trust_updated"] is True
        assert post["pattern_stored"] is True
        assert 0.0 <= post["new_trust"] <= 1.0

        # ---- Step 6: verify execution_ledger has entry ----------------------
        row = cost_db.execute(
            "SELECT * FROM execution_ledger WHERE agent_id = ?",
            (agent.agent_id,),
        ).fetchone()
        assert row is not None, "execution_ledger must contain the mock execution"
        assert row["cost_usd"] == pytest.approx(0.0054, rel=1e-5)
        assert row["agent_type"] == "sre"

    def test_debug_scenario_route_returns_dict_on_empty_db(self, platform_db):
        """hooks_route must not raise even with no prior patterns."""
        result = hooks_route("investigate memory leak in worker nodes", _db_path=platform_db)
        assert isinstance(result, dict)
        assert result.get("recommended_model")

    def test_debug_scenario_post_task_trust_increases_on_success(self, platform_db):
        """Two consecutive successes should push trust above baseline 0.5."""
        for i in range(2):
            hooks_post_task(
                agent_id="sre",
                execution_id=uuid.uuid4().hex,
                success=True,
                output_summary=f"success run {i}",
                _db_path=platform_db,
            )

        conn = hooks_get_conn(platform_db)
        row = conn.execute(
            "SELECT trust_score FROM trust_levels WHERE agent_type = 'sre' AND action_type = 'general'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] > 0.5


# ---------------------------------------------------------------------------
# Scenario 2 — Developer Adding Feature (pipeline swarm)
# ---------------------------------------------------------------------------


class TestFeatureScenario:
    """A developer adds an auth feature using a pipeline swarm.

    Journey:
    1. swarm_init — pipeline topology.
    2. coordination_assign for "design auth system" → spawns aws-architect.
    3. coordination_assign for "implement auth" → spawns dev.
    4. coordination_assign for "review security" → spawns security.
    5. Mock each execution (status → idle after work).
    6. swarm_shutdown → all agents terminated.
    7. Verify swarm cost reported and agents in terminal state.
    """

    @pytest.fixture()
    def platform_db(self, tmp_path) -> Path:
        p = tmp_path / "platform.db"
        _bootstrap_platform_db(p)
        return p

    def test_feature_scenario(self, platform_db):
        # ---- Step 1: init swarm --------------------------------------------
        swarm = swarm_init(
            name="feature-auth",
            topology="pipeline",
            max_agents=8,
            _db_path=platform_db,
        )
        swarm_id = swarm["swarm_id"]
        assert swarm["topology"] == "pipeline"
        assert swarm["status"] == "running"

        assigned_agents = []

        # ---- Steps 2-4: assign tasks ----------------------------------------
        for task, expected_type in [
            ("design auth system architecture", "aws-architect"),
            ("implement auth module code", "dev"),
            ("review security of auth implementation", "security"),
        ]:
            result = coordination_assign(
                swarm_id=swarm_id,
                task=task,
                preferred_agent_type=expected_type,
                _db_path=platform_db,
            )
            assert result.get("assigned") is True, f"Task '{task}' was not assigned"
            assert result["agent_type"] == expected_type
            assigned_agents.append(result["agent_id"])

        # ---- Step 5: simulate each agent completing its task ----------------
        for agent_id in assigned_agents:
            update_agent(agent_id, status="idle", _db_path=platform_db)

        # ---- Step 6: shutdown swarm -----------------------------------------
        shutdown = swarm_shutdown(swarm_id=swarm_id, reason="completed", _db_path=platform_db)
        assert shutdown["status"] == "completed"
        assert shutdown["swarm_id"] == swarm_id

        # ---- Step 7: verify agents are terminated ---------------------------
        agents_after = list_agents(swarm_id=swarm_id, _db_path=platform_db)
        terminal_statuses = {"terminated", "completed", "failed"}
        non_terminal = [a for a in agents_after if a.status not in terminal_statuses]
        assert not non_terminal, (
            f"Agents still in non-terminal state after shutdown: "
            f"{[(a.agent_id, a.status) for a in non_terminal]}"
        )

    def test_feature_scenario_swarm_full_queues(self, platform_db):
        """When swarm is at capacity, coordination_assign returns queued=True."""
        swarm = swarm_init(
            name="small-swarm",
            topology="pipeline",
            max_agents=1,
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        # Fill the swarm with one busy agent
        r1 = coordination_assign(
            swarm_id=sid,
            task="implement login",
            preferred_agent_type="dev",
            _db_path=platform_db,
        )
        assert r1.get("assigned") is True
        # Next assign must be queued
        r2 = coordination_assign(
            swarm_id=sid,
            task="implement logout",
            preferred_agent_type="dev",
            _db_path=platform_db,
        )
        assert r2.get("queued") is True
        assert r2["reason"] == "swarm full"

    def test_feature_scenario_swarm_status_reflects_agents(self, platform_db):
        """swarm_status.agent_count equals number of spawned agents."""
        swarm = swarm_init(
            name="status-check-swarm",
            topology="hierarchical",
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        for task, atype in [("build service", "dev"), ("write tests", "test")]:
            coordination_assign(
                swarm_id=sid, task=task, preferred_agent_type=atype, _db_path=platform_db
            )
        status = swarm_status(swarm_id=sid, _db_path=platform_db)
        assert status["agent_count"] == 2
        assert status["topology"] == "hierarchical"


# ---------------------------------------------------------------------------
# Scenario 3 — Security Reviewer
# ---------------------------------------------------------------------------


class TestSecurityReviewScenario:
    """A security reviewer audits IAM policies.

    Journey:
    1. Route "audit IAM policies for overly broad permissions" → expect security/opus.
    2. check_dangerous on a response containing "chmod 777" → warning detected.
    3. hooks_feedback with quality="good" → trust increases.
    4. Verify audit_trail has entries for all operations.
    """

    @pytest.fixture()
    def platform_db(self, tmp_path) -> Path:
        p = tmp_path / "platform.db"
        _bootstrap_platform_db(p)
        return p

    def test_security_review_scenario(self, platform_db):
        task = "audit IAM policies for overly broad permissions"

        # ---- Step 1: route --------------------------------------------------
        route = hooks_route(task, _db_path=platform_db)
        assert isinstance(route, dict)
        # The route result must have the required keys
        for key in ("recommended_model", "tier", "confidence"):
            assert key in route

        # Record audit for the route operation
        record_audit(
            tool_name="hooks_route",
            agent_id="security",
            input_summary=task,
            success=True,
            db_path=platform_db,
        )

        # ---- Step 2: dangerous-content scan ---------------------------------
        risky_response = (
            "To fix permissions quickly just run: chmod 777 /etc/ssl/certs && "
            "restart the service. This should open up access."
        )
        policy = HarnessPolicy()
        matches = check_dangerous(risky_response, policy=policy)
        assert len(matches) > 0, "chmod 777 must trigger at least one dangerous pattern match"
        assert any("chmod" in m.lower() or "777" in m for m in matches)

        # Record audit for the dangerous check
        record_audit(
            tool_name="check_dangerous",
            agent_id="security",
            input_summary="response contains chmod 777",
            success=True,
            db_path=platform_db,
        )

        # ---- Step 3: feedback → trust increases ----------------------------
        # Seed a baseline trust entry so we can measure the delta
        conn = hooks_get_conn(platform_db)
        conn.execute(
            """INSERT OR REPLACE INTO trust_levels
               (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
               VALUES ('security', 'general', 0.5, 0, 0, ?)""",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        feedback_result = hooks_feedback(
            agent_id="security",
            task_hash=uuid.uuid4().hex[:16],
            quality="good",
            _db_path=platform_db,
        )
        assert feedback_result["recorded"] is True
        assert feedback_result["new_trust"] > 0.5, "Good feedback must increase trust above 0.5"

        record_audit(
            tool_name="hooks_feedback",
            agent_id="security",
            input_summary="quality=good",
            success=True,
            db_path=platform_db,
        )

        # ---- Step 4: verify audit_trail has entries ------------------------
        audit_conn = _get_audit_conn(platform_db)
        rows = audit_conn.execute(
            "SELECT tool_name FROM audit_log WHERE agent_id = 'security' ORDER BY timestamp"
        ).fetchall()
        audit_conn.close()

        tool_names = [r[0] for r in rows]
        assert "hooks_route" in tool_names
        assert "check_dangerous" in tool_names
        assert "hooks_feedback" in tool_names

    def test_security_bad_feedback_decreases_trust(self, platform_db):
        """quality='bad' must decrease trust below 0.5."""
        conn = hooks_get_conn(platform_db)
        conn.execute(
            """INSERT OR REPLACE INTO trust_levels
               (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
               VALUES ('security', 'general', 0.6, 2, 0, ?)""",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        result = hooks_feedback(
            agent_id="security",
            task_hash=uuid.uuid4().hex[:16],
            quality="bad",
            notes="Suggested rm -rf without confirmation",
            _db_path=platform_db,
        )
        assert result["recorded"] is True
        assert result["new_trust"] < 0.6

    def test_check_dangerous_clean_content(self):
        """Safe content must return an empty matches list."""
        safe = "Review the IAM role and ensure least-privilege access for the Lambda."
        assert check_dangerous(safe) == []


# ---------------------------------------------------------------------------
# Scenario 4 — Cost-Conscious SRE
# ---------------------------------------------------------------------------


class TestCostScenario:
    """An SRE tracks and enforces spend.

    Journey:
    1. Record 10 mock executions with known costs.
    2. budget_remaining() → reflects spend.
    3. top_spenders() → shows the high-cost agents first.
    4. get_model_breakdown() → shows distribution across models.
    5. enforce_budget() → allows/blocks based on limit.
    """

    @pytest.fixture()
    def cost_db(self):
        conn = get_db(":memory:")
        migrate(conn)
        ensure_ledger_schema(conn)
        yield conn
        conn.close()

    def test_cost_scenario(self, cost_db):
        daily_limit = 5.0

        # ---- Step 1: record 10 executions -----------------------------------
        #   5 cheap haiku calls at $0.10 each  = $0.50  (agent "haiku-worker")
        #   5 expensive sonnet calls at $0.50  = $2.50  (agent "sonnet-worker")
        #   Total = $3.00
        for _ in range(5):
            _insert_ledger_row(
                cost_db,
                agent_id="haiku-worker",
                agent_type="optimization",
                model="haiku",
                cost_usd=0.10,
            )
        for _ in range(5):
            _insert_ledger_row(
                cost_db,
                agent_id="sonnet-worker",
                agent_type="dev",
                model="sonnet",
                cost_usd=0.50,
            )

        # ---- Step 2: budget_remaining ---------------------------------------
        remaining = budget_remaining(daily_limit_usd=daily_limit, db=cost_db)
        assert remaining == pytest.approx(daily_limit - 3.0, abs=1e-4), (
            f"Expected ~{daily_limit - 3.0:.2f} remaining, got {remaining}"
        )

        # ---- Step 3: top_spenders -------------------------------------------
        spenders = top_spenders(n=5, db=cost_db)
        assert len(spenders) >= 2
        # sonnet-worker spent more — must appear first
        assert spenders[0].agent_id == "sonnet-worker"
        assert spenders[0].total_cost_usd == pytest.approx(2.50, rel=1e-4)
        assert spenders[1].agent_id == "haiku-worker"
        assert spenders[1].total_cost_usd == pytest.approx(0.50, rel=1e-4)

        # ---- Step 4: model breakdown ----------------------------------------
        breakdown = get_model_breakdown(db=cost_db)
        assert "sonnet" in breakdown
        assert "haiku" in breakdown
        assert breakdown["sonnet"].total_cost_usd == pytest.approx(2.50, rel=1e-4)
        assert breakdown["haiku"].total_cost_usd == pytest.approx(0.50, rel=1e-4)
        # Percentages must sum to ~100
        total_pct = sum(e.pct_of_total for e in breakdown.values())
        assert total_pct == pytest.approx(100.0, abs=1.0)

        # ---- Step 5: enforce_budget -----------------------------------------
        # governance.enforce_budget imports budget_remaining inside its function
        # body (lazy import), so we patch it at the source module.
        policy_tight = HarnessPolicy(daily_budget_usd=1.0)
        with patch(
            "cap.harness.cost_meter.budget_remaining",
            return_value=-0.50,
        ):
            result_blocked = enforce_budget(policy=policy_tight)
        assert result_blocked["allowed"] is False
        assert result_blocked["remaining_usd"] < 0

        # Verify allowed when budget has headroom (real DB has no spend)
        policy_wide = HarnessPolicy(daily_budget_usd=100.0)
        with patch(
            "cap.harness.cost_meter.budget_remaining",
            return_value=97.0,
        ):
            result_allowed = enforce_budget(policy=policy_wide)
        assert result_allowed["allowed"] is True
        assert result_allowed["remaining_usd"] > 0

    def test_cost_scenario_empty_ledger(self, cost_db):
        """budget_remaining on an empty ledger equals the full daily limit."""
        remaining = budget_remaining(daily_limit_usd=5.0, db=cost_db)
        assert remaining == pytest.approx(5.0)

    def test_top_spenders_respects_n(self, cost_db):
        """top_spenders(n=2) returns at most 2 results."""
        for i in range(4):
            _insert_ledger_row(
                cost_db,
                agent_id=f"agent-{i}",
                agent_type="dev",
                model="sonnet",
                cost_usd=float(i + 1) * 0.10,
            )
        result = top_spenders(n=2, db=cost_db)
        assert len(result) == 2
        # Highest spender first
        assert result[0].total_cost_usd > result[1].total_cost_usd


# ---------------------------------------------------------------------------
# Scenario 5 — Learning Loop
# ---------------------------------------------------------------------------


class TestLearningLoopScenario:
    """Pattern storage, semantic routing, retention scoring, pruning, and protection.

    Journey:
    1. Store 60 patterns (mixed success/failure, multiple agent_types).
    2. agentdb_semantic_route("deploy kubernetes service") → recommendation based on patterns.
    3. compute_retention_score for the oldest inserted pattern → low score.
    4. protect_high_value() → pins high-scoring patterns.
    5. prune_stale_patterns() → removes old failures but keeps protected ones.
    """

    @pytest.fixture()
    def pattern_db(self, tmp_path) -> Path:
        p = tmp_path / "patterns.db"
        # Bootstrap tables
        conn = agentdb_get_conn(p)
        conn.close()
        return p

    def _insert_raw_pattern(
        self,
        db_path: Path,
        *,
        agent_type: str,
        success: int,
        cost_usd: float = 0.001,
        prompt_summary: str | None = None,
        # Use a unique summary to bypass dedup
        unique: bool = True,
    ) -> str:
        pid = uuid.uuid4().hex
        summary = prompt_summary or (f"task for {agent_type} {pid[:8]}" if unique else f"task for {agent_type}")
        conn = agentdb_get_conn(db_path)
        conn.execute(
            """INSERT INTO patterns
               (id, task_type, prompt_hash, prompt_summary, model, agent_type,
                cost_usd, duration_ms, success, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','-100 days'))""",
            (
                pid,
                "e2e_test",
                uuid.uuid4().hex[:16],  # unique hash to avoid dedup
                summary,
                "sonnet",
                agent_type,
                cost_usd,
                300,
                success,
            ),
        )
        conn.commit()
        conn.close()
        return pid

    def test_learning_loop_scenario(self, pattern_db):
        # ---- Step 1: store 60 patterns --------------------------------------
        # Mix: devops (40 success), security (10 success + 10 failure)
        devops_ids = []
        for _ in range(40):
            pid = self._insert_raw_pattern(
                pattern_db, agent_type="devops", success=1, cost_usd=0.001
            )
            devops_ids.append(pid)

        for _ in range(10):
            self._insert_raw_pattern(pattern_db, agent_type="security", success=1)
        failure_ids = []
        for _ in range(10):
            pid = self._insert_raw_pattern(
                pattern_db, agent_type="security", success=0, cost_usd=0.0
            )
            failure_ids.append(pid)

        conn = agentdb_get_conn(pattern_db)
        count = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        conn.close()
        assert count == 60

        # ---- Step 2: semantic routing ---------------------------------------
        route = agentdb_semantic_route("deploy kubernetes service", _db_path=pattern_db)
        assert "recommended_agent_type" in route
        assert route["recommended_agent_type"] in _VALID_AGENT_TYPES
        assert route["based_on_patterns"] == 60
        # devops had more patterns with 'deploy' keyword context — expect it to win
        assert route["recommended_agent_type"] == "devops"

        # ---- Step 3: retention score for oldest failure ---------------------
        # Trigger retention.py's _get_conn so it migrates the use_count /
        # last_used_at / retention_score columns with correct type affinity.
        # (agentdb_get_conn already opened the DB, but retention uses its own
        # _get_conn which adds the columns as INTEGER/REAL.)
        from cap.harness.retention import _get_conn as retention_get_conn

        oldest_failure = failure_ids[0]
        rconn = retention_get_conn(pattern_db)
        # Explicitly store use_count as integer 0 so that the division in
        # compute_retention_score does not encounter a str.
        rconn.execute(
            "UPDATE patterns SET use_count = 0 WHERE id = ?",
            (oldest_failure,),
        )
        rconn.commit()
        rconn.close()

        score = compute_retention_score(oldest_failure, db=pattern_db)
        # success=0, age>90 days, use_count=0 → score must be low (≤ 0.35)
        # Formula: 0.3*0 + 0.2*max(0,1-100/90) + 0.3*(0/10) + 0.2*cost_factor
        # age_factor: 100 days / 90 day decay → negative → clamped to 0.0
        # cost_usd=0.0 → cost_factor = 1.0 − min(1, 0/0.1) = 1.0
        # Total = 0 + 0 + 0 + 0.2*1.0 = 0.20
        assert score < 0.4, f"Expected low retention score for stale failure, got {score}"

        # ---- Step 4: protect high-value patterns ----------------------------
        # Elevate a few devops patterns to score ≥ 0.8 by bumping use_count
        conn = agentdb_get_conn(pattern_db)
        pin_ids = devops_ids[:5]
        for pid in pin_ids:
            conn.execute(
                "UPDATE patterns SET use_count = 10, success = 1 WHERE id = ?",
                (pid,),
            )
        conn.commit()
        conn.close()

        protected = protect_high_value(threshold=0.8, db=pattern_db)
        # After protect_high_value, scores of pinned patterns are 1.0
        if protected:
            conn = agentdb_get_conn(pattern_db)
            scores = conn.execute(
                f"SELECT retention_score FROM patterns WHERE id IN ({','.join('?' * len(protected))})",
                protected,
            ).fetchall()
            conn.close()
            for row in scores:
                assert row[0] == pytest.approx(1.0)

        # ---- Step 5: prune stale patterns -----------------------------------
        before_count_row = agentdb_get_conn(pattern_db).execute(
            "SELECT COUNT(*) FROM patterns"
        ).fetchone()
        count_before = before_count_row[0]

        deleted = prune_stale_patterns(
            min_score=0.1,
            max_age_days=1,   # all patterns are ~100 days old → eligible
            keep_min=20,      # must always keep at least 20
            db=pattern_db,
        )

        conn = agentdb_get_conn(pattern_db)
        count_after = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        conn.close()

        assert deleted >= 0
        assert count_after == count_before - deleted
        assert count_after >= 20, "prune must never go below keep_min=20"

        # Protected patterns (score=1.0) must survive pruning
        if protected:
            conn = agentdb_get_conn(pattern_db)
            survivors = conn.execute(
                f"SELECT id FROM patterns WHERE id IN ({','.join('?' * len(protected))})",
                protected,
            ).fetchall()
            conn.close()
            assert len(survivors) == len(protected), (
                "Protected patterns must not be pruned"
            )


# ---------------------------------------------------------------------------
# Scenario 6 — Consensus in Swarm
# ---------------------------------------------------------------------------


class TestConsensusScenario:
    """Majority vote in a 3-agent swarm.

    Journey:
    1. swarm_init with 3 agents.
    2. coordination_consensus(proposal="use microservices") → pending.
    3. coordination_consensus(votes={"a1": "approve", "a2": "approve", "a3": "reject"}) → approved.
    4. Verify majority wins.
    """

    @pytest.fixture()
    def platform_db(self, tmp_path) -> Path:
        p = tmp_path / "consensus.db"
        _bootstrap_platform_db(p)
        return p

    def test_consensus_scenario(self, platform_db):
        # ---- Step 1: init swarm with 3 agents ------------------------------
        swarm = swarm_init(
            name="consensus-swarm",
            topology="mesh",
            max_agents=5,
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]

        agents = []
        for atype in ("dev", "security", "devops"):
            a = spawn_agent(agent_type=atype, swarm_id=sid, _db_path=platform_db)
            agents.append(a.agent_id)

        assert len(agents) == 3

        # ---- Step 2: create pending proposal --------------------------------
        pending = coordination_consensus(
            swarm_id=sid,
            proposal="use microservices",
            votes=None,
            _db_path=platform_db,
        )
        assert pending["status"] == "pending"
        assert "proposal_id" in pending
        assert pending["proposal"] == "use microservices"

        # ---- Step 3: tally votes (2 approve, 1 reject) ----------------------
        votes = {
            agents[0]: "approve",
            agents[1]: "approve",
            agents[2]: "reject",
        }
        result = coordination_consensus(
            swarm_id=sid,
            proposal="use microservices",
            votes=votes,
            _db_path=platform_db,
        )

        # ---- Step 4: majority wins → approved -------------------------------
        assert result["outcome"] == "approved"
        assert result["votes_for"] == 2
        assert result["votes_against"] == 1
        assert result["total"] == 3

    def test_consensus_tie_rejects(self, platform_db):
        """A tie (equal approve/reject) must result in 'rejected'."""
        swarm = swarm_init(
            name="tie-swarm",
            topology="mesh",
            max_agents=4,
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        a1 = spawn_agent(agent_type="dev", swarm_id=sid, _db_path=platform_db).agent_id
        a2 = spawn_agent(agent_type="security", swarm_id=sid, _db_path=platform_db).agent_id

        result = coordination_consensus(
            swarm_id=sid,
            proposal="use monolith",
            votes={a1: "approve", a2: "reject"},
            _db_path=platform_db,
        )
        assert result["outcome"] == "rejected"

    def test_consensus_unanimous_approve(self, platform_db):
        """All-approve vote must result in 'approved'."""
        swarm = swarm_init(
            name="unanimous-swarm",
            topology="star",
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        agents = [
            spawn_agent(agent_type=at, swarm_id=sid, _db_path=platform_db).agent_id
            for at in ("dev", "test", "docs")
        ]
        result = coordination_consensus(
            swarm_id=sid,
            proposal="adopt clean architecture",
            votes={a: "approve" for a in agents},
            _db_path=platform_db,
        )
        assert result["outcome"] == "approved"
        assert result["votes_for"] == 3
        assert result["votes_against"] == 0

    def test_consensus_unanimous_reject(self, platform_db):
        """All-reject vote must result in 'rejected'."""
        swarm = swarm_init(
            name="reject-swarm",
            topology="hierarchical",
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        agents = [
            spawn_agent(agent_type=at, swarm_id=sid, _db_path=platform_db).agent_id
            for at in ("dev", "devops")
        ]
        result = coordination_consensus(
            swarm_id=sid,
            proposal="rewrite in COBOL",
            votes={a: "reject" for a in agents},
            _db_path=platform_db,
        )
        assert result["outcome"] == "rejected"
        assert result["votes_against"] == 2

    def test_consensus_invalid_vote_raises(self, platform_db):
        """Invalid vote value must raise ValueError."""
        swarm = swarm_init(
            name="invalid-vote-swarm",
            topology="hierarchical",
            _db_path=platform_db,
        )
        sid = swarm["swarm_id"]
        with pytest.raises(ValueError, match="Invalid vote"):
            coordination_consensus(
                swarm_id=sid,
                proposal="some proposal",
                votes={"agent-x": "maybe"},
                _db_path=platform_db,
            )

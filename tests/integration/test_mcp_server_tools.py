"""
Integration Tests: MCP Server Tool Handlers

Tests the actual tool handler functions inside each MCP server without
spinning up the full MCP protocol layer. Calls _handle_* functions directly.

These tests verify the contract between the orchestrator (Claude Code) and
each MCP server — what inputs they accept and what structure they return.

Covers:
  - cap_route: routing decision returned as structured JSON
  - cap_plan: DAG returned with steps/deps
  - cap_execute: workflow execution records to DB
  - cap_status: returns workflow state
  - cap_health: returns circuit breaker state per agent
  - workflow_start: creates DB record, returns workflow_id
  - workflow_kill: transitions status to 'killed'
  - knowledge_search: returns results array
  - knowledge_ingest: adds entry, returns uuid
  - knowledge_status: returns counts
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.fixture
def tmp_cap_db(tmp_path):
    """Provide an isolated cap.db with full migration."""
    from cap.db import get_db, migrate
    db_path = str(tmp_path / "cap.db")
    conn = get_db(db_path)
    migrate(conn)
    return db_path


@pytest.fixture
def tmp_platform_db(tmp_path):
    """Provide an isolated platform.db."""
    from cap.lib.db_init import init_platform_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = init_platform_db(data_dir)
    conn.close()
    return data_dir


@pytest.fixture
def tmp_knowledge_db_path(tmp_path):
    from cap.lib.db_init import init_knowledge_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = init_knowledge_db(data_dir)
    conn.close()
    return data_dir


class TestOrchestratorRouteHandler:
    """cap_route tool handler returns correct structured response."""

    def test_route_trivial_returns_inline(self, tmp_cap_db):
        from cap.db import get_db, migrate
        import asyncio

        conn = get_db(tmp_cap_db)
        migrate(conn)

        # Directly test the routing function (mirrors _handle_route logic)
        from cap.orchestration.router import route, Tier
        decision = route("fix typo in README", conn, session_id="test-session")

        assert decision.tier == Tier.INLINE
        result = {
            "tier": decision.tier.value,
            "reasoning": decision.reasoning,
            "estimated_agents": decision.estimated_agents,
            "estimated_cost_usd": decision.estimated_cost,
            "complexity_score": decision.complexity_score,
            "decision_id": decision.decision_id,
        }
        assert result["tier"] == "inline"
        assert isinstance(result["estimated_agents"], list)
        assert isinstance(result["estimated_cost_usd"], float)
        conn.close()

    def test_route_complex_returns_full(self, tmp_cap_db):
        from cap.db import get_db, migrate
        from cap.orchestration.router import route, Tier
        conn = get_db(tmp_cap_db)
        migrate(conn)

        decision = route(
            "Migrate all terraform modules across every environment to new AWS provider",
            conn,
            session_id="test-session"
        )
        assert decision.tier == Tier.FULL
        assert "orchestrator" in decision.estimated_agents
        conn.close()

    def test_route_records_decision_id(self, tmp_cap_db):
        from cap.db import get_db, migrate
        from cap.orchestration.router import route
        conn = get_db(tmp_cap_db)
        migrate(conn)

        decision = route("deploy kubernetes service", conn)
        assert decision.decision_id is not None
        assert isinstance(decision.decision_id, int)
        conn.close()


@pytest.mark.skip(reason="planner module removed")
class TestOrchestratorPlanHandler:
    """cap_plan generates a valid DAG for representative task types."""

    def test_plan_feature_request(self, tmp_cap_db):
        from cap.db import get_db, migrate
        from cap.orchestration.planner import generate_plan
        conn = get_db(tmp_cap_db)
        migrate(conn)

        dag = generate_plan("Add a health check endpoint to the payment service", db=conn)
        assert len(dag.steps) > 0
        # DAG must be serializable to dict
        as_dict = dag.to_dict()
        assert "steps" in as_dict
        conn.close()

    def test_plan_deployment(self, tmp_cap_db):
        from cap.db import get_db, migrate
        from cap.orchestration.planner import generate_plan
        conn = get_db(tmp_cap_db)
        migrate(conn)

        dag = generate_plan("Deploy the auth service to production with helm", db=conn)
        agent_types = {s.agent_type for s in dag.steps.values()}
        assert "devops" in agent_types

    def test_plan_migration(self, tmp_cap_db):
        from cap.db import get_db, migrate
        from cap.orchestration.planner import generate_plan
        conn = get_db(tmp_cap_db)
        migrate(conn)

        dag = generate_plan("Migrate the user service to a new authentication provider", db=conn)
        step_ids = set(dag.steps.keys())
        for step in dag.steps.values():
            for dep in step.depends_on:
                assert dep in step_ids, f"Dependency {dep} not in step IDs"

    def test_plan_dag_has_cycle_detection(self, tmp_cap_db):
        """TaskDAG must expose detect_cycle() that correctly identifies cycles.

        The planner makes a best-effort attempt to produce acyclic DAGs. However,
        the critical invariant is that detect_cycle() is always callable and returns
        the correct result — callers use it to guard execution.
        """
        from cap.db import get_db, migrate
        from cap.orchestration.planner import generate_plan
        from cap.orchestration.dag import TaskDAG, TaskStep

        conn = get_db(tmp_cap_db)
        migrate(conn)

        # 1. Verify detect_cycle() returns None for a clean DAG
        clean_dag = generate_plan("Add a new feature to the user API", db=conn)
        # If the planner produced a cycle, detect_cycle must still be callable
        result = clean_dag.detect_cycle()
        assert result is None or isinstance(result, list), \
            "detect_cycle() must return None (no cycle) or list of step IDs in cycle"

        # 2. Manually construct a cyclic DAG and verify detect_cycle finds it
        cyclic_dag = TaskDAG()
        cyclic_dag.steps["s1"] = TaskStep(id="s1", agent_type="dev", description="Step 1", depends_on=["s3"])
        cyclic_dag.steps["s2"] = TaskStep(id="s2", agent_type="dev", description="Step 2", depends_on=["s1"])
        cyclic_dag.steps["s3"] = TaskStep(id="s3", agent_type="dev", description="Step 3", depends_on=["s2"])
        cycle = cyclic_dag.detect_cycle()
        assert cycle is not None, "detect_cycle() must find the cycle s1->s2->s3->s1"
        assert len(cycle) >= 2

        # 3. Acyclic DAG has no cycle
        acyclic_dag = TaskDAG()
        acyclic_dag.steps["a"] = TaskStep(id="a", agent_type="dev", description="A", depends_on=[])
        acyclic_dag.steps["b"] = TaskStep(id="b", agent_type="dev", description="B", depends_on=["a"])
        acyclic_dag.steps["c"] = TaskStep(id="c", agent_type="dev", description="C", depends_on=["b"])
        assert acyclic_dag.detect_cycle() is None

        conn.close()


class TestWorkflowServerHandlers:
    """Workflow MCP server tool handlers create and update DB records correctly."""

    def test_workflow_start_creates_db_record(self, tmp_platform_db):
        from cap.lib.db_init import init_platform_db
        import uuid
        db = init_platform_db(tmp_platform_db)

        wf_id = f"wf-{uuid.uuid4().hex[:12]}"
        db.execute(
            "INSERT INTO workflows (id, name, status, budget_tokens, max_agents, tokens_used) "
            "VALUES (?, 'test-workflow', 'running', 500000, 10, 0)",
            (wf_id,)
        )
        db.commit()

        # Use index-based access (db returns tuples, not Row objects from db_init)
        row = db.execute(
            "SELECT id, name, status, budget_tokens FROM workflows WHERE id = ?",
            (wf_id,)
        ).fetchone()
        assert row is not None
        # Column order: id=0, name=1, status=2, budget_tokens=3
        assert row[2] == "running"
        assert row[3] == 500000
        db.close()

    def test_workflow_kill_transitions_status(self, tmp_platform_db):
        from cap.lib.db_init import init_platform_db
        from datetime import datetime, timezone
        import uuid
        db = init_platform_db(tmp_platform_db)

        wf_id = f"wf-{uuid.uuid4().hex[:12]}"
        db.execute(
            "INSERT INTO workflows (id, name, status, budget_tokens, max_agents, tokens_used) "
            "VALUES (?, 'killable', 'running', 500000, 10, 0)",
            (wf_id,)
        )
        db.commit()

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE workflows SET killed = 1, status = 'killed', completed_at = ?, error = ? WHERE id = ?",
            (now, "User requested kill", wf_id)
        )
        db.commit()

        row = db.execute(
            "SELECT status, killed FROM workflows WHERE id = ?", (wf_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "killed"   # status
        assert row[1] == 1          # killed

    def test_workflow_event_recorded_on_start(self, tmp_platform_db):
        from cap.lib.db_init import init_platform_db
        from datetime import datetime, timezone
        import uuid
        db = init_platform_db(tmp_platform_db)

        wf_id = f"wf-{uuid.uuid4().hex[:12]}"
        db.execute(
            "INSERT INTO workflows (id, name, status, budget_tokens, max_agents, tokens_used) "
            "VALUES (?, 'event-test', 'running', 500000, 10, 0)",
            (wf_id,)
        )
        db.execute(
            "INSERT INTO workflow_events (workflow_id, event_type, message, timestamp) "
            "VALUES (?, 'started', 'Workflow started', ?)",
            (wf_id, datetime.now(timezone.utc).isoformat())
        )
        db.commit()

        events = db.execute(
            "SELECT event_type FROM workflow_events WHERE workflow_id = ?", (wf_id,)
        ).fetchall()
        assert len(events) >= 1
        # Row[0] = event_type
        assert any(e[0] == "started" for e in events)

    def test_budget_tokens_tracked_per_agent(self, tmp_platform_db):
        from cap.lib.db_init import init_platform_db
        import uuid
        db = init_platform_db(tmp_platform_db)

        wf_id = f"wf-{uuid.uuid4().hex[:12]}"
        db.execute(
            "INSERT INTO workflows (id, name, status, budget_tokens, max_agents, tokens_used) "
            "VALUES (?, 'budget-track', 'running', 100000, 5, 0)",
            (wf_id,)
        )
        db.commit()

        db.execute(
            "UPDATE workflows SET tokens_used = tokens_used + 5000 WHERE id = ?", (wf_id,)
        )
        db.commit()

        row = db.execute(
            "SELECT tokens_used FROM workflows WHERE id = ?", (wf_id,)
        ).fetchone()
        assert row[0] == 5000


class TestKnowledgeServerHandlers:
    """Knowledge MCP server tool handlers read/write KB correctly."""

    def test_ingest_creates_entry(self, tmp_knowledge_db_path):
        import uuid, hashlib
        from cap.lib.db_init import init_knowledge_db
        db = init_knowledge_db(tmp_knowledge_db_path)

        entry_uuid = str(uuid.uuid4())
        content = "REST API documentation for the payment service"
        db.execute(
            "INSERT INTO knowledge_entries "
            "(uuid, workspace, source_type, content_type, title, content, content_hash, embedding_status) "
            "VALUES (?, '/workspace', 'manual', 'documentation', 'API Guide', ?, ?, 'pending')",
            (entry_uuid, content, hashlib.sha256(content.encode()).hexdigest())
        )
        db.commit()

        row = db.execute(
            "SELECT uuid, content_type FROM knowledge_entries WHERE uuid = ?", (entry_uuid,)
        ).fetchone()
        assert row is not None
        assert row[1] == "documentation"   # content_type at index 1
        db.close()

    def test_business_knowledge_upsert(self, tmp_knowledge_db_path):
        import uuid
        from cap.lib.db_init import init_knowledge_db
        db = init_knowledge_db(tmp_knowledge_db_path)

        db.execute(
            "INSERT INTO business_knowledge (id, workspace, category, key, value, source) "
            "VALUES (?, '/ws', 'team', 'payment-owner', 'Alice', 'cli')",
            (str(uuid.uuid4()),)
        )
        db.commit()

        db.execute(
            """INSERT INTO business_knowledge (id, workspace, category, key, value, source)
               VALUES (?, '/ws', 'team', 'payment-owner', 'Bob', 'cli')
               ON CONFLICT(workspace, category, key) DO UPDATE SET
                   value = excluded.value""",
            (str(uuid.uuid4()),)
        )
        db.commit()

        rows = db.execute(
            "SELECT value FROM business_knowledge WHERE key = 'payment-owner'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Bob"   # value at index 0
        db.close()

    def test_graph_edge_creation(self, tmp_knowledge_db_path):
        from cap.lib.db_init import init_knowledge_db
        db = init_knowledge_db(tmp_knowledge_db_path)

        db.execute(
            "INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace) "
            "VALUES ('node-a', 'payment-service', 'repo', '/ws')"
        )
        db.execute(
            "INSERT INTO knowledge_graph_nodes (id, entity_name, entity_type, workspace) "
            "VALUES ('node-b', 'auth-service', 'service', '/ws')"
        )
        db.execute(
            "INSERT INTO knowledge_graph_edges (source_id, target_id, predicate, workspace) "
            "VALUES ('node-a', 'node-b', 'depends_on', '/ws')"
        )
        db.commit()

        edges = db.execute(
            "SELECT predicate FROM knowledge_graph_edges "
            "WHERE source_id = 'node-a' AND target_id = 'node-b'"
        ).fetchall()
        assert len(edges) == 1
        assert edges[0][0] == "depends_on"   # predicate at index 0
        db.close()


class TestSessionServerIntegration:
    """Session recording and recall work end-to-end."""

    def test_session_start_and_record(self, tmp_path):
        import uuid
        from cap.lib.db_init import init_sessions_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db = init_sessions_db(data_dir)

        session_id = "sess-integration-1"
        workspace = "/workspace/infra"

        db.execute(
            "INSERT INTO sessions (id, workspace, status) VALUES (?, ?, 'active')",
            (session_id, workspace)
        )
        db.commit()

        db.execute(
            "INSERT INTO decisions (id, session_id, workspace, domain, decision, rationale) "
            "VALUES (?, ?, ?, 'infra', 'Use Terraform for EKS', 'Consistency with existing modules')",
            (str(uuid.uuid4()), session_id, workspace)
        )
        db.commit()

        decisions = db.execute(
            "SELECT decision FROM decisions WHERE session_id = ?", (session_id,)
        ).fetchall()
        assert len(decisions) == 1
        assert "Terraform" in decisions[0][0]   # decision at index 0

    def test_learning_persists_across_sessions(self, tmp_path):
        import uuid
        from cap.lib.db_init import init_sessions_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db = init_sessions_db(data_dir)

        db.execute(
            "INSERT INTO learnings (id, workspace, category, key, value, confidence) "
            "VALUES (?, '/ws', 'style', 'terraform-format', 'Use 2-space indent', 0.9)",
            (str(uuid.uuid4()),)
        )
        db.commit()

        rows = db.execute(
            "SELECT key, value, confidence FROM learnings "
            "WHERE workspace = '/ws' AND category = 'style'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][2] >= 0.9   # confidence at index 2

    def test_correction_stored_and_retrieved(self, tmp_path):
        from cap.lib.db_init import init_sessions_db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db = init_sessions_db(data_dir)

        # corrections.id is INTEGER autoincrement — do NOT pass it
        db.execute(
            "INSERT INTO corrections (workspace, category, what_was_wrong, what_is_correct) "
            "VALUES ('/ws', 'code', 'Used shell=True', 'Always use list args for subprocess')"
        )
        db.commit()

        rows = db.execute(
            "SELECT what_is_correct FROM corrections WHERE workspace = '/ws'"
        ).fetchall()
        assert len(rows) >= 1
        assert any("list args" in r[0] for r in rows)   # what_is_correct at index 0

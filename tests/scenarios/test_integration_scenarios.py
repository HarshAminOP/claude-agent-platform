"""Integration scenario tests — real user workflows end-to-end.

Mocks only external boundaries (Bedrock API, filesystem repos) and tests
the contracts between modules:

  Scenario 1: Fresh Installation
  Scenario 2: Query After Indexing (routing tier classification)
  Scenario 3: Multi-Step Orchestration (full DAG execution)
  Scenario 4: Single-Step Routing (INLINE / LIGHTWEIGHT)
  Scenario 5: Budget Enforcement (cascade skip on budget error)
  Scenario 6: Embedding Fallback (Bedrock AccessDenied → fallback)
  Scenario 7: Agent Communication via Bus (pub/sub message delivery)

All tests use in-memory or tmp_path SQLite — no real Bedrock calls.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.db import get_db, migrate
from cap.orchestration.dag import StepState, TaskDAG, TaskStep
from cap.orchestration.router import Tier, RoutingDecision, route
from cap.lib.agent_bus import AgentBus, BusMessage
from cap.lib.agent_context import SharedState
from cap.lib.coordination_engine import CoordinationEngine, CoordinationResult
from cap.lib.embeddings import EmbeddingClient, EmbeddingConfig
from cap.harness.converse_executor import ConversationResult


# ===========================================================================
# Test infrastructure
# ===========================================================================


class FakeBedrock:
    """Mock boto3 bedrock-runtime client returning canned responses.

    Args:
        responses: dict mapping call index or model_id -> response body dict.
        errors: dict mapping call index or model_id -> botocore ClientError to raise.
    """

    def __init__(
        self,
        responses: Optional[dict] = None,
        errors: Optional[dict] = None,
    ) -> None:
        self._responses = responses or {}
        self._errors = errors or {}
        self._call_count = 0

    def invoke_model(self, **kwargs) -> dict:
        model_id = kwargs.get("modelId", "")
        key = self._call_count
        self._call_count += 1

        # Check errors first by call index, then by model_id
        error = self._errors.get(key) or self._errors.get(model_id)
        if error is not None:
            raise error

        # Return response by call index, then by model_id, then default
        resp_body = (
            self._responses.get(key)
            or self._responses.get(model_id)
            or {"embedding": [0.1] * 1024}
        )
        body_bytes = json.dumps(resp_body).encode()
        return {"body": BytesIO(body_bytes)}


class FakeGitRepo:
    """Context manager that creates a temp git repo with realistic structure.

    Usage::

        with FakeGitRepo("my-service", files={"README.md": "# My Service"}) as repo:
            assert (repo / "README.md").exists()
    """

    def __init__(self, name: str, files: Optional[dict[str, str]] = None) -> None:
        self._name = name
        self._files = files or {
            "README.md": f"# {name}\n\nA service that does things.\n",
            "pyproject.toml": f'[project]\nname = "{name}"\nversion = "0.1.0"\n',
        }
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.TemporaryDirectory(prefix=f"fake-repo-{self._name}-")
        repo_root = Path(self._tmpdir.name) / self._name
        repo_root.mkdir(parents=True)

        # Write files
        for filename, content in self._files.items():
            file_path = repo_root / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        # Initialise git repo with a commit
        _run = lambda args: subprocess.run(
            args, cwd=str(repo_root), capture_output=True, check=False
        )
        _run(["git", "init"])
        _run(["git", "config", "user.email", "test@example.com"])
        _run(["git", "config", "user.name", "Test"])
        _run(["git", "add", "."])
        _run(["git", "commit", "-m", "init"])

        return repo_root

    def __exit__(self, *args) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()


def _mock_executor(
    responses: dict[str, str],
    errors: Optional[dict[str, str]] = None,
) -> MagicMock:
    """Return a mock ConverseExecutor with canned responses per agent_type."""
    call_counts: dict[str, int] = {}

    def execute(agent_id, agent_type, prompt, model=None, max_tokens=8192, context=None):
        call_counts[agent_type] = call_counts.get(agent_type, 0) + 1
        error_str = (errors or {}).get(agent_type)
        response_str = responses.get(agent_type, f"output from {agent_type}")
        return ConversationResult(
            agent_id=agent_id,
            agent_type=agent_type,
            model="haiku",
            response=None if error_str else response_str,
            error=error_str,
            total_input_tokens=10,
            total_output_tokens=20,
            total_cost_usd=0.001,
            duration_ms=50,
            turns=1,
        )

    mock = MagicMock()
    mock.execute.side_effect = execute
    mock._call_counts = call_counts
    return mock


def _make_dag(*specs: tuple) -> TaskDAG:
    """Build a TaskDAG from (step_id, agent_type, depends_on) tuples."""
    dag = TaskDAG()
    for step_id, agent_type, deps in specs:
        dag.steps[step_id] = TaskStep(
            id=step_id,
            description=f"Execute {step_id} via {agent_type}",
            agent_type=agent_type,
            depends_on=deps,
        )
    return dag


def _fresh_db(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    """Create a migrated test DB and return (path_str, connection)."""
    db_path = str(tmp_path / "test_integration.db")
    conn = get_db(db_path)
    migrate(conn)
    return db_path, conn


# ===========================================================================
# Scenario 1: Fresh Installation
# ===========================================================================


class TestFreshInstallation:
    """Scenario 1 — cap init equivalent: detect repos + quick-index workspace."""

    def test_detect_workspace_repos_finds_git_repos(self, tmp_path):
        """_detect_workspace_repos should discover .git repos under CWD."""
        from cap.cli.lifecycle import _detect_workspace_repos

        # Create two fake git repos under tmp_path
        repo_a = tmp_path / "service-a"
        repo_b = tmp_path / "service-b"
        for repo in (repo_a, repo_b):
            repo.mkdir()
            (repo / ".git").mkdir()

        repos = _detect_workspace_repos(tmp_path)

        assert len(repos) >= 2
        repo_names = {r.name for r in repos}
        assert "service-a" in repo_names
        assert "service-b" in repo_names

    def test_detect_workspace_repos_includes_cwd_itself(self, tmp_path):
        """If CWD contains .git, it is included as a repo."""
        from cap.cli.lifecycle import _detect_workspace_repos

        (tmp_path / ".git").mkdir()
        repos = _detect_workspace_repos(tmp_path)

        assert tmp_path in repos

    def test_quick_index_workspace_creates_fts_entries(self, tmp_path):
        """_quick_index_workspace inserts knowledge_entries for README and pyproject.toml."""
        from cap.cli.lifecycle import _quick_index_workspace

        # Stand up the knowledge.db with the expected schema
        knowledge_db_path = tmp_path / "knowledge.db"
        conn = sqlite3.connect(str(knowledge_db_path))
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid         TEXT    NOT NULL UNIQUE,
                workspace    TEXT    NOT NULL,
                source_path  TEXT,
                source_type  TEXT    NOT NULL,
                content_type TEXT    NOT NULL,
                title        TEXT    NOT NULL,
                content      TEXT    NOT NULL,
                content_hash TEXT    NOT NULL,
                metadata     TEXT
            );
        """)
        conn.commit()
        conn.close()

        # Create a fake repo with indexable files
        with FakeGitRepo(
            "monitoring-watcher",
            files={
                "README.md": "# monitoring-watcher\nWatches metrics.\n",
                "pyproject.toml": '[project]\nname = "monitoring-watcher"\n',
            },
        ) as repo_root:
            data_dir = tmp_path
            workspace = tmp_path
            count = _quick_index_workspace(data_dir, workspace, [repo_root])

        assert count >= 1
        # Verify rows are in the DB
        conn = sqlite3.connect(str(knowledge_db_path))
        row_count = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        conn.close()
        assert row_count >= 1

    def test_quick_index_workspace_skips_duplicate_paths(self, tmp_path):
        """Running _quick_index_workspace twice does not double-insert the same path."""
        from cap.cli.lifecycle import _quick_index_workspace

        knowledge_db_path = tmp_path / "knowledge.db"
        conn = sqlite3.connect(str(knowledge_db_path))
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid         TEXT    NOT NULL UNIQUE,
                workspace    TEXT    NOT NULL,
                source_path  TEXT,
                source_type  TEXT    NOT NULL,
                content_type TEXT    NOT NULL,
                title        TEXT    NOT NULL,
                content      TEXT    NOT NULL,
                content_hash TEXT    NOT NULL,
                metadata     TEXT
            );
        """)
        conn.commit()
        conn.close()

        with FakeGitRepo("svc-a", files={"README.md": "# svc-a"}) as repo_root:
            data_dir = tmp_path
            workspace = tmp_path

            count_first = _quick_index_workspace(data_dir, workspace, [repo_root])
            count_second = _quick_index_workspace(data_dir, workspace, [repo_root])

        # Second pass should find everything already indexed
        assert count_second == 0
        # First pass indexed at least one file
        assert count_first >= 1


# ===========================================================================
# Scenario 2: Query After Indexing
# ===========================================================================


class TestQueryAfterIndexing:
    """Scenario 2 — router classifies queries correctly after knowledge is present."""

    @pytest.fixture
    def router_db(self, tmp_path):
        db_path, conn = _fresh_db(tmp_path)
        yield conn
        conn.close()

    def test_complex_query_routes_to_full_tier(self, router_db):
        """A cross-repo migration query should be routed FULL tier."""
        decision = route(
            "Refactor and migrate all services across terraform, kubernetes, and helm to new infra",
            db=router_db,
            session_id="test-session",
        )

        assert isinstance(decision, RoutingDecision)
        assert decision.tier == Tier.FULL
        assert decision.complexity_score > 0.5

    def test_simple_lookup_routes_to_inline_or_lightweight(self, router_db):
        """A simple description lookup should not be FULL tier."""
        decision = route(
            "What does the monitoring-watcher repo do?",
            db=router_db,
            session_id="test-session",
        )

        assert isinstance(decision, RoutingDecision)
        assert decision.tier in (Tier.INLINE, Tier.LIGHTWEIGHT)

    def test_routing_decision_persisted_to_db(self, router_db):
        """route() should write a row to routing_decisions."""
        decision = route(
            "Review the security audit logs",
            db=router_db,
            session_id="s1",
        )

        assert decision.decision_id is not None
        row = router_db.execute(
            "SELECT * FROM routing_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        assert row is not None
        assert row["tier_selected"] in ("inline", "lightweight", "full")
        assert row["session_id"] == "s1"

    def test_routing_decision_contains_reasoning(self, router_db):
        """RoutingDecision.reasoning must be a non-empty string."""
        decision = route(
            "Deploy the new terraform module to kubernetes cluster",
            db=router_db,
        )

        assert isinstance(decision.reasoning, str)
        assert len(decision.reasoning) > 0
        assert decision.estimated_cost >= 0.0


# ===========================================================================
# Scenario 3: Multi-Step Orchestration
# ===========================================================================


class TestMultiStepOrchestration:
    """Scenario 3 — TaskDAG with 3 steps, steps 2+3 depend on step 1."""

    @pytest.fixture
    def db_path(self, tmp_path):
        path = str(tmp_path / "orch.db")
        conn = get_db(path)
        migrate(conn)
        conn.close()
        return path

    @pytest.mark.asyncio
    async def test_steps_execute_in_dependency_order(self, db_path, tmp_path):
        """Steps 2 and 3 must not start until step 1 completes."""
        execution_order: list[str] = []

        def execute(agent_id, agent_type, prompt, model=None, max_tokens=8192, context=None):
            execution_order.append(agent_type)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model="haiku",
                response=f"done: {agent_type}",
                error=None,
                total_input_tokens=5,
                total_output_tokens=10,
                total_cost_usd=0.001,
                duration_ms=20,
                turns=1,
            )

        executor = MagicMock()
        executor.execute.side_effect = execute

        shared = SharedState(session_id="orch-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        plan = _make_dag(
            ("step-1", "dev", []),
            ("step-2", "security", ["step-1"]),
            ("step-3", "sre", ["step-1"]),
        )

        result = await engine.execute_plan(plan, workspace=str(tmp_path))

        assert result.status == "completed"
        assert len(result.completed_steps) == 3
        # step-1 must appear before step-2 and step-3
        assert execution_order.index("dev") < execution_order.index("security")
        assert execution_order.index("dev") < execution_order.index("sre")

    @pytest.mark.asyncio
    async def test_dependent_steps_receive_context_from_step1(self, db_path, tmp_path):
        """Step 2 and 3 should have context injected from step 1's output."""
        received_contexts: dict[str, Optional[str]] = {}

        def execute(agent_id, agent_type, prompt, model=None, max_tokens=8192, context=None):
            received_contexts[agent_type] = context
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model="haiku",
                response=f"output from {agent_type}: findings here",
                error=None,
                total_input_tokens=5,
                total_output_tokens=10,
                total_cost_usd=0.001,
                duration_ms=20,
                turns=1,
            )

        executor = MagicMock()
        executor.execute.side_effect = execute

        shared = SharedState(session_id="ctx-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=2, db_path=db_path
        )

        plan = _make_dag(
            ("step-1", "dev", []),
            ("step-2", "security", ["step-1"]),
            ("step-3", "sre", ["step-1"]),
        )

        await engine.execute_plan(plan, workspace=str(tmp_path))

        # step-1 has no dependencies, so context should be empty or None
        assert not received_contexts.get("dev")
        # step-2 and step-3 should have received context from step-1's output
        assert received_contexts.get("security") is not None
        assert "dev" in received_contexts["security"]

    @pytest.mark.asyncio
    async def test_shared_state_updated_after_each_step(self, db_path, tmp_path):
        """CoordinationEngine writes findings.{agent_type} into SharedState."""
        executor = _mock_executor(
            {"dev": "dev output", "security": "sec output", "sre": "sre output"}
        )
        shared = SharedState(session_id="ss-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        plan = _make_dag(
            ("step-1", "dev", []),
            ("step-2", "security", ["step-1"]),
            ("step-3", "sre", ["step-1"]),
        )

        await engine.execute_plan(plan, workspace=str(tmp_path))

        # After execution, SharedState should hold findings for each agent type
        findings_dev = await shared.get("findings.dev")
        findings_sec = await shared.get("findings.security")
        assert findings_dev is not None
        assert findings_sec is not None
        assert findings_dev["status"] == "completed"

    @pytest.mark.asyncio
    async def test_final_response_synthesises_all_outputs(self, db_path, tmp_path):
        """CoordinationResult.final_response must mention all agent types."""
        executor = _mock_executor(
            {
                "dev": "here is the implementation plan",
                "security": "no vulnerabilities found",
                "sre": "monitoring checks passed",
            }
        )
        shared = SharedState(session_id="synth-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        plan = _make_dag(
            ("step-1", "dev", []),
            ("step-2", "security", ["step-1"]),
            ("step-3", "sre", ["step-1"]),
        )

        result = await engine.execute_plan(plan, workspace=str(tmp_path))

        assert result.final_response is not None
        response_lower = result.final_response.lower()
        assert "dev" in response_lower
        assert "security" in response_lower
        assert "sre" in response_lower


# ===========================================================================
# Scenario 4: Single-Step Routing
# ===========================================================================


class TestSingleStepRouting:
    """Scenario 4 — simple task routes to INLINE or LIGHTWEIGHT, not FULL."""

    @pytest.fixture
    def router_db(self, tmp_path):
        db_path, conn = _fresh_db(tmp_path)
        yield conn
        conn.close()

    def test_simple_description_query_not_full_tier(self, router_db):
        """'What does X repo do?' should not trigger FULL orchestration."""
        decision = route(
            "What does the monitoring-watcher repo do?",
            db=router_db,
        )

        assert decision.tier != Tier.FULL

    def test_simple_query_has_short_agent_list(self, router_db):
        """INLINE or LIGHTWEIGHT should have 0–2 estimated agents."""
        decision = route(
            "What does the monitoring-watcher repo do?",
            db=router_db,
        )

        assert len(decision.estimated_agents) <= 2

    def test_typo_fix_routes_inline(self, router_db):
        """A trivial fix typo task must score low enough for INLINE."""
        decision = route(
            "fix typo in the README file",
            db=router_db,
        )

        assert decision.tier == Tier.INLINE
        assert decision.estimated_agents == []
        assert decision.estimated_cost == 0.0

    def test_security_audit_routes_lightweight_or_full(self, router_db):
        """A task touching review + refactor signals must reach LIGHTWEIGHT or FULL.

        review_keywords (0.15) + refactor_keywords (0.20) = 0.35, which is
        above the INLINE ceiling (0.2) and below the FULL floor (0.5),
        so the tier must be LIGHTWEIGHT.
        """
        decision = route(
            "Review the authentication module and refactor for security issues",
            db=router_db,
        )

        # review (0.15) + refactor (0.20) = 0.35, above INLINE threshold (0.2)
        assert decision.tier in (Tier.LIGHTWEIGHT, Tier.FULL)
        assert len(decision.estimated_agents) >= 1


# ===========================================================================
# Scenario 5: Budget Enforcement
# ===========================================================================


class TestBudgetEnforcement:
    """Scenario 5 — step 2 returns budget error, steps 3+4 must be SKIPPED."""

    @pytest.fixture
    def db_path(self, tmp_path):
        path = str(tmp_path / "budget_test.db")
        conn = get_db(path)
        migrate(conn)
        conn.close()
        return path

    @pytest.mark.asyncio
    async def test_downstream_steps_skipped_on_budget_error(self, db_path, tmp_path):
        """Steps 3 and 4 must be SKIPPED when step 2 reports budget exceeded."""
        executor = _mock_executor(
            responses={"dev": "step 1 done"},
            errors={"security": "budget exceeded — daily limit reached"},
        )
        shared = SharedState(session_id="budget-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        # 4-step plan: step-1 -> step-2, step-1 -> step-3, step-2 -> step-4
        plan = _make_dag(
            ("step-1", "dev", []),
            ("step-2", "security", ["step-1"]),
            ("step-3", "sre", ["step-1"]),
            ("step-4", "docs", ["step-2"]),
        )

        result = await engine.execute_plan(plan, workspace=str(tmp_path))

        # Overall status must not be "completed"
        assert result.status != "completed"
        # step-1 should have completed
        completed_ids = {s.step_id for s in result.completed_steps}
        assert "step-1" in completed_ids
        # step-2 failed
        failed_ids = {s.step_id for s in result.failed_steps}
        assert "step-2" in failed_ids
        # step-4 must be skipped (depends on failed step-2)
        skipped_ids = {s.step_id for s in result.steps if s.status == "skipped"}
        assert "step-4" in skipped_ids

    @pytest.mark.asyncio
    async def test_budget_error_recorded_in_result_errors(self, db_path, tmp_path):
        """CoordinationResult.errors must include the budget error message."""
        executor = _mock_executor(
            responses={"dev": "ok"},
            errors={"security": "budget paused — per-agent cap exceeded"},
        )
        shared = SharedState(session_id="budget-err-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        plan = _make_dag(
            ("s1", "dev", []),
            ("s2", "security", ["s1"]),
            ("s3", "sre", ["s2"]),
        )

        result = await engine.execute_plan(plan, workspace=str(tmp_path))

        assert len(result.errors) > 0
        errors_combined = " ".join(result.errors).lower()
        assert "budget" in errors_combined or "security" in errors_combined

    @pytest.mark.asyncio
    async def test_steps_before_budget_error_complete_normally(self, db_path, tmp_path):
        """Step 1 runs and completes even though step 2 later hits budget."""
        executor = _mock_executor(
            responses={"dev": "analysis complete"},
            errors={"security": "daily budget exceeded"},
        )
        shared = SharedState(session_id="pre-budget-test", db_path=db_path)
        engine = CoordinationEngine(
            executor=executor, shared=shared, max_parallel=3, db_path=db_path
        )

        plan = _make_dag(
            ("s1", "dev", []),
            ("s2", "security", ["s1"]),
        )

        result = await engine.execute_plan(plan, workspace=str(tmp_path))

        completed_ids = {s.step_id for s in result.completed_steps}
        assert "s1" in completed_ids
        # s1's response must appear in final_response
        assert result.final_response is not None
        assert "analysis complete" in result.final_response


# ===========================================================================
# Scenario 6: Embedding Fallback
# ===========================================================================


class TestEmbeddingFallback:
    """Scenario 6 — Bedrock AccessDenied causes _available=False, fallback used."""

    @pytest.mark.asyncio
    async def test_access_denied_sets_available_false(self):
        """ClientError(AccessDeniedException) must flip _available to False."""
        from botocore.exceptions import ClientError

        access_denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "InvokeModel",
        )

        config = EmbeddingConfig(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")
        client = EmbeddingClient(config=config)

        # Inject the fake bedrock that raises on first call
        fake_bedrock = FakeBedrock(errors={0: access_denied})
        client._client = fake_bedrock

        # Mock _fallback_embed so we don't need sentence-transformers installed
        fake_vector = [0.5] * 384
        with patch.object(client, "_fallback_embed", return_value=fake_vector) as mock_fallback:
            result = await client.embed_single("hello world")

        assert client._available is False
        mock_fallback.assert_called_once()
        assert result == fake_vector

    @pytest.mark.asyncio
    async def test_subsequent_calls_skip_bedrock_use_fallback(self):
        """After _available=False, embed_single goes straight to _fallback_embed."""
        config = EmbeddingConfig(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")
        client = EmbeddingClient(config=config)
        client._available = False  # Simulate prior failure

        fake_vector = [0.1] * 384
        with patch.object(client, "_fallback_embed", return_value=fake_vector) as mock_fallback:
            result = await client.embed_single("some text")

        mock_fallback.assert_called_once_with("some text")
        assert result == fake_vector

    @pytest.mark.asyncio
    async def test_empty_text_returns_none_without_bedrock_call(self):
        """Empty input must return None immediately, not hit Bedrock."""
        config = EmbeddingConfig(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")
        client = EmbeddingClient(config=config)

        fake_bedrock = FakeBedrock()
        client._client = fake_bedrock

        result = await client.embed_single("   ")

        assert result is None
        # No Bedrock calls should have been made
        assert fake_bedrock._call_count == 0

    @pytest.mark.asyncio
    async def test_successful_embed_sets_available_true(self):
        """A successful Bedrock call must set _available=True."""
        embedding_body = {"embedding": [0.2] * 1024}
        fake_bedrock = FakeBedrock(responses={0: embedding_body})

        config = EmbeddingConfig(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")
        client = EmbeddingClient(config=config)
        client._client = fake_bedrock

        result = await client.embed_single("Hello there")

        assert client._available is True
        assert result == [0.2] * 1024
        assert len(result) == 1024


# ===========================================================================
# Scenario 7: Agent Communication via Bus
# ===========================================================================


class TestAgentCommunicationViaBus:
    """Scenario 7 — two agents communicate through AgentBus pub/sub."""

    @pytest.fixture
    def bus_db_path(self, tmp_path):
        return str(tmp_path / "bus_test.db")

    @pytest.mark.asyncio
    async def test_published_message_reaches_subscriber(self, bus_db_path):
        """security-1 subscribes to findings.*, dev-1 publishes to findings.dev."""
        bus = AgentBus(session_id="comm-test", db_path=bus_db_path)

        await bus.subscribe("security-1", "findings.*")

        vuln_payload = {
            "severity": "high",
            "cve": "CVE-2024-1234",
            "component": "auth-service",
            "description": "Unauthenticated endpoint exposed",
        }
        await bus.publish("dev-1", "dev", "findings.dev", vuln_payload)

        messages = await bus.get_messages("security-1")

        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, BusMessage)
        assert msg.topic == "findings.dev"
        assert msg.sender == "dev-1"
        assert msg.sender_type == "dev"
        assert msg.payload["cve"] == "CVE-2024-1234"
        assert msg.payload["severity"] == "high"

    @pytest.mark.asyncio
    async def test_non_matching_topic_not_delivered(self, bus_db_path):
        """security-1 subscribed to findings.* should not get status.dev messages."""
        bus = AgentBus(session_id="filter-test", db_path=bus_db_path)

        await bus.subscribe("security-1", "findings.*")

        # Publish to a topic that does NOT match findings.*
        await bus.publish("dev-1", "dev", "status.dev.step-1", {"status": "running"})

        messages = await bus.get_messages("security-1")

        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive_message(self, bus_db_path):
        """Both security-1 and audit-1 subscribed to findings.* should each get the message."""
        bus = AgentBus(session_id="multi-sub-test", db_path=bus_db_path)

        await bus.subscribe("security-1", "findings.*")
        await bus.subscribe("audit-1", "findings.*")

        payload = {"issue": "leaked secret in logs", "file": "app.py"}
        await bus.publish("sre-1", "sre", "findings.sre", payload)

        sec_msgs = await bus.get_messages("security-1")
        audit_msgs = await bus.get_messages("audit-1")

        assert len(sec_msgs) == 1
        assert len(audit_msgs) == 1
        assert sec_msgs[0].payload == audit_msgs[0].payload

    @pytest.mark.asyncio
    async def test_bus_message_count_increments(self, bus_db_path):
        """message_count should increment with each publish."""
        bus = AgentBus(session_id="count-test", db_path=bus_db_path)

        await bus.subscribe("a-1", "notifications.*")
        assert bus.message_count == 0

        await bus.publish("b-1", "b", "notifications.build", {"build": "passed"})
        await bus.publish("b-1", "b", "notifications.deploy", {"env": "staging"})

        assert bus.message_count == 2

    @pytest.mark.asyncio
    async def test_bus_drain_clears_queues(self, bus_db_path):
        """After drain(), the subscriber queue should be empty."""
        bus = AgentBus(session_id="drain-test", db_path=bus_db_path)

        await bus.subscribe("worker-1", "jobs.*")
        await bus.publish("dispatcher-1", "dispatcher", "jobs.process", {"task": "render"})

        # Drain without reading first
        await bus.drain()

        # Queue should be empty after drain
        msgs = await bus.get_messages("worker-1")
        assert len(msgs) == 0

"""End-to-end integration test for cap_orchestrate pipeline.

Tests the full flow: route -> execute -> cost recording.
Requires AWS credentials and network access.

Run: pytest tests/integration/test_orchestrate_e2e.py -v
"""

import os
import pytest
from unittest.mock import patch, MagicMock

# Skip entire module if no AWS profile is set
pytestmark = pytest.mark.skipif(
    not os.environ.get("AWS_PROFILE"),
    reason="AWS_PROFILE not set — skipping integration tests",
)


class TestOrchestrateE2E:
    """Integration tests for the cap_orchestrate pipeline."""

    def test_route_returns_valid_tier(self):
        """cap_route should return a valid tier and agent recommendation."""
        from cap.orchestration.router import route, Tier
        from cap.db import get_db, migrate

        db = get_db(":memory:")
        migrate(db)

        decision = route("Write a Python function that sorts a list", db)
        assert decision.tier in (Tier.INLINE, Tier.LIGHTWEIGHT, Tier.FULL)
        assert isinstance(decision.estimated_agents, list)
        assert decision.complexity_score >= 0

    def test_executor_model_resolution(self):
        """Model aliases should resolve to region-prefixed Bedrock IDs."""
        from cap.harness.executor import _resolve_model, MODEL_ALIASES

        assert "anthropic.claude-haiku" in MODEL_ALIASES["haiku"]
        assert "anthropic.claude-sonnet" in MODEL_ALIASES["sonnet"]
        assert "anthropic.claude-opus" in MODEL_ALIASES["opus"]

        assert _resolve_model("haiku") == MODEL_ALIASES["haiku"]
        assert _resolve_model("sonnet") == MODEL_ALIASES["sonnet"]
        assert _resolve_model("opus") == MODEL_ALIASES["opus"]
        assert _resolve_model(None) == MODEL_ALIASES["sonnet"]

        # Pass-through for full IDs
        full_id = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
        assert _resolve_model(full_id) == full_id

    def test_agent_store_supports_all_types(self):
        """Agent store should support all required agent types."""
        from cap.harness.agent_store import _VALID_AGENT_TYPES

        required = {
            "dev", "devops", "security", "sre", "code-review",
            "test", "docs", "explore", "aws-architect", "optimization", "cicd",
        }
        missing = required - _VALID_AGENT_TYPES
        assert not missing, f"Missing agent types: {missing}"

    def test_agent_prompt_loading(self):
        """All agent types should have loadable system prompts."""
        from cap.harness.converse_executor import load_agent_system_prompt

        required_agents = [
            "dev", "devops", "security", "sre",
            "code-review", "test", "docs", "explore",
        ]
        for agent_type in required_agents:
            prompt = load_agent_system_prompt(agent_type)
            assert prompt is not None, f"No system prompt for agent: {agent_type}"
            assert len(prompt) > 100, f"System prompt too short for agent: {agent_type}"

    def test_converse_executor_init(self):
        """ConverseExecutor should initialize with config from environment."""
        from cap.harness.converse_executor import ConverseExecutor

        executor = ConverseExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region=os.environ.get("AWS_DEFAULT_REGION", "eu-central-1"),
        )
        assert executor._region == "eu-central-1"

    @pytest.mark.skipif(
        not os.environ.get("CAP_RUN_LIVE_TESTS"),
        reason="Set CAP_RUN_LIVE_TESTS=1 to run live Bedrock calls",
    )
    def test_live_execute_haiku(self):
        """Live test: execute a simple task with haiku model."""
        from cap.harness.converse_executor import ConverseExecutor

        executor = ConverseExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region="eu-central-1",
            budget_limit_usd=1.0,
        )

        result = executor.execute(
            agent_id="test-live-001",
            agent_type="dev",
            prompt="Return the single word 'hello'. Nothing else.",
            model="haiku",
            max_tokens=50,
            temperature=0.0,
        )

        assert result.error is None, f"Execution failed: {result.error}"
        assert result.response is not None
        assert "hello" in result.response.lower()
        assert result.total_input_tokens > 0
        assert result.total_output_tokens > 0
        assert result.total_cost_usd > 0

    @pytest.mark.skipif(
        not os.environ.get("CAP_RUN_LIVE_TESTS"),
        reason="Set CAP_RUN_LIVE_TESTS=1 to run live Bedrock calls",
    )
    def test_live_orchestrate_routes_correctly(self):
        """Live test: cap_orchestrate routes and executes correctly."""
        from cap.harness.hooks import hooks_route
        from cap.harness.converse_executor import ConverseExecutor

        routing = hooks_route("Write a unit test for a sorting function")
        assert "recommended_model" in routing
        assert "tier" in routing

        executor = ConverseExecutor(
            profile=os.environ.get("AWS_PROFILE"),
            region="eu-central-1",
            budget_limit_usd=1.0,
        )

        result = executor.execute(
            agent_id="test-orchestrate-001",
            agent_type="dev",
            prompt="Return only the word 'routed'. Nothing else.",
            model="haiku",
            max_tokens=50,
            temperature=0.0,
        )

        assert result.error is None, f"Execution failed: {result.error}"
        assert result.response is not None
        assert result.total_cost_usd >= 0

    def test_cost_meter_records(self):
        """Cost meter should record execution results."""
        from cap.harness.executor import ExecutionResult
        from cap.harness.cost_meter import record_execution, budget_remaining
        from cap.db import get_db, migrate
        from datetime import datetime, timezone

        db = get_db(":memory:")
        migrate(db)

        fake_result = ExecutionResult(
            agent_id="test-cost-001",
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0003,
            duration_ms=500,
            response="test",
            error=None,
            timestamp=datetime.now(timezone.utc),
        )

        entry_id = record_execution(fake_result, agent_type="dev", db=db)
        assert entry_id is not None

        remaining = budget_remaining(daily_limit_usd=5.0, db=db)
        assert remaining < 5.0

    def test_config_loaded(self):
        """Config should be loadable from .harness/config.json."""
        from cap.harness.executor import _CONFIG

        # If config was loaded, verify the expected structure
        if _CONFIG:
            assert "models" in _CONFIG or "aws" in _CONFIG

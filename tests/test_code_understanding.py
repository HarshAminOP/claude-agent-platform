"""Unit tests for cap.lib.code_understanding.

All Bedrock calls are mocked via unittest.mock. No AWS credentials are
required to run the test suite.
"""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cap.lib.code_understanding import (
    BudgetExceeded,
    CodeUnderstanding,
    ModuleUnderstanding,
    RepoUnderstanding,
    ServiceInteraction,
    UnderstandingConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bedrock_response(text: str) -> dict:
    """Build a boto3-style invoke_model response containing ``text``."""
    body_bytes = json.dumps({"content": [{"text": text}]}).encode()
    return {"body": BytesIO(body_bytes)}


def _make_client(response_text: str) -> MagicMock:
    """Return a mock boto3 bedrock-runtime client."""
    client = MagicMock()
    client.invoke_model.return_value = _make_bedrock_response(response_text)
    return client


def _default_config() -> UnderstandingConfig:
    return UnderstandingConfig(
        max_retries=1,
        budget_limit_usd=10.0,
        max_concurrent=2,
    )


def _client_with_config(
    response_text: str = "{}",
    config: UnderstandingConfig | None = None,
) -> CodeUnderstanding:
    cfg = config or _default_config()
    cu = CodeUnderstanding(config=cfg)
    cu._client = _make_client(response_text)
    cu._available = True
    return cu


# ---------------------------------------------------------------------------
# UnderstandingConfig defaults
# ---------------------------------------------------------------------------


class TestUnderstandingConfig:
    def test_default_analysis_model(self) -> None:
        cfg = UnderstandingConfig()
        assert "haiku" in cfg.analysis_model.lower()

    def test_default_complex_model(self) -> None:
        cfg = UnderstandingConfig()
        assert "sonnet" in cfg.complex_model.lower()

    def test_default_budget(self) -> None:
        cfg = UnderstandingConfig()
        assert cfg.budget_limit_usd == pytest.approx(2.0)

    def test_custom_overrides(self) -> None:
        cfg = UnderstandingConfig(max_concurrent=5, budget_limit_usd=1.0)
        assert cfg.max_concurrent == 5
        assert cfg.budget_limit_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# BudgetExceeded
# ---------------------------------------------------------------------------


class TestBudgetExceeded:
    def test_is_exception(self) -> None:
        exc = BudgetExceeded("over budget")
        assert isinstance(exc, Exception)
        assert "over budget" in str(exc)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TestRepoUnderstanding:
    def test_required_fields(self) -> None:
        ru = RepoUnderstanding(
            name="my-service",
            summary="A service.",
            architectural_pattern="request-response",
            domain="application",
            complexity=3,
        )
        assert ru.name == "my-service"
        assert ru.exposes == []
        assert ru.consumes == []
        assert ru.tags == []
        assert ru.confidence == pytest.approx(0.0)
        assert ru.cost_usd == pytest.approx(0.0)


class TestModuleUnderstanding:
    def test_required_fields(self) -> None:
        mu = ModuleUnderstanding(name="vpc", module_type="terraform", summary="VPC module.")
        assert mu.provisions == []
        assert mu.consumed_by == []
        assert mu.inputs == []
        assert mu.outputs == []
        assert mu.tags == []


class TestServiceInteraction:
    def test_required_fields(self) -> None:
        si = ServiceInteraction(
            source="a", target="b", protocol="http", pattern="sync", description="REST call."
        )
        assert si.source == "a"
        assert si.target == "b"
        assert si.confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_direct_json_object(self) -> None:
        data = CodeUnderstanding._extract_json('{"key": "value"}')
        assert data == {"key": "value"}

    def test_direct_json_array(self) -> None:
        data = CodeUnderstanding._extract_json('[{"a": 1}]')
        assert isinstance(data, list)
        assert data[0]["a"] == 1

    def test_markdown_fenced_json(self) -> None:
        text = "Here is the result:\n```json\n{\"x\": 42}\n```\nDone."
        data = CodeUnderstanding._extract_json(text)
        assert data == {"x": 42}

    def test_embedded_json_object(self) -> None:
        text = "Analysis: { \"summary\": \"test\", \"complexity\": 2 } end."
        data = CodeUnderstanding._extract_json(text)
        assert data is not None
        assert data["complexity"] == 2

    def test_invalid_json_returns_none(self) -> None:
        data = CodeUnderstanding._extract_json("not json at all")
        assert data is None

    def test_empty_string_returns_none(self) -> None:
        data = CodeUnderstanding._extract_json("")
        assert data is None


# ---------------------------------------------------------------------------
# _regex_extract_repo
# ---------------------------------------------------------------------------


class TestRegexExtractRepo:
    def test_extracts_architectural_pattern(self) -> None:
        text = "This is an event-driven system with queues."
        data = CodeUnderstanding._regex_extract_repo(text)
        assert data.get("architectural_pattern") == "event-driven"

    def test_extracts_domain(self) -> None:
        text = "Domain: infrastructure — manages VPCs and subnets."
        data = CodeUnderstanding._regex_extract_repo(text)
        assert data.get("domain") == "infrastructure"

    def test_extracts_complexity(self) -> None:
        text = 'complexity: 4, other fields...'
        data = CodeUnderstanding._regex_extract_repo(text)
        assert data.get("complexity") == 4

    def test_returns_empty_dict_for_unrecognised_text(self) -> None:
        data = CodeUnderstanding._regex_extract_repo("absolutely nothing useful here xyz123")
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_haiku_model_uses_haiku_rates(self) -> None:
        cfg = UnderstandingConfig(
            cost_per_1k_input_haiku=0.001,
            cost_per_1k_output_haiku=0.005,
        )
        cu = CodeUnderstanding(config=cfg)
        # 4000 chars ~ 1000 tokens input, 400 chars ~ 100 tokens output
        cost = cu._estimate_cost("A" * 4000, "B" * 400, "us.anthropic.claude-haiku-4-5-v1")
        expected = (1000 * 0.001 / 1000) + (100 * 0.005 / 1000)
        assert cost == pytest.approx(expected, rel=1e-3)

    def test_sonnet_model_uses_sonnet_rates(self) -> None:
        cfg = UnderstandingConfig(
            cost_per_1k_input_sonnet=0.003,
            cost_per_1k_output_sonnet=0.015,
        )
        cu = CodeUnderstanding(config=cfg)
        cost = cu._estimate_cost("A" * 4000, "B" * 400, "us.anthropic.claude-sonnet-4-6-v1")
        expected = (1000 * 0.003 / 1000) + (100 * 0.015 / 1000)
        assert cost == pytest.approx(expected, rel=1e-3)

    def test_zero_length_inputs_give_zero_cost(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        assert cu._estimate_cost("", "", "haiku") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# analyze_repo — happy path
# ---------------------------------------------------------------------------


class TestAnalyzeRepo:
    @pytest.fixture()
    def good_response(self) -> str:
        return json.dumps({
            "summary": "An event-driven notification service.",
            "architectural_pattern": "event-driven",
            "domain": "application",
            "complexity": 3,
            "exposes": ["SNS topic: notifications"],
            "consumes": ["DynamoDB: user-table"],
            "tags": ["notifications", "async"],
            "confidence": 0.9,
        })

    @pytest.mark.asyncio
    async def test_returns_repo_understanding(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        result = await cu.analyze_repo({"name": "notif-service", "purpose": "Send notifications"})
        assert isinstance(result, RepoUnderstanding)
        assert result.name == "notif-service"
        assert result.architectural_pattern == "event-driven"
        assert result.domain == "application"
        assert result.complexity == 3
        assert "notifications" in result.tags
        assert result.confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_exposes_and_consumes_populated(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        result = await cu.analyze_repo({"name": "notif-service"})
        assert len(result.exposes) == 1
        assert len(result.consumes) == 1

    @pytest.mark.asyncio
    async def test_model_used_set_to_analysis_model(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        result = await cu.analyze_repo({"name": "svc"})
        assert result.model_used == cu.config.analysis_model

    @pytest.mark.asyncio
    async def test_fallback_on_bedrock_error(self) -> None:
        """When Bedrock raises unexpectedly, a fallback RepoUnderstanding is returned."""
        cu = CodeUnderstanding(config=_default_config())
        cu._available = True
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = Exception("network error")
        result = await cu.analyze_repo({"name": "broken-svc"})
        assert isinstance(result, RepoUnderstanding)
        assert result.name == "broken-svc"
        assert result.architectural_pattern == "unknown"

    @pytest.mark.asyncio
    async def test_complexity_clamped_to_1_5(self) -> None:
        response = json.dumps({
            "summary": "x", "architectural_pattern": "batch",
            "domain": "data", "complexity": 99,
            "exposes": [], "consumes": [], "tags": [], "confidence": 0.5,
        })
        cu = _client_with_config(response)
        result = await cu.analyze_repo({"name": "svc"})
        assert result.complexity == 5

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_0_1(self) -> None:
        response = json.dumps({
            "summary": "x", "architectural_pattern": "batch",
            "domain": "data", "complexity": 2,
            "exposes": [], "consumes": [], "tags": [], "confidence": 5.0,
        })
        cu = _client_with_config(response)
        result = await cu.analyze_repo({"name": "svc"})
        assert result.confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# analyze_module — happy path
# ---------------------------------------------------------------------------


class TestAnalyzeModule:
    @pytest.fixture()
    def good_response(self) -> str:
        return json.dumps({
            "summary": "Creates an RDS cluster with Multi-AZ.",
            "provisions": ["RDS cluster", "security group"],
            "consumed_by": ["backend-service"],
            "inputs": ["instance_class", "db_name"],
            "outputs": ["endpoint", "port"],
            "tags": ["rds", "database"],
        })

    @pytest.mark.asyncio
    async def test_returns_module_understanding(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        result = await cu.analyze_module("resource aws_rds_cluster {}", "terraform", "rds-module")
        assert isinstance(result, ModuleUnderstanding)
        assert result.name == "rds-module"
        assert result.module_type == "terraform"
        assert "RDS cluster" in result.provisions
        assert "database" in result.tags

    @pytest.mark.asyncio
    async def test_fallback_on_bedrock_error(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        cu._available = True
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = Exception("timeout")
        result = await cu.analyze_module("content", "helm", "my-chart")
        assert isinstance(result, ModuleUnderstanding)
        assert result.name == "my-chart"
        assert result.provisions == []


# ---------------------------------------------------------------------------
# analyze_service_interactions — happy path
# ---------------------------------------------------------------------------


class TestAnalyzeServiceInteractions:
    @pytest.fixture()
    def good_response(self) -> str:
        return json.dumps([
            {
                "source": "api-gateway",
                "target": "order-service",
                "protocol": "http",
                "pattern": "sync",
                "description": "REST API calls",
                "confidence": 0.95,
            },
            {
                "source": "order-service",
                "target": "notification-service",
                "protocol": "sqs",
                "pattern": "async",
                "description": "Order events via SQS",
                "confidence": 0.85,
            },
        ])

    @pytest.mark.asyncio
    async def test_returns_service_interactions(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        services = [
            {"name": "api-gateway", "exposes": ["HTTP"]},
            {"name": "order-service"},
            {"name": "notification-service"},
        ]
        result = await cu.analyze_service_interactions(services)
        assert len(result) == 2
        assert all(isinstance(i, ServiceInteraction) for i in result)
        assert result[0].source == "api-gateway"
        assert result[0].protocol == "http"
        assert result[1].pattern == "async"

    @pytest.mark.asyncio
    async def test_uses_complex_model(self, good_response: str) -> None:
        cu = _client_with_config(good_response)
        services = [{"name": "a"}, {"name": "b"}]
        await cu.analyze_service_interactions(services)
        call_kwargs = cu._client.invoke_model.call_args
        assert cu.config.complex_model in call_kwargs[1]["modelId"]

    @pytest.mark.asyncio
    async def test_empty_services_returns_empty_list(self) -> None:
        cu = _client_with_config()
        result = await cu.analyze_service_interactions([])
        assert result == []

    @pytest.mark.asyncio
    async def test_fallback_on_bedrock_error(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        cu._available = True
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = Exception("network error")
        result = await cu.analyze_service_interactions([{"name": "a"}, {"name": "b"}])
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_interactions_missing_source_or_target(self) -> None:
        response = json.dumps([
            {"source": "", "target": "b", "protocol": "http", "pattern": "sync", "description": ""},
            {"source": "a", "target": "", "protocol": "http", "pattern": "sync", "description": ""},
            {"source": "a", "target": "b", "protocol": "http", "pattern": "sync", "description": "ok", "confidence": 0.8},
        ])
        cu = _client_with_config(response)
        result = await cu.analyze_service_interactions([{"name": "a"}, {"name": "b"}])
        assert len(result) == 1
        assert result[0].source == "a"


# ---------------------------------------------------------------------------
# generate_embeddings_text
# ---------------------------------------------------------------------------


class TestGenerateEmbeddingsText:
    @pytest.mark.asyncio
    async def test_includes_summary(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        cu._client = None
        ru = RepoUnderstanding(
            name="svc",
            summary="An event-driven notification service.",
            architectural_pattern="event-driven",
            domain="application",
            complexity=3,
            exposes=["SNS topic"],
            consumes=["DynamoDB"],
            tags=["notifications"],
        )
        text = await cu.generate_embeddings_text(ru)
        assert "event-driven notification service" in text
        assert "event-driven" in text
        assert "application" in text
        assert "SNS topic" in text
        assert "DynamoDB" in text
        assert "notifications" in text

    @pytest.mark.asyncio
    async def test_no_exposes_consumes_still_valid(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        cu._client = None
        ru = RepoUnderstanding(
            name="lib",
            summary="A utility library.",
            architectural_pattern="library",
            domain="tooling",
            complexity=1,
        )
        text = await cu.generate_embeddings_text(ru)
        assert "library" in text
        assert isinstance(text, str)
        assert len(text) > 0


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_raises_budget_exceeded_when_limit_hit(self) -> None:
        cfg = UnderstandingConfig(
            budget_limit_usd=0.000001,  # effectively zero
            max_retries=1,
        )
        cu = CodeUnderstanding(config=cfg)
        cu._client = _make_client("{}")
        cu._available = True
        with pytest.raises(BudgetExceeded):
            await cu.analyze_repo({"name": "svc", "purpose": "anything"})

    @pytest.mark.asyncio
    async def test_budget_used_accumulates(self) -> None:
        response = json.dumps({
            "summary": "S", "architectural_pattern": "batch",
            "domain": "data", "complexity": 1,
            "exposes": [], "consumes": [], "tags": [], "confidence": 0.5,
        })
        cu = _client_with_config(response)
        await cu.analyze_repo({"name": "svc"})
        assert cu.budget_used > 0.0


# ---------------------------------------------------------------------------
# _invoke_model — retry behaviour
# ---------------------------------------------------------------------------


class TestInvokeModelRetry:
    @pytest.mark.asyncio
    async def test_retries_on_throttling_then_succeeds(self) -> None:
        from botocore.exceptions import ClientError

        throttle_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
            "invoke_model",
        )
        # Reuse the bytes — need a fresh BytesIO each call
        success_body = json.dumps({"content": [{"text": "ok"}]}).encode()

        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise throttle_error
            return {"body": BytesIO(success_body)}

        cfg = UnderstandingConfig(max_retries=2, budget_limit_usd=10.0, max_concurrent=1)
        cu = CodeUnderstanding(config=cfg)
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = side_effect
        cu._available = True

        result = await cu._invoke_model("test prompt", cfg.analysis_model)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self) -> None:
        from botocore.exceptions import ClientError

        throttle_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
            "invoke_model",
        )
        cfg = UnderstandingConfig(max_retries=2, budget_limit_usd=10.0, max_concurrent=1)
        cu = CodeUnderstanding(config=cfg)
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = throttle_error
        cu._available = True

        with pytest.raises(RuntimeError, match="attempts exhausted"):
            await cu._invoke_model("test prompt", cfg.analysis_model)

    @pytest.mark.asyncio
    async def test_permanent_error_raises_immediately(self) -> None:
        from botocore.exceptions import ClientError

        access_denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "invoke_model",
        )
        cfg = UnderstandingConfig(max_retries=3, budget_limit_usd=10.0, max_concurrent=1)
        cu = CodeUnderstanding(config=cfg)
        cu._client = MagicMock()
        cu._client.invoke_model.side_effect = access_denied
        cu._available = True

        with pytest.raises(RuntimeError, match="AccessDeniedException"):
            await cu._invoke_model("prompt", cfg.analysis_model)
        # Only one call — no retries on permanent errors
        assert cu._client.invoke_model.call_count == 1
        assert cu._available is False

    @pytest.mark.asyncio
    async def test_raises_when_client_is_none(self) -> None:
        cu = CodeUnderstanding(config=_default_config())
        cu._client = None
        with pytest.raises(RuntimeError, match="not available"):
            await cu._invoke_model("prompt", "model-id")

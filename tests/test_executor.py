"""Unit tests for cap.harness.executor.

All tests are fully offline — boto3 is patched so no AWS credentials are
required.
"""

import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.executor import (
    MODEL_ALIASES,
    MODEL_PRICING,
    AgentExecutor,
    ExecutionResult,
    _compute_cost,
    _resolve_model,
    _tier_for_model_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bedrock_response(text: str, input_tokens: int = 10, output_tokens: int = 20) -> dict:
    """Build a minimal fake ``invoke_model`` response dict."""
    body = json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    ).encode()
    return {"body": BytesIO(body)}


def _client_error(code: str, message: str = "error") -> Exception:
    """Build a botocore ClientError for the given error code."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "invoke_model",
    )


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_none_defaults_to_sonnet(self):
        assert _resolve_model(None) == MODEL_ALIASES["sonnet"]

    def test_logical_haiku(self):
        assert _resolve_model("haiku") == MODEL_ALIASES["haiku"]

    def test_logical_sonnet(self):
        assert _resolve_model("sonnet") == MODEL_ALIASES["sonnet"]

    def test_logical_opus(self):
        assert _resolve_model("opus") == MODEL_ALIASES["opus"]

    def test_passthrough_full_id(self):
        fq = "us.anthropic.claude-sonnet-4-6-20250514"
        assert _resolve_model(fq) == fq

    def test_unknown_name_passes_through(self):
        assert _resolve_model("my-custom-model") == "my-custom-model"


# ---------------------------------------------------------------------------
# Tier and pricing helpers
# ---------------------------------------------------------------------------


class TestTierForModelId:
    def test_haiku_prefix(self):
        assert _tier_for_model_id("us.anthropic.claude-haiku-4-5-20251001") == "haiku"

    def test_sonnet_prefix(self):
        assert _tier_for_model_id("us.anthropic.claude-sonnet-4-6-20250514") == "sonnet"

    def test_opus_prefix(self):
        assert _tier_for_model_id("us.anthropic.claude-opus-4-6-20250610") == "opus"

    def test_unknown_returns_none(self):
        assert _tier_for_model_id("unknown-model") is None


class TestComputeCost:
    def test_haiku_cost(self):
        # 1M input @ $0.80 + 1M output @ $4.00 = $4.80
        cost = _compute_cost(MODEL_ALIASES["haiku"], 1_000_000, 1_000_000)
        assert abs(cost - 4.80) < 1e-9

    def test_sonnet_cost(self):
        # 1M input @ $3.00 + 1M output @ $15.00 = $18.00
        cost = _compute_cost(MODEL_ALIASES["sonnet"], 1_000_000, 1_000_000)
        assert abs(cost - 18.00) < 1e-9

    def test_opus_cost(self):
        # 1M input @ $15.00 + 1M output @ $75.00 = $90.00
        cost = _compute_cost(MODEL_ALIASES["opus"], 1_000_000, 1_000_000)
        assert abs(cost - 90.00) < 1e-9

    def test_unknown_model_zero_cost(self):
        assert _compute_cost("unknown-model", 500_000, 500_000) == 0.0

    def test_zero_tokens_zero_cost(self):
        assert _compute_cost(MODEL_ALIASES["sonnet"], 0, 0) == 0.0

    def test_proportional_cost(self):
        cost = _compute_cost(MODEL_ALIASES["sonnet"], 1000, 500)
        expected = 1000 * 3.00 / 1_000_000 + 500 * 15.00 / 1_000_000
        assert abs(cost - expected) < 1e-12


# ---------------------------------------------------------------------------
# ExecutionResult dataclass
# ---------------------------------------------------------------------------


class TestExecutionResult:
    def test_fields_present(self):
        r = ExecutionResult(
            agent_id="a1",
            model="sonnet",
            input_tokens=5,
            output_tokens=10,
            cost_usd=0.0001,
            duration_ms=250,
            response="hello",
            error=None,
        )
        assert r.agent_id == "a1"
        assert r.response == "hello"
        assert r.error is None
        assert isinstance(r.timestamp, datetime)
        assert r.timestamp.tzinfo is not None  # timezone-aware

    def test_error_result(self):
        r = ExecutionResult(
            agent_id="a1",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            duration_ms=5,
            response=None,
            error="throttled",
        )
        assert r.error == "throttled"
        assert r.response is None


# ---------------------------------------------------------------------------
# AgentExecutor — happy path
# ---------------------------------------------------------------------------


class TestAgentExecutorHappyPath:
    def _make_executor(self) -> AgentExecutor:
        executor = AgentExecutor(profile="test", region="eu-central-1")
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _bedrock_response(
            "Hello!", input_tokens=15, output_tokens=25
        )
        executor._client = mock_client
        executor._available = None  # not yet determined
        return executor

    def test_returns_execution_result(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello")
        assert isinstance(r, ExecutionResult)

    def test_response_text_extracted(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello")
        assert r.response == "Hello!"
        assert r.error is None

    def test_token_counts_populated(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello")
        assert r.input_tokens == 15
        assert r.output_tokens == 25

    def test_cost_computed(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello", model="sonnet")
        expected = 15 * 3.00 / 1_000_000 + 25 * 15.00 / 1_000_000
        assert abs(r.cost_usd - expected) < 1e-12

    def test_duration_ms_positive(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello")
        assert r.duration_ms >= 0

    def test_model_resolved_and_stored(self):
        ex = self._make_executor()
        r = ex.execute("agent-1", "Say hello", model="haiku")
        assert r.model == MODEL_ALIASES["haiku"]

    def test_is_available_true_after_success(self):
        ex = self._make_executor()
        ex.execute("agent-1", "Say hello")
        assert ex.is_available is True

    def test_agent_id_in_result(self):
        ex = self._make_executor()
        r = ex.execute("my-agent", "prompt")
        assert r.agent_id == "my-agent"

    def test_timestamp_is_utc(self):
        ex = self._make_executor()
        r = ex.execute("a", "p")
        assert r.timestamp.tzinfo == timezone.utc

    def test_system_prompt_included_in_body(self):
        ex = self._make_executor()
        ex.execute("a", "user prompt", system_prompt="You are helpful.")
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["system"] == "You are helpful."

    def test_no_system_key_when_omitted(self):
        ex = self._make_executor()
        ex.execute("a", "user prompt")
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert "system" not in body

    def test_max_tokens_forwarded(self):
        ex = self._make_executor()
        ex.execute("a", "p", max_tokens=512)
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["max_tokens"] == 512

    def test_temperature_forwarded(self):
        ex = self._make_executor()
        ex.execute("a", "p", temperature=0.2)
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["temperature"] == 0.2

    def test_anthropic_version_in_body(self):
        ex = self._make_executor()
        ex.execute("a", "p")
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["anthropic_version"] == "bedrock-2023-05-31"

    def test_messages_format(self):
        ex = self._make_executor()
        ex.execute("a", "hello world")
        call_kwargs = ex._client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["messages"] == [{"role": "user", "content": "hello world"}]


# ---------------------------------------------------------------------------
# AgentExecutor — credential failure (lazy init)
# ---------------------------------------------------------------------------


class TestAgentExecutorCredentialFailure:
    def test_no_credentials_sets_unavailable(self):
        from botocore.exceptions import NoCredentialsError

        with patch("boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.client.side_effect = NoCredentialsError()
            ex = AgentExecutor()
            ex._ensure_client()
            assert ex.is_available is False

    def test_execute_returns_error_when_unavailable(self):
        ex = AgentExecutor()
        ex._available = False
        r = ex.execute("a", "p")
        assert r.error is not None
        assert r.response is None

    def test_execute_zero_tokens_when_unavailable(self):
        ex = AgentExecutor()
        ex._available = False
        r = ex.execute("a", "p")
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.cost_usd == 0.0

    def test_is_available_none_before_first_call(self):
        ex = AgentExecutor()
        assert ex.is_available is None


# ---------------------------------------------------------------------------
# AgentExecutor — Bedrock error handling
# ---------------------------------------------------------------------------


class TestAgentExecutorBedrockErrors:
    def _make_executor_with_error(self, error_code: str, message: str = "msg") -> AgentExecutor:
        ex = AgentExecutor()
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = _client_error(error_code, message)
        ex._client = mock_client
        ex._available = None
        return ex

    def test_throttling_returns_throttled(self):
        ex = self._make_executor_with_error("ThrottlingException")
        r = ex.execute("a", "p")
        assert r.error == "throttled"
        assert r.response is None

    def test_model_not_ready_returns_model_not_ready(self):
        ex = self._make_executor_with_error("ModelNotReadyException")
        r = ex.execute("a", "p")
        assert r.error == "model_not_ready"

    def test_validation_exception_prefixes_details(self):
        ex = self._make_executor_with_error("ValidationException", "bad input")
        r = ex.execute("a", "p")
        assert r.error is not None
        assert r.error.startswith("validation:")
        assert "bad input" in r.error

    def test_unknown_client_error_returns_str(self):
        ex = self._make_executor_with_error("SomeOtherException", "oops")
        r = ex.execute("a", "p")
        assert r.error is not None
        assert r.response is None

    def test_unknown_client_error_marks_unavailable(self):
        ex = self._make_executor_with_error("InternalServerError")
        ex.execute("a", "p")
        assert ex.is_available is False

    def test_throttle_does_not_mark_unavailable(self):
        ex = self._make_executor_with_error("ThrottlingException")
        ex.execute("a", "p")
        # ThrottlingException is transient — should not permanently mark unavailable
        assert ex.is_available is not False

    def test_generic_exception_returns_error_string(self):
        ex = AgentExecutor()
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = RuntimeError("network failure")
        ex._client = mock_client
        ex._available = None
        r = ex.execute("a", "p")
        assert r.error == "network failure"
        assert r.response is None

    def test_error_result_has_zero_tokens(self):
        ex = self._make_executor_with_error("ThrottlingException")
        r = ex.execute("a", "p")
        assert r.input_tokens == 0
        assert r.output_tokens == 0

    def test_error_result_has_zero_cost(self):
        ex = self._make_executor_with_error("ThrottlingException")
        r = ex.execute("a", "p")
        assert r.cost_usd == 0.0


# ---------------------------------------------------------------------------
# AgentExecutor — multi-content-block response
# ---------------------------------------------------------------------------


class TestAgentExecutorMultiBlock:
    def test_multiple_text_blocks_concatenated(self):
        body = json.dumps(
            {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "world"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        ).encode()
        ex = AgentExecutor()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": BytesIO(body)}
        ex._client = mock_client
        ex._available = None
        r = ex.execute("a", "p")
        assert r.response == "Hello world"

    def test_empty_content_returns_empty_string(self):
        body = json.dumps(
            {"content": [], "usage": {"input_tokens": 2, "output_tokens": 0}}
        ).encode()
        ex = AgentExecutor()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": BytesIO(body)}
        ex._client = mock_client
        ex._available = None
        r = ex.execute("a", "p")
        assert r.response == ""
        assert r.error is None


# ---------------------------------------------------------------------------
# MODEL_PRICING constant completeness
# ---------------------------------------------------------------------------


class TestModelPricingConstant:
    def test_all_tiers_present(self):
        for tier in ("haiku", "sonnet", "opus"):
            assert tier in MODEL_PRICING

    def test_each_tier_has_input_output(self):
        for tier, prices in MODEL_PRICING.items():
            assert "input" in prices, f"{tier} missing input price"
            assert "output" in prices, f"{tier} missing output price"

    def test_prices_are_positive(self):
        for tier, prices in MODEL_PRICING.items():
            assert prices["input"] > 0, f"{tier} input price must be positive"
            assert prices["output"] > 0, f"{tier} output price must be positive"

    def test_output_more_expensive_than_input(self):
        for tier, prices in MODEL_PRICING.items():
            assert prices["output"] > prices["input"], (
                f"{tier}: output tokens should cost more than input tokens"
            )

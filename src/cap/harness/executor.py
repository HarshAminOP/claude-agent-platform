"""Bedrock execution engine for CAP harness.

Makes direct calls to AWS Bedrock (Claude models via the Anthropic Messages API
format) and instruments every call with timing, token counts, and cost.

Graceful degradation: if credentials are absent or Bedrock is unreachable the
executor sets ``_available = False`` and every ``execute()`` call returns an
``ExecutionResult`` with ``error`` set rather than raising.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError

logger = logging.getLogger("cap.harness.executor")

# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

#: Logical name → Bedrock cross-region inference profile ID (us.*).
MODEL_ALIASES: dict[str, str] = {
    "haiku": "us.anthropic.claude-haiku-4-5-20251001",
    "sonnet": "us.anthropic.claude-sonnet-4-6-20250514",
    "opus": "us.anthropic.claude-opus-4-6-20250610",
}

#: Per-model pricing in USD per 1 000 000 tokens.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "haiku": {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus": {"input": 15.00, "output": 75.00},
}

# Prefix → tier mapping used to look up pricing for resolved model IDs.
_MODEL_ID_TO_TIER: dict[str, str] = {
    "us.anthropic.claude-haiku": "haiku",
    "us.anthropic.claude-sonnet": "sonnet",
    "us.anthropic.claude-opus": "opus",
    "anthropic.claude-haiku": "haiku",
    "anthropic.claude-sonnet": "sonnet",
    "anthropic.claude-opus": "opus",
}


def _resolve_model(model: Optional[str]) -> str:
    """Return a fully-qualified Bedrock model ID.

    Accepts logical shortnames (``haiku``, ``sonnet``, ``opus``) or a
    pass-through fully-qualified Bedrock model/inference-profile ID.
    Defaults to ``sonnet`` when *model* is ``None``.
    """
    if not model:
        return MODEL_ALIASES["sonnet"]
    return MODEL_ALIASES.get(model, model)


def _tier_for_model_id(model_id: str) -> Optional[str]:
    """Derive the pricing tier from a fully-qualified Bedrock model ID."""
    for prefix, tier in _MODEL_ID_TO_TIER.items():
        if model_id.startswith(prefix):
            return tier
    return None


def _compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a single Bedrock call."""
    tier = _tier_for_model_id(model_id)
    if tier is None:
        return 0.0
    prices = MODEL_PRICING[tier]
    return (
        input_tokens * prices["input"] / 1_000_000
        + output_tokens * prices["output"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Outcome of a single Bedrock invocation.

    Fields are always present; ``response`` and ``error`` are mutually
    exclusive — exactly one will be ``None`` for a completed call.
    """

    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    response: Optional[str]
    error: Optional[str]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class AgentExecutor:
    """Synchronous executor that calls Bedrock ``invoke_model`` directly.

    Parameters
    ----------
    profile:
        AWS named profile.  ``None`` uses the ambient credential chain
        (instance role, env vars, ``~/.aws/credentials``, etc.).
    region:
        AWS region for the Bedrock Runtime endpoint.
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        region: str = "eu-central-1",
    ) -> None:
        self._profile = profile
        self._region = region
        self._client = None
        self._available: Optional[bool] = None

    # ------------------------------------------------------------------
    # Lazy client initialisation
    # ------------------------------------------------------------------

    def _ensure_client(self) -> None:
        """Create the boto3 Bedrock Runtime client on first use."""
        if self._client is not None or self._available is False:
            return

        session_kwargs: dict = {"region_name": self._region}
        if self._profile:
            session_kwargs["profile_name"] = self._profile

        try:
            session = boto3.Session(**session_kwargs)
            self._client = session.client("bedrock-runtime")
            logger.debug(
                "AgentExecutor: Bedrock client initialised (region=%s, profile=%s)",
                self._region,
                self._profile or "<ambient>",
            )
        except (NoCredentialsError, NoRegionError) as exc:
            logger.warning(
                "AgentExecutor: no AWS credentials/region — executor unavailable: %s",
                exc,
            )
            self._available = False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AgentExecutor: failed to create Bedrock client — executor unavailable: %s",
                exc,
            )
            self._available = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> Optional[bool]:
        """Availability state derived from the most recent operation.

        Returns
        -------
        True
            At least one ``execute()`` call has succeeded.
        False
            The client could not be created (missing credentials) or a
            permanent Bedrock error was encountered.
        None
            ``execute()`` has not been called yet.
        """
        return self._available

    def execute(
        self,
        agent_id: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ExecutionResult:
        """Invoke a Bedrock Claude model and return a fully-populated result.

        Never raises — all errors are captured in ``ExecutionResult.error``.

        Parameters
        ----------
        agent_id:
            Caller-supplied identifier embedded in the result for tracing.
        prompt:
            User-turn content sent to the model.
        system_prompt:
            Optional system turn.
        model:
            Logical name (``haiku``, ``sonnet``, ``opus``) or a
            fully-qualified Bedrock model ID.  Defaults to ``sonnet``.
        max_tokens:
            Maximum tokens in the model response.
        temperature:
            Sampling temperature (0.0–1.0).
        """
        self._ensure_client()

        resolved_model = _resolve_model(model)
        start_ts = time.monotonic()

        def _result(
            response: Optional[str] = None,
            error: Optional[str] = None,
            input_tokens: int = 0,
            output_tokens: int = 0,
        ) -> ExecutionResult:
            elapsed_ms = int((time.monotonic() - start_ts) * 1000)
            return ExecutionResult(
                agent_id=agent_id,
                model=resolved_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                duration_ms=elapsed_ms,
                response=response,
                error=error,
            )

        if self._available is False or self._client is None:
            return _result(error="bedrock unavailable: client not initialised")

        body: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            body["system"] = system_prompt

        try:
            raw = self._client.invoke_model(
                modelId=resolved_model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"].get("Message", str(exc))

            if code == "ThrottlingException":
                logger.warning("AgentExecutor: throttled (agent=%s)", agent_id)
                return _result(error="throttled")

            if code == "ModelNotReadyException":
                logger.warning(
                    "AgentExecutor: model not ready model=%s agent=%s",
                    resolved_model,
                    agent_id,
                )
                return _result(error="model_not_ready")

            if code == "ValidationException":
                logger.error(
                    "AgentExecutor: validation error agent=%s: %s", agent_id, msg
                )
                return _result(error=f"validation: {msg}")

            logger.error(
                "AgentExecutor: ClientError code=%s agent=%s: %s", code, agent_id, exc
            )
            self._available = False
            return _result(error=str(exc))

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "AgentExecutor: unexpected error agent=%s: %s", agent_id, exc,
                exc_info=True,
            )
            return _result(error=str(exc))

        # Parse response -------------------------------------------------------
        try:
            payload = json.loads(raw["body"].read())
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentExecutor: failed to parse response body: %s", exc)
            return _result(error=f"response parse error: {exc}")

        content_blocks = payload.get("content", [])
        response_text = "".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        )

        usage = payload.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        self._available = True
        logger.debug(
            "AgentExecutor: OK agent=%s model=%s in=%d out=%d",
            agent_id,
            resolved_model,
            input_tokens,
            output_tokens,
        )
        return _result(
            response=response_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

"""Region-aware model probe for CAP init.

Probes Bedrock models during setup wizard to determine which are accessible,
then assigns working models to haiku/sonnet/opus tiers.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def region_prefix(region: str) -> str:
    """Derive the Bedrock cross-region inference prefix from an AWS region.

    Returns:
        "eu" for eu-* regions, "us" for us-* regions, "ap" for ap-* regions,
        or "" for other/unknown regions (try without prefix).
    """
    if not region:
        return ""
    if region.startswith("eu-"):
        return "eu"
    if region.startswith("us-"):
        return "us"
    if region.startswith("ap-"):
        return "ap"
    return ""


def get_candidate_models(prefix: str) -> dict[str, list[str]]:
    """Return candidate model IDs per tier for the given region prefix.

    Each tier has an ordered list; the first working model wins.
    When prefix is empty, model IDs are used without a region prefix
    (i.e., "anthropic.claude-..." instead of "eu.anthropic.claude-...").
    """
    def _prefixed(model_id: str) -> str:
        if prefix:
            return f"{prefix}.{model_id}"
        return model_id

    return {
        "haiku": [
            _prefixed("anthropic.claude-haiku-4-5-20251001-v1:0"),
        ],
        "sonnet": [
            _prefixed("anthropic.claude-sonnet-4-6"),
            _prefixed("anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ],
        "opus": [
            _prefixed("anthropic.claude-opus-4-8"),
            _prefixed("anthropic.claude-opus-4-7"),
            _prefixed("anthropic.claude-opus-4-6-v1"),
        ],
    }


def get_default_models_for_region(region: str) -> dict[str, str]:
    """Return default model IDs for a region without probing.

    Used in non-interactive mode where we cannot make Bedrock calls.
    Returns the first candidate for each tier (optimistic default).
    """
    prefix = region_prefix(region)
    candidates = get_candidate_models(prefix)
    return {tier: models[0] for tier, models in candidates.items()}


def probe_model(
    client,
    model_id: str,
    max_tokens: int = 5,
) -> bool:
    """Probe a single Bedrock model with a minimal converse() call.

    Args:
        client: A boto3 bedrock-runtime client.
        model_id: Fully-qualified Bedrock model ID to test.
        max_tokens: Maximum tokens for the test response.

    Returns:
        True if the model responded successfully, False otherwise.
    """
    try:
        client.converse(
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": "hi"}],
                }
            ],
            inferenceConfig={"maxTokens": max_tokens},
        )
        return True
    except Exception as exc:
        logger.debug("Model probe failed for %s: %s", model_id, exc)
        return False


def probe_all_models(
    client,
    region: str,
    progress_callback: Optional[callable] = None,
) -> dict[str, str]:
    """Probe all candidate models and return the best working model per tier.

    Args:
        client: A boto3 bedrock-runtime client.
        region: AWS region string (e.g., "eu-central-1").
        progress_callback: Optional callback(model_id: str, success: bool) for UI.

    Returns:
        Dict mapping tier names to working model IDs.
        Tiers with no working model are omitted from the result.
    """
    prefix = region_prefix(region)
    candidates = get_candidate_models(prefix)
    result: dict[str, str] = {}

    for tier, model_ids in candidates.items():
        for model_id in model_ids:
            success = probe_model(client, model_id)
            if progress_callback:
                progress_callback(model_id, success)
            if success:
                result[tier] = model_id
                break

    return result


def create_bedrock_client(
    region: str,
    profile: str = "",
    auth_method: str = "",
):
    """Create a boto3 bedrock-runtime client with the given config.

    Args:
        region: AWS region.
        profile: AWS profile name (empty = default/env).
        auth_method: Auth method hint (not used in client creation,
                     but indicates whether profile should be passed).

    Returns:
        A boto3 bedrock-runtime client.

    Raises:
        ImportError: If boto3 is not available.
        Various botocore exceptions on credential issues.
    """
    import boto3

    session_kwargs: dict = {}
    if profile and auth_method in ("sso-profile", "static-credentials"):
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region

    session = boto3.Session(**session_kwargs)
    return session.client("bedrock-runtime", region_name=region)

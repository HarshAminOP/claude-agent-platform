"""LangChain-based LLM provider abstraction.

Factory function to create the appropriate LangChain ChatModel based on
harness configuration. Supports 3 providers:
- aws-bedrock: ChatBedrockConverse (via langchain-aws)
- anthropic-api: ChatAnthropic (via langchain-anthropic)
- local: ChatOllama (via langchain-ollama)

Azure is intentionally NOT supported and has been removed from the codebase.
"""

import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel


def create_llm(config: dict, tier: str = "sonnet") -> BaseChatModel:
    """Create a LangChain ChatModel from harness configuration.

    Parameters
    ----------
    config:
        Harness config dict (from harness_config.load_harness_config()).
    tier:
        Model tier to use: 'haiku', 'sonnet', or 'opus'.
        Maps to a specific model_id per provider.

    Returns
    -------
    A fully-configured LangChain BaseChatModel instance.

    Raises
    ------
    ValueError:
        If provider is unknown or required credentials are missing.
    """
    provider = config.get("provider", "aws-bedrock")
    models = config.get("models", {})
    model_id = models.get(tier, models.get("sonnet", ""))

    if provider == "aws-bedrock":
        return _create_bedrock(config, model_id)
    elif provider == "anthropic-api":
        return _create_anthropic(config, model_id)
    elif provider == "local":
        return _create_ollama(config)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Supported: aws-bedrock, anthropic-api, local"
        )


def _create_bedrock(config: dict, model_id: str) -> BaseChatModel:
    """Create a ChatBedrockConverse instance.

    Auth routing:
    - sso-profile / static-credentials: pass credentials_profile_name
    - env-vars / instance-role: let boto3 default chain handle it
    """
    from langchain_aws import ChatBedrockConverse

    aws_config = config.get("aws", {})
    from cap.lib.harness_config import DEFAULT_AWS_REGION
    region = aws_config.get("region", DEFAULT_AWS_REGION)
    auth_method = aws_config.get("auth_method", "sso-profile")
    profile = aws_config.get("profile", "")

    kwargs: dict[str, Any] = {
        "model": model_id,
        "region_name": region,
    }

    # Pass credentials_profile_name for profile-based auth
    if auth_method in ("sso-profile", "static-credentials") and profile:
        kwargs["credentials_profile_name"] = profile

    return ChatBedrockConverse(**kwargs)


def _create_anthropic(config: dict, model_id: str) -> BaseChatModel:
    """Create a ChatAnthropic instance.

    API key is resolved from the environment variable specified in config.
    """
    from langchain_anthropic import ChatAnthropic

    anthropic_cfg = config.get("anthropic", {})
    api_key_env = anthropic_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(api_key_env)

    if not api_key:
        raise ValueError(
            f"Anthropic API key not found. "
            f"Set the ${api_key_env} environment variable."
        )

    return ChatAnthropic(
        model=model_id,
        api_key=api_key,
    )


def _create_ollama(config: dict) -> BaseChatModel:
    """Create a ChatOllama instance for local model inference."""
    from langchain_ollama import ChatOllama

    local_cfg = config.get("local", {})
    model = local_cfg.get("model", "llama3")
    base_url = local_cfg.get("base_url", "http://localhost:11434")

    return ChatOllama(
        model=model,
        base_url=base_url,
    )

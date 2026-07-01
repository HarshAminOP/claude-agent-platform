"""Load harness configuration (provider, auth, models, budget).

Supports multiple LLM providers:
- aws-bedrock (default, backward compatible)
- anthropic-api (direct Anthropic SDK)
- azure-openai (Azure OpenAI Service)
- local (Ollama, vLLM, etc.)

Backward compatibility: configs without a "provider" field are assumed to be aws-bedrock.
"""
import json
import os
from pathlib import Path


def load_harness_config() -> dict:
    """Load the harness config, falling back to sensible defaults.

    If the config file exists but lacks a "provider" field, assumes "aws-bedrock"
    for backward compatibility with pre-universal configs.
    """
    config_path = Path.home() / ".claude-platform" / "harness-config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        # Backward compat: if no provider field, assume aws-bedrock
        if "provider" not in config:
            config["provider"] = "aws-bedrock"
        # Backward compat: if no auth_method in aws section, assume sso-profile
        if "aws" in config and "auth_method" not in config["aws"]:
            config["aws"]["auth_method"] = "sso-profile"
        return config

    # Fallback defaults — used when cap init hasn't been run yet
    return {
        "provider": "aws-bedrock",
        "aws": {"profile": "", "region": "eu-central-1", "auth_method": "env-vars"},
        "models": {
            "haiku": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "sonnet": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "opus": "eu.anthropic.claude-opus-4-6-v1",
        },
        "budget": {"daily_limit_usd": 5.0, "alert_threshold_pct": 80},
        "agent_defaults": {
            "dev": "sonnet", "devops": "sonnet", "security": "opus",
            "code-review": "opus", "sre": "sonnet", "test": "sonnet",
            "docs": "haiku", "optimization": "haiku", "aws-architect": "opus",
            "explore": "sonnet", "cicd": "sonnet",
        },
        "execution": {
            "max_tool_iterations": 15,
            "max_retries": 2,
            "backoff_base_s": 1.0,
            "default_max_tokens": 8192,
            "temperature": 0.7,
        },
    }


def get_provider(config: dict | None = None) -> str:
    """Get the configured LLM provider name.

    Returns one of: 'aws-bedrock', 'anthropic-api', 'azure-openai', 'local'.
    """
    if config is None:
        config = load_harness_config()
    return config.get("provider", "aws-bedrock")


def get_anthropic_api_key(config: dict | None = None) -> str | None:
    """Resolve the Anthropic API key from the configured environment variable.

    The key is NEVER stored in config — only the env var name is stored.
    Returns None if the env var is not set.
    """
    if config is None:
        config = load_harness_config()
    anthropic_cfg = config.get("anthropic", {})
    env_var = anthropic_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    return os.environ.get(env_var)

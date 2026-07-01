"""Load harness configuration (AWS profile, region, models, budget)."""
import json
from pathlib import Path


def load_harness_config() -> dict:
    """Load the harness config, falling back to sensible defaults."""
    config_path = Path.home() / ".claude-platform" / "harness-config.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    # Fallback defaults — used when cap init hasn't been run yet
    return {
        "aws": {"profile": "", "region": "eu-central-1"},
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

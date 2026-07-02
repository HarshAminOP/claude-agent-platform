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

#: Default AWS region used as a last-resort fallback when no config exists.
#: Overridden by: harness-config.json > AWS_DEFAULT_REGION env var.
DEFAULT_AWS_REGION = "us-east-1"


def _default_knowledge_config() -> dict:
    """Return default knowledge indexing configuration."""
    return {
        "indexed_roots": [],
        "exclude_patterns": [
            "node_modules", ".git", "__pycache__", "dist", "build",
            ".terraform", ".venv", "venv", "vendor", "target",
        ],
        "auto_index_new_workspaces": True,
        "file_extensions": [
            ".py", ".ts", ".js", ".tf", ".yaml", ".yml", ".json", ".md",
            ".toml", ".sh", ".go", ".java", ".rs", ".hcl",
        ],
        "max_file_size_kb": 500,
    }


def _default_indexing_config() -> dict:
    """Return default multi-path indexing configuration."""
    return {
        "local_paths": [],
        "remotes": [],
        "clone_base_path": str(Path.home() / ".claude-platform" / "repos"),
        "exclude_patterns": ["node_modules", ".git", "vendor", "__pycache__", ".terraform", "dist", "build"],
        "file_extensions": [".py", ".go", ".ts", ".tf", ".yaml", ".yml", ".json", ".md", ".sh", ".rs", ".hcl", ".toml", ".js"],
        "max_file_size_kb": 512,
        "reindex_interval_minutes": 360,
    }


def _default_embeddings_config() -> dict:
    """Return default embedding model configuration.

    The region falls back to the ``AWS_DEFAULT_REGION`` environment variable
    so the default works across any AWS region without code changes.
    """
    import os
    return {
        "provider": "bedrock",
        "model_id": "amazon.titan-embed-text-v2:0",
        "dimensions": 1024,
        "fallback": "sentence-transformers",
        "fallback_model": "all-MiniLM-L6-v2",
        "region": os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION),
        "profile": None,
    }


def load_harness_config() -> dict:
    """Load the harness config, falling back to sensible defaults.

    If the config file exists but lacks a "provider" field, assumes "aws-bedrock"
    for backward compatibility with pre-universal configs.
    """
    from cap.config import get_harness_config_path
    config_path = get_harness_config_path()
    if config_path.exists():
        config = json.loads(config_path.read_text())
        # Backward compat: if no provider field, assume aws-bedrock
        if "provider" not in config:
            config["provider"] = "aws-bedrock"
        # Backward compat: if no auth_method in aws section, assume sso-profile
        if "aws" in config and "auth_method" not in config["aws"]:
            config["aws"]["auth_method"] = "sso-profile"
        # Ensure knowledge config is present (added in v0.6)
        if "knowledge" not in config:
            config["knowledge"] = _default_knowledge_config()
        # Ensure indexing config is present (added in v0.8)
        if "indexing" not in config:
            config["indexing"] = _default_indexing_config()
        # Ensure embeddings config is present (added in v0.8)
        if "embeddings" not in config:
            config["embeddings"] = _default_embeddings_config()
        return config

    # Fallback defaults — used when cap init hasn't been run yet.
    # Model IDs are derived from region using the model_probe module.
    from cap.lib.model_probe import get_default_models_for_region

    default_region = DEFAULT_AWS_REGION
    return {
        "provider": "aws-bedrock",
        "aws": {"profile": "", "region": default_region, "auth_method": "env-vars"},
        "models": get_default_models_for_region(default_region),
        "budget": {
            "daily_limit_usd": 5.0,
            "alert_threshold_pct": 80,
            "per_project": False,
            "agent_caps": {},
        },
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
        "knowledge": _default_knowledge_config(),
        "indexing": _default_indexing_config(),
        "embeddings": _default_embeddings_config(),
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


def get_knowledge_config(config: dict | None = None) -> dict:
    """Get the knowledge indexing configuration section.

    Returns dict with keys: indexed_roots, exclude_patterns,
    auto_index_new_workspaces, file_extensions, max_file_size_kb.
    """
    if config is None:
        config = load_harness_config()
    return config.get("knowledge", _default_knowledge_config())


def add_indexed_root(root_path: str, config: dict | None = None) -> bool:
    """Add a root path to knowledge.indexed_roots if not already present.

    Persists the change to harness-config.json. Returns True if added.
    """
    from cap.config import get_harness_config_path
    config_path = get_harness_config_path()
    if config is None:
        config = load_harness_config()

    knowledge = config.setdefault("knowledge", _default_knowledge_config())
    indexed_roots = knowledge.setdefault("indexed_roots", [])

    # Normalize path
    normalized = str(Path(root_path).resolve())
    if normalized in indexed_roots:
        return False

    indexed_roots.append(normalized)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return True


def is_path_under_indexed_root(path: str, config: dict | None = None) -> bool:
    """Check if a given path is under any of the configured indexed_roots."""
    if config is None:
        config = load_harness_config()

    knowledge = config.get("knowledge", _default_knowledge_config())
    indexed_roots = knowledge.get("indexed_roots", [])

    normalized = str(Path(path).resolve())
    for root in indexed_roots:
        if normalized.startswith(root):
            return True
    return False


def get_indexing_config(config: dict | None = None) -> dict:
    """Get the multi-path indexing configuration section.

    Returns dict with keys: local_paths, remotes, clone_base_path,
    exclude_patterns, file_extensions, max_file_size_kb, reindex_interval_minutes.
    """
    if config is None:
        config = load_harness_config()
    return config.get("indexing", _default_indexing_config())


def get_embeddings_config(config: dict | None = None) -> dict:
    """Get the embedding model configuration section.

    Returns dict with keys: provider, model_id, dimensions, fallback,
    fallback_model, region, profile.
    """
    if config is None:
        config = load_harness_config()
    return config.get("embeddings", _default_embeddings_config())


def get_local_paths(config: dict | None = None) -> list[str]:
    """Get the list of local workspace paths configured for indexing.

    Returns an empty list if no paths are configured. Each path is
    returned as-is from config (not resolved/expanded).
    """
    indexing = get_indexing_config(config)
    return indexing.get("local_paths", [])


def get_remotes(config: dict | None = None) -> list[dict]:
    """Get the list of remote git endpoint configurations.

    Each remote dict has keys: type (github/bitbucket/gitlab),
    org or group, ssh_endpoint, auto_clone.
    Returns an empty list if no remotes are configured.
    """
    indexing = get_indexing_config(config)
    return indexing.get("remotes", [])

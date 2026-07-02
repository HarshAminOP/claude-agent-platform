"""
CAP Centralized Configuration — Single source of truth for all paths, databases, and config.

This module is THE canonical source for:
- CAP_HOME resolution (always ~/.claude-platform, overridable via CAP_HOME env var)
- All database paths (derived from CAP_HOME/data/)
- Config file paths (harness-config.json, config.toml)
- MCP server environment generation
- Legacy config loading (backward-compatible with ~/.cap/config.toml)

IMPORTANT: All other modules MUST import path functions from here.
Do NOT use hardcoded ~/.cap/ paths anywhere else in the codebase.
"""

import json
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # Fallback for Python 3.10
    except ModuleNotFoundError:
        tomllib = None


# =============================================================================
# PATH RESOLUTION — Single source of truth
# =============================================================================

_DEFAULT_CAP_HOME = str(Path.home() / ".claude-platform")
_LEGACY_CAP_HOME = str(Path.home() / ".cap")


def get_cap_home() -> Path:
    """Return the CAP home directory.

    Resolution order:
    1. CAP_HOME environment variable (if set)
    2. ~/.claude-platform (default)

    All other path functions derive from this.
    """
    return Path(os.environ.get("CAP_HOME", _DEFAULT_CAP_HOME))


def get_data_dir() -> Path:
    """Return the data directory: CAP_HOME/data/"""
    return get_cap_home() / "data"


def get_logs_dir() -> Path:
    """Return the logs directory: CAP_HOME/logs/"""
    return get_cap_home() / "logs"


def get_run_dir() -> Path:
    """Return the runtime directory: CAP_HOME/run/"""
    return get_cap_home() / "run"


def get_backups_dir() -> Path:
    """Return the backups directory: CAP_HOME/backups/"""
    return get_cap_home() / "backups"


# --- Database paths ---

def get_db_path(db_name: str = "platform.db") -> Path:
    """Return path to a named database in the data directory.

    Args:
        db_name: Database filename. One of:
            - "platform.db" (orchestration, enforcement, cost, health, agents)
            - "knowledge.db" (knowledge graph, FTS, embeddings)
            - "sessions.db" (session state, checkpoints, feedback)
            - "fleet.db" (MCP server fleet management)

    Returns:
        Absolute Path to the database file.
    """
    return get_data_dir() / db_name


def get_platform_db_path() -> Path:
    """Return path to the primary platform database (platform.db).

    This is the database used for orchestration, enforcement, cost tracking,
    agent health, circuit breakers, witness manifests, and DAG task plans.

    Note: This replaces the legacy ~/.cap/cap.db path.
    """
    return get_data_dir() / "platform.db"


def get_knowledge_db_path() -> Path:
    """Return path to the knowledge database (knowledge.db)."""
    return get_data_dir() / "knowledge.db"


def get_sessions_db_path() -> Path:
    """Return path to the sessions database (sessions.db)."""
    return get_data_dir() / "sessions.db"


def get_fleet_db_path() -> Path:
    """Return path to the fleet database (fleet.db)."""
    return get_data_dir() / "fleet.db"


def get_legacy_db_path() -> Path:
    """Return path to the legacy database (~/.cap/cap.db).

    DEPRECATED: This path exists only for migration purposes.
    Emits a deprecation warning when called.
    """
    warnings.warn(
        "Legacy DB path ~/.cap/cap.db is deprecated. Use get_platform_db_path() instead. "
        "Run 'cap migrate' to move data to the new location.",
        DeprecationWarning,
        stacklevel=2,
    )
    return Path(_LEGACY_CAP_HOME) / "cap.db"


# --- Config file paths ---

def get_harness_config_path() -> Path:
    """Return path to harness-config.json (LLM provider, models, budget)."""
    return get_cap_home() / "harness-config.json"


def get_config_toml_path() -> Path:
    """Return path to config.toml (platform behavior tuning)."""
    return get_cap_home() / "config.toml"


def get_witness_key_path() -> Path:
    """Return path to the witness HMAC key file."""
    return get_cap_home() / "witness.key"


# --- Legacy detection ---

def has_legacy_cap_dir() -> bool:
    """Check if the legacy ~/.cap/ directory exists."""
    return Path(_LEGACY_CAP_HOME).exists()


def check_legacy_and_warn():
    """Emit a warning if ~/.cap/ directory exists alongside ~/.claude-platform/.

    Call this during startup to alert users about migration.
    """
    if has_legacy_cap_dir() and get_cap_home().exists():
        warnings.warn(
            "Legacy ~/.cap/ directory detected alongside ~/.claude-platform/. "
            "Run 'cap migrate' to consolidate data and remove the legacy directory.",
            UserWarning,
            stacklevel=2,
        )


# =============================================================================
# CONFIGURATION — Legacy config.toml loading (backward compatible)
# =============================================================================

# Legacy alias — kept for backward compat imports
DEFAULT_CONFIG_PATH = str(get_cap_home() / "config.toml")


@dataclass
class Config:
    """CAP configuration with typed fields and defaults.

    This dataclass merges settings from config.toml (platform behavior tuning).
    """

    # Budget & limits
    daily_budget_usd: float = 5.0
    disk_budget_mb: int = 256
    enforcement_threshold: int = 3
    passthrough_ttl: int = 300
    passthrough_max_per_hour: int = 3

    # Database — always derived from centralized path
    db_path: str = ""

    # Memory
    working_memory_tokens: int = 15_000
    eviction_score_threshold: float = 0.15
    stale_days: int = 90
    delete_days: int = 365
    delete_min_accesses: int = 3

    # Sync
    staleness_ttl: int = 300
    hash_check_interval: int = 300

    # Agent timeouts (seconds)
    agent_timeouts: dict = field(default_factory=lambda: {
        "dev": 300,
        "devops": 300,
        "security": 180,
        "sre": 180,
        "test": 240,
        "optimization": 240,
        "docs": 120,
        "cicd": 180,
        "aws-architect": 240,
        "code-review": 120,
        "data": 240,
        "frontend": 240,
    })

    # Model pricing per 1M tokens
    model_pricing: dict = field(default_factory=lambda: {
        "opus": {"input": 15.00, "output": 75.00},
        "sonnet": {"input": 3.00, "output": 15.00},
        "haiku": {"input": 0.25, "output": 1.25},
    })

    # Reliability
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_window_seconds: int = 300
    circuit_breaker_cooldown_seconds: int = 120
    cascade_window_seconds: int = 10
    cascade_threshold: int = 3

    def __post_init__(self):
        # Always derive db_path from centralized config
        if not self.db_path:
            self.db_path = str(get_platform_db_path())

    def get(self, key: str, default=None):
        """Dict-like access for backward compatibility."""
        return getattr(self, key, default)


def load(path: str = None) -> Config:
    """
    Load configuration from TOML file.

    Falls back to defaults if file does not exist or tomllib is unavailable.
    Checks both the canonical path (CAP_HOME/config.toml) and the legacy path
    (~/.cap/config.toml), preferring the canonical path.

    Args:
        path: Path to config.toml. Defaults to CAP_HOME/config.toml

    Returns:
        Config dataclass with values from file merged over defaults.
    """
    if path is None:
        canonical = str(get_config_toml_path())
        legacy = os.path.join(_LEGACY_CAP_HOME, "config.toml")
        if os.path.exists(canonical):
            path = canonical
        elif os.path.exists(legacy):
            path = legacy
        else:
            path = canonical  # Will return defaults

    config = Config()

    if not os.path.exists(path):
        return config

    if tomllib is None:
        # Cannot parse TOML without library, return defaults
        return config

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return config

    # Merge top-level scalar fields
    for key in (
        "daily_budget_usd", "disk_budget_mb", "enforcement_threshold",
        "passthrough_ttl", "passthrough_max_per_hour",
        "working_memory_tokens", "eviction_score_threshold",
        "stale_days", "delete_days", "delete_min_accesses",
        "staleness_ttl", "hash_check_interval",
        "circuit_breaker_failure_threshold", "circuit_breaker_window_seconds",
        "circuit_breaker_cooldown_seconds", "cascade_window_seconds",
        "cascade_threshold",
    ):
        if key in data:
            setattr(config, key, data[key])

    # Note: db_path from file is IGNORED — always derived from get_platform_db_path()
    # This prevents legacy config files from pointing to wrong DB locations.

    # Merge dict fields (agent_timeouts, model_pricing)
    if "agent_timeouts" in data and isinstance(data["agent_timeouts"], dict):
        config.agent_timeouts.update(data["agent_timeouts"])

    if "model_pricing" in data and isinstance(data["model_pricing"], dict):
        config.model_pricing.update(data["model_pricing"])

    return config


def save(config: Config, path: str = None) -> None:
    """
    Save configuration to TOML file.

    Args:
        config: Config dataclass to save
        path: Path to write. Defaults to CAP_HOME/config.toml
    """
    if path is None:
        path = str(get_config_toml_path())

    # Ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    lines.append("# CAP Configuration")
    lines.append("# Generated by cap init. Edit as needed.")
    lines.append("")

    # Scalar fields
    lines.append(f"daily_budget_usd = {config.daily_budget_usd}")
    lines.append(f"disk_budget_mb = {config.disk_budget_mb}")
    lines.append(f"enforcement_threshold = {config.enforcement_threshold}")
    lines.append(f"passthrough_ttl = {config.passthrough_ttl}")
    lines.append(f"passthrough_max_per_hour = {config.passthrough_max_per_hour}")
    lines.append(f"working_memory_tokens = {config.working_memory_tokens}")
    lines.append(f"eviction_score_threshold = {config.eviction_score_threshold}")
    lines.append(f"stale_days = {config.stale_days}")
    lines.append(f"delete_days = {config.delete_days}")
    lines.append(f"delete_min_accesses = {config.delete_min_accesses}")
    lines.append(f"staleness_ttl = {config.staleness_ttl}")
    lines.append(f"hash_check_interval = {config.hash_check_interval}")
    lines.append(f"circuit_breaker_failure_threshold = {config.circuit_breaker_failure_threshold}")
    lines.append(f"circuit_breaker_window_seconds = {config.circuit_breaker_window_seconds}")
    lines.append(f"circuit_breaker_cooldown_seconds = {config.circuit_breaker_cooldown_seconds}")
    lines.append(f"cascade_window_seconds = {config.cascade_window_seconds}")
    lines.append(f"cascade_threshold = {config.cascade_threshold}")
    lines.append("")

    # Agent timeouts
    lines.append("[agent_timeouts]")
    for agent, timeout in config.agent_timeouts.items():
        lines.append(f'"{agent}" = {timeout}')
    lines.append("")

    # Model pricing
    lines.append("[model_pricing]")
    lines.append("[model_pricing.opus]")
    lines.append(f"input = {config.model_pricing['opus']['input']}")
    lines.append(f"output = {config.model_pricing['opus']['output']}")
    lines.append("[model_pricing.sonnet]")
    lines.append(f"input = {config.model_pricing['sonnet']['input']}")
    lines.append(f"output = {config.model_pricing['sonnet']['output']}")
    lines.append("[model_pricing.haiku]")
    lines.append(f"input = {config.model_pricing['haiku']['input']}")
    lines.append(f"output = {config.model_pricing['haiku']['output']}")
    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


# =============================================================================
# MCP ENVIRONMENT GENERATION
# =============================================================================

def _load_harness_config_for_env() -> dict:
    """Load harness-config.json for env var extraction. Returns empty dict on failure."""
    config_path = get_harness_config_path()
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def generate_mcp_env() -> dict:
    """Generate the complete environment block that any MCP server needs.

    Returns a dict suitable for use as env vars in MCP server registration.
    Reads AWS profile/region from harness-config.json if available.

    Keys returned:
    - CAP_HOME: the platform home directory
    - AWS_PROFILE: AWS SSO profile for Bedrock calls (if configured)
    - AWS_DEFAULT_REGION: AWS region for Bedrock inference (if configured)

    Note: PYTHONPATH is NOT included. Entry-point scripts resolve their own imports.
    Note: AWS_REGION is NOT included (dead var, code only reads AWS_DEFAULT_REGION).
    """
    harness = _load_harness_config_for_env()
    aws_config = harness.get("aws", {})

    env = {
        "CAP_HOME": str(get_cap_home()),
    }

    # AWS profile from harness-config.json
    aws_profile = aws_config.get("profile", "")
    if aws_profile:
        env["AWS_PROFILE"] = aws_profile

    # AWS region from harness-config.json, falling back to env var
    aws_region = aws_config.get("region", "")
    if not aws_region:
        aws_region = os.environ.get("AWS_DEFAULT_REGION", "")
    if aws_region:
        env["AWS_DEFAULT_REGION"] = aws_region

    return env


def generate_mcp_env_list() -> list[str]:
    """Generate the MCP env block as a list of 'KEY=VALUE' strings.

    This format is used by `claude mcp add --env` commands.
    """
    return [f"{k}={v}" for k, v in generate_mcp_env().items()]

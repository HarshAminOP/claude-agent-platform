"""Configuration loader for the CAP platform."""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomli as tomllib
except ImportError:
    import tomllib


@dataclass
class BedrockConfig:
    region: str = "us-east-1"
    profile: str | None = None
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    embedding_dimensions: int = 1024
    embedding_max_concurrent: int = 3
    embedding_max_input_tokens: int = 8192
    max_retries: int = 3
    base_delay_ms: int = 500
    max_delay_ms: int = 10000
    backoff_multiplier: float = 2.0


@dataclass
class ConcurrencyConfig:
    min_slots: int = 3
    max_slots: int = 8
    initial_slots: int = 4
    scale_up_after_seconds: float = 60.0


@dataclass
class BudgetConfig:
    monthly_cap_usd: float = 50.0
    warning_threshold: float = 0.8
    per_workflow_default_usd: float = 5.0
    kill_on_exceed: bool = True


@dataclass
class RetrievalConfig:
    default_strategy: str = "hybrid"
    rrf_k: int = 60
    keyword_weight: float = 0.3
    semantic_weight: float = 0.5
    graph_weight: float = 0.2
    fallback_keyword_weight: float = 0.6
    fallback_graph_weight: float = 0.4
    default_top_k: int = 10
    recency_boost_halflife_days: int = 30


@dataclass
class SyncConfig:
    auto_sync_on_session_start: bool = True
    auto_sync_on_git_pull: bool = True
    scheduled_interval_minutes: int = 60
    max_file_size_kb: int = 500
    skip_patterns: list[str] = field(default_factory=lambda: [
        r"\.git/", r"node_modules/", r"vendor/", r"\.terraform/",
        r"__pycache__/", r"\.pyc$", r"\.lock$", r"\.env$",
        r"\.(png|jpg|gif|ico|woff|ttf|eot|so|dylib)$",
    ])


@dataclass
class MaintenanceConfig:
    wal_checkpoint_threshold_mb: float = 50.0
    vacuum_growth_threshold_mb: float = 100.0
    daily_prune_hour: int = 3
    weekly_vacuum_day: int = 6
    backup_retention_count: int = 5


@dataclass
class GitHubConfig:
    org: str = ""
    clone_base_path: str = ""
    use_ssh: bool = True
    auto_clone_on_missing_dep: bool = True
    max_auto_clones_per_session: int = 10
    default_branch: str = "main"
    clone_depth: int = 1

    @property
    def org_url(self) -> str:
        if self.use_ssh:
            return f"git@github.com:{self.org}" if self.org else ""
        return f"https://github.com/{self.org}" if self.org else ""


@dataclass
class FleetConfig:
    health_check_interval_seconds: int = 30
    health_check_timeout_seconds: int = 5
    unhealthy_threshold: int = 3
    max_restarts: int = 5
    restart_backoff_base: float = 2.0
    auto_restart_enabled: bool = True


@dataclass
class SessionConfig:
    checkpoint_interval_seconds: int = 300
    max_corrections_loaded: int = 20
    max_learnings_loaded: int = 30
    max_decisions_loaded: int = 15
    recency_weight: float = 0.7


@dataclass
class PlatformConfig:
    home: Path = field(default_factory=lambda: Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform"))))
    log_level: str = "INFO"
    bedrock: BedrockConfig = field(default_factory=BedrockConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def inbox_dir(self) -> Path:
        return self.home / "inbox"

    @property
    def locks_dir(self) -> Path:
        return self.home / "locks"


def load_config(config_path: Path | None = None) -> PlatformConfig:
    cap_home = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))
    if config_path is None:
        config_path = cap_home / "config.toml"

    if not config_path.exists():
        return PlatformConfig(home=cap_home)

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    config = PlatformConfig(home=cap_home)

    if "platform" in raw:
        config.log_level = raw["platform"].get("log_level", config.log_level)

    if "bedrock" in raw:
        b = raw["bedrock"]
        config.bedrock = BedrockConfig(
            region=b.get("region", config.bedrock.region),
            profile=b.get("profile", config.bedrock.profile),
            embedding_model=b.get("embedding_model", config.bedrock.embedding_model),
            embedding_dimensions=b.get("embedding_dimensions", config.bedrock.embedding_dimensions),
            embedding_max_concurrent=b.get("embedding_max_concurrent", config.bedrock.embedding_max_concurrent),
            embedding_max_input_tokens=b.get("embedding_max_input_tokens", config.bedrock.embedding_max_input_tokens),
            max_retries=b.get("retry", {}).get("max_retries", config.bedrock.max_retries),
            base_delay_ms=b.get("retry", {}).get("base_delay_ms", config.bedrock.base_delay_ms),
            max_delay_ms=b.get("retry", {}).get("max_delay_ms", config.bedrock.max_delay_ms),
            backoff_multiplier=b.get("retry", {}).get("backoff_multiplier", config.bedrock.backoff_multiplier),
        )

    if "concurrency" in raw:
        c = raw["concurrency"]
        config.concurrency = ConcurrencyConfig(
            min_slots=c.get("min_slots", config.concurrency.min_slots),
            max_slots=c.get("max_slots", config.concurrency.max_slots),
            initial_slots=c.get("initial_slots", config.concurrency.initial_slots),
            scale_up_after_seconds=c.get("scale_up_after_seconds", config.concurrency.scale_up_after_seconds),
        )

    if "budget" in raw:
        bu = raw["budget"]
        config.budget = BudgetConfig(
            monthly_cap_usd=bu.get("monthly_cap_usd", config.budget.monthly_cap_usd),
            warning_threshold=bu.get("warning_threshold", config.budget.warning_threshold),
            per_workflow_default_usd=bu.get("per_workflow_default_usd", config.budget.per_workflow_default_usd),
            kill_on_exceed=bu.get("kill_on_exceed", config.budget.kill_on_exceed),
        )

    if "knowledge" in raw:
        k = raw["knowledge"]
        if "retrieval" in k:
            r = k["retrieval"]
            weights = r.get("weights", {})
            fallback = r.get("fallback_weights", {})
            config.retrieval = RetrievalConfig(
                default_strategy=r.get("default_strategy", config.retrieval.default_strategy),
                rrf_k=r.get("rrf_k", config.retrieval.rrf_k),
                keyword_weight=weights.get("keyword", config.retrieval.keyword_weight),
                semantic_weight=weights.get("semantic", config.retrieval.semantic_weight),
                graph_weight=weights.get("graph", config.retrieval.graph_weight),
                fallback_keyword_weight=fallback.get("keyword", config.retrieval.fallback_keyword_weight),
                fallback_graph_weight=fallback.get("graph", config.retrieval.fallback_graph_weight),
                default_top_k=r.get("default_top_k", config.retrieval.default_top_k),
            )
        if "sync" in k:
            s = k["sync"]
            config.sync = SyncConfig(
                auto_sync_on_session_start=s.get("auto_sync_on_session_start", config.sync.auto_sync_on_session_start),
                auto_sync_on_git_pull=s.get("auto_sync_on_git_pull", config.sync.auto_sync_on_git_pull),
                scheduled_interval_minutes=s.get("scheduled_interval_minutes", config.sync.scheduled_interval_minutes),
                max_file_size_kb=s.get("max_file_size_kb", config.sync.max_file_size_kb),
                skip_patterns=s.get("skip_patterns", config.sync.skip_patterns),
            )

    if "maintenance" in raw:
        m = raw["maintenance"]
        config.maintenance = MaintenanceConfig(
            wal_checkpoint_threshold_mb=m.get("wal_checkpoint_threshold_mb", config.maintenance.wal_checkpoint_threshold_mb),
            vacuum_growth_threshold_mb=m.get("vacuum_growth_threshold_mb", config.maintenance.vacuum_growth_threshold_mb),
            daily_prune_hour=m.get("daily_prune_hour", config.maintenance.daily_prune_hour),
            weekly_vacuum_day=m.get("weekly_vacuum_day", config.maintenance.weekly_vacuum_day),
            backup_retention_count=m.get("backup_retention_count", config.maintenance.backup_retention_count),
        )

    if "fleet" in raw:
        fl = raw["fleet"]
        config.fleet = FleetConfig(
            health_check_interval_seconds=fl.get("health_check_interval_seconds", config.fleet.health_check_interval_seconds),
            health_check_timeout_seconds=fl.get("health_check_timeout_seconds", config.fleet.health_check_timeout_seconds),
            unhealthy_threshold=fl.get("unhealthy_threshold", config.fleet.unhealthy_threshold),
            max_restarts=fl.get("max_restarts", config.fleet.max_restarts),
            restart_backoff_base=fl.get("restart_backoff_base", config.fleet.restart_backoff_base),
            auto_restart_enabled=fl.get("auto_restart_enabled", config.fleet.auto_restart_enabled),
        )

    if "session" in raw:
        se = raw["session"]
        config.session = SessionConfig(
            checkpoint_interval_seconds=se.get("checkpoint_interval_seconds", config.session.checkpoint_interval_seconds),
            max_corrections_loaded=se.get("max_corrections_loaded", config.session.max_corrections_loaded),
            max_learnings_loaded=se.get("max_learnings_loaded", config.session.max_learnings_loaded),
            max_decisions_loaded=se.get("max_decisions_loaded", config.session.max_decisions_loaded),
            recency_weight=se.get("recency_weight", config.session.recency_weight),
        )

    if "github" in raw:
        gh = raw["github"]
        config.github = GitHubConfig(
            org=gh.get("org", config.github.org),
            clone_base_path=gh.get("clone_base_path", config.github.clone_base_path),
            use_ssh=gh.get("use_ssh", config.github.use_ssh),
            auto_clone_on_missing_dep=gh.get("auto_clone_on_missing_dep", config.github.auto_clone_on_missing_dep),
            max_auto_clones_per_session=gh.get("max_auto_clones_per_session", config.github.max_auto_clones_per_session),
            default_branch=gh.get("default_branch", config.github.default_branch),
            clone_depth=gh.get("clone_depth", config.github.clone_depth),
        )

    return config

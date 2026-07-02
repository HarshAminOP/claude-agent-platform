"""Shared data models and SQLite schema for the Claude Agent Platform."""

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


def _resolve_data_dir() -> Path:
    """Resolve data directory via cap.config (avoids circular import)."""
    from cap.config import get_data_dir
    return get_data_dir()


class ModelTier(str, Enum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# Slot weights for adaptive concurrency pool
MODEL_SLOT_WEIGHTS = {
    ModelTier.OPUS: 3,
    ModelTier.SONNET: 2,
    ModelTier.HAIKU: 1,
}

# Cost per 1M tokens (USD) — Bedrock EU pricing
MODEL_PRICING = {
    ModelTier.OPUS: {"input": 15.0, "output": 75.0},
    ModelTier.SONNET: {"input": 3.0, "output": 15.0},
    ModelTier.HAIKU: {"input": 0.80, "output": 4.0},
}


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class WorkflowConfig:
    max_tokens: int = 500_000
    max_agents: int = 15
    max_retries: int = 2
    backoff_base_seconds: float = 30.0
    backoff_max_seconds: float = 120.0


@dataclass
class ConcurrencyConfig:
    min_slots: int = 3
    max_slots: int = 8
    initial_slots: int = 4
    scale_up_after_seconds: float = 60.0
    scale_down_on_throttle: bool = True


@dataclass
class PlatformConfig:
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    data_dir: Path = field(default_factory=lambda: _resolve_data_dir())


SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Workflow runs
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    budget_tokens INTEGER NOT NULL DEFAULT 500000,
    max_agents INTEGER NOT NULL DEFAULT 15,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    agents_spawned INTEGER NOT NULL DEFAULT 0,
    killed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status);

-- Workflow events (phase transitions, agent completions)
CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    event_type TEXT NOT NULL,
    agent_id TEXT,
    phase TEXT,
    message TEXT,
    tokens_delta INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_workflow ON workflow_events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON workflow_events(timestamp);

-- API call log (every Bedrock invocation)
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    agent_id TEXT,
    model_tier TEXT NOT NULL,
    model_id TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    cost_usd REAL NOT NULL DEFAULT 0.0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_workflow ON api_calls(workflow_id);
CREATE INDEX IF NOT EXISTS idx_api_timestamp ON api_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_model ON api_calls(model_tier);

-- System state
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

PRAGMA user_version = 1;
"""


def init_database(db_path: Path) -> sqlite3.Connection:
    """Initialize platform database with schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version < SCHEMA_VERSION:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    return conn

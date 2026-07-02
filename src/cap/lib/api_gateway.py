"""APIGateway — concurrency pool and cost accounting for workflow engine.

Provides workflow-scoped usage tracking and concurrency slot management.
Reads usage from the api_calls table written by Bedrock invocations.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from cap.lib.models import ConcurrencyConfig, ModelTier, MODEL_PRICING


@dataclass
class _PoolState:
    current_slots: int = 4
    active_calls: int = 0

    def available_slots(self) -> int:
        return max(0, self.current_slots - self.active_calls)


class APIGateway:
    """Concurrency-aware API gateway with cost tracking.

    Manages a slot-based concurrency pool and provides read-only cost
    reporting from the api_calls table.  It does NOT make Bedrock calls
    directly — that responsibility lives in AgentExecutor / ConverseExecutor.
    """

    def __init__(self, db_path: Path, config: ConcurrencyConfig | None = None) -> None:
        self._db_path = str(db_path)
        self._config = config or ConcurrencyConfig()
        self._pool = _PoolState(current_slots=self._config.initial_slots)

    def _conn(self) -> sqlite3.Connection:
        """Open a short-lived read connection to the platform DB."""
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------------
    # Concurrency pool helpers
    # ------------------------------------------------------------------

    def pool_status(self) -> dict:
        """Return current pool slot metrics."""
        return {
            "current_slots": self._pool.current_slots,
            "active_calls": self._pool.active_calls,
            "available_slots": self._pool.available_slots(),
            "min_slots": self._config.min_slots,
            "max_slots": self._config.max_slots,
        }

    # ------------------------------------------------------------------
    # Cost reporting
    # ------------------------------------------------------------------

    def estimate_cost(self, tier: ModelTier, tokens: int) -> float:
        """Estimate USD cost for *tokens* tokens on *tier* (input pricing used)."""
        pricing = MODEL_PRICING.get(tier, {"input": 3.0, "output": 15.0})
        return round(tokens / 1_000_000 * pricing["input"], 6)

    def get_usage_by_model(self, workflow_id: str) -> dict:
        """Return per-model-tier token and cost totals for *workflow_id*."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT model_tier, SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)
                   FROM api_calls
                   WHERE workflow_id = ?
                   GROUP BY model_tier""",
                (workflow_id,),
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

        return {
            row[0]: {
                "input_tokens": row[1] or 0,
                "output_tokens": row[2] or 0,
                "cost_usd": round(row[3] or 0.0, 6),
            }
            for row in rows
        }

    def get_usage(self, workflow_id: str) -> dict:
        """Return aggregate token and cost totals for *workflow_id*."""
        conn = self._conn()
        try:
            row = conn.execute(
                """SELECT SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), COUNT(*)
                   FROM api_calls
                   WHERE workflow_id = ?""",
                (workflow_id,),
            ).fetchone()
        except sqlite3.Error:
            return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "call_count": 0}
        finally:
            conn.close()

        return {
            "input_tokens": row[0] or 0,
            "output_tokens": row[1] or 0,
            "cost_usd": round(row[2] or 0.0, 6),
            "call_count": row[3] or 0,
        }

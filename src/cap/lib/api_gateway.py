"""API Gateway — adaptive concurrency, rate limiting, budget enforcement, cost tracking.

Protects shared corporate Bedrock endpoint from overload.
Every Bedrock API call routes through this layer.
"""

import logging
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    MODEL_PRICING,
    MODEL_SLOT_WEIGHTS,
    ConcurrencyConfig,
    ModelTier,
    WorkflowStatus,
    init_database,
)

logger = logging.getLogger("platform.gateway")


class BudgetExhaustedError(Exception):
    """Workflow has exceeded its token budget."""
    pass


class WorkflowKilledError(Exception):
    """Workflow was killed by user."""
    pass


class MonthlyBudgetExceededError(Exception):
    """Monthly spending cap has been exceeded."""
    pass


class RateLimitedError(Exception):
    """Concurrency pool full, request queued too long."""
    pass


@dataclass
class AdaptivePool:
    """Adaptive concurrency pool with cost-weighted slots.

    Starts conservative, scales up if no throttling, halves on any throttle signal.
    Slot weights: opus=3, sonnet=2, haiku=1.
    Pool of N slots means you can run N/3 opus OR N/2 sonnet OR N haiku concurrently.
    """

    config: ConcurrencyConfig
    _current_slots: int = 0
    _used_slots: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _condition: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()), repr=False)
    _last_throttle: float = 0.0
    _last_scale_up: float = 0.0
    _throttle_count: int = 0
    _success_streak: int = 0

    def __post_init__(self):
        self._current_slots = self.config.initial_slots
        self._lock = threading.Lock()
        self._condition = threading.Condition(threading.Lock())

    @property
    def available_slots(self) -> int:
        return self._current_slots - self._used_slots

    @property
    def current_capacity(self) -> int:
        return self._current_slots

    def acquire(self, model_tier: ModelTier, timeout: float = 60.0) -> bool:
        """Acquire slots for a model call. Blocks until slots available or timeout."""
        weight = MODEL_SLOT_WEIGHTS[model_tier]
        deadline = time.monotonic() + timeout

        with self._condition:
            while self._used_slots + weight > self._current_slots:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

            self._used_slots += weight
            return True

    def release(self, model_tier: ModelTier, throttled: bool = False):
        """Release slots after a call completes. Signal throttle if needed."""
        weight = MODEL_SLOT_WEIGHTS[model_tier]
        now = time.monotonic()

        with self._condition:
            self._used_slots = max(0, self._used_slots - weight)

            if throttled:
                self._on_throttle(now)
            else:
                self._on_success(now)

            self._condition.notify_all()

    def _on_throttle(self, now: float):
        """Halve capacity on throttle signal."""
        self._throttle_count += 1
        self._success_streak = 0
        self._last_throttle = now

        new_slots = max(self.config.min_slots, self._current_slots // 2)
        if new_slots < self._current_slots:
            logger.warning(
                "Throttle detected. Reducing concurrency: %d → %d",
                self._current_slots, new_slots
            )
            self._current_slots = new_slots

    def _on_success(self, now: float):
        """Scale up after sustained success."""
        self._success_streak += 1

        time_since_throttle = now - self._last_throttle if self._last_throttle else float("inf")
        time_since_scale = now - self._last_scale_up if self._last_scale_up else float("inf")

        if (
            self._success_streak >= 5
            and time_since_throttle > self.config.scale_up_after_seconds
            and time_since_scale > self.config.scale_up_after_seconds
            and self._current_slots < self.config.max_slots
        ):
            self._current_slots += 1
            self._last_scale_up = now
            self._success_streak = 0
            logger.info("Scaling up concurrency: → %d", self._current_slots)

    def status(self) -> dict:
        return {
            "current_slots": self._current_slots,
            "used_slots": self._used_slots,
            "available_slots": self.available_slots,
            "throttle_count": self._throttle_count,
            "min_slots": self.config.min_slots,
            "max_slots": self.config.max_slots,
        }


class APIGateway:
    """Central rate limiter, budget enforcer, and cost tracker for Bedrock calls."""

    # Default monthly cap in USD — override via MONTHLY_BUDGET_USD env var or system_state table
    DEFAULT_MONTHLY_CAP_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "150.0"))

    def __init__(self, db_path: Path, concurrency_config: ConcurrencyConfig = None):
        self._db = init_database(db_path)
        self._pool = AdaptivePool(config=concurrency_config or ConcurrencyConfig())
        self._db_lock = threading.Lock()

    def check_monthly_budget(self) -> dict:
        """Check monthly spend against cap. Raises MonthlyBudgetExceededError if over.

        Returns dict with month_spend_usd, monthly_cap_usd, remaining_usd.
        Monthly cap is read from system_state table (key='monthly_budget_usd')
        or falls back to DEFAULT_MONTHLY_CAP_USD.
        """
        # Determine the cap: check system_state first, then env/default
        cap_row = self._db.execute(
            "SELECT value FROM system_state WHERE key = 'monthly_budget_usd'"
        ).fetchone()
        monthly_cap = float(cap_row[0]) if cap_row else self.DEFAULT_MONTHLY_CAP_USD

        # Sum cost_usd for the current calendar month (UTC)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_calls WHERE timestamp >= ?",
            (month_start,)
        ).fetchone()
        month_spend = row[0]

        if month_spend >= monthly_cap:
            raise MonthlyBudgetExceededError(
                f"Monthly budget exhausted: ${month_spend:.2f} spent of ${monthly_cap:.2f} cap "
                f"(month started {month_start})"
            )

        return {
            "month_spend_usd": round(month_spend, 4),
            "monthly_cap_usd": monthly_cap,
            "remaining_usd": round(monthly_cap - month_spend, 4),
        }

    def check_budget(self, workflow_id: str) -> tuple[int, int]:
        """Check remaining budget. Returns (tokens_used, budget_tokens).
        Raises BudgetExhaustedError if over budget.
        Raises WorkflowKilledError if workflow was killed.
        """
        row = self._db.execute(
            "SELECT tokens_used, budget_tokens, killed, status FROM workflows WHERE id = ?",
            (workflow_id,)
        ).fetchone()

        if not row:
            return (0, 500_000)  # No workflow tracking — use default budget

        tokens_used, budget_tokens, killed, status = row

        if killed:
            raise WorkflowKilledError(f"Workflow {workflow_id} was killed by user")

        if status in (WorkflowStatus.KILLED, WorkflowStatus.FAILED):
            raise WorkflowKilledError(f"Workflow {workflow_id} is {status}")

        if tokens_used >= budget_tokens:
            raise BudgetExhaustedError(
                f"Workflow {workflow_id} exhausted budget: {tokens_used}/{budget_tokens} tokens"
            )

        return (tokens_used, budget_tokens)

    def pre_call(self, workflow_id: str, model_tier: ModelTier) -> bool:
        """Pre-call check: monthly cap + workflow budget + kill switch + acquire concurrency slot.
        Returns True if call can proceed. Raises on kill/budget/monthly cap.
        """
        # Monthly budget gate — applies to all calls regardless of workflow
        self.check_monthly_budget()

        if workflow_id:
            self.check_budget(workflow_id)

        acquired = self._pool.acquire(model_tier, timeout=60.0)
        if not acquired:
            raise RateLimitedError(
                f"Could not acquire slot for {model_tier.value} within 60s. "
                f"Pool: {self._pool.status()}"
            )
        return True

    def post_call(
        self,
        workflow_id: str,
        agent_id: str,
        model_tier: ModelTier,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        throttled: bool = False,
        error: str = None,
    ):
        """Post-call: release slot, log usage, update budget."""
        self._pool.release(model_tier, throttled=throttled)

        total_tokens = input_tokens + output_tokens
        cost = self._calculate_cost(model_tier, input_tokens, output_tokens)
        status = "throttled" if throttled else ("error" if error else "success")
        now = datetime.now(timezone.utc).isoformat()

        with self._db_lock:
            self._db.execute(
                """INSERT INTO api_calls
                   (workflow_id, agent_id, model_tier, model_id, input_tokens,
                    output_tokens, latency_ms, status, cost_usd, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (workflow_id, agent_id, model_tier.value, model_id,
                 input_tokens, output_tokens, latency_ms, status, cost, now)
            )

            if workflow_id and status == "success":
                self._db.execute(
                    "UPDATE workflows SET tokens_used = tokens_used + ? WHERE id = ?",
                    (total_tokens, workflow_id)
                )

            self._db.commit()

    def get_usage(self, workflow_id: str = None) -> dict:
        """Get usage stats. If workflow_id, scoped to that workflow. Otherwise global."""
        if workflow_id:
            row = self._db.execute(
                """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
                          SUM(cost_usd), SUM(latency_ms)
                   FROM api_calls WHERE workflow_id = ?""",
                (workflow_id,)
            ).fetchone()
        else:
            row = self._db.execute(
                """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
                          SUM(cost_usd), SUM(latency_ms)
                   FROM api_calls"""
            ).fetchone()

        calls, inp, out, cost, latency = row
        return {
            "total_calls": calls or 0,
            "input_tokens": inp or 0,
            "output_tokens": out or 0,
            "total_tokens": (inp or 0) + (out or 0),
            "cost_usd": round(cost or 0, 4),
            "avg_latency_ms": round((latency or 0) / max(calls or 1, 1)),
        }

    def get_usage_by_model(self, workflow_id: str = None) -> dict:
        """Breakdown by model tier."""
        where = "WHERE workflow_id = ?" if workflow_id else ""
        params = (workflow_id,) if workflow_id else ()

        rows = self._db.execute(
            f"""SELECT model_tier, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)
                FROM api_calls {where} GROUP BY model_tier""",
            params
        ).fetchall()

        return {
            row[0]: {
                "calls": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
                "cost_usd": round(row[4], 4),
            }
            for row in rows
        }

    def pool_status(self) -> dict:
        """Current state of the adaptive concurrency pool."""
        return self._pool.status()

    def _calculate_cost(self, model_tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
        pricing = MODEL_PRICING[model_tier]
        cost = (input_tokens / 1_000_000) * pricing["input"] + \
               (output_tokens / 1_000_000) * pricing["output"]
        return round(cost, 6)

    def estimate_cost(self, model_tier: ModelTier, estimated_tokens: int) -> float:
        """Estimate cost for a given token count (assumes 30% input, 70% output)."""
        input_est = int(estimated_tokens * 0.3)
        output_est = int(estimated_tokens * 0.7)
        return self._calculate_cost(model_tier, input_est, output_est)

    def close(self):
        if self._db:
            self._db.close()

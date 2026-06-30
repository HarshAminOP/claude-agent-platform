"""
CAP Cost Tracker — Track token usage, estimate costs, enforce daily budget.

Provides:
- MODEL_PRICING: per-1M-token pricing for supported models
- CostTracker: track(), estimate(), budget_check(), get_workflow_cost()
"""

import sqlite3
import time

# Pricing per 1M tokens (as of 2024, update via cap config)
MODEL_PRICING = {
    "opus": {"input": 15.00, "output": 75.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "haiku": {"input": 0.25, "output": 1.25},
}


class CostTracker:
    """Track token usage, estimate costs, enforce daily budget."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def track(
        self,
        agent_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        workflow_id: str = None,
    ) -> None:
        """Record a completed agent call's cost."""
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["sonnet"])
        cost = (
            input_tokens * pricing["input"] + output_tokens * pricing["output"]
        ) / 1_000_000
        self.db.execute(
            """
            INSERT INTO cost_events (agent_type, model, input_tokens, output_tokens, cost_usd, workflow_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_type, model, input_tokens, output_tokens, cost, workflow_id, time.time()),
        )
        self.db.commit()

    def estimate(self, agent_type: str, task_complexity: str) -> dict:
        """Estimate cost before execution based on historical data."""
        row = self.db.execute(
            """
            SELECT AVG(input_tokens), AVG(output_tokens), AVG(cost_usd), COUNT(*)
            FROM cost_events WHERE agent_type = ?
            AND timestamp > ?
            """,
            (agent_type, time.time() - 7 * 86400),
        ).fetchone()

        if row and row[3] >= 5:  # Need at least 5 samples
            avg_input, avg_output, avg_cost, samples = row
            # Adjust by complexity multiplier
            multiplier = {"inline": 0.3, "lightweight": 1.0, "full": 2.5}.get(
                task_complexity, 1.0
            )
            return {
                "estimated_cost_usd": round(avg_cost * multiplier, 4),
                "estimated_tokens": int((avg_input + avg_output) * multiplier),
                "confidence": "high" if samples >= 20 else "medium",
                "based_on_samples": samples,
            }

        # Fallback: estimate from model pricing and typical sizes
        model = (
            "opus"
            if agent_type in ("orchestrator", "security", "aws-architect")
            else "sonnet"
        )
        typical_tokens = {"inline": 2000, "lightweight": 15000, "full": 50000}.get(
            task_complexity, 15000
        )
        pricing = MODEL_PRICING[model]
        est_cost = (typical_tokens * (pricing["input"] + pricing["output"]) / 2) / 1_000_000
        return {
            "estimated_cost_usd": round(est_cost, 4),
            "estimated_tokens": typical_tokens,
            "confidence": "low",
            "based_on_samples": 0,
        }

    def budget_check(self) -> dict:
        """Check if daily budget allows more spending."""
        row = self.db.execute(
            """
            SELECT SUM(cost_usd) FROM cost_events WHERE timestamp > ?
            """,
            (time.time() - 86400,),
        ).fetchone()
        spent_today = row[0] or 0.0

        cap_row = self.db.execute(
            "SELECT value FROM runtime_state WHERE key = 'daily_budget_usd'"
        ).fetchone()
        daily_cap = float(cap_row[0]) if cap_row else 5.0
        remaining = daily_cap - spent_today

        return {
            "spent_today_usd": round(spent_today, 4),
            "daily_cap_usd": daily_cap,
            "remaining_usd": round(remaining, 4),
            "allowed": remaining > 0,
            "mode": (
                "online"
                if remaining > daily_cap * 0.2
                else "degraded" if remaining > 0 else "offline"
            ),
        }

    def get_workflow_cost(self, workflow_id: str) -> dict:
        """Real-time cost for active workflow (displayed during execution)."""
        row = self.db.execute(
            """
            SELECT SUM(cost_usd), SUM(input_tokens + output_tokens), COUNT(*)
            FROM cost_events WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        return {
            "total_cost_usd": round(row[0] or 0, 4),
            "total_tokens": row[1] or 0,
            "agent_calls": row[2] or 0,
        }

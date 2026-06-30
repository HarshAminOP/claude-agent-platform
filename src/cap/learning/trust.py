"""
CAP Progressive Trust Manager.

Manages per-agent trust levels using Bayesian updates.
Trust determines autonomy: auto-approve, require confirmation, or deny.

Trust starts at 0.5 (neutral). Increases with success, decreases with failure.
Thresholds:
- trust > 0.8  -> auto-approve (agent acts without user confirmation)
- 0.3 <= trust <= 0.8 -> confirm (ask user before executing)
- trust < 0.3  -> deny (block action, require explicit user override)

Uses Beta distribution posterior: trust = (successes + 1) / (successes + failures + 2)
"""

import sqlite3
import time
from typing import Literal

AutonomyLevel = Literal["auto", "confirm", "deny"]

# Thresholds for autonomy decisions
AUTO_APPROVE_THRESHOLD = 0.8
DENY_THRESHOLD = 0.3

# Default trust for new agent+action combos
DEFAULT_TRUST = 0.5


class TrustManager:
    """
    Progressive trust system for agent autonomy management.

    Tracks per-agent, per-action trust scores based on historical outcomes.
    Provides autonomy level decisions based on accumulated trust.
    """

    def __init__(self, db: sqlite3.Connection):
        """
        Initialize TrustManager with a database connection.

        Args:
            db: SQLite connection with CAP schema (trust_levels table).
        """
        self.db = db

    def get_trust_level(self, agent_type: str, action_type: str = "general") -> float:
        """
        Get the current trust score for an agent+action pair.

        Args:
            agent_type: Type of agent (e.g., 'dev', 'devops', 'security', 'sre').
            action_type: Type of action (e.g., 'refactor', 'deploy', 'general').

        Returns:
            Trust score between 0.0 and 1.0. Returns 0.5 if no history exists.
        """
        row = self.db.execute(
            """SELECT trust_score FROM trust_levels
               WHERE agent_type = ? AND action_type = ?""",
            (agent_type, action_type),
        ).fetchone()

        if row:
            return row[0] if isinstance(row, tuple) else row["trust_score"]
        return DEFAULT_TRUST

    def record_outcome(
        self,
        agent_type: str,
        success: bool,
        action_type: str = "general",
    ) -> float:
        """
        Record an outcome and update trust via Bayesian update.

        Uses Beta(1,1) prior (uniform). Posterior mean:
            trust = (successes + 1) / (successes + failures + 2)

        Args:
            agent_type: Type of agent.
            success: Whether the action succeeded.
            action_type: Type of action performed.

        Returns:
            The updated trust score.
        """
        now = time.time()

        row = self.db.execute(
            """SELECT trust_score, success_count, failure_count
               FROM trust_levels
               WHERE agent_type = ? AND action_type = ?""",
            (agent_type, action_type),
        ).fetchone()

        if row:
            successes = row[1] if isinstance(row, tuple) else row["success_count"]
            failures = row[2] if isinstance(row, tuple) else row["failure_count"]

            if success:
                successes += 1
            else:
                failures += 1

            # Bayesian posterior mean with Beta(1,1) prior
            new_score = (successes + 1) / (successes + failures + 2)

            self.db.execute(
                """UPDATE trust_levels
                   SET trust_score = ?, success_count = ?, failure_count = ?, last_updated = ?
                   WHERE agent_type = ? AND action_type = ?""",
                (new_score, successes, failures, now, agent_type, action_type),
            )
        else:
            # First record: start slightly biased based on outcome
            successes = 1 if success else 0
            failures = 0 if success else 1
            # Beta posterior: (1+1)/(1+0+2) = 0.667 for success, (0+1)/(0+1+2) = 0.333 for failure
            new_score = (successes + 1) / (successes + failures + 2)

            self.db.execute(
                """INSERT INTO trust_levels
                   (agent_type, action_type, trust_score, success_count, failure_count, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent_type, action_type, new_score, successes, failures, now),
            )

        self.db.commit()
        return new_score

    def get_autonomy_level(
        self,
        agent_type: str,
        action_type: str = "general",
    ) -> AutonomyLevel:
        """
        Determine the autonomy level for an agent+action pair.

        Decision logic:
        - trust > 0.8: 'auto' — agent proceeds without user confirmation
        - 0.3 <= trust <= 0.8: 'confirm' — requires user approval
        - trust < 0.3: 'deny' — action blocked, user must explicitly override

        Args:
            agent_type: Type of agent.
            action_type: Type of action.

        Returns:
            One of 'auto', 'confirm', 'deny'.
        """
        trust = self.get_trust_level(agent_type, action_type)

        if trust > AUTO_APPROVE_THRESHOLD:
            return "auto"
        elif trust < DENY_THRESHOLD:
            return "deny"
        else:
            return "confirm"

    def get_agent_summary(self, agent_type: str) -> dict:
        """
        Get a summary of trust across all action types for an agent.

        Args:
            agent_type: Type of agent.

        Returns:
            Dict with action_type -> {trust_score, success_count, failure_count, autonomy}.
        """
        rows = self.db.execute(
            """SELECT action_type, trust_score, success_count, failure_count
               FROM trust_levels
               WHERE agent_type = ?""",
            (agent_type,),
        ).fetchall()

        summary = {}
        for row in rows:
            action = row[0] if isinstance(row, tuple) else row["action_type"]
            score = row[1] if isinstance(row, tuple) else row["trust_score"]
            successes = row[2] if isinstance(row, tuple) else row["success_count"]
            failures = row[3] if isinstance(row, tuple) else row["failure_count"]

            if score > AUTO_APPROVE_THRESHOLD:
                autonomy = "auto"
            elif score < DENY_THRESHOLD:
                autonomy = "deny"
            else:
                autonomy = "confirm"

            summary[action] = {
                "trust_score": score,
                "success_count": successes,
                "failure_count": failures,
                "autonomy": autonomy,
            }

        return summary

    def reset_trust(self, agent_type: str, action_type: str = "general") -> None:
        """
        Reset trust for an agent+action pair back to neutral (0.5).

        Useful when agent behavior has fundamentally changed (e.g., updated prompts).

        Args:
            agent_type: Type of agent.
            action_type: Type of action.
        """
        self.db.execute(
            """UPDATE trust_levels
               SET trust_score = 0.5, success_count = 0, failure_count = 0, last_updated = ?
               WHERE agent_type = ? AND action_type = ?""",
            (time.time(), agent_type, action_type),
        )
        self.db.commit()

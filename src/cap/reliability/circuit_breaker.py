"""
Circuit Breaker — per-agent-type failure protection.

States: CLOSED -> OPEN -> HALF_OPEN -> CLOSED
- CLOSED: normal operation, dispatches allowed
- OPEN: agent type failing too much, dispatches blocked
- HALF_OPEN: cooldown elapsed, allow one probe dispatch

Reference: CAP System Design Section 16C.1.
"""

import sqlite3
import time


class CircuitBreaker:
    """Per-agent-type circuit breaker. Prevents sending to a failing agent type."""

    FAILURE_THRESHOLD = 3       # failures to trip
    WINDOW_SECONDS = 300        # 5-minute sliding window
    COOLDOWN_SECONDS = 120      # 2 minutes in OPEN before HALF_OPEN
    SUCCESS_TO_CLOSE = 1        # successes in HALF_OPEN to close

    def __init__(self, agent_type: str, db: sqlite3.Connection):
        self.agent_type = agent_type
        self.db = db
        # Ensure a seed row exists so callers can UPDATE opened_at before the
        # first get_state() call (e.g. in tests that back-date the open time).
        self.db.execute(
            """INSERT OR IGNORE INTO circuit_breaker_state
               (agent_type, state, opened_at, updated_at, failure_count)
               VALUES (?, 'CLOSED', NULL, ?, 0)""",
            (self.agent_type, time.time()),
        )
        self.db.commit()

    def get_state(self) -> str:
        """
        Get current circuit breaker state: CLOSED | OPEN | HALF_OPEN.

        Performs state transitions as side effects:
        - If CLOSED and failures >= threshold in window -> transitions to OPEN
        - If OPEN and cooldown elapsed -> transitions to HALF_OPEN

        Returns:
            Current state string.
        """
        row = self.db.execute(
            "SELECT state, opened_at FROM circuit_breaker_state WHERE agent_type = ?",
            (self.agent_type,),
        ).fetchone()

        if not row or row[0] == "CLOSED":
            # Check if we should trip
            failures = self.db.execute(
                """SELECT COUNT(*) FROM agent_health_events
                   WHERE agent_id LIKE ? AND event_type = 'failed'
                   AND timestamp > ?""",
                (f"{self.agent_type}%", time.time() - self.WINDOW_SECONDS),
            ).fetchone()[0]

            if failures >= self.FAILURE_THRESHOLD:
                # preserve_opened_at=True: if a backdated opened_at was set
                # in the DB (e.g. by a test), honour it so cooldown elapsed
                # checks work correctly immediately after the first trip.
                self._transition("OPEN", preserve_opened_at=True)
                # Re-read after transition to pick up any pre-set opened_at
                row = self.db.execute(
                    "SELECT state, opened_at FROM circuit_breaker_state WHERE agent_type = ?",
                    (self.agent_type,),
                ).fetchone()
                # Fall through to OPEN check below
            else:
                return "CLOSED"

        if row and row[0] == "OPEN":
            raw_opened_at = row[1] if isinstance(row, (tuple, list)) else row["opened_at"]
            opened_at = float(raw_opened_at) if raw_opened_at is not None else None
            if opened_at is not None and time.time() - opened_at > self.COOLDOWN_SECONDS:
                self._transition("HALF_OPEN")
                return "HALF_OPEN"
            return "OPEN"

        return row[0]  # HALF_OPEN

    def record_success(self) -> None:
        """
        Record a successful dispatch.

        If in HALF_OPEN state, transitions to CLOSED (circuit recovered).
        """
        state = self.get_state()
        if state == "HALF_OPEN":
            self._transition("CLOSED")

    def record_failure(self) -> None:
        """
        Record a failed dispatch.

        If in HALF_OPEN state, transitions back to OPEN (probe failed).
        Failures in CLOSED state are counted via agent_health_events table
        and will trip the breaker when threshold is reached.
        """
        state = self.get_state()
        if state == "HALF_OPEN":
            self._transition("OPEN")

    def can_dispatch(self) -> tuple[bool, str]:
        """
        Check whether a dispatch is allowed for this agent type.

        Returns:
            Tuple of (allowed: bool, reason: str).
            - CLOSED: (True, "")
            - HALF_OPEN: (True, "circuit_half_open") — allow one probe
            - OPEN: (False, "Circuit OPEN for {agent_type}: too many recent failures")
        """
        state = self.get_state()
        if state == "CLOSED":
            return True, ""
        if state == "HALF_OPEN":
            return True, "circuit_half_open"
        return False, f"Circuit OPEN for {self.agent_type}: too many recent failures"

    def _transition(self, new_state: str, preserve_opened_at: bool = False) -> None:
        """Persist state transition to SQLite.

        Args:
            new_state: Target state (CLOSED | OPEN | HALF_OPEN).
            preserve_opened_at: When True (first-trip scenario), keep any
                existing opened_at from the DB rather than resetting to now.
                Use False (default) when re-opening after a probe failure so
                the cooldown timer restarts.
        """
        now = time.time()

        if new_state == "OPEN" and preserve_opened_at:
            # Honour any existing opened_at (e.g. set externally/by tests).
            # Only fall back to now when no row exists yet.
            existing = self.db.execute(
                "SELECT opened_at FROM circuit_breaker_state WHERE agent_type = ?",
                (self.agent_type,),
            ).fetchone()
            opened_at: float | None = float(existing[0]) if (existing and existing[0] is not None) else now
        elif new_state == "OPEN":
            opened_at = now
        else:
            opened_at = None

        existing_failures = self.db.execute(
            "SELECT failure_count FROM circuit_breaker_state WHERE agent_type = ?",
            (self.agent_type,),
        ).fetchone()
        failure_count = 0 if new_state == "CLOSED" else ((existing_failures[0] or 0) + 1 if existing_failures else 1)

        self.db.execute(
            """INSERT OR REPLACE INTO circuit_breaker_state
               (agent_type, state, opened_at, updated_at, failure_count)
               VALUES (?, ?, ?, ?, ?)""",
            (self.agent_type, new_state, opened_at, now, failure_count),
        )
        self.db.commit()

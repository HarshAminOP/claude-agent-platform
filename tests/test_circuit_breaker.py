"""Tests for CAP circuit breaker (reliability/circuit_breaker.py)."""
import pytest
import sys
import time
import sqlite3
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.db import get_db, migrate
from cap.reliability.circuit_breaker import CircuitBreaker


@pytest.fixture
def db(tmp_path):
    """Provide a migrated database connection."""
    db_path = str(tmp_path / "test_cb.db")
    conn = get_db(db_path)
    migrate(conn)
    yield conn
    conn.close()


def _record_failures(db, agent_type: str, count: int, timestamp: float = None):
    """Helper to record N failure events for an agent type."""
    if timestamp is None:
        timestamp = time.time()
    for i in range(count):
        db.execute(
            """INSERT INTO agent_health_events
               (agent_id, event_type, timestamp)
               VALUES (?, ?, ?)""",
            (f"{agent_type}-instance-{i}", "failed", timestamp - i),
        )
    db.commit()


class TestClosedToOpen:
    """Test CLOSED -> OPEN transition after 3 failures."""

    def test_stays_closed_below_threshold(self, db):
        """With fewer than 3 failures, circuit stays CLOSED."""
        _record_failures(db, "dev", 2)
        cb = CircuitBreaker("dev", db)
        assert cb.get_state() == "CLOSED"

    def test_opens_at_threshold(self, db):
        """With exactly 3 failures in window, circuit opens."""
        _record_failures(db, "dev", 3)
        cb = CircuitBreaker("dev", db)
        assert cb.get_state() == "OPEN"

    def test_opens_above_threshold(self, db):
        """With more than 3 failures, circuit is definitely OPEN."""
        _record_failures(db, "dev", 5)
        cb = CircuitBreaker("dev", db)
        assert cb.get_state() == "OPEN"

    def test_old_failures_outside_window_ignored(self, db):
        """Failures older than 5 minutes should not count."""
        old_time = time.time() - 600  # 10 minutes ago (outside 300s window)
        _record_failures(db, "dev", 5, timestamp=old_time)
        cb = CircuitBreaker("dev", db)
        assert cb.get_state() == "CLOSED"

    def test_dispatch_blocked_when_open(self, db):
        """can_dispatch should return False when OPEN."""
        _record_failures(db, "dev", 3)
        cb = CircuitBreaker("dev", db)
        allowed, reason = cb.can_dispatch()
        assert allowed is False
        assert "OPEN" in reason

    def test_dispatch_allowed_when_closed(self, db):
        """can_dispatch should return True when CLOSED."""
        cb = CircuitBreaker("dev", db)
        allowed, reason = cb.can_dispatch()
        assert allowed is True
        assert reason == ""


class TestOpenToHalfOpen:
    """Test OPEN -> HALF_OPEN transition after cooldown."""

    def test_stays_open_before_cooldown(self, db):
        """Circuit stays OPEN before cooldown elapses."""
        _record_failures(db, "devops", 3)
        cb = CircuitBreaker("devops", db)
        assert cb.get_state() == "OPEN"

        # Immediately check again -- still OPEN
        assert cb.get_state() == "OPEN"

    def test_transitions_to_half_open_after_cooldown(self, db):
        """After cooldown (120s), circuit should transition to HALF_OPEN."""
        _record_failures(db, "devops", 3)
        cb = CircuitBreaker("devops", db)
        assert cb.get_state() == "OPEN"

        # Manually set opened_at to past (simulate cooldown elapsed)
        db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "devops"),  # 200s ago > 120s cooldown
        )
        db.commit()

        assert cb.get_state() == "HALF_OPEN"

    def test_dispatch_allowed_in_half_open(self, db):
        """In HALF_OPEN, one probe dispatch should be allowed."""
        _record_failures(db, "security", 3)
        cb = CircuitBreaker("security", db)
        cb.get_state()  # Trigger OPEN transition

        # Simulate cooldown elapsed
        db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "security"),
        )
        db.commit()

        allowed, reason = cb.can_dispatch()
        assert allowed is True
        assert reason == "circuit_half_open"


class TestHalfOpenToClosed:
    """Test HALF_OPEN -> CLOSED on success."""

    def test_success_closes_circuit(self, db):
        """A successful dispatch in HALF_OPEN should close the circuit."""
        # Setup: get to HALF_OPEN state
        _record_failures(db, "sre", 3)
        cb = CircuitBreaker("sre", db)
        cb.get_state()  # OPEN

        # Fast-forward past cooldown
        db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "sre"),
        )
        db.commit()

        assert cb.get_state() == "HALF_OPEN"

        # Record success
        cb.record_success()

        # Should now be CLOSED
        # Need to clear the failure events so get_state doesn't re-open
        db.execute("DELETE FROM agent_health_events WHERE agent_id LIKE 'sre%'")
        db.commit()

        assert cb.get_state() == "CLOSED"

    def test_failure_in_half_open_reopens(self, db):
        """A failure in HALF_OPEN should reopen the circuit."""
        _record_failures(db, "test-agent", 3)
        cb = CircuitBreaker("test-agent", db)
        cb.get_state()  # OPEN

        # Fast-forward past cooldown
        db.execute(
            "UPDATE circuit_breaker_state SET opened_at = ? WHERE agent_type = ?",
            (time.time() - 200, "test-agent"),
        )
        db.commit()

        assert cb.get_state() == "HALF_OPEN"

        # Record failure -- should go back to OPEN
        cb.record_failure()
        assert cb.get_state() == "OPEN"


class TestPerAgentIsolation:
    """Test that circuit breakers are isolated per agent type."""

    def test_different_agents_independent(self, db):
        """Failures in one agent type should not affect another."""
        _record_failures(db, "dev", 5)

        cb_dev = CircuitBreaker("dev", db)
        cb_devops = CircuitBreaker("devops", db)

        assert cb_dev.get_state() == "OPEN"
        assert cb_devops.get_state() == "CLOSED"

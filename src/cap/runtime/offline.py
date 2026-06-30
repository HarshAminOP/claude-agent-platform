"""
CAP Offline Detector — Detect network/budget state and switch modes.

Provides:
- OfflineDetector: get_mode(), is_offline(), is_degraded(), should_skip_network_ops()

Modes:
- online: full functionality
- degraded: no network but budget ok (local memory/code intel still works)
- offline: budget exceeded (hard stop on all API calls)
"""

import socket
import sqlite3
import time


class OfflineDetector:
    """Detects network/budget state and switches modes."""

    MODES = ("online", "degraded", "offline")
    CHECK_INTERVAL = 60  # re-check every 60s

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._mode = "online"
        self._last_check = 0.0

    def get_mode(self) -> str:
        """Returns current mode, re-checking if stale."""
        if time.time() - self._last_check > self.CHECK_INTERVAL:
            self._mode = self._detect()
            self._last_check = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES ('mode', ?, ?)",
                (self._mode, time.time()),
            )
            self.db.commit()
        return self._mode

    def _detect(self) -> str:
        """Detect current mode by checking budget (fast) then network (2s timeout)."""
        # Check budget first (fast, local)
        budget_ok = self._check_budget()
        if not budget_ok:
            return "offline"  # Budget exceeded -> full offline

        # Check network (timeout 2s)
        network_ok = self._check_network()
        if not network_ok:
            return "degraded"  # No network but budget ok -> degraded

        return "online"

    def _check_budget(self) -> bool:
        """Is daily budget remaining?"""
        row = self.db.execute(
            """
            SELECT SUM(cost_usd) FROM cost_events
            WHERE timestamp > ?
            """,
            (time.time() - 86400,),
        ).fetchone()
        spent = row[0] or 0.0
        cap = self.db.execute(
            "SELECT value FROM runtime_state WHERE key = 'daily_budget_usd'"
        ).fetchone()
        daily_cap = float(cap[0]) if cap else 5.0
        return spent < daily_cap

    def _check_network(self) -> bool:
        """Can we reach external services?"""
        try:
            socket.create_connection(("api.anthropic.com", 443), timeout=2)
            return True
        except (socket.timeout, OSError):
            return False

    def is_offline(self) -> bool:
        """True if mode is offline (budget exceeded)."""
        return self.get_mode() == "offline"

    def is_degraded(self) -> bool:
        """True if mode is offline or degraded (any non-online state)."""
        return self.get_mode() in ("offline", "degraded")

    def should_skip_network_ops(self) -> bool:
        """Skip git fetch, embedding calls, model upgrades when not online."""
        return self.get_mode() != "online"

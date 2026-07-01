"""Root conftest — ensures test isolation for CAP test suite."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def _reset_agent_store_singleton():
    """Reset the module-level connection singleton in agent_store after each test.

    This prevents cross-test pollution where one test's DB connection
    leaks into another test's execution context.
    """
    yield
    try:
        import cap.harness.agent_store as store
        if store._conn is not None:
            try:
                store._conn.close()
            except Exception:
                pass
            store._conn = None
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def _restore_umask():
    """Ensure os.umask is restored even if a test crashes mid-umask-change.

    Captures the umask before each test and restores it after, preventing
    cross-test pollution from process-wide umask changes.
    """
    original = os.umask(0o022)
    os.umask(original)
    yield
    # Restore in case a test left a restrictive umask active
    os.umask(original)

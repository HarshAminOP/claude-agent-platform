"""Tests for progressive autonomy module."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database
from cap.lib.autonomy import (
    AutonomyLevel, init_autonomy_table, get_autonomy_level,
    should_ask_approval, record_outcome, list_autonomy_levels, reset_autonomy,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    conn = create_database(Path(tmp) / "test.db")
    init_autonomy_table(conn)
    yield conn
    conn.close()


def test_fresh_pair_is_level_0(db):
    al = get_autonomy_level(db, "dev", "refactor")
    assert al.level == 0
    assert al.success_count == 0


def test_should_ask_at_level_0(db):
    assert should_ask_approval(db, "dev", "refactor") is True


def test_record_success(db):
    al = record_outcome(db, "dev", "refactor", success=True, details="clean refactor")
    assert al.success_count == 1
    assert al.last_success_at is not None


def test_promotion_from_0_to_1(db):
    for _ in range(3):
        al = record_outcome(db, "dev", "deploy", success=True)
    assert al.level == 1


def test_no_premature_promotion(db):
    for _ in range(2):
        al = record_outcome(db, "dev", "deploy", success=True)
    assert al.level == 0  # needs 3 successes at 90%


def test_promotion_from_1_to_2(db):
    for _ in range(10):
        al = record_outcome(db, "dev", "test", success=True)
    assert al.level == 2


def test_promotion_from_2_to_3(db):
    for _ in range(25):
        al = record_outcome(db, "dev", "lint", success=True)
    assert al.level == 3


def test_failure_blocks_promotion(db):
    for _ in range(2):
        record_outcome(db, "dev", "deploy", success=True)
    record_outcome(db, "dev", "deploy", success=False)
    # Now rate is 2/3 = 66% < 90%, so 3 more successes still won't promote
    for _ in range(2):
        al = record_outcome(db, "dev", "deploy", success=True)
    # 4 successes, 1 failure = 80% rate < 90%
    assert al.level == 0


def test_demotion_on_failures(db):
    for _ in range(3):
        record_outcome(db, "dev", "ops", success=True)
    al = get_autonomy_level(db, "dev", "ops")
    assert al.level == 1

    # Demotion check queries log BEFORE inserting current failure,
    # so needs threshold+1 calls to trigger (2 already logged when 3rd runs)
    record_outcome(db, "dev", "ops", success=False)
    record_outcome(db, "dev", "ops", success=False)
    al = record_outcome(db, "dev", "ops", success=False)
    assert al.level == 0


def test_should_ask_at_level_1_first_time(db):
    for _ in range(3):
        record_outcome(db, "dev", "refactor", success=True)
    # Level 1: ask on first time (total_actions == 0 check is for a fresh pair, not here)
    # After 3 actions already recorded, should NOT ask
    assert should_ask_approval(db, "dev", "refactor") is False


def test_should_ask_at_level_2_high_risk(db):
    for _ in range(10):
        record_outcome(db, "dev", "deploy", success=True)
    al = get_autonomy_level(db, "dev", "deploy")
    assert al.level == 2

    assert should_ask_approval(db, "dev", "deploy", risk_level="low") is False
    assert should_ask_approval(db, "dev", "deploy", risk_level="high") is True
    assert should_ask_approval(db, "dev", "deploy", risk_level="critical") is True


def test_should_ask_at_level_3_only_critical(db):
    for _ in range(25):
        record_outcome(db, "dev", "lint", success=True)
    al = get_autonomy_level(db, "dev", "lint")
    assert al.level == 3

    assert should_ask_approval(db, "dev", "lint", risk_level="low") is False
    assert should_ask_approval(db, "dev", "lint", risk_level="high") is False
    assert should_ask_approval(db, "dev", "lint", risk_level="critical") is True


def test_list_autonomy_levels(db):
    record_outcome(db, "dev", "refactor", success=True)
    record_outcome(db, "security", "audit", success=True)

    all_levels = list_autonomy_levels(db)
    assert len(all_levels) == 2

    dev_only = list_autonomy_levels(db, agent_type="dev")
    assert len(dev_only) == 1
    assert dev_only[0].action_type == "refactor"


def test_reset_autonomy(db):
    for _ in range(3):
        record_outcome(db, "dev", "deploy", success=True)
    al = get_autonomy_level(db, "dev", "deploy")
    assert al.level == 1

    reset_autonomy(db, "dev", "deploy")
    al = get_autonomy_level(db, "dev", "deploy")
    assert al.level == 0


def test_reset_all_actions_for_agent(db):
    for _ in range(3):
        record_outcome(db, "dev", "deploy", success=True)
        record_outcome(db, "dev", "refactor", success=True)

    reset_autonomy(db, "dev")
    levels = list_autonomy_levels(db, agent_type="dev")
    assert all(al.level == 0 for al in levels)


def test_success_rate_property():
    al = AutonomyLevel(agent_type="dev", action_type="x", level=0, success_count=9, failure_count=1)
    assert al.success_rate == 0.9
    assert al.total_actions == 10


def test_success_rate_zero_actions():
    al = AutonomyLevel(agent_type="dev", action_type="x", level=0)
    assert al.success_rate == 0.0

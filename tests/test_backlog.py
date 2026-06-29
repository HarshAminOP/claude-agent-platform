"""Tests for persistent backlog module."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database
from cap.lib.backlog import (
    BacklogTask, TaskStatus, TaskPriority, AcceptanceCriterion,
    init_backlog_table, create_task, get_task, claim_next_task,
    complete_task, verify_criteria, list_tasks, backlog_stats,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    conn = create_database(Path(tmp) / "test.db")
    init_backlog_table(conn)
    yield conn
    conn.close()


def test_create_and_get(db):
    task = create_task(db, BacklogTask(
        title="Deploy v2",
        description="Deploy the new version",
        priority=TaskPriority.high,
        status=TaskStatus.ready,
    ))
    assert task.id
    retrieved = get_task(db, task.id)
    assert retrieved.title == "Deploy v2"
    assert retrieved.priority == TaskPriority.high


def test_claim_respects_priority(db):
    create_task(db, BacklogTask(title="Low task", priority=TaskPriority.low, status=TaskStatus.ready))
    create_task(db, BacklogTask(title="Critical task", priority=TaskPriority.critical, status=TaskStatus.ready))
    create_task(db, BacklogTask(title="Medium task", priority=TaskPriority.medium, status=TaskStatus.ready))

    claimed = claim_next_task(db, "agent-1")
    assert claimed.title == "Critical task"
    assert claimed.status == TaskStatus.in_progress
    assert claimed.assigned_to == "agent-1"


def test_claim_skips_unmet_deps(db):
    t1 = create_task(db, BacklogTask(title="First", status=TaskStatus.ready))
    t2 = create_task(db, BacklogTask(title="Second", status=TaskStatus.ready, depends_on=[t1.id]))

    claimed = claim_next_task(db, "agent-1")
    assert claimed.title == "First"  # t2 skipped due to dep on t1


def test_claim_allows_met_deps(db):
    t1 = create_task(db, BacklogTask(title="Done task", status=TaskStatus.ready))
    complete_task(db, t1.id, output="done")
    t2 = create_task(db, BacklogTask(title="Dependent", status=TaskStatus.ready, depends_on=[t1.id]))

    claimed = claim_next_task(db, "agent-1")
    assert claimed.title == "Dependent"


def test_complete_task(db):
    t = create_task(db, BacklogTask(title="Work", status=TaskStatus.ready))
    claim_next_task(db, "agent-1")
    completed = complete_task(db, t.id, output="result here")
    assert completed.status == TaskStatus.done
    assert completed.output == "result here"
    assert completed.completed_at is not None


def test_acceptance_criteria(db):
    t = create_task(db, BacklogTask(
        title="Feature",
        status=TaskStatus.ready,
        acceptance_criteria=[
            AcceptanceCriterion("Tests pass"),
            AcceptanceCriterion("Docs updated"),
        ],
    ))
    assert not t.all_criteria_met

    verify_criteria(db, t.id, 0, verified_by="test-agent")
    task = get_task(db, t.id)
    assert not task.all_criteria_met

    verify_criteria(db, t.id, 1, verified_by="test-agent")
    task = get_task(db, t.id)
    assert task.all_criteria_met


def test_list_tasks_filters(db):
    create_task(db, BacklogTask(title="A", status=TaskStatus.ready))
    create_task(db, BacklogTask(title="B", status=TaskStatus.done))
    create_task(db, BacklogTask(title="C", status=TaskStatus.ready, assigned_to="dev"))

    ready = list_tasks(db, status=TaskStatus.ready)
    assert len(ready) == 2

    done = list_tasks(db, status=TaskStatus.done)
    assert len(done) == 1

    dev_tasks = list_tasks(db, assigned_to="dev")
    assert len(dev_tasks) == 1


def test_backlog_stats(db):
    create_task(db, BacklogTask(title="A", status=TaskStatus.ready))
    create_task(db, BacklogTask(title="B", status=TaskStatus.done))
    create_task(db, BacklogTask(title="C", status=TaskStatus.in_progress))

    stats = backlog_stats(db)
    assert stats["total"] == 3
    assert stats["completion_pct"] == pytest.approx(33.3, abs=0.1)
    assert stats["in_progress"] == 1


def test_claim_returns_none_when_empty(db):
    result = claim_next_task(db, "agent")
    assert result is None

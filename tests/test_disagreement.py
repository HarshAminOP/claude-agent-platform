"""Tests for inter-agent disagreement protocol."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database
from cap.lib.disagreement import (
    Conflict, ConflictSide, ConflictSeverity, ConflictStatus, Resolution,
    init_conflicts_table, raise_conflict, resolve_conflict,
    override_conflict, get_conflict, list_conflicts, get_blocking_conflicts,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    conn = create_database(Path(tmp) / "test.db")
    init_conflicts_table(conn)
    yield conn
    conn.close()


def _make_conflict(severity=ConflictSeverity.warning, **kwargs) -> Conflict:
    defaults = {
        "title": "Wildcard IAM vs fast deploy",
        "workflow_id": "wf-123",
        "phase": "deploy",
        "severity": severity,
        "side_a": ConflictSide(
            agent_id="security-1",
            agent_type="security",
            position="Wildcard IAM is too broad",
            evidence=["IAM best practices doc"],
            risk_assessment="High blast radius",
            proposed_action="Scope down to specific resources",
        ),
        "side_b": ConflictSide(
            agent_id="devops-1",
            agent_type="devops",
            position="Need wildcard for dynamic resources",
            evidence=["Lambda creates random bucket names"],
            risk_assessment="Low if time-boxed",
            proposed_action="Use wildcard with condition key",
        ),
    }
    defaults.update(kwargs)
    return Conflict(**defaults)


def test_raise_and_get(db):
    conflict = raise_conflict(db, _make_conflict())
    assert conflict.id
    retrieved = get_conflict(db, conflict.id)
    assert retrieved.title == "Wildcard IAM vs fast deploy"
    assert retrieved.side_a.agent_type == "security"
    assert retrieved.side_b.position == "Need wildcard for dynamic resources"


def test_blocking_auto_escalates(db):
    conflict = raise_conflict(db, _make_conflict(severity=ConflictSeverity.blocking))
    assert conflict.status == ConflictStatus.escalated


def test_advisory_stays_open(db):
    conflict = raise_conflict(db, _make_conflict(severity=ConflictSeverity.advisory))
    assert conflict.status == ConflictStatus.open


def test_resolve_conflict(db):
    conflict = raise_conflict(db, _make_conflict())
    resolved = resolve_conflict(db, conflict.id, Resolution.compromise, notes="Use condition key on wildcard")
    assert resolved.status == ConflictStatus.resolved
    assert resolved.resolution == Resolution.compromise
    assert resolved.resolution_notes == "Use condition key on wildcard"
    assert resolved.resolved_at is not None


def test_override_conflict(db):
    conflict = raise_conflict(db, _make_conflict(severity=ConflictSeverity.blocking))
    overridden = override_conflict(db, conflict.id, notes="Ship it, fix later")
    assert overridden.status == ConflictStatus.overridden
    assert overridden.resolution == Resolution.overridden_by_po
    assert overridden.resolved_by == "po"


def test_list_conflicts_filters(db):
    raise_conflict(db, _make_conflict(severity=ConflictSeverity.advisory))
    raise_conflict(db, _make_conflict(severity=ConflictSeverity.blocking))
    raise_conflict(db, _make_conflict(severity=ConflictSeverity.warning, workflow_id="wf-other"))

    blocking = list_conflicts(db, severity=ConflictSeverity.blocking)
    assert len(blocking) == 1

    wf123 = list_conflicts(db, workflow_id="wf-123")
    assert len(wf123) == 2


def test_get_blocking_conflicts(db):
    raise_conflict(db, _make_conflict(severity=ConflictSeverity.blocking))
    raise_conflict(db, _make_conflict(severity=ConflictSeverity.warning))
    c3 = raise_conflict(db, _make_conflict(severity=ConflictSeverity.blocking))
    resolve_conflict(db, c3.id, Resolution.side_a_wins)

    blockers = get_blocking_conflicts(db, "wf-123")
    assert len(blockers) == 1


def test_is_blocking_property():
    c = Conflict(severity=ConflictSeverity.blocking, status=ConflictStatus.escalated)
    assert c.is_blocking

    c.status = ConflictStatus.resolved
    assert not c.is_blocking


def test_conflict_serialization():
    conflict = _make_conflict()
    d = conflict.to_dict()
    restored = Conflict.from_dict(d)
    assert restored.title == conflict.title
    assert restored.side_a.agent_id == "security-1"
    assert restored.side_b.evidence == ["Lambda creates random bucket names"]

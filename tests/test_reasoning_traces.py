"""Tests for reasoning traces module."""
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import create_database
from cap.lib.reasoning_traces import (
    ReasoningTrace, ReasoningStep, init_traces_table,
    record_trace, get_trace, list_traces, explain_decision,
)


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    conn = create_database(Path(tmp) / "test.db")
    init_traces_table(conn)
    yield conn
    conn.close()


def _make_trace(**kwargs) -> ReasoningTrace:
    defaults = {
        "agent_id": "dev-1",
        "workflow_id": "wf-abc",
        "action": "refactor auth module",
        "decision": "Extract token validation into shared middleware",
        "steps": [
            ReasoningStep(
                description="Analyzed current auth implementation",
                evidence=["auth.py:45-89", "middleware.py:12-34"],
                confidence=0.95,
                alternatives_considered=["Keep inline", "Use decorator pattern"],
                rejected_reason="Decorator adds complexity for single-use case",
            ),
            ReasoningStep(
                description="Verified no downstream breakage",
                evidence=["grep results: 3 callers", "test suite passes"],
                confidence=0.9,
            ),
        ],
        "context_used": ["session:corrections", "knowledge:auth-patterns"],
        "tools_invoked": ["Read", "Edit", "Bash(pytest)"],
        "files_modified": ["src/auth.py", "src/middleware.py"],
        "duration_ms": 4500,
        "tokens_used": 12000,
        "model": "claude-opus-4",
    }
    defaults.update(kwargs)
    return ReasoningTrace(**defaults)


def test_record_and_get(db):
    trace = _make_trace()
    record_trace(db, trace)
    retrieved = get_trace(db, trace.id)
    assert retrieved is not None
    assert retrieved.action == "refactor auth module"
    assert retrieved.decision == "Extract token validation into shared middleware"
    assert len(retrieved.steps) == 2
    assert retrieved.steps[0].confidence == 0.95
    assert retrieved.steps[0].alternatives_considered == ["Keep inline", "Use decorator pattern"]


def test_steps_preserved(db):
    trace = _make_trace()
    record_trace(db, trace)
    retrieved = get_trace(db, trace.id)
    step = retrieved.steps[0]
    assert step.evidence == ["auth.py:45-89", "middleware.py:12-34"]
    assert step.rejected_reason == "Decorator adds complexity for single-use case"


def test_metadata_fields(db):
    trace = _make_trace()
    record_trace(db, trace)
    retrieved = get_trace(db, trace.id)
    assert retrieved.context_used == ["session:corrections", "knowledge:auth-patterns"]
    assert retrieved.tools_invoked == ["Read", "Edit", "Bash(pytest)"]
    assert retrieved.files_modified == ["src/auth.py", "src/middleware.py"]
    assert retrieved.duration_ms == 4500
    assert retrieved.tokens_used == 12000
    assert retrieved.model == "claude-opus-4"


def test_list_traces_by_agent(db):
    record_trace(db, _make_trace(agent_id="dev-1", action="fix bug"))
    record_trace(db, _make_trace(agent_id="security-1", action="audit"))
    record_trace(db, _make_trace(agent_id="dev-1", action="refactor"))

    dev_traces = list_traces(db, agent_id="dev-1")
    assert len(dev_traces) == 2

    sec_traces = list_traces(db, agent_id="security-1")
    assert len(sec_traces) == 1


def test_list_traces_by_workflow(db):
    record_trace(db, _make_trace(workflow_id="wf-1"))
    record_trace(db, _make_trace(workflow_id="wf-2"))

    wf1 = list_traces(db, workflow_id="wf-1")
    assert len(wf1) == 1


def test_list_traces_by_action(db):
    record_trace(db, _make_trace(action="deploy service"))
    record_trace(db, _make_trace(action="refactor module"))
    record_trace(db, _make_trace(action="deploy database"))

    deploy = list_traces(db, action="deploy")
    assert len(deploy) == 2


def test_explain_decision(db):
    record_trace(db, _make_trace(workflow_id="wf-x", action="chose blue-green"))
    record_trace(db, _make_trace(workflow_id="wf-x", action="chose canary"))
    record_trace(db, _make_trace(workflow_id="wf-other", action="chose blue-green"))

    explanations = explain_decision(db, "wf-x", "chose")
    assert len(explanations) == 2


def test_get_nonexistent(db):
    assert get_trace(db, "nonexistent-id") is None


def test_serialization_roundtrip():
    trace = _make_trace()
    d = trace.to_dict()
    restored = ReasoningTrace.from_dict(d)
    assert restored.action == trace.action
    assert len(restored.steps) == len(trace.steps)
    assert restored.steps[0].confidence == trace.steps[0].confidence
    assert restored.files_modified == trace.files_modified
